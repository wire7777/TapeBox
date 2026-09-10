import argparse
import subprocess
import threading
import uuid
import shutil
import time
from pathlib import Path
from flask import send_file

from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    redirect,
    url_for,
)

from tapebox.database import (
    initialize_database,
    list_tapes,
    list_files,
    list_archive_jobs,
    get_archive_job,
    get_archive_job_files,
    get_archive_restore_plan,
    get_tape_by_id,
    get_files_by_tape,
    search_files,
    get_file_parts,
    get_setting,
    get_settings,
    set_setting,
    initialize_default_settings,
    backup_catalog,
    validate_catalog_database,
    restore_catalog,
    DB_PATH,
    BACKUP_DIR,
)

from tapebox.restore import (
    restore_archive_job,
)

from tapebox.archive import (
    archive_path,
)

from tapebox.tape import (
    discover_drives,
    get_tape_status,
    mount_ltfs_inspector,
    browse_ltfs_inspector,
    get_ltfs_virtual_attribute,
    _is_mounted,
    _unmount_ltfs,
    eject_tape,
)


app = Flask(__name__)


LAST_RESTORE_RESULTS = {}

#
# TapeBox currently owns one physical tape drive, so only one
# restore/archive tape operation may be active at a time.
#
OPERATION_STATE_LOCK = threading.Lock()

OPERATIONS = {}

ACTIVE_TAPE_OPERATION_ID = None


def _operation_snapshot(operation):
    """
    Return a JSON-safe copy of operation state.
    """

    return {
        "id": operation["id"],
        "type": operation["type"],
        "job_id": operation["job_id"],
        "destination": operation["destination"],
        "status": operation["status"],
        "message": operation["message"],
        "messages": list(
            operation["messages"]
        ),
        "transfer": (
            dict(operation["transfer"])
            if operation["transfer"]
            else None
        ),
        "result": operation["result"],
    }


def _restore_worker(
    operation_id,
    job_id,
    destination,
):
    """
    Run one archive-job restore outside the HTTP request thread.
    """

    global ACTIVE_TAPE_OPERATION_ID

    def progress(event):
        with OPERATION_STATE_LOCK:
            operation = OPERATIONS.get(
                operation_id
            )

            if operation is None:
                return

            if isinstance(event, dict):
                message = str(
                    event.get(
                        "message",
                        "",
                    )
                )

                operation["message"] = message

                if (
                    event.get("type")
                    in {
                        "transfer",
                        "transfer_start",
                    }
                ):
                    #
                    # Keep only the latest high-frequency transfer
                    # sample. The browser does not need thousands
                    # of 16 MiB copy events in its activity log.
                    #
                    operation["transfer"] = dict(
                        event
                    )

                    if (
                        event.get("type")
                        == "transfer_start"
                        and message
                    ):
                        operation["messages"].append(
                            message
                        )

                elif message:
                    operation["messages"].append(
                        message
                    )

            else:
                message = str(event)

                operation["message"] = message
                operation["messages"].append(
                    message
                )

    #
    # Tell restore.py that this callback accepts structured events.
    #
    progress.tapebox_structured_progress = True

    try:
        result = restore_archive_job(
            job_id,
            Path(destination),
            progress=progress,
        )

        with OPERATION_STATE_LOCK:
            operation = OPERATIONS[
                operation_id
            ]

            operation["result"] = result

            if result.get("success"):
                if result.get(
                    "completed",
                    False,
                ):
                    operation["status"] = (
                        "completed"
                    )

                    operation["message"] = (
                        "Restore complete."
                    )

                else:
                    operation["status"] = (
                        "waiting_for_tape"
                    )

                    operation["message"] = (
                        "Insert the next required "
                        "cartridge."
                    )

            else:
                operation["status"] = "failed"

                operation["message"] = (
                    result.get(
                        "error",
                        "Restore failed.",
                    )
                )

            LAST_RESTORE_RESULTS[job_id] = {
                "result": result,
                "messages": list(
                    operation["messages"]
                ),
                "destination": destination,
            }

    except Exception as exc:
        result = {
            "success": False,
            "error": str(exc),
        }

        with OPERATION_STATE_LOCK:
            operation = OPERATIONS[
                operation_id
            ]

            operation["result"] = result
            operation["status"] = "failed"
            operation["message"] = str(exc)

            LAST_RESTORE_RESULTS[job_id] = {
                "result": result,
                "messages": list(
                    operation["messages"]
                ),
                "destination": destination,
            }

    finally:
        with OPERATION_STATE_LOCK:
            if (
                ACTIVE_TAPE_OPERATION_ID
                == operation_id
            ):
                ACTIVE_TAPE_OPERATION_ID = None


def _archive_staging_worker(
    operation_id,
    source_path,
):
    """
    Archive one staging file or directory outside the HTTP
    request thread.
    """

    global ACTIVE_TAPE_OPERATION_ID

    try:
        with OPERATION_STATE_LOCK:
            operation = OPERATIONS[
                operation_id
            ]

            operation["status"] = "running"
            operation["message"] = (
                "Archiving to tape..."
            )

            operation["messages"].append(
                "Archiving to tape..."
            )

        result = archive_path(
            Path(source_path)
        )

        with OPERATION_STATE_LOCK:
            operation = OPERATIONS[
                operation_id
            ]

            operation["result"] = result

            #
            # Folder jobs may successfully stop because another
            # cartridge is required. That is not a failure.
            #
            if result.get("success"):
                if (
                    result.get(
                        "completed"
                    ) is False
                    or result.get(
                        "waiting_for_tape",
                        False,
                    )
                    or result.get("status")
                    == "waiting_for_tape"
                ):
                    operation["status"] = (
                        "waiting_for_tape"
                    )

                    operation["message"] = (
                        "Insert the next cartridge "
                        "to continue the archive."
                    )

                else:
                    operation["status"] = (
                        "completed"
                    )

                    operation["message"] = (
                        "Archive complete."
                    )

            else:
                operation["status"] = "failed"

                operation["message"] = (
                    result.get(
                        "error",
                        "Archive failed.",
                    )
                )

            if operation["message"]:
                operation["messages"].append(
                    operation["message"]
                )

            #
            # A folder archive creates an archive job. Preserve
            # that ID in the generic operation structure so the
            # browser can later offer Resume Archive.
            #
            if result.get("job_id") is not None:
                operation["job_id"] = (
                    result["job_id"]
                )

    except Exception as exc:
        result = {
            "success": False,
            "error": str(exc),
        }

        with OPERATION_STATE_LOCK:
            operation = OPERATIONS.get(
                operation_id
            )

            if operation is not None:
                operation["result"] = result
                operation["status"] = "failed"
                operation["message"] = str(exc)
                operation["messages"].append(
                    str(exc)
                )

    finally:
        with OPERATION_STATE_LOCK:
            if (
                ACTIVE_TAPE_OPERATION_ID
                == operation_id
            ):
                ACTIVE_TAPE_OPERATION_ID = None



def format_bytes(value):
    if value is None:
        return "-"

    value = int(value)

    if value >= 1_000_000_000_000:
        return f"{value / 1_000_000_000_000:.2f} TB"

    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f} GB"

    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f} MB"

    if value >= 1_000:
        return f"{value / 1_000:.2f} KB"

    return f"{value} B"


app.jinja_env.filters["format_bytes"] = format_bytes


def get_drive_summary():
    drives = discover_drives()

    if not drives:
        return {
            "detected": False,
            "online": False,
            "description": "No tape drive detected",
            "serial": "-",
            "device": "-",
            "sg_device": "-",
            "density": "-",
            "write_protected": False,
        }

    drive = drives[0]

    nst_device = drive.get("nst_device")
    sg_device = drive.get("sg_device")

    summary = {
        "detected": True,
        "online": False,
        "description": drive.get(
            "description",
            "Tape Drive",
        ),
        "serial": drive.get(
            "serial",
            "-",
        ),
        "device": nst_device or "-",
        "sg_device": sg_device or "-",
        "density": "-",
        "write_protected": False,
    }

    if not nst_device:
        return summary

    status = get_tape_status(
        nst_device,
    )

    summary["online"] = bool(
        status.get("online")
    )

    summary["density"] = (
        status.get("density")
        or "-"
    )

    summary["write_protected"] = bool(
        status.get("write_protected")
    )

    summary["available"] = bool(
        status.get("available")
    )

    summary["error"] = status.get(
        "error"
    )

    return summary


@app.route("/")
def dashboard():
    initialize_database()

    tapes = list_tapes()
    files = list_files()
    jobs = list_archive_jobs()

    drive = get_drive_summary()

    total_capacity = sum(
        int(tape["capacity_bytes"] or 0)
        for tape in tapes
    )

    total_used = sum(
        int(tape["used_bytes"] or 0)
        for tape in tapes
    )

    total_archived_bytes = sum(
        int(file["size_bytes"] or 0)
        for file in files
    )

    recent_jobs = list(
        reversed(jobs[-8:])
    )

    return render_template(
        "dashboard.html",
        active_page="dashboard",
        drive=drive,
        tapes=tapes,
        recent_jobs=recent_jobs,
        tape_count=len(tapes),
        file_count=len(files),
        job_count=len(jobs),
        total_capacity=total_capacity,
        total_used=total_used,
        total_archived_bytes=total_archived_bytes,
    )





@app.route("/jobs")
def jobs_page():
    initialize_database()

    jobs = list_archive_jobs()

    return render_template(
        "jobs.html",
        active_page="jobs",
        jobs=jobs,
    )


@app.route("/jobs/<int:job_id>")
def job_detail_page(job_id):
    initialize_database()

    plan = get_archive_restore_plan(
        job_id
    )

    if plan is None:
        return (
            "Archive job not found",
            404,
        )

    job = plan["job"]
    files = plan["files"]
    tapes = plan["tapes"]

    last_restore = LAST_RESTORE_RESULTS.get(
        job_id
    )

    return render_template(
        "job_detail.html",
        active_page="jobs",
        job=job,
        files=files,
        tapes=tapes,
        last_restore=last_restore,
        default_destination=get_setting(
            "restore_directory",
            "/mnt/tapebox/restored",
        ),
    )


@app.route(
    "/jobs/<int:job_id>/restore",
    methods=["POST"],
)
def job_restore_action(job_id):
    global ACTIVE_TAPE_OPERATION_ID

    initialize_database()

    plan = get_archive_restore_plan(
        job_id
    )

    if plan is None:
        return jsonify(
            {
                "success": False,
                "error": (
                    "Archive job not found."
                ),
            }
        ), 404

    destination = request.form.get(
        "destination",
        "",
    ).strip()

    if not destination:
        return jsonify(
            {
                "success": False,
                "error": (
                    "Restore destination is required."
                ),
            }
        ), 400

    destination_path = Path(
        destination
    )

    if not destination_path.is_absolute():
        return jsonify(
            {
                "success": False,
                "error": (
                    "Restore destination must be "
                    "an absolute path."
                ),
            }
        ), 400

    #
    # Tape Inspector may still own the physical drive even
    # after a TapeBox/Flask restart, when in-memory operation
    # state has been lost. The mounted LTFS filesystem is
    # therefore authoritative.
    #
    inspector_mount = Path(
        "/mnt/tapebox/ltfs-inspect"
    )

    if _is_mounted(inspector_mount):
        return jsonify(
            {
                "success": False,
                "busy": True,
                "error": (
                    "Tape Inspector currently owns "
                    "the tape drive. Unmount and eject "
                    "the Inspector cartridge first."
                ),
                "operation": None,
            }
        ), 409

    with OPERATION_STATE_LOCK:
        if ACTIVE_TAPE_OPERATION_ID:
            active = OPERATIONS.get(
                ACTIVE_TAPE_OPERATION_ID
            )

            return jsonify(
                {
                    "success": False,
                    "busy": True,
                    "error": (
                        "Another tape operation is "
                        "already running."
                    ),
                    "operation": (
                        _operation_snapshot(active)
                        if active
                        else None
                    ),
                }
            ), 409

        operation_id = uuid.uuid4().hex

        operation = {
            "id": operation_id,
            "type": "restore_job",
            "job_id": job_id,
            "destination": destination,
            "status": "starting",
            "message": (
                "Starting restore..."
            ),
            "messages": [
                "Starting restore..."
            ],
            "transfer": None,
            "result": None,
        }

        OPERATIONS[operation_id] = (
            operation
        )

        ACTIVE_TAPE_OPERATION_ID = (
            operation_id
        )

    worker = threading.Thread(
        target=_restore_worker,
        args=(
            operation_id,
            job_id,
            destination,
        ),
        daemon=True,
        name=(
            f"tapebox-restore-{operation_id[:8]}"
        ),
    )

    try:
        worker.start()

    except Exception as exc:
        with OPERATION_STATE_LOCK:
            operation["status"] = "failed"
            operation["message"] = str(exc)
            operation["result"] = {
                "success": False,
                "error": str(exc),
            }

            if (
                ACTIVE_TAPE_OPERATION_ID
                == operation_id
            ):
                ACTIVE_TAPE_OPERATION_ID = (
                    None
                )

        return jsonify(
            {
                "success": False,
                "error": str(exc),
            }
        ), 500

    return jsonify(
        {
            "success": True,
            "operation_id": operation_id,
        }
    )


@app.route(
    "/api/operations/<operation_id>"
)
def operation_status(operation_id):
    with OPERATION_STATE_LOCK:
        operation = OPERATIONS.get(
            operation_id
        )

        if operation is None:
            return jsonify(
                {
                    "success": False,
                    "error": (
                        "Operation not found."
                    ),
                }
            ), 404

        snapshot = _operation_snapshot(
            operation
        )

    return jsonify(
        {
            "success": True,
            "operation": snapshot,
        }
    )


@app.route("/api/tape/status")
def tape_status_api():
    #
    # When LTFS Inspector is mounted it owns the tape device.
    # Low-level /dev/nst status may temporarily report the
    # drive unavailable while LTFS is seeking/reading, so the
    # active Inspector mount is authoritative.
    #
    inspector_mount = Path(
        "/mnt/tapebox/ltfs-inspect"
    )

    inspector_mounted = _is_mounted(
        inspector_mount
    )

    drives = discover_drives()

    if not drives:
        return jsonify(
            {
                "success": True,
                "detected": False,
                "online": inspector_mounted,
                "available": inspector_mounted,
                "mounted": inspector_mounted,
                "drive": None,
                "device": None,
                "density": None,
                "write_protected": False,
                "beginning_of_tape": False,
            }
        )

    drive = drives[0]

    nst_device = drive.get(
        "nst_device"
    )

    if not nst_device:
        return jsonify(
            {
                "success": True,
                "detected": True,
                "online": inspector_mounted,
                "available": inspector_mounted,
                "mounted": inspector_mounted,
                "drive": drive.get(
                    "description",
                    "Tape Drive",
                ),
                "device": None,
                "density": None,
                "write_protected": False,
                "beginning_of_tape": False,
            }
        )

    status = get_tape_status(
        nst_device
    )

    online = bool(
        status.get("online")
    )

    available = bool(
        status.get("available")
    )

    if inspector_mounted:
        online = True
        available = True

    return jsonify(
        {
            "success": True,
            "detected": True,
            "online": online,
            "available": available,
            "mounted": inspector_mounted,
            "drive": drive.get(
                "description",
                "Tape Drive",
            ),
            "device": nst_device,
            "density": status.get(
                "density"
            ),
            "write_protected": bool(
                status.get(
                    "write_protected"
                )
            ),
            "beginning_of_tape": bool(
                status.get(
                    "beginning_of_tape"
                )
            ),
            "error": status.get(
                "error"
            ),
        }
    )


@app.route("/restore")
def restore_page():
    initialize_database()

    jobs = list_archive_jobs()

    return render_template(
        "restore.html",
        active_page="restore",
        jobs=jobs,
    )


@app.route("/tapes")
def tapes_page():
    initialize_database()

    tapes = list_tapes()

    return render_template(
        "tapes.html",
        active_page="tapes",
        tapes=tapes,
    )


@app.route("/tapes/<int:tape_id>")
def tape_detail_page(tape_id):
    initialize_database()

    tape = get_tape_by_id(
        tape_id
    )

    if tape is None:
        return (
            "Tape not found",
            404,
        )

    #
    # Normal files physically assigned directly to this tape.
    #
    normal_files = get_files_by_tape(
        tape_id
    )

    #
    # Also find physical parts belonging to spanned logical files.
    #
    spanned_parts = []

    for file_row in list_files():
        if not file_row["is_spanned"]:
            continue

        parts = get_file_parts(
            file_row["id"]
        )

        for part in parts:
            if part["tape_id"] != tape_id:
                continue

            spanned_parts.append(
                {
                    "file_id": file_row["id"],
                    "filename": file_row["filename"],
                    "relative_path": file_row["relative_path"],
                    "logical_size": file_row["size_bytes"],
                    "part_number": part["part_number"],
                    "part_size": part["size_bytes"],
                    "tape_path": part["tape_path"],
                }
            )

    return render_template(
        "tape_detail.html",
        active_page="tapes",
        tape=tape,
        normal_files=normal_files,
        spanned_parts=spanned_parts,
    )


@app.route("/files")
def files_page():
    from flask import request

    initialize_database()

    query = request.args.get(
        "q",
        "",
    ).strip()

    if query:
        files = search_files(
            query
        )
    else:
        files = list_files()

    rows = []

    for file_row in files:
        required_tapes = []

        if file_row["is_spanned"]:
            parts = get_file_parts(
                file_row["id"]
            )

            for part in parts:
                label = (
                    part["tape_label"]
                    or part["ltfs_uuid"]
                    or f"Tape #{part['tape_id']}"
                )

                if label not in required_tapes:
                    required_tapes.append(
                        label
                    )

        else:
            label = (
                file_row["tape_label"]
                or file_row["ltfs_uuid"]
                or "-"
            )

            if label != "-":
                required_tapes.append(
                    label
                )

        rows.append(
            {
                "file": file_row,
                "required_tapes": required_tapes,
            }
        )

    return render_template(
        "files.html",
        active_page="files",
        query=query,
        rows=rows,
    )



def _get_restored_files_root():
    return Path(
        get_setting(
            "restore_directory",
            "/mnt/tapebox/restored",
        )
    )


def _resolve_restored_path(relative_path=""):
    root = _get_restored_files_root().resolve()

    relative = Path(
        relative_path or ""
    )

    if relative.is_absolute():
        raise ValueError(
            "Absolute paths are not allowed."
        )

    if ".." in relative.parts:
        raise ValueError(
            "Parent path traversal is not allowed."
        )

    target = (
        root / relative
    ).resolve()

    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError(
            "Path is outside the restored files area."
        )

    return root, target


def _get_staging_root():
    """
    Return the configured TapeBox staging directory.
    """

    return Path(
        get_setting(
            "staging_directory",
            "/mnt/tapebox/staging",
        )
    )


def _resolve_staging_path(relative_path=""):
    """
    Safely resolve a path inside the configured staging root.
    """

    root = _get_staging_root().resolve()

    relative = Path(
        relative_path or ""
    )

    if relative.is_absolute():
        raise ValueError(
            "Absolute paths are not allowed."
        )

    if ".." in relative.parts:
        raise ValueError(
            "Parent path traversal is not allowed."
        )

    target = (
        root / relative
    ).resolve()

    try:
        target.relative_to(root)

    except ValueError:
        raise ValueError(
            "Path is outside the staging area."
        )

    return root, target


@app.route("/staging")
def staging_page():
    """
    Browse the configured TapeBox staging directory.
    """

    initialize_default_settings()

    relative_path = request.args.get(
        "path",
        "",
    )

    error = None
    rows = []
    current_path = ""
    parent_path = None

    staging_root = _get_staging_root()

    total_bytes = 0
    used_bytes = 0
    free_bytes = 0

    try:
        staging_root.mkdir(
            parents=True,
            exist_ok=True,
        )

        root, target = _resolve_staging_path(
            relative_path
        )

        if not target.exists():
            raise ValueError(
                "Folder does not exist."
            )

        if not target.is_dir():
            raise ValueError(
                "Requested path is not a folder."
            )

        current_path = (
            ""
            if target == root
            else str(
                target.relative_to(root)
            )
        )

        if target != root:
            parent = target.parent

            parent_path = (
                ""
                if parent == root
                else str(
                    parent.relative_to(root)
                )
            )

        for entry in sorted(
            target.iterdir(),
            key=lambda item: (
                not item.is_dir(),
                item.name.lower(),
            ),
        ):
            #
            # Do not follow symlinks out of staging.
            #
            if entry.is_symlink():
                continue

            if entry.is_dir():
                entry_type = "directory"
                size = None

            elif entry.is_file():
                entry_type = "file"
                size = entry.stat().st_size

            else:
                continue

            rows.append(
                {
                    "name": entry.name,
                    "type": entry_type,
                    "size": size,
                    "path": str(
                        entry.relative_to(root)
                    ),
                }
            )

        usage = shutil.disk_usage(
            staging_root
        )

        total_bytes = usage.total
        used_bytes = usage.used
        free_bytes = usage.free

    except (OSError, ValueError) as exc:
        error = str(exc)

    return render_template(
        "staging.html",
        active_page="staging",
        rows=rows,
        error=error,
        current_path=current_path,
        parent_path=parent_path,
        staging_root=str(staging_root),
        total_bytes=total_bytes,
        used_bytes=used_bytes,
        free_bytes=free_bytes,
    )



@app.route(
    "/api/staging/archive",
    methods=["POST"],
)
def staging_archive_api():
    """
    Start archiving one file or directory from the configured
    staging area.
    """

    global ACTIVE_TAPE_OPERATION_ID

    initialize_database()
    initialize_default_settings()

    relative_path = request.form.get(
        "path",
        "",
    ).strip()

    if not relative_path:
        return jsonify(
            {
                "success": False,
                "error": (
                    "A staging file or folder "
                    "must be selected."
                ),
            }
        ), 400

    try:
        root, target = _resolve_staging_path(
            relative_path
        )

        #
        # Explicitly reject the selected item itself if it is a
        # symlink. Folder archive already rejects symlinks inside
        # a directory tree.
        #
        candidate = root / Path(
            relative_path
        )

        if candidate.is_symlink():
            raise ValueError(
                "Symbolic links cannot be archived."
            )

        if not target.exists():
            raise ValueError(
                "Selected staging item does not exist."
            )

        if not (
            target.is_file()
            or target.is_dir()
        ):
            raise ValueError(
                "Selected staging item is not "
                "a regular file or directory."
            )

    except (OSError, ValueError) as exc:
        return jsonify(
            {
                "success": False,
                "error": str(exc),
            }
        ), 400

    #
    # A mounted Inspector cartridge owns the physical tape drive
    # even if Flask's in-memory state has been lost.
    #
    inspector_mount = Path(
        "/mnt/tapebox/ltfs-inspect"
    )

    if _is_mounted(inspector_mount):
        return jsonify(
            {
                "success": False,
                "busy": True,
                "error": (
                    "Tape Inspector currently owns "
                    "the tape drive. Unmount and eject "
                    "the Inspector cartridge first."
                ),
                "operation": None,
            }
        ), 409

    with OPERATION_STATE_LOCK:
        if ACTIVE_TAPE_OPERATION_ID:
            active = OPERATIONS.get(
                ACTIVE_TAPE_OPERATION_ID
            )

            return jsonify(
                {
                    "success": False,
                    "busy": True,
                    "error": (
                        "Another tape operation is "
                        "already running."
                    ),
                    "operation": (
                        _operation_snapshot(active)
                        if active
                        else None
                    ),
                }
            ), 409

        operation_id = uuid.uuid4().hex

        operation = {
            "id": operation_id,
            "type": "archive_staging",
            "job_id": None,

            #
            # Keep this key because _operation_snapshot() is shared
            # with restore operations. For archive operations it is
            # the selected source rather than a restore destination.
            #
            "destination": str(target),

            "status": "starting",
            "message": (
                "Starting archive..."
            ),
            "messages": [
                "Starting archive..."
            ],
            "transfer": None,
            "result": None,
        }

        OPERATIONS[operation_id] = operation

        ACTIVE_TAPE_OPERATION_ID = (
            operation_id
        )

    worker = threading.Thread(
        target=_archive_staging_worker,
        args=(
            operation_id,
            str(target),
        ),
        daemon=True,
        name=(
            f"tapebox-archive-"
            f"{operation_id[:8]}"
        ),
    )

    try:
        worker.start()

    except Exception as exc:
        with OPERATION_STATE_LOCK:
            operation["status"] = "failed"
            operation["message"] = str(exc)
            operation["result"] = {
                "success": False,
                "error": str(exc),
            }

            if (
                ACTIVE_TAPE_OPERATION_ID
                == operation_id
            ):
                ACTIVE_TAPE_OPERATION_ID = (
                    None
                )

        return jsonify(
            {
                "success": False,
                "error": str(exc),
            }
        ), 500

    return jsonify(
        {
            "success": True,
            "operation_id": operation_id,
            "source": str(target),
        }
    )



@app.route("/restored")
def restored_files_page():
    relative_path = request.args.get(
        "path",
        "",
    )

    error = None
    rows = []
    current_path = ""
    parent_path = None

    try:
        _get_restored_files_root().mkdir(
            parents=True,
            exist_ok=True,
        )

        root, target = _resolve_restored_path(
            relative_path
        )

        if not target.exists():
            raise ValueError(
                "Folder does not exist."
            )

        if not target.is_dir():
            raise ValueError(
                "Requested path is not a folder."
            )

        current_path = (
            ""
            if target == root
            else str(
                target.relative_to(root)
            )
        )

        if target != root:
            parent = target.parent

            parent_path = (
                ""
                if parent == root
                else str(
                    parent.relative_to(root)
                )
            )

        entries = sorted(
            target.iterdir(),
            key=lambda item: (
                not item.is_dir(),
                item.name.casefold(),
            ),
        )

        for item in entries:
            try:
                resolved = item.resolve()

                resolved.relative_to(root)

                relative = str(
                    resolved.relative_to(root)
                )

                if item.is_dir():
                    rows.append(
                        {
                            "name": item.name,
                            "path": relative,
                            "type": "directory",
                            "size": None,
                        }
                    )

                elif item.is_file():
                    rows.append(
                        {
                            "name": item.name,
                            "path": relative,
                            "type": "file",
                            "size": item.stat().st_size,
                        }
                    )

            except (
                OSError,
                ValueError,
            ):
                continue

    except (
        OSError,
        ValueError,
    ) as exc:
        error = str(exc)

    return render_template(
        "restored_files.html",
        active_page="restored",
        rows=rows,
        current_path=current_path,
        parent_path=parent_path,
        error=error,
    )


@app.route("/restored/download")
def restored_file_download():
    relative_path = request.args.get(
        "path",
        "",
    )

    try:
        root, target = _resolve_restored_path(
            relative_path
        )

        if not target.exists():
            return (
                "File does not exist.",
                404,
            )

        if not target.is_file():
            return (
                "Requested path is not a file.",
                400,
            )

        target.relative_to(root)

        return send_file(
            target,
            as_attachment=True,
            download_name=target.name,
            conditional=True,
            max_age=0,
        )

    except ValueError as exc:
        return str(exc), 400

    except OSError as exc:
        return str(exc), 500



SETTINGS_BROWSE_ROOTS = (
    Path("/mnt"),
    Path("/media"),
    Path("/srv"),
)


def _settings_browse_directory(raw_path):
    """
    Return directories available to the Settings folder picker.
    """

    if not raw_path:
        roots = []

        for root in SETTINGS_BROWSE_ROOTS:
            if root.exists() and root.is_dir():
                roots.append(
                    {
                        "name": str(root),
                        "path": str(root),
                    }
                )

        return {
            "path": "",
            "parent": None,
            "directories": roots,
            "root_view": True,
        }

    requested = Path(raw_path)

    if not requested.is_absolute():
        raise ValueError(
            "Folder browser requires an absolute path."
        )

    target = requested.resolve()

    allowed_root = None

    for root in SETTINGS_BROWSE_ROOTS:
        root_resolved = root.resolve()

        try:
            target.relative_to(root_resolved)
            allowed_root = root_resolved
            break
        except ValueError:
            continue

    if allowed_root is None:
        raise ValueError(
            "That location is outside the allowed storage roots."
        )

    if not target.exists():
        raise ValueError(
            "Directory does not exist."
        )

    if not target.is_dir():
        raise ValueError(
            "Location is not a directory."
        )

    directories = []

    for item in target.iterdir():
        try:
            if item.is_symlink():
                continue

            if not item.is_dir():
                continue

            resolved = item.resolve()
            resolved.relative_to(allowed_root)

            directories.append(
                {
                    "name": item.name,
                    "path": str(resolved),
                }
            )

        except (OSError, ValueError):
            continue

    directories.sort(
        key=lambda item: item["name"].casefold()
    )

    if target == allowed_root:
        parent = ""
    else:
        parent = str(target.parent)

    return {
        "path": str(target),
        "parent": parent,
        "directories": directories,
        "root_view": False,
    }


def _latest_catalog_backup():
    """
    Return the newest TapeBox database backup, if one exists.
    """

    BACKUP_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    backups = sorted(
        BACKUP_DIR.glob("*.db"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )

    return backups[0] if backups else None


@app.route("/api/settings/database/status")
def settings_database_status_api():
    """
    Return catalog and latest backup information.
    """

    try:
        latest = _latest_catalog_backup()

        return jsonify(
            {
                "success": True,
                "catalog": {
                    "path": str(DB_PATH),
                    "size_bytes": (
                        DB_PATH.stat().st_size
                        if DB_PATH.exists()
                        else 0
                    ),
                },
                "latest_backup": (
                    {
                        "name": latest.name,
                        "path": str(latest),
                        "size_bytes": latest.stat().st_size,
                        "modified": latest.stat().st_mtime,
                    }
                    if latest
                    else None
                ),
            }
        )

    except OSError as exc:
        return jsonify(
            {
                "success": False,
                "error": str(exc),
            }
        ), 500


@app.route(
    "/api/settings/database/backup",
    methods=["POST"],
)
def settings_database_backup_api():
    """
    Create a consistent backup of the TapeBox catalog.
    """

    try:
        result = backup_catalog()

        backup_path = Path(
            result["path"]
        )

        return jsonify(
            {
                "success": True,
                "message": "Database backup completed.",
                "backup": {
                    "name": backup_path.name,
                    "path": str(backup_path),
                    "size_bytes": result["size_bytes"],
                },
            }
        )

    except Exception as exc:
        return jsonify(
            {
                "success": False,
                "error": str(exc),
            }
        ), 500


@app.route("/settings/database/download-latest")
def settings_database_download_latest():
    """
    Download the newest TapeBox database backup.
    """

    try:
        latest = _latest_catalog_backup()

        if latest is None:
            return jsonify(
                {
                    "success": False,
                    "error": "No database backups exist yet.",
                }
            ), 404

        return send_file(
            latest,
            as_attachment=True,
            download_name=latest.name,
            conditional=True,
            max_age=0,
        )

    except OSError as exc:
        return jsonify(
            {
                "success": False,
                "error": str(exc),
            }
        ), 500


@app.route(
    "/api/settings/database/restore",
    methods=["POST"],
)
def settings_database_restore_api():
    """
    Validate and restore an uploaded TapeBox catalog.
    """

    with OPERATION_STATE_LOCK:
        active_operation = ACTIVE_TAPE_OPERATION_ID

    if active_operation is not None:
        return jsonify(
            {
                "success": False,
                "busy": True,
                "error": (
                    "A tape operation is active. "
                    "Wait for it to finish before "
                    "restoring the database."
                ),
            }
        ), 409

    upload = request.files.get("backup")

    if upload is None:
        return jsonify(
            {
                "success": False,
                "error": "No database backup was uploaded.",
            }
        ), 400

    if not upload.filename:
        return jsonify(
            {
                "success": False,
                "error": "No backup file was selected.",
            }
        ), 400

    BACKUP_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    upload_path = (
        BACKUP_DIR
        / (
            "uploaded-"
            + uuid.uuid4().hex
            + ".db"
        )
    )

    try:
        upload.save(upload_path)

        validation = validate_catalog_database(
            upload_path
        )

        if not validation.get("success"):
            try:
                upload_path.unlink()
            except OSError:
                pass

            return jsonify(
                {
                    "success": False,
                    "error": validation.get(
                        "error",
                        "Database validation failed.",
                    ),
                }
            ), 400

        result = restore_catalog(
            upload_path
        )

        #
        # Recreate any newer schema additions,
        # such as the Settings table, when an
        # older valid TapeBox backup is restored.
        #
        initialize_database()
        initialize_default_settings()

        return jsonify(
            {
                "success": True,
                "message": "Database restored successfully.",
                "restored_from": str(upload_path),
                "pre_restore_backup": result[
                    "pre_restore_backup"
                ],
                "tapes": result["tapes"],
                "files": result["files"],
            }
        )

    except Exception as exc:
        return jsonify(
            {
                "success": False,
                "error": str(exc),
            }
        ), 500



@app.route("/api/settings/drive-status")
def settings_drive_status_api():
    """
    Return the currently discovered TapeBox tape drive.
    """

    try:
        drives = discover_drives()
        drive = drives[0] if drives else None

        return jsonify(
            {
                "success": True,
                "online": drive is not None,
                "drive_count": len(drives),
                "drive": drive,
            }
        )

    except OSError as exc:
        return jsonify(
            {
                "success": False,
                "online": False,
                "error": str(exc),
                "drive": None,
            }
        ), 500



@app.route("/api/settings/rescan-scsi", methods=["POST"])
def settings_rescan_scsi_api():
    """
    Rescan the Linux SCSI bus and rediscover TapeBox tape drives.
    """

    command = [
        "sudo",
        "-n",
        "/usr/bin/rescan-scsi-bus.sh",
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

        output = (
            (result.stdout or "")
            + (result.stderr or "")
        ).strip()

        drives = discover_drives()

        drive = drives[0] if drives else None

        if result.returncode != 0:
            return jsonify(
                {
                    "success": False,
                    "returncode": result.returncode,
                    "error": (
                        "SCSI rescan failed."
                    ),
                    "output": output,
                    "drive": drive,
                }
            ), 500

        return jsonify(
            {
                "success": True,
                "returncode": result.returncode,
                "message": (
                    "SCSI bus rescan completed."
                ),
                "output": output,
                "drive": drive,
                "drive_count": len(drives),
            }
        )

    except subprocess.TimeoutExpired:
        return jsonify(
            {
                "success": False,
                "error": (
                    "SCSI rescan timed out after 120 seconds."
                ),
            }
        ), 504

    except OSError as exc:
        return jsonify(
            {
                "success": False,
                "error": str(exc),
            }
        ), 500



@app.route("/api/settings/directories")
def settings_directories_api():
    """
    Browse server storage directories for Settings.
    """

    try:
        result = _settings_browse_directory(
            request.args.get("path", "")
        )

        return jsonify(
            {
                "success": True,
                **result,
            }
        )

    except (OSError, ValueError) as exc:
        return jsonify(
            {
                "success": False,
                "error": str(exc),
            }
        ), 400



@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    """
    TapeBox configuration page.
    """

    initialize_default_settings()

    message = None
    error = None

    if request.method == "POST":
        restore_directory = (
            request.form.get(
                "restore_directory",
                "",
            ).strip()
        )

        staging_directory = (
            request.form.get(
                "staging_directory",
                "",
            ).strip()
        )

        try:
            if not restore_directory:
                raise ValueError(
                    "Restore directory cannot be empty."
                )

            if not staging_directory:
                raise ValueError(
                    "Staging directory cannot be empty."
                )

            if not Path(restore_directory).is_absolute():
                raise ValueError(
                    "Restore directory must be an absolute path."
                )

            if not Path(staging_directory).is_absolute():
                raise ValueError(
                    "Staging directory must be an absolute path."
                )

            Path(restore_directory).mkdir(
                parents=True,
                exist_ok=True,
            )

            Path(staging_directory).mkdir(
                parents=True,
                exist_ok=True,
            )

            set_setting(
                "restore_directory",
                restore_directory,
            )

            set_setting(
                "staging_directory",
                staging_directory,
            )

            message = "Settings saved."

        except (OSError, ValueError) as exc:
            error = str(exc)

    return render_template(
        "settings.html",
        active_page="settings",
        settings=get_settings(),
        message=message,
        error=error,
    )



@app.route("/inspector")
def tape_inspector_page():
    return render_template(
        "inspector.html",
        active_page="inspector",
    )


@app.route(
    "/api/inspector/browse",
    methods=["GET"],
)
def inspector_browse_api():
    relative_path = request.args.get(
        "path",
        "",
    )

    result = browse_ltfs_inspector(
        relative_path=relative_path,
    )

    if not result.get("success"):
        return jsonify(result), 400

    #
    # Recover cartridge identity from the already-mounted
    # read-only LTFS filesystem. This allows the Inspector
    # page to survive a browser refresh without remounting.
    #
    mount_path = Path(
        "/mnt/tapebox/ltfs-inspect"
    )

    result["label"] = (
        get_ltfs_virtual_attribute(
            mount_path,
            "ltfs.volumeName",
        )
        or None
    )

    result["uuid"] = (
        get_ltfs_virtual_attribute(
            mount_path,
            "ltfs.volumeUUID",
        )
        or None
    )

    result["volume_serial"] = (
        get_ltfs_virtual_attribute(
            mount_path,
            "ltfs.volumeSerial",
        )
        or None
    )

    drives = discover_drives()

    if drives:
        drive = drives[0]

        result["drive"] = (
            drive.get("description")
            or drive.get("name")
            or "Tape Drive"
        )

        nst_device = (
            drive.get("nst_device")
            or "/dev/tapebox-drive-nst"
        )

        tape_status = get_tape_status(
            nst_device
        )

        result["density"] = (
            tape_status.get("density")
            or drive.get("density")
            or "-"
        )
    else:
        result["drive"] = "Tape Drive"
        result["density"] = "-"

    return jsonify(result)


@app.route(
    "/api/inspector/eject",
    methods=["POST"],
)
def inspector_eject_api():
    global ACTIVE_TAPE_OPERATION_ID

    with OPERATION_STATE_LOCK:
        active_operation = (
            ACTIVE_TAPE_OPERATION_ID
        )

    if active_operation not in {
        None,
        "inspector",
    }:
        return jsonify(
            {
                "success": False,
                "busy": True,
                "error": (
                    "A tape operation is currently "
                    "active. Wait for it to finish "
                    "before unmounting or ejecting."
                ),
            }
        ), 409

    mount_path = Path(
        "/mnt/tapebox/ltfs-inspect"
    )

    drives = discover_drives()

    if not drives:
        return jsonify(
            {
                "success": False,
                "error": "No tape drive detected.",
            }
        ), 404

    drive = drives[0]

    nst_device = drive.get(
        "nst_device"
    )

    if not nst_device:
        return jsonify(
            {
                "success": False,
                "error": (
                    "Tape device could not be resolved."
                ),
            }
        ), 500

    #
    # SAFETY:
    # Never eject unless LTFS has cleanly unmounted
    # and released the tape device.
    #
    success, error = _unmount_ltfs(
        mount_path
    )

    if not success:
        return jsonify(
            {
                "success": False,
                "unmounted": False,
                "ejected": False,
                "error": (
                    "LTFS could not be cleanly unmounted. "
                    "Tape was NOT ejected. "
                    + str(error)
                ),
            }
        ), 500

    #
    # LTFS has cleanly released the device. Inspector no
    # longer owns the drive, even if the following physical
    # eject command happens to fail.
    #
    with OPERATION_STATE_LOCK:
        if (
            ACTIVE_TAPE_OPERATION_ID
            == "inspector"
        ):
            ACTIVE_TAPE_OPERATION_ID = None

    eject_result = eject_tape(
        nst_device
    )

    if not eject_result.get("success"):
        return jsonify(
            {
                "success": False,
                "unmounted": True,
                "ejected": False,
                "error": (
                    eject_result.get("error")
                    or "Tape eject failed."
                ),
            }
        ), 500

    return jsonify(
        {
            "success": True,
            "unmounted": True,
            "ejected": True,
            "message": (
                "LTFS unmounted cleanly "
                "and cartridge ejected."
            ),
        }
    )


@app.route(
    "/api/inspector/mount",
    methods=["POST"],
)
def inspector_mount_api():
    global ACTIVE_TAPE_OPERATION_ID

    inspector_owner = "inspector"

    #
    # Reserve the physical tape drive BEFORE beginning LTFS
    # mounting. This closes the race where a restore could
    # start while Inspector was in the middle of mounting.
    #
    with OPERATION_STATE_LOCK:
        if ACTIVE_TAPE_OPERATION_ID:
            return jsonify(
                {
                    "success": False,
                    "busy": True,
                    "error": (
                        "Another tape operation is "
                        "currently active."
                    ),
                }
            ), 409

        ACTIVE_TAPE_OPERATION_ID = (
            inspector_owner
        )

    try:
        drives = discover_drives()

        if not drives:
            return jsonify(
                {
                    "success": False,
                    "error": (
                        "No tape drive detected."
                    ),
                }
            ), 404

        drive = drives[0]

        nst_device = drive.get(
            "nst_device"
        )

        sg_device = drive.get(
            "sg_device"
        )

        if not nst_device or not sg_device:
            return jsonify(
                {
                    "success": False,
                    "error": (
                        "Tape drive devices could "
                        "not be resolved."
                    ),
                }
            ), 500

        status = get_tape_status(
            nst_device
        )

        if not (
            status.get("available")
            and status.get("online")
        ):
            return jsonify(
                {
                    "success": False,
                    "error": (
                        "Tape cartridge is not online."
                    ),
                }
            ), 409

        result = mount_ltfs_inspector(
            sg_device=sg_device,
        )

        if not result.get("success"):
            return jsonify(result), 500

        result["drive"] = drive.get(
            "description"
        )

        result["density"] = status.get(
            "density"
        )

        #
        # IMPORTANT:
        # Do NOT release ACTIVE_TAPE_OPERATION_ID here.
        # Inspector owns the drive for the lifetime of the
        # mounted LTFS filesystem.
        #
        return jsonify(result)

    finally:
        #
        # Keep ownership only if Inspector successfully
        # established/retained the LTFS mount.
        #
        inspector_mount = Path(
            "/mnt/tapebox/ltfs-inspect"
        )

        if not _is_mounted(inspector_mount):
            with OPERATION_STATE_LOCK:
                if (
                    ACTIVE_TAPE_OPERATION_ID
                    == inspector_owner
                ):
                    ACTIVE_TAPE_OPERATION_ID = None



def _inspector_copy_worker(
    operation_id,
    selected,
    destination,
):
    """
    Copy selected Inspector files/folders in the background.
    """

    global ACTIVE_TAPE_OPERATION_ID

    source_root = Path(
        "/mnt/tapebox/ltfs-inspect"
    )

    destination_root = Path(
        destination
    )

    chunk_size = 8 * 1024 * 1024
    started = time.monotonic()

    def update_operation(
        *,
        message=None,
        transfer=None,
        status=None,
        result=None,
    ):
        with OPERATION_STATE_LOCK:
            operation = OPERATIONS.get(
                operation_id
            )

            if operation is None:
                return

            if message is not None:
                operation["message"] = message

            if transfer is not None:
                operation["transfer"] = transfer

            if status is not None:
                operation["status"] = status

            if result is not None:
                operation["result"] = result

    try:
        if not _is_mounted(source_root):
            raise RuntimeError(
                "Tape Inspector is no longer mounted."
            )

        root_real = source_root.resolve(
            strict=True
        )

        #
        # Validate selections and build the exact file list.
        # Directory recursion happens only after Copy Selected.
        #
        files_to_copy = []
        directories = []

        update_operation(
            message="Scanning selected items..."
        )

        for value in selected:
            if (
                not isinstance(value, str)
                or not value.strip()
            ):
                raise ValueError(
                    "Invalid selected path."
                )

            relative = Path(
                value.strip()
            )

            if (
                relative.is_absolute()
                or ".." in relative.parts
            ):
                raise ValueError(
                    "Selected path is outside "
                    "the LTFS cartridge."
                )

            current = source_root

            for part in relative.parts:
                current = current / part

                if current.is_symlink():
                    raise ValueError(
                        "Symbolic links are not "
                        "allowed in Inspector copies."
                    )

            source = (
                source_root / relative
            ).resolve(
                strict=True
            )

            try:
                source.relative_to(
                    root_real
                )
            except ValueError:
                raise ValueError(
                    "Selected path is outside "
                    "the LTFS cartridge."
                )

            if source.is_file():
                files_to_copy.append(
                    (
                        source,
                        relative,
                        source.stat().st_size,
                    )
                )

                continue

            if not source.is_dir():
                raise ValueError(
                    f"Unsupported LTFS item: "
                    f"{relative}"
                )

            directories.append(
                relative
            )

            for child in source.rglob("*"):
                if child.is_symlink():
                    raise ValueError(
                        "Symbolic links are not "
                        "allowed in Inspector copies."
                    )

                child_relative = (
                    child.relative_to(
                        source_root
                    )
                )

                if child.is_dir():
                    directories.append(
                        child_relative
                    )

                elif child.is_file():
                    files_to_copy.append(
                        (
                            child,
                            child_relative,
                            child.stat().st_size,
                        )
                    )

        total_bytes = sum(
            item[2]
            for item in files_to_copy
        )

        total_files = len(
            files_to_copy
        )

        for relative in directories:
            (
                destination_root
                / relative
            ).mkdir(
                parents=True,
                exist_ok=True,
            )

        copied_bytes = 0
        completed_files = 0

        update_operation(
            message=(
                f"Copying {total_files} file"
                + (
                    ""
                    if total_files == 1
                    else "s"
                )
                + "..."
            ),
            transfer={
                "type": "transfer_start",
                "current_file": None,
                "file_bytes": 0,
                "file_size": 0,
                "bytes_copied": 0,
                "total_bytes": total_bytes,
                "files_completed": 0,
                "total_files": total_files,
                "speed_bps": 0,
                "elapsed_seconds": 0,
                "eta_seconds": None,
            },
        )

        for (
            source,
            relative,
            file_size,
        ) in files_to_copy:
            if not _is_mounted(source_root):
                raise RuntimeError(
                    "Tape Inspector mount was lost "
                    "during copy."
                )

            destination_file = (
                destination_root
                / relative
            )

            destination_file.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            temp_file = (
                destination_file.parent
                / (
                    "."
                    + destination_file.name
                    + ".tapebox-copying-"
                    + operation_id
                )
            )

            file_bytes = 0

            try:
                with source.open("rb") as src:
                    with temp_file.open("wb") as dst:
                        while True:
                            chunk = src.read(
                                chunk_size
                            )

                            if not chunk:
                                break

                            dst.write(chunk)

                            length = len(chunk)

                            file_bytes += length
                            copied_bytes += length

                            elapsed = max(
                                time.monotonic()
                                - started,
                                0.001,
                            )

                            speed = (
                                copied_bytes
                                / elapsed
                            )

                            remaining = max(
                                total_bytes
                                - copied_bytes,
                                0,
                            )

                            eta = (
                                remaining / speed
                                if speed > 0
                                else None
                            )

                            update_operation(
                                message=(
                                    "Copying "
                                    + str(relative)
                                ),
                                transfer={
                                    "type": "transfer",
                                    "current_file":
                                        str(relative),
                                    "file_bytes":
                                        file_bytes,
                                    "file_size":
                                        file_size,
                                    "bytes_copied":
                                        copied_bytes,
                                    "total_bytes":
                                        total_bytes,
                                    "files_completed":
                                        completed_files,
                                    "total_files":
                                        total_files,
                                    "speed_bps":
                                        speed,
                                    "elapsed_seconds":
                                        elapsed,
                                    "eta_seconds":
                                        eta,
                                },
                            )

                shutil.copystat(
                    source,
                    temp_file,
                    follow_symlinks=False,
                )

                temp_file.replace(
                    destination_file
                )

            except Exception:
                try:
                    temp_file.unlink(
                        missing_ok=True
                    )
                except Exception:
                    pass

                raise

            completed_files += 1

        elapsed = max(
            time.monotonic() - started,
            0.001,
        )

        speed = (
            copied_bytes / elapsed
            if copied_bytes
            else 0
        )

        result = {
            "success": True,
            "completed": True,
            "files_copied": completed_files,
            "bytes_copied": copied_bytes,
            "destination": str(
                destination_root
            ),
        }

        update_operation(
            status="completed",
            message="Copy complete.",
            result=result,
            transfer={
                "type": "transfer",
                "current_file": None,
                "file_bytes": 0,
                "file_size": 0,
                "bytes_copied": copied_bytes,
                "total_bytes": total_bytes,
                "files_completed":
                    completed_files,
                "total_files": total_files,
                "speed_bps": speed,
                "elapsed_seconds": elapsed,
                "eta_seconds": 0,
            },
        )

    except Exception as exc:
        result = {
            "success": False,
            "error": str(exc),
        }

        update_operation(
            status="failed",
            message=str(exc),
            result=result,
        )

    finally:
        #
        # Copy is finished, but Inspector should continue
        # owning the tape if its LTFS mount still exists.
        #
        with OPERATION_STATE_LOCK:
            if (
                ACTIVE_TAPE_OPERATION_ID
                == operation_id
            ):
                if _is_mounted(
                    source_root
                ):
                    ACTIVE_TAPE_OPERATION_ID = (
                        "inspector"
                    )
                else:
                    ACTIVE_TAPE_OPERATION_ID = None


@app.route(
    "/api/inspector/copy",
    methods=["POST"],
)
def inspector_copy_api():
    global ACTIVE_TAPE_OPERATION_ID

    inspector_owner = "inspector"

    source_root = Path(
        "/mnt/tapebox/ltfs-inspect"
    )

    destination_root = _get_restored_files_root()

    if not _is_mounted(source_root):
        return jsonify(
            {
                "success": False,
                "error": (
                    "Tape Inspector is not mounted."
                ),
            }
        ), 409

    payload = request.get_json(
        silent=True,
    )

    if not isinstance(payload, dict):
        return jsonify(
            {
                "success": False,
                "error": (
                    "Invalid copy request."
                ),
            }
        ), 400

    selected = payload.get(
        "paths"
    )

    if (
        not isinstance(selected, list)
        or not selected
    ):
        return jsonify(
            {
                "success": False,
                "error": (
                    "No files or folders were selected."
                ),
            }
        ), 400

    #
    # Basic request validation happens before creating
    # the background operation.
    #
    for value in selected:
        if (
            not isinstance(value, str)
            or not value.strip()
        ):
            return jsonify(
                {
                    "success": False,
                    "error": (
                        "Invalid selected path."
                    ),
                }
            ), 400

    operation_id = str(
        uuid.uuid4()
    )

    #
    # Atomically transfer ownership:
    #
    #     inspector -> copy operation UUID
    #
    # None is also accepted to recover cleanly after a
    # Flask restart while LTFS remains mounted.
    #
    with OPERATION_STATE_LOCK:
        if (
            ACTIVE_TAPE_OPERATION_ID
            not in {
                None,
                inspector_owner,
            }
        ):
            return jsonify(
                {
                    "success": False,
                    "busy": True,
                    "error": (
                        "Another tape operation is "
                        "currently active."
                    ),
                }
            ), 409

        OPERATIONS[operation_id] = {
            "id": operation_id,
            "type": "inspector_copy",
            "job_id": None,
            "destination": str(
                destination_root
            ),
            "status": "running",
            "message": (
                "Preparing selected items..."
            ),
            "messages": [],
            "transfer": None,
            "result": None,
        }

        ACTIVE_TAPE_OPERATION_ID = (
            operation_id
        )

    worker = threading.Thread(
        target=_inspector_copy_worker,
        args=(
            operation_id,
            list(selected),
            str(destination_root),
        ),
        daemon=True,
    )

    try:
        worker.start()

    except Exception:
        with OPERATION_STATE_LOCK:
            OPERATIONS.pop(
                operation_id,
                None,
            )

            if (
                ACTIVE_TAPE_OPERATION_ID
                == operation_id
            ):
                ACTIVE_TAPE_OPERATION_ID = (
                    inspector_owner
                )

        raise

    return jsonify(
        {
            "success": True,
            "operation_id": operation_id,
        }
    )



def main():
    parser = argparse.ArgumentParser(
        description="TapeBox Web UI"
    )

    parser.add_argument(
        "--host",
        default="127.0.0.1",
    )

    parser.add_argument(
        "--port",
        type=int,
        default=8080,
    )

    parser.add_argument(
        "--debug",
        action="store_true",
    )

    args = parser.parse_args()

    initialize_database()

    app.run(
        host=args.host,
        port=args.port,
        debug=args.debug,
    )


if __name__ == "__main__":
    main()
