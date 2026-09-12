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
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from tapebox import __version__
from tapebox.database import (
    backup_catalog,
    restore_catalog,
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
    Compare the installed TapeBox build with the latest
    stable release.

    Version numbers determine whether a newer stable
    release is available. When versions are equal, the
    current Git commit is also compared with the release
    commit so development/different builds are not
    incorrectly reported as the exact stable release.

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

    current_commit = get_current_commit()

    if release is None:
        return {
            "success": True,
            "repository": discovery[
                "repository"
            ],
            "current_version": __version__,
            "current_commit": current_commit,
            "update_available": False,
            "release_status": "no_release",
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

    if latest_parsed is None:
        raise UpdateError(
            "Latest TapeBox release version is not a "
            "supported stable semantic version: "
            f"{release['version']}"
        )

    release_commit = str(
        release.get("commit") or ""
    ).strip()

    if latest_parsed > current_parsed:
        release_status = "update_available"
        update_available = True

        message = (
            f"TapeBox {release['version']} is available."
        )

    elif latest_parsed < current_parsed:
        release_status = "ahead_of_stable"
        update_available = False

        message = (
            "This TapeBox build is newer than the latest "
            "stable release."
        )

    elif (
        release_commit
        and current_commit == release_commit
    ):
        release_status = "stable"
        update_available = False

        message = (
            "TapeBox is running the latest stable release."
        )

    else:
        release_status = "different_build"
        update_available = False

        message = (
            "TapeBox has the same version number as the "
            "latest stable release, but is running a "
            "different source commit."
        )

    return {
        "success": True,
        "repository": discovery[
            "repository"
        ],
        "current_version": __version__,
        "current_commit": current_commit,
        "update_available": update_available,
        "release_status": release_status,
        "latest_version": release[
            "version"
        ],
        "latest_tag": release[
            "tag_name"
        ],
        "release": release,
        "message": message,
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



def checkout_release_commit(
    commit,
):
    """
    Switch the TapeBox source tree to an exact validated release commit.

    The caller is responsible for creating a rollback checkpoint first.

    This function changes source code only. It does not restart TapeBox
    and does not modify the catalog database.
    """

    commit = str(
        commit or ""
    ).strip()

    if not commit:
        raise UpdateError(
            "Release commit cannot be empty."
        )

    if not working_tree_clean():
        raise UpdateError(
            "TapeBox has uncommitted source changes. "
            "Refusing to switch source code."
        )

    if not git_commit_exists(
        commit
    ):
        raise UpdateError(
            "Release commit does not exist locally: "
            f"{commit}"
        )

    previous_commit = get_current_commit()
    previous_branch = get_current_branch()

    if commit == previous_commit:
        raise UpdateError(
            "Release commit is already installed."
        )

    #
    # Use detached HEAD for installed release commits. This keeps the
    # installation pinned to the exact commit represented by the release
    # instead of silently following a development branch.
    #
    _run(
        [
            "git",
            "checkout",
            "--detach",
            commit,
        ],
        cwd=APP_DIR,
        timeout=120,
    )

    installed_commit = get_current_commit()

    if installed_commit != commit:
        raise UpdateError(
            "Git checkout completed but TapeBox is not at the "
            "requested release commit."
        )

    if not working_tree_clean():
        raise UpdateError(
            "TapeBox working tree is not clean after release checkout."
        )

    return {
        "success": True,
        "previous_commit": previous_commit,
        "previous_branch": previous_branch,
        "installed_commit": installed_commit,
        "branch": get_current_branch(),
    }


def restore_source_checkpoint(
    *,
    previous_commit,
    previous_branch=None,
):
    """
    Restore TapeBox source code to a previously recorded Git checkpoint.

    If the checkpoint originally used a branch, restore that branch and
    force it back to the exact recorded commit. If it was detached, return
    to the recorded commit in detached mode.

    This function restores source code only. Catalog restoration is a
    separate operation because code and database rollback must be
    coordinated by the external updater.
    """

    previous_commit = str(
        previous_commit or ""
    ).strip()

    previous_branch = (
        str(previous_branch).strip()
        if previous_branch
        else None
    )

    if not previous_commit:
        raise UpdateError(
            "Previous commit cannot be empty."
        )

    if not git_commit_exists(
        previous_commit
    ):
        raise UpdateError(
            "Previous TapeBox commit does not exist locally: "
            f"{previous_commit}"
        )

    #
    # An update checkout should itself be clean. Refuse to destroy
    # unexpected files or modifications created after the update.
    #
    if not working_tree_clean():
        raise UpdateError(
            "TapeBox source has unexpected changes. "
            "Refusing automatic source rollback."
        )

    if previous_branch:
        #
        # Restore the original branch, then force its working tree/index
        # to the exact checkpoint commit. This is appropriate because the
        # rollback record captured that exact branch+commit pair before
        # the update began.
        #
        _run(
            [
                "git",
                "checkout",
                previous_branch,
            ],
            cwd=APP_DIR,
            timeout=120,
        )

        _run(
            [
                "git",
                "reset",
                "--hard",
                previous_commit,
            ],
            cwd=APP_DIR,
            timeout=120,
        )

    else:
        _run(
            [
                "git",
                "checkout",
                "--detach",
                previous_commit,
            ],
            cwd=APP_DIR,
            timeout=120,
        )

    restored_commit = get_current_commit()
    restored_branch = get_current_branch()

    if restored_commit != previous_commit:
        raise UpdateError(
            "Source rollback completed but the expected "
            "commit was not restored."
        )

    if previous_branch:
        if restored_branch != previous_branch:
            raise UpdateError(
                "Source rollback restored the commit but not "
                "the original branch."
            )

    if not working_tree_clean():
        raise UpdateError(
            "TapeBox working tree is not clean after source rollback."
        )

    return {
        "success": True,
        "restored_commit": restored_commit,
        "restored_branch": restored_branch,
    }



def rollback_update_checkpoint(
    record=None,
):
    """
    Restore both TapeBox source code and its pre-update catalog database.

    The TapeBox service/process must already be stopped by the external
    updater before this function is used.

    Source and database are treated as one rollback checkpoint:
        1. validate the saved catalog backup
        2. restore the exact previous source commit
        3. restore the pre-update catalog database
        4. validate the restored catalog
        5. persist rollback completion state
    """

    if record is None:
        record = read_state()

    if not isinstance(record, dict):
        raise UpdateError(
            "No valid TapeBox rollback checkpoint is available."
        )

    previous_commit = str(
        record.get(
            "previous_commit"
        )
        or ""
    ).strip()

    previous_branch = (
        str(
            record.get(
                "previous_branch"
            )
        ).strip()
        if record.get(
            "previous_branch"
        )
        else None
    )

    catalog_backup_value = (
        record.get(
            "catalog_backup"
        )
    )

    if not previous_commit:
        raise UpdateError(
            "Rollback checkpoint is missing the previous commit."
        )

    if not catalog_backup_value:
        raise UpdateError(
            "Rollback checkpoint is missing the catalog backup."
        )

    catalog_backup = Path(
        catalog_backup_value
    )

    if not catalog_backup.is_file():
        raise UpdateError(
            "Rollback catalog backup does not exist: "
            f"{catalog_backup}"
        )

    #
    # Never change source code until we know the saved database backup
    # still exists and is valid.
    #
    backup_validation = (
        validate_catalog_database(
            catalog_backup
        )
    )

    if not backup_validation.get(
        "success"
    ):
        raise UpdateError(
            "Rollback catalog backup failed validation: "
            + backup_validation.get(
                "error",
                "unknown validation error",
            )
        )

    rollback_started_at = datetime.now(
        timezone.utc
    ).isoformat()

    rollback_record = dict(
        record
    )

    rollback_record[
        "status"
    ] = "rollback_in_progress"

    rollback_record[
        "rollback_started_at"
    ] = rollback_started_at

    write_state(
        rollback_record
    )

    try:
        source_result = (
            restore_source_checkpoint(
                previous_commit=previous_commit,
                previous_branch=previous_branch,
            )
        )

    except Exception as exc:
        rollback_record[
            "status"
        ] = "rollback_failed"

        rollback_record[
            "rollback_failure_stage"
        ] = "source"

        rollback_record[
            "rollback_error"
        ] = str(exc)

        write_state(
            rollback_record
        )

        raise UpdateError(
            "TapeBox source rollback failed: "
            f"{exc}"
        ) from exc

    try:
        database_result = restore_catalog(
            catalog_backup
        )

        if not database_result.get(
            "success"
        ):
            raise UpdateError(
                database_result.get(
                    "error",
                    "Catalog restore failed.",
                )
            )

    except Exception as exc:
        rollback_record[
            "status"
        ] = "rollback_failed"

        rollback_record[
            "rollback_failure_stage"
        ] = "database"

        rollback_record[
            "rollback_error"
        ] = str(exc)

        rollback_record[
            "source_restored"
        ] = True

        write_state(
            rollback_record
        )

        raise UpdateError(
            "TapeBox source was restored, but catalog rollback failed: "
            f"{exc}"
        ) from exc

    restored_validation = (
        validate_catalog_database(
            Path(
                database_result.get(
                    "database_path",
                    "/var/lib/tapebox/catalog.db",
                )
            )
        )
        if database_result.get(
            "database_path"
        )
        else None
    )

    #
    # Some versions of restore_catalog() do not return database_path.
    # In that case its own successful post-restore validation is the
    # authority and we retain that result below.
    #
    if (
        restored_validation is not None
        and not restored_validation.get(
            "success"
        )
    ):
        rollback_record[
            "status"
        ] = "rollback_failed"

        rollback_record[
            "rollback_failure_stage"
        ] = "post_restore_validation"

        rollback_record[
            "rollback_error"
        ] = restored_validation.get(
            "error",
            "Restored catalog failed validation.",
        )

        write_state(
            rollback_record
        )

        raise UpdateError(
            "Catalog was restored but failed post-restore validation."
        )

    rollback_record.update(
        {
            "status": "rolled_back",
            "rollback_completed_at": datetime.now(
                timezone.utc
            ).isoformat(),
            "rollback_failure_stage": None,
            "rollback_error": None,
            "source_restored": True,
            "database_restored": True,
            "restored_commit": source_result[
                "restored_commit"
            ],
            "restored_branch": source_result[
                "restored_branch"
            ],
        }
    )

    write_state(
        rollback_record
    )

    rollback_dir_value = (
        rollback_record.get(
            "rollback_dir"
        )
    )

    if rollback_dir_value:
        rollback_dir = Path(
            rollback_dir_value
        )

        if rollback_dir.is_dir():
            (
                rollback_dir
                / "rollback.json"
            ).write_text(
                json.dumps(
                    rollback_record,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

    return {
        "success": True,
        "status": "rolled_back",
        "source": source_result,
        "database": database_result,
        "catalog_backup": str(
            catalog_backup
        ),
        "previous_commit": previous_commit,
        "previous_branch": previous_branch,
    }



def stop_runtime():
    """
    Stop TapeBox when managed by systemd.

    Development installations are intentionally left
    alone because the developer owns the Flask process.
    """

    runtime_mode = detect_runtime_mode()

    if runtime_mode == "development":
        return {
            "success": True,
            "runtime_mode": "development",
            "stop_performed": False,
            "manual_stop_required": True,
        }

    if runtime_mode != "systemd":
        raise UpdateError(
            f"Unsupported TapeBox runtime mode: {runtime_mode}"
        )

    _run(
        [
            "systemctl",
            "stop",
            SERVICE_NAME,
        ],
        timeout=120,
    )

    if systemd_service_active():
        raise UpdateError(
            "TapeBox systemd service is still active "
            "after stop."
        )

    return {
        "success": True,
        "runtime_mode": "systemd",
        "stop_performed": True,
        "manual_stop_required": False,
    }


def start_runtime():
    """
    Start TapeBox when managed by systemd.

    Development installations are intentionally not
    started because the developer owns Flask manually.
    """

    runtime_mode = detect_runtime_mode()

    if runtime_mode == "development":
        return {
            "success": True,
            "runtime_mode": "development",
            "start_performed": False,
            "manual_start_required": True,
        }

    if runtime_mode != "systemd":
        raise UpdateError(
            f"Unsupported TapeBox runtime mode: {runtime_mode}"
        )

    _run(
        [
            "systemctl",
            "start",
            SERVICE_NAME,
        ],
        timeout=120,
    )

    if not systemd_service_active():
        raise UpdateError(
            "TapeBox systemd service did not become active "
            "after start."
        )

    return {
        "success": True,
        "runtime_mode": "systemd",
        "start_performed": True,
        "manual_start_required": False,
    }


def perform_rollback(
    *,
    record=None,
    health_url="http://127.0.0.1:8080/",
):
    """
    Perform a complete TapeBox rollback from the external
    updater process.

    Development mode:
        - restore source + catalog
        - leave manually-controlled Flask alone
        - require manual restart afterward

    systemd mode:
        - stop TapeBox
        - restore source + catalog
        - start TapeBox
        - verify HTTP health
    """

    if record is None:
        record = read_state()

    if not isinstance(record, dict):
        raise UpdateError(
            "No TapeBox rollback checkpoint is available."
        )

    runtime_mode = (
        record.get("runtime_mode")
        or detect_runtime_mode()
    )

    if runtime_mode not in {
        "development",
        "systemd",
    }:
        raise UpdateError(
            "Unsupported rollback runtime mode: "
            f"{runtime_mode}"
        )

    #
    # Development Flask is manually controlled.
    # Refuse before rollback begins and before any updater
    # failure state is written.
    #
    if runtime_mode == "development":
        running_probe = probe_http_health(
            health_url,
            timeout=2,
        )

        if running_probe.get("healthy"):
            raise UpdateError(
                "Development TapeBox is still running. "
                "Stop the Flask process manually before "
                "running rollback."
            )

    stop_result = None
    rollback_result = None
    start_result = None
    health_result = None

    try:
        if runtime_mode == "systemd":
            stop_result = stop_runtime()

        rollback_result = rollback_update_checkpoint(
            record
        )

        if runtime_mode == "development":
            state = read_state() or dict(record)

            state.update(
                {
                    "status": "rolled_back",
                    "runtime_mode": "development",
                    "manual_restart_required": True,
                    "rollback_runtime_validated": False,
                }
            )

            write_state(
                state
            )

            return {
                "success": True,
                "status": "rolled_back",
                "runtime_mode": "development",
                "rollback": rollback_result,
                "stop": stop_result,
                "start": None,
                "health": None,
                "manual_restart_required": True,
            }

        start_result = start_runtime()

        health_result = wait_for_http_health(
            health_url
        )

        state = read_state() or dict(record)

        state.update(
            {
                "status": "rolled_back",
                "runtime_mode": "systemd",
                "manual_restart_required": False,
                "rollback_runtime_validated": True,
                "rollback_health_checked": True,
            }
        )

        write_state(
            state
        )

        return {
            "success": True,
            "status": "rolled_back",
            "runtime_mode": "systemd",
            "rollback": rollback_result,
            "stop": stop_result,
            "start": start_result,
            "health": health_result,
            "manual_restart_required": False,
        }

    except Exception as exc:
        failure_state = (
            read_state()
            or dict(record)
        )

        failure_state.update(
            {
                "status": "rollback_failed",
                "runtime_mode": runtime_mode,
                "rollback_orchestrator_error": str(exc),
            }
        )

        write_state(
            failure_state
        )

        #
        # If production rollback restored source/database
        # but failed while bringing the old runtime back,
        # make one best-effort attempt to start the old
        # TapeBox service before reporting failure.
        #
        if (
            runtime_mode == "systemd"
            and rollback_result is not None
        ):
            try:
                if not systemd_service_active():
                    start_runtime()
            except Exception:
                pass

        raise UpdateError(
            "TapeBox rollback failed: "
            f"{exc}"
        ) from exc


def restart_runtime():
    """
    Restart TapeBox when managed by systemd.

    Development installations are intentionally not restarted because the
    developer owns the Flask process manually.
    """

    runtime_mode = detect_runtime_mode()

    if runtime_mode == "development":
        return {
            "success": True,
            "runtime_mode": "development",
            "restart_performed": False,
            "manual_restart_required": True,
        }

    if runtime_mode != "systemd":
        raise UpdateError(
            f"Unsupported TapeBox runtime mode: {runtime_mode}"
        )

    _run(
        [
            "systemctl",
            "restart",
            SERVICE_NAME,
        ],
        timeout=120,
    )

    if not systemd_service_active():
        raise UpdateError(
            "TapeBox systemd service did not become active "
            "after restart."
        )

    return {
        "success": True,
        "runtime_mode": "systemd",
        "restart_performed": True,
        "manual_restart_required": False,
    }


def probe_http_health(
    url="http://127.0.0.1:8080/",
    *,
    timeout=5,
):
    """
    Perform one TapeBox HTTP health probe.

    A healthy response must return HTTP 200.
    """

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                f"TapeBox-Updater/{__version__}"
            ),
        },
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout,
        ) as response:
            status = response.getcode()

            #
            # Read a small amount so the response is actually consumed,
            # but health checking does not need the full page.
            #
            response.read(
                4096
            )

    except urllib.error.HTTPError as exc:
        return {
            "success": False,
            "healthy": False,
            "url": url,
            "status": exc.code,
            "error": (
                f"HTTP {exc.code}"
            ),
        }

    except urllib.error.URLError as exc:
        return {
            "success": False,
            "healthy": False,
            "url": url,
            "status": None,
            "error": str(
                exc.reason
            ),
        }

    except TimeoutError:
        return {
            "success": False,
            "healthy": False,
            "url": url,
            "status": None,
            "error": "timeout",
        }

    healthy = (
        status == 200
    )

    return {
        "success": healthy,
        "healthy": healthy,
        "url": url,
        "status": status,
        "error": (
            None
            if healthy
            else f"Unexpected HTTP status {status}"
        ),
    }


def wait_for_http_health(
    url="http://127.0.0.1:8080/",
    *,
    attempts=20,
    delay_seconds=1,
    timeout=5,
):
    """
    Wait for TapeBox to begin returning HTTP 200.

    Intended for post-update startup validation.
    """

    attempts = int(
        attempts
    )

    if attempts < 1:
        raise UpdateError(
            "Health-check attempts must be at least 1."
        )

    last_result = None

    for attempt in range(
        1,
        attempts + 1,
    ):
        result = probe_http_health(
            url,
            timeout=timeout,
        )

        result[
            "attempt"
        ] = attempt

        if result.get(
            "healthy"
        ):
            result[
                "attempts_used"
            ] = attempt

            return result

        last_result = result

        if attempt < attempts:
            time.sleep(
                delay_seconds
            )

    raise UpdateError(
        "TapeBox failed post-update HTTP health check "
        f"after {attempts} attempts. "
        f"Last result: {last_result}"
    )


def post_update_runtime_check(
    url="http://127.0.0.1:8080/",
):
    """
    Handle runtime restart behavior after source installation.

    Normal installed TapeBox:
        restart systemd service and perform HTTP health check.

    Development mode:
        leave the manually controlled Flask process untouched and report
        that a manual restart is required before final validation.
    """

    restart = restart_runtime()

    if restart[
        "manual_restart_required"
    ]:
        return {
            "success": True,
            "runtime_mode": "development",
            "restart_performed": False,
            "manual_restart_required": True,
            "health_checked": False,
            "message": (
                "Development mode detected. Restart the Flask "
                "process manually before validating the update."
            ),
        }

    health = wait_for_http_health(
        url
    )

    return {
        "success": True,
        "runtime_mode": "systemd",
        "restart_performed": True,
        "manual_restart_required": False,
        "health_checked": True,
        "health": health,
    }



def perform_update(
    *,
    tag_name,
    expected_version=None,
    health_url="http://127.0.0.1:8080/",
):
    """
    Execute a validated TapeBox software update.

    IMPORTANT:
        This function is intended to be run by the external TapeBox
        updater helper, not by the Flask web process being updated.

    Workflow:
        1. validate/fetch exact release
        2. create code + catalog rollback checkpoint
        3. switch source to exact release commit
        4. restart/validate runtime when systemd-managed
        5. finalize success

    Development mode intentionally stops at awaiting_manual_restart.

    If installation or automatic runtime validation fails after the
    checkpoint is created, the previous source and catalog are restored.
    """

    if not working_tree_clean():
        raise UpdateError(
            "TapeBox has uncommitted source changes. "
            "Refusing to begin update."
        )

    candidate = fetch_and_validate_release(
        tag_name=tag_name,
        expected_version=expected_version,
    )

    checkpoint = prepare_update_checkpoint(
        target_version=candidate[
            "version"
        ],
        target_commit=candidate[
            "commit"
        ],
    )

    state = read_state()

    if not isinstance(state, dict):
        raise UpdateError(
            "Update checkpoint was created but updater state "
            "could not be loaded."
        )

    state.update(
        {
            "status": "installing",
            "target_tag": candidate[
                "tag_name"
            ],
            "target_version": candidate[
                "version"
            ],
            "target_commit": candidate[
                "commit"
            ],
            "install_started_at": datetime.now(
                timezone.utc
            ).isoformat(),
        }
    )

    write_state(
        state
    )

    source_switch_started = False

    try:
        source_switch_started = True

        checkout_result = checkout_release_commit(
            candidate[
                "commit"
            ]
        )

        state = read_state() or state

        state.update(
            {
                "status": "source_installed",
                "installed_commit": checkout_result[
                    "installed_commit"
                ],
                "source_installed_at": datetime.now(
                    timezone.utc
                ).isoformat(),
            }
        )

        write_state(
            state
        )

        runtime_result = post_update_runtime_check(
            health_url
        )

        #
        # Development installations are controlled manually. Do not
        # pretend the update is fully validated until the developer
        # restarts Flask and performs the final validation.
        #
        if runtime_result.get(
            "manual_restart_required"
        ):
            state = read_state() or state

            state.update(
                {
                    "status": "awaiting_manual_restart",
                    "manual_restart_required": True,
                    "runtime_mode": "development",
                    "health_checked": False,
                }
            )

            write_state(
                state
            )

            return {
                "success": True,
                "completed": False,
                "status": "awaiting_manual_restart",
                "candidate": candidate,
                "checkpoint": checkpoint,
                "checkout": checkout_result,
                "runtime": runtime_result,
            }

        #
        # systemd mode only reaches here after restart + HTTP health
        # validation succeeds.
        #
        state = read_state() or state

        state.update(
            {
                "status": "update_complete",
                "manual_restart_required": False,
                "health_checked": True,
                "completed_at": datetime.now(
                    timezone.utc
                ).isoformat(),
            }
        )

        write_state(
            state
        )

        return {
            "success": True,
            "completed": True,
            "status": "update_complete",
            "candidate": candidate,
            "checkpoint": checkpoint,
            "checkout": checkout_result,
            "runtime": runtime_result,
        }

    except Exception as exc:
        #
        # Once source switching has begun, the checkpoint is authoritative
        # and rollback should be attempted automatically.
        #
        if source_switch_started:
            try:
                rollback_record = (
                    read_state()
                    or state
                )

                rollback_result = (
                    rollback_update_checkpoint(
                        rollback_record
                    )
                )

                #
                # On an installed/systemd runtime, bring the restored old
                # version back online and validate it too.
                #
                restored_runtime = None

                if (
                    rollback_record.get(
                        "runtime_mode"
                    )
                    == "systemd"
                ):
                    restart_result = restart_runtime()

                    health_result = wait_for_http_health(
                        health_url
                    )

                    restored_runtime = {
                        "restart": restart_result,
                        "health": health_result,
                    }

                failure_state = (
                    read_state()
                    or rollback_record
                )

                failure_state.update(
                    {
                        "status": "rolled_back",
                        "update_error": str(
                            exc
                        ),
                        "failed_target_tag": tag_name,
                        "failed_target_version": (
                            expected_version
                        ),
                    }
                )

                write_state(
                    failure_state
                )

                raise UpdateError(
                    "TapeBox update failed and was rolled back "
                    f"successfully: {exc}"
                ) from exc

            except UpdateError as rollback_exc:
                #
                # Preserve a successful rollback result. The UpdateError
                # above intentionally reports the failed update.
                #
                if (
                    "rolled back successfully"
                    in str(rollback_exc)
                ):
                    raise

                failure_state = (
                    read_state()
                    or state
                )

                failure_state.update(
                    {
                        "status": "rollback_failed",
                        "update_error": str(
                            exc
                        ),
                        "rollback_error": str(
                            rollback_exc
                        ),
                    }
                )

                write_state(
                    failure_state
                )

                raise UpdateError(
                    "TapeBox update failed and automatic rollback "
                    f"also failed. Update error: {exc}. "
                    f"Rollback error: {rollback_exc}"
                ) from rollback_exc

            except Exception as rollback_exc:
                failure_state = (
                    read_state()
                    or state
                )

                failure_state.update(
                    {
                        "status": "rollback_failed",
                        "update_error": str(
                            exc
                        ),
                        "rollback_error": str(
                            rollback_exc
                        ),
                    }
                )

                write_state(
                    failure_state
                )

                raise UpdateError(
                    "TapeBox update failed and automatic rollback "
                    f"also failed. Update error: {exc}. "
                    f"Rollback error: {rollback_exc}"
                ) from rollback_exc

        raise



def complete_manual_update(
    health_url="http://127.0.0.1:8080/",
):
    """
    Complete an update that is waiting for a manual development restart.

    This is intended for development-mode installations where the Flask
    process is controlled manually.

    Validation:
        1. updater state must be awaiting_manual_restart
        2. current Git commit must match target_commit
        3. HTTP health check must succeed
        4. live catalog database must validate
        5. mark update complete

    If validation fails, restore source + catalog from the saved rollback
    checkpoint.
    """

    state = read_state()

    if not isinstance(state, dict):
        raise UpdateError(
            "No TapeBox update state is available."
        )

    if state.get(
        "status"
    ) != "awaiting_manual_restart":
        raise UpdateError(
            "TapeBox is not waiting for a manual update restart. "
            f"Current state: {state.get('status')}"
        )

    if state.get(
        "runtime_mode"
    ) != "development":
        raise UpdateError(
            "Manual update completion is only valid in "
            "development runtime mode."
        )

    target_commit = str(
        state.get(
            "target_commit"
        )
        or ""
    ).strip()

    if not target_commit:
        raise UpdateError(
            "Update state is missing target_commit."
        )

    try:
        current_commit = get_current_commit()

        if current_commit != target_commit:
            raise UpdateError(
                "TapeBox source does not match the expected "
                "updated commit. "
                f"Expected {target_commit}, found {current_commit}."
            )

        health = wait_for_http_health(
            health_url,
            attempts=5,
            delay_seconds=1,
            timeout=5,
        )

        #
        # Validate the actual live database after restart.
        #
        live_catalog = Path(
            "/var/lib/tapebox/catalog.db"
        )

        catalog_validation = (
            validate_catalog_database(
                live_catalog
            )
        )

        if not catalog_validation.get(
            "success"
        ):
            raise UpdateError(
                "Updated TapeBox catalog failed validation: "
                + catalog_validation.get(
                    "error",
                    "unknown validation error",
                )
            )

        state.update(
            {
                "status": "update_complete",
                "manual_restart_required": False,
                "health_checked": True,
                "health": health,
                "catalog_validation_after_update": {
                    "success": True,
                    "tapes": catalog_validation.get(
                        "tapes"
                    ),
                    "files": catalog_validation.get(
                        "files"
                    ),
                },
                "completed_at": datetime.now(
                    timezone.utc
                ).isoformat(),
            }
        )

        write_state(
            state
        )

        return {
            "success": True,
            "completed": True,
            "status": "update_complete",
            "current_commit": current_commit,
            "target_commit": target_commit,
            "health": health,
            "catalog_validation": state[
                "catalog_validation_after_update"
            ],
        }

    except Exception as exc:
        rollback_record = (
            read_state()
            or state
        )

        try:
            rollback_result = (
                rollback_update_checkpoint(
                    rollback_record
                )
            )

        except Exception as rollback_exc:
            failure_state = (
                read_state()
                or rollback_record
            )

            failure_state.update(
                {
                    "status": "rollback_failed",
                    "manual_completion_error": str(
                        exc
                    ),
                    "rollback_error": str(
                        rollback_exc
                    ),
                }
            )

            write_state(
                failure_state
            )

            raise UpdateError(
                "Manual update validation failed and rollback "
                f"also failed. Validation error: {exc}. "
                f"Rollback error: {rollback_exc}"
            ) from rollback_exc

        failure_state = (
            read_state()
            or rollback_record
        )

        failure_state.update(
            {
                "status": "rolled_back",
                "manual_completion_error": str(
                    exc
                ),
            }
        )

        write_state(
            failure_state
        )

        raise UpdateError(
            "Manual update validation failed and TapeBox "
            f"was rolled back successfully: {exc}"
        ) from exc
