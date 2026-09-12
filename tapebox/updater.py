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
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from tapebox import __version__
from tapebox.database import (
    backup_catalog,
    validate_catalog_database,
)


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



def prepare_update_checkpoint(
    *,
    target_version=None,
    target_commit=None,
):
    """
    Create and validate the database backup required before an update.

    This prepares a durable rollback checkpoint but does not modify Git,
    install software, restart TapeBox, or restore anything.
    """

    if not APP_DIR.is_dir():
        raise UpdateError(
            f"TapeBox application directory does not exist: {APP_DIR}"
        )

    if not (APP_DIR / ".git").is_dir():
        raise UpdateError(
            "TapeBox application directory is not a Git repository."
        )

    #
    # Check this before creating a database backup. We do not want an
    # updater run to proceed when local development work could be lost.
    #
    status = working_tree_status()

    if status.strip():
        raise UpdateError(
            "TapeBox has uncommitted source changes. "
            "Commit, stash, or remove them before preparing an update."
        )

    previous_commit = get_current_commit()
    previous_branch = get_current_branch()

    #
    # Create the SQLite-consistent catalog backup using TapeBox's normal
    # database backup engine.
    #
    backup_result = backup_catalog()

    if not backup_result.get("success"):
        raise UpdateError(
            backup_result.get(
                "error",
                "Catalog backup failed.",
            )
        )

    backup_path = Path(
        backup_result["path"]
    )

    if not backup_path.is_file():
        raise UpdateError(
            "Catalog backup reported success but the backup file "
            f"does not exist: {backup_path}"
        )

    #
    # Never accept an unvalidated database as an update rollback point.
    #
    validation = validate_catalog_database(
        backup_path
    )

    if not validation.get("success"):
        raise UpdateError(
            "Catalog backup validation failed: "
            + validation.get(
                "error",
                "unknown validation error",
            )
        )

    timestamp = datetime.now(
        timezone.utc
    ).strftime("%Y%m%d-%H%M%S-%f")

    rollback_id = (
        f"{timestamp}-{previous_commit[:12]}"
    )

    rollback_path = (
        ROLLBACK_DIR / rollback_id
    )

    rollback_path.mkdir(
        parents=True,
        exist_ok=False,
    )

    record = create_rollback_record(
        catalog_backup=backup_path,
        target_version=target_version,
        target_commit=target_commit,
    )

    record.update(
        {
            "rollback_id": rollback_id,
            "rollback_dir": str(
                rollback_path
            ),
            "catalog_backup_size_bytes": (
                backup_path.stat().st_size
            ),
            "catalog_validation": {
                "success": True,
                "tapes": validation.get(
                    "tapes"
                ),
                "files": validation.get(
                    "files"
                ),
            },
            "previous_commit": previous_commit,
            "previous_branch": previous_branch,
            "status": "checkpoint_ready",
        }
    )

    #
    # Save the complete rollback record both globally and inside the
    # individual rollback directory. The latter remains useful even if a
    # later update state file is replaced.
    #
    write_state(record)

    rollback_record_file = (
        rollback_path / "rollback.json"
    )

    rollback_record_file.write_text(
        json.dumps(
            record,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    return {
        "success": True,
        "rollback_id": rollback_id,
        "rollback_dir": str(
            rollback_path
        ),
        "previous_version": record[
            "previous_version"
        ],
        "previous_commit": previous_commit,
        "previous_branch": previous_branch,
        "runtime_mode": record[
            "runtime_mode"
        ],
        "catalog_backup": str(
            backup_path
        ),
        "catalog_backup_size_bytes": (
            backup_path.stat().st_size
        ),
        "catalog_validation": record[
            "catalog_validation"
        ],
        "target_version": target_version,
        "target_commit": target_commit,
        "status": "checkpoint_ready",
    }



def github_repository():
    """
    Resolve the GitHub owner/repository pair from remote.origin.url.

    Supported examples:
        https://github.com/wire7777/TapeBox.git
        git@github.com:wire7777/TapeBox.git
    """

    origin = get_origin_url().strip()

    https_prefix = "https://github.com/"

    if origin.startswith(https_prefix):
        repository = origin[
            len(https_prefix):
        ]

    elif origin.startswith(
        "git@github.com:"
    ):
        repository = origin[
            len("git@github.com:"):
        ]

    else:
        raise UpdateError(
            "TapeBox origin is not a supported GitHub repository: "
            f"{origin}"
        )

    if repository.endswith(".git"):
        repository = repository[:-4]

    repository = repository.strip("/")

    pieces = repository.split("/")

    if len(pieces) != 2:
        raise UpdateError(
            "Could not determine GitHub owner and repository "
            f"from origin: {origin}"
        )

    owner, repo = pieces

    if not owner or not repo:
        raise UpdateError(
            f"Invalid GitHub repository origin: {origin}"
        )

    return {
        "owner": owner,
        "repository": repo,
        "full_name": f"{owner}/{repo}",
    }


def _parse_version(value):
    """
    Parse a simple semantic version such as:
        0.1.0
        v0.1.0

    Returns a numeric tuple for comparison, or None when the value is not
    a supported stable semantic version.
    """

    value = str(
        value or ""
    ).strip()

    if value.lower().startswith("v"):
        value = value[1:]

    #
    # Stable releases only for the first updater version.
    # Pre-release handling can be added later as an explicit option.
    #
    if "-" in value or "+" in value:
        return None

    pieces = value.split(".")

    if not pieces:
        return None

    if len(pieces) > 3:
        return None

    numbers = []

    for piece in pieces:
        if not piece.isdigit():
            return None

        numbers.append(
            int(piece)
        )

    while len(numbers) < 3:
        numbers.append(0)

    return tuple(numbers)


def _github_json(url):
    """
    Fetch JSON from GitHub using only the Python standard library.

    This is read-only and does not authenticate or modify the repository.
    """

    request = urllib.request.Request(
        url,
        headers={
            "Accept": (
                "application/vnd.github+json"
            ),
            "User-Agent": (
                f"TapeBox/{__version__}"
            ),
            "X-GitHub-Api-Version": (
                "2022-11-28"
            ),
        },
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=15,
        ) as response:
            payload = response.read()

    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None

        raise UpdateError(
            "GitHub update check failed with "
            f"HTTP {exc.code}."
        ) from exc

    except urllib.error.URLError as exc:
        raise UpdateError(
            "Could not contact GitHub while checking "
            f"for TapeBox updates: {exc.reason}"
        ) from exc

    except TimeoutError as exc:
        raise UpdateError(
            "GitHub update check timed out."
        ) from exc

    try:
        return json.loads(
            payload.decode("utf-8")
        )

    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise UpdateError(
            "GitHub returned an invalid update response."
        ) from exc


def _latest_github_tag(
    repository,
):
    """
    Return the newest stable semantic-version tag from GitHub.

    This acts as a fallback when a repository has tags but has not yet
    published GitHub Releases.
    """

    url = (
        "https://api.github.com/repos/"
        f"{repository}/tags"
        "?per_page=100"
    )

    payload = _github_json(url)

    if not payload:
        return None

    candidates = []

    for item in payload:
        if not isinstance(item, dict):
            continue

        tag_name = str(
            item.get("name") or ""
        ).strip()

        parsed = _parse_version(
            tag_name
        )

        commit = item.get(
            "commit"
        ) or {}

        commit_sha = (
            commit.get("sha")
            if isinstance(commit, dict)
            else None
        )

        if parsed is None:
            continue

        candidates.append(
            (
                parsed,
                tag_name,
                commit_sha,
            )
        )

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    parsed, tag_name, commit_sha = (
        candidates[0]
    )

    return {
        "source": "tag",
        "tag_name": tag_name,
        "version": ".".join(
            str(part)
            for part in parsed
        ),
        "commit": commit_sha,
        "name": tag_name,
        "body": "",
        "html_url": (
            "https://github.com/"
            f"{repository}/releases/tag/"
            f"{tag_name}"
        ),
        "published_at": None,
        "prerelease": False,
        "draft": False,
    }


def latest_github_release():
    """
    Discover the newest stable TapeBox release.

    Prefer an actual GitHub Release. If no Release exists yet, fall back
    to the newest semantic-version Git tag.

    This function is completely read-only.
    """

    repo = github_repository()

    repository = repo[
        "full_name"
    ]

    latest_release_url = (
        "https://api.github.com/repos/"
        f"{repository}/releases/latest"
    )

    payload = _github_json(
        latest_release_url
    )

    release = None

    if isinstance(payload, dict):
        tag_name = str(
            payload.get("tag_name")
            or ""
        ).strip()

        parsed = _parse_version(
            tag_name
        )

        if (
            parsed is not None
            and not payload.get(
                "draft",
                False,
            )
            and not payload.get(
                "prerelease",
                False,
            )
        ):
            release = {
                "source": "release",
                "tag_name": tag_name,
                "version": ".".join(
                    str(part)
                    for part in parsed
                ),
                "commit": (
                    payload.get(
                        "target_commitish"
                    )
                ),
                "name": (
                    payload.get("name")
                    or tag_name
                ),
                "body": (
                    payload.get("body")
                    or ""
                ),
                "html_url": (
                    payload.get(
                        "html_url"
                    )
                ),
                "published_at": (
                    payload.get(
                        "published_at"
                    )
                ),
                "prerelease": False,
                "draft": False,
            }

    if release is None:
        release = _latest_github_tag(
            repository
        )

    return {
        "success": True,
        "repository": repository,
        "release": release,
    }


def check_for_updates():
    """
    Compare the installed TapeBox version with the latest stable release.

    No source code or database state is changed.
    """

    discovery = latest_github_release()

    release = discovery.get(
        "release"
    )

    current_parsed = _parse_version(
        __version__
    )

    if current_parsed is None:
        raise UpdateError(
            "Installed TapeBox version is not a supported "
            f"semantic version: {__version__}"
        )

    if release is None:
        return {
            "success": True,
            "repository": discovery[
                "repository"
            ],
            "current_version": __version__,
            "current_commit": (
                get_current_commit()
            ),
            "update_available": False,
            "latest_version": None,
            "latest_tag": None,
            "release": None,
            "message": (
                "No stable TapeBox release was found."
            ),
        }

    latest_parsed = _parse_version(
        release["version"]
    )

    update_available = (
        latest_parsed > current_parsed
    )

    return {
        "success": True,
        "repository": discovery[
            "repository"
        ],
        "current_version": __version__,
        "current_commit": get_current_commit(),
        "update_available": update_available,
        "latest_version": release[
            "version"
        ],
        "latest_tag": release[
            "tag_name"
        ],
        "release": release,
        "message": (
            f"TapeBox {release['version']} is available."
            if update_available
            else (
                "TapeBox is up to date with the latest "
                "stable release."
            )
        ),
    }



def fetch_github_refs():
    """
    Fetch tags and origin refs without checking anything out.

    This updates Git metadata only. It does not modify the working tree.
    """

    if not working_tree_clean():
        raise UpdateError(
            "TapeBox has uncommitted source changes. "
            "Refusing to fetch update refs."
        )

    result = _run(
        [
            "git",
            "fetch",
            "--tags",
            "--prune",
            "origin",
        ],
        cwd=APP_DIR,
        timeout=120,
    )

    return {
        "success": True,
        "stdout": result["stdout"],
        "stderr": result["stderr"],
    }


def resolve_git_ref(ref_name):
    """
    Resolve a Git ref to an exact commit SHA.

    For annotated tags, ^{} dereferences the tag object to its commit.
    """

    ref_name = str(
        ref_name or ""
    ).strip()

    if not ref_name:
        raise UpdateError(
            "Git ref cannot be empty."
        )

    result = _run(
        [
            "git",
            "rev-parse",
            "--verify",
            f"{ref_name}^{{commit}}",
        ],
        cwd=APP_DIR,
        check=False,
    )

    if result["returncode"] != 0:
        raise UpdateError(
            f"Could not resolve Git ref: {ref_name}"
        )

    commit = result["stdout"].strip()

    if len(commit) != 40:
        raise UpdateError(
            "Resolved Git commit is not a full SHA-1: "
            f"{commit}"
        )

    return commit


def git_commit_exists(commit):
    """
    Verify a commit object exists in the local repository.
    """

    commit = str(
        commit or ""
    ).strip()

    if not commit:
        return False

    result = _run(
        [
            "git",
            "cat-file",
            "-e",
            f"{commit}^{{commit}}",
        ],
        cwd=APP_DIR,
        check=False,
    )

    return result["returncode"] == 0


def version_from_tag(tag_name):
    """
    Convert a supported stable release tag to its normalized version.
    """

    parsed = _parse_version(
        tag_name
    )

    if parsed is None:
        raise UpdateError(
            "Unsupported TapeBox release tag: "
            f"{tag_name}"
        )

    return ".".join(
        str(part)
        for part in parsed
    )


def validate_release_candidate(
    *,
    tag_name,
    expected_version=None,
):
    """
    Validate a release tag already present in the local Git repository.

    This does not modify the working tree.
    """

    normalized_version = version_from_tag(
        tag_name
    )

    if expected_version is not None:
        expected_normalized = (
            version_from_tag(
                expected_version
            )
        )

        if (
            normalized_version
            != expected_normalized
        ):
            raise UpdateError(
                "Release version mismatch: "
                f"tag {tag_name} resolves to "
                f"{normalized_version}, expected "
                f"{expected_normalized}."
            )

    current_parsed = _parse_version(
        __version__
    )

    candidate_parsed = _parse_version(
        normalized_version
    )

    if current_parsed is None:
        raise UpdateError(
            "Installed TapeBox version cannot be compared: "
            f"{__version__}"
        )

    if candidate_parsed is None:
        raise UpdateError(
            "Candidate TapeBox version cannot be compared: "
            f"{normalized_version}"
        )

    if candidate_parsed <= current_parsed:
        raise UpdateError(
            "Release is not newer than the installed TapeBox "
            f"version ({__version__}): {normalized_version}"
        )

    commit = resolve_git_ref(
        f"refs/tags/{tag_name}"
    )

    if not git_commit_exists(
        commit
    ):
        raise UpdateError(
            "Release commit could not be verified locally: "
            f"{commit}"
        )

    return {
        "success": True,
        "tag_name": tag_name,
        "version": normalized_version,
        "commit": commit,
        "current_version": __version__,
        "current_commit": get_current_commit(),
    }


def fetch_and_validate_release(
    *,
    tag_name,
    expected_version=None,
):
    """
    Fetch GitHub refs and validate the requested TapeBox release.

    This is the final safety gate before an update checkpoint/install.
    It does not checkout code, modify the database, or restart TapeBox.
    """

    if not working_tree_clean():
        raise UpdateError(
            "TapeBox has uncommitted source changes. "
            "Refusing to prepare a release."
        )

    fetch_github_refs()

    result = validate_release_candidate(
        tag_name=tag_name,
        expected_version=expected_version,
    )

    result["fetched"] = True

    return result
