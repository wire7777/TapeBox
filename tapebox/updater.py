"""
TapeBox safe software update support.

The updater is deliberately separated from web.py so update and rollback
logic can eventually be executed independently of the running TapeBox web
process.

This module does not perform an update merely by being imported.
"""

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from tapebox import __version__


APP_DIR = Path("/opt/tapebox")
DATA_DIR = Path("/var/lib/tapebox")

UPDATE_DIR = DATA_DIR / "updates"
ROLLBACK_DIR = UPDATE_DIR / "rollback"
STATE_FILE = UPDATE_DIR / "state.json"

SERVICE_NAME = "tapebox"


class UpdateError(RuntimeError):
    """Raised when a TapeBox software update operation is unsafe."""


def _run(
    command,
    *,
    cwd=None,
    check=True,
    timeout=30,
):
    """
    Run a local command and return stripped stdout.
    """

    result = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )

    if check and result.returncode != 0:
        detail = (
            result.stderr.strip()
            or result.stdout.strip()
            or f"exit status {result.returncode}"
        )

        raise UpdateError(
            f"Command failed: {' '.join(command)}: {detail}"
        )

    return {
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def get_current_commit():
    """
    Return the exact Git commit currently installed.
    """

    result = _run(
        [
            "git",
            "rev-parse",
            "HEAD",
        ],
        cwd=APP_DIR,
    )

    return result["stdout"]


def get_current_short_commit():
    commit = get_current_commit()

    return commit[:7]


def get_current_branch():
    """
    Return the checked-out branch, or None for detached HEAD.
    """

    result = _run(
        [
            "git",
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
        ],
        cwd=APP_DIR,
        check=False,
    )

    if result["returncode"] != 0:
        return None

    return result["stdout"] or None


def get_origin_url():
    result = _run(
        [
            "git",
            "config",
            "--get",
            "remote.origin.url",
        ],
        cwd=APP_DIR,
    )

    return result["stdout"]


def working_tree_status():
    """
    Return porcelain status.

    An updater must refuse to overwrite local development changes.
    """

    result = _run(
        [
            "git",
            "status",
            "--porcelain",
        ],
        cwd=APP_DIR,
    )

    return result["stdout"]


def working_tree_clean():
    return not bool(
        working_tree_status().strip()
    )


def systemd_service_exists():
    """
    Determine whether TapeBox has a systemd service installed.
    """

    service_file = Path(
        f"/etc/systemd/system/{SERVICE_NAME}.service"
    )

    return service_file.is_file()


def systemd_service_active():
    """
    Return True only when the installed TapeBox systemd service is active.
    """

    if not systemd_service_exists():
        return False

    result = _run(
        [
            "systemctl",
            "is-active",
            "--quiet",
            SERVICE_NAME,
        ],
        check=False,
    )

    return result["returncode"] == 0


def detect_runtime_mode():
    """
    Identify how this TapeBox installation should be updated.

    systemd:
        Normal installed TapeBox. The external updater may restart the
        service and perform post-restart health checks.

    development:
        No active TapeBox systemd service. Code may be prepared safely,
        but the developer controls the Flask process manually.
    """

    if (
        systemd_service_exists()
        and systemd_service_active()
    ):
        return "systemd"

    return "development"


def update_environment():
    """
    Return non-destructive information about this installation.
    """

    return {
        "success": True,
        "version": __version__,
        "commit": get_current_commit(),
        "short_commit": get_current_short_commit(),
        "branch": get_current_branch(),
        "origin": get_origin_url(),
        "working_tree_clean": working_tree_clean(),
        "runtime_mode": detect_runtime_mode(),
        "systemd_service_exists": systemd_service_exists(),
        "systemd_service_active": systemd_service_active(),
        "app_dir": str(APP_DIR),
        "update_dir": str(UPDATE_DIR),
    }


def ensure_update_directories():
    """
    Create persistent updater state directories.

    This does not alter source code or the catalog database.
    """

    UPDATE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    ROLLBACK_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )


def write_state(state):
    """
    Atomically persist updater state.
    """

    ensure_update_directories()

    temporary = STATE_FILE.with_suffix(
        ".json.tmp"
    )

    payload = dict(state)

    payload["updated_at"] = datetime.now(
        timezone.utc
    ).isoformat()

    temporary.write_text(
        json.dumps(
            payload,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    os.replace(
        temporary,
        STATE_FILE,
    )


def read_state():
    """
    Read persistent updater state if one exists.
    """

    if not STATE_FILE.is_file():
        return None

    try:
        return json.loads(
            STATE_FILE.read_text(
                encoding="utf-8"
            )
        )

    except (
        OSError,
        json.JSONDecodeError,
    ) as exc:
        raise UpdateError(
            f"Could not read updater state: {exc}"
        ) from exc


def create_rollback_record(
    *,
    catalog_backup=None,
    target_version=None,
    target_commit=None,
):
    """
    Record everything required to identify a rollback point.

    This function records state only. It does not modify Git or restore
    the database.
    """

    if not working_tree_clean():
        raise UpdateError(
            "TapeBox has uncommitted source changes. "
            "Refusing to create an update rollback point."
        )

    now = datetime.now(
        timezone.utc
    )

    record = {
        "schema_version": 1,
        "status": "prepared",
        "created_at": now.isoformat(),
        "previous_version": __version__,
        "previous_commit": get_current_commit(),
        "previous_branch": get_current_branch(),
        "origin": get_origin_url(),
        "runtime_mode": detect_runtime_mode(),
        "catalog_backup": (
            str(catalog_backup)
            if catalog_backup
            else None
        ),
        "target_version": target_version,
        "target_commit": target_commit,
    }

    write_state(record)

    return record
