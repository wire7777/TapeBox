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
    update_archive_job,
    get_archive_job_files,
    get_archive_restore_plan,
    get_tape_by_id,
    get_tape_by_uuid,
    update_tape_catalog_metadata,
    register_existing_ltfs_tape,
    get_files_by_tape,
    search_files,
    get_file_by_id,
    get_file_parts,
    get_setting,
    get_settings,
    set_setting,
    initialize_default_settings,
    backup_catalog,
    validate_catalog_database,
    restore_catalog,
    check_catalog_health,
    repair_catalog_database,
    save_restore_operation,
    get_restore_operation,
    get_latest_resumable_restore_operation,
    DB_PATH,
    BACKUP_DIR,
)

from tapebox.restore import (
    restore_archive_job,
    restore_selected_files,
)

from tapebox.archive import (
    archive_path,
    create_archive_selection_snapshot,
)

from tapebox.tape import (
    discover_drives,
    get_tape_status,
    inspect_ltfs,
    mount_ltfs_inspector,
    browse_ltfs_inspector,
    get_ltfs_virtual_attribute,
    _is_mounted,
    _unmount_ltfs,
    eject_tape,
    format_ltfs,
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


def _persist_selected_restore(operation):
    """
    Persist selected-file restore state without allowing a
    catalog persistence error to interrupt tape I/O.
    """

    if (
        not operation
        or operation.get("type")
        != "restore_selected"
    ):
        return

    try:
        save_restore_operation(operation)

    except Exception as exc:
        print(
            "WARNING: Could not persist selected "
            f"restore operation: {exc}"
        )


def _operation_snapshot(operation):
    """
    Return a JSON-safe copy of operation state.
    """

    return {
        "id": operation["id"],
        "type": operation["type"],
        "job_id": operation["job_id"],
        "file_ids": list(
            operation.get(
                "file_ids",
                [],
            )
        ),
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

                    if result.get(
                        "already_restored",
                        False,
                    ):
                        operation["message"] = (
                            "Already restored — existing "
                            "file matches the TapeBox catalog."
                        )
                    else:
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

            elif result.get("wrong_tape"):
                operation["status"] = (
                    "waiting_for_tape"
                )

                operation["message"] = (
                    result.get(
                        "error",
                        "Wrong cartridge inserted.",
                    )
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


def _selected_restore_worker(
    operation_id,
    file_ids,
    destination,
):
    """
    Run one arbitrary selected-file restore outside the HTTP
    request thread.
    """

    global ACTIVE_TAPE_OPERATION_ID

    last_persist_at = 0.0

    def progress(event):
        nonlocal last_persist_at

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

            event_type = (
                event.get("type")
                if isinstance(event, dict)
                else None
            )

            now = time.monotonic()

            should_persist = (
                event_type != "transfer"
                or (
                    now - last_persist_at
                    >= 5.0
                )
            )

            if should_persist:
                _persist_selected_restore(
                    operation
                )

                last_persist_at = now

    progress.tapebox_structured_progress = True

    try:
        result = restore_selected_files(
            file_ids,
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

            elif result.get("wrong_tape"):
                operation["status"] = (
                    "waiting_for_tape"
                )

                operation["message"] = (
                    result.get(
                        "error",
                        "Wrong cartridge inserted.",
                    )
                )

            else:
                operation["status"] = "failed"

                operation["message"] = (
                    result.get(
                        "error",
                        "Restore failed.",
                    )
                )

            _persist_selected_restore(
                operation
            )

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

            _persist_selected_restore(
                operation
            )

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
    cleanup_on_complete=False,
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

        def archive_progress(progress):
            with OPERATION_STATE_LOCK:
                operation = OPERATIONS.get(
                    operation_id
                )

                if operation is None:
                    return

                operation["transfer"] = dict(
                    progress
                )

                phase = progress.get(
                    "phase"
                )

                filename = progress.get(
                    "filename"
                )

                if phase == "copying":
                    operation["message"] = (
                        f"Writing {filename} to tape..."
                        if filename
                        else "Writing to tape..."
                    )

                elif phase == "finalizing_file":
                    operation["message"] = (
                        f"Finalizing {filename}..."
                        if filename
                        else "Finalizing file..."
                    )

                elif phase == "finalizing":
                    operation["message"] = (
                        f"Finalizing {filename}..."
                        if filename
                        else "Finalizing archive..."
                    )

        result = archive_path(
            Path(source_path),
            progress_callback=archive_progress,
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

        #
        # A web selection snapshot consists only of hard links.
        # Once the entire archive job is complete, it is safe to
        # remove those links. Keep the snapshot for waiting/error
        # states so the resumable archive job still has its source.
        #
        if (
            cleanup_on_complete
            and result.get("success")
            and result.get("completed") is True
        ):
            snapshot_path = Path(
                source_path
            )

            try:
                if (
                    snapshot_path.is_dir()
                    and snapshot_path.parent.name
                    == ".tapebox-jobs"
                ):
                    shutil.rmtree(
                        snapshot_path
                    )

            except Exception as cleanup_exc:
                with OPERATION_STATE_LOCK:
                    operation = OPERATIONS.get(
                        operation_id
                    )

                    if operation is not None:
                        operation["messages"].append(
                            "Archive completed, but the "
                            "selection snapshot could not be "
                            "removed: "
                            + str(cleanup_exc)
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



@app.route(
    "/jobs/<int:job_id>/cancel",
    methods=["POST"],
)
def cancel_archive_job_action(job_id):
    """
    Cancel an abandoned/stale archive job.

    A job that still has an active in-memory TapeBox operation
    cannot be cancelled here because the worker may currently be
    writing or finalizing tape data.
    """

    global ACTIVE_TAPE_OPERATION_ID

    initialize_database()

    job = get_archive_job(
        job_id
    )

    if job is None:
        return jsonify(
            {
                "success": False,
                "error": "Archive job not found.",
            }
        ), 404

    if job["status"] not in (
        "running",
        "waiting_for_tape",
    ):
        return jsonify(
            {
                "success": False,
                "error": (
                    "Only RUNNING or WAITING_FOR_TAPE "
                    "jobs can be cancelled."
                ),
            }
        ), 409

    #
    # Never change SQLite underneath a worker that is still
    # actively using this archive job.
    #
    with OPERATION_STATE_LOCK:
        if ACTIVE_TAPE_OPERATION_ID:
            active = OPERATIONS.get(
                ACTIVE_TAPE_OPERATION_ID
            )

            if (
                active is not None
                and active.get("job_id")
                == job_id
                and active.get("status")
                not in (
                    "completed",
                    "failed",
                    "waiting_for_tape",
                )
            ):
                return jsonify(
                    {
                        "success": False,
                        "busy": True,
                        "error": (
                            "This archive job is still actively "
                            "running. TapeBox will not cancel it "
                            "while a tape write may be in progress."
                        ),
                    }
                ), 409

    update_archive_job(
        job_id,
        status="cancelled",
        error="Cancelled by user.",
    )

    return jsonify(
        {
            "success": True,
            "job_id": job_id,
            "status": "cancelled",
        }
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
            operation = get_restore_operation(
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

            if operation.get("status") in {
                "starting",
                "running",
            }:
                operation["status"] = (
                    "waiting_for_tape"
                )
                operation["message"] = (
                    "Restore was interrupted. "
                    "Continue restore when ready."
                )
                operation["messages"].append(
                    "TapeBox restarted during "
                    "the restore."
                )

                _persist_selected_restore(
                    operation
                )

            OPERATIONS[operation_id] = (
                operation
            )

        snapshot = _operation_snapshot(
            operation
        )

    return jsonify(
        {
            "success": True,
            "operation": snapshot,
        }
    )


@app.route(
    "/api/operations/<operation_id>/resume",
    methods=["POST"],
)
def operation_resume_api(operation_id):
    """
    Resume a selected-file restore that is waiting for the
    next cartridge.
    """

    global ACTIVE_TAPE_OPERATION_ID

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
            }
        ), 409

    with OPERATION_STATE_LOCK:
        operation = OPERATIONS.get(
            operation_id
        )

        if operation is None:
            operation = get_restore_operation(
                operation_id
            )

            if operation is None:
                return jsonify(
                    {
                        "success": False,
                        "error": "Operation not found.",
                    }
                ), 404

            if operation.get("status") in {
                "starting",
                "running",
            }:
                operation["status"] = (
                    "waiting_for_tape"
                )
                operation["message"] = (
                    "Restore was interrupted. "
                    "Continue restore when ready."
                )
                operation["messages"].append(
                    "TapeBox restarted during "
                    "the restore."
                )

                _persist_selected_restore(
                    operation
                )

            OPERATIONS[operation_id] = (
                operation
            )

        if (
            operation.get("type")
            != "restore_selected"
        ):
            return jsonify(
                {
                    "success": False,
                    "error": (
                        "This operation cannot be "
                        "resumed here."
                    ),
                }
            ), 409

        if (
            operation.get("status")
            != "waiting_for_tape"
        ):
            return jsonify(
                {
                    "success": False,
                    "error": (
                        "Restore is not waiting "
                        "for a cartridge."
                    ),
                }
            ), 409

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

        file_ids = list(
            operation.get(
                "file_ids",
                [],
            )
        )

        destination = str(
            operation["destination"]
        )

        if not file_ids:
            return jsonify(
                {
                    "success": False,
                    "error": (
                        "Restore operation has no "
                        "selected files."
                    ),
                }
            ), 409

        operation["status"] = "starting"
        operation["message"] = (
            "Continuing restore..."
        )
        operation["messages"].append(
            "Continuing restore..."
        )
        operation["transfer"] = None
        operation["result"] = None

        ACTIVE_TAPE_OPERATION_ID = (
            operation_id
        )

        _persist_selected_restore(
            operation
        )

    worker = threading.Thread(
        target=_selected_restore_worker,
        args=(
            operation_id,
            file_ids,
            destination,
        ),
        daemon=True,
        name=(
            f"tapebox-selected-restore-"
            f"{operation_id[:8]}"
        ),
    )

    try:
        worker.start()

    except Exception as exc:
        with OPERATION_STATE_LOCK:
            operation = OPERATIONS.get(
                operation_id
            )

            if operation is not None:
                operation["status"] = (
                    "waiting_for_tape"
                )
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


@app.route(
    "/tapes/<int:tape_id>/metadata",
    methods=["POST"],
)
def update_tape_metadata_action(tape_id):
    """
    Update TapeBox-only cartridge metadata.

    This does not modify the physical LTFS cartridge.
    """

    from flask import (
        request,
        redirect,
        url_for,
    )

    initialize_database()

    tape = get_tape_by_id(
        tape_id
    )

    if tape is None:
        return (
            "Tape not found",
            404,
        )

    update_tape_catalog_metadata(
        tape_id=tape_id,
        friendly_name=request.form.get(
            "friendly_name",
            "",
        ),
        location=request.form.get(
            "location",
            "",
        ),
        notes=request.form.get(
            "notes",
            "",
        ),
    )

    return redirect(
        url_for(
            "tape_detail_page",
            tape_id=tape_id,
            saved="1",
        )
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


@app.route(
    "/api/files/restore-plan",
    methods=["POST"],
)
def files_restore_plan_api():
    """
    Build a read-only restore plan for selected catalog files.

    This endpoint does not access the tape drive.
    """

    initialize_database()

    payload = request.get_json(
        silent=True
    ) or {}

    raw_file_ids = payload.get(
        "file_ids",
        [],
    )

    if not isinstance(raw_file_ids, list):
        return jsonify(
            {
                "success": False,
                "error": "file_ids must be a list.",
            }
        ), 400

    file_ids = []

    for value in raw_file_ids:
        try:
            file_id = int(value)
        except (TypeError, ValueError):
            return jsonify(
                {
                    "success": False,
                    "error": (
                        f"Invalid file ID: {value}"
                    ),
                }
            ), 400

        if file_id <= 0:
            return jsonify(
                {
                    "success": False,
                    "error": (
                        f"Invalid file ID: {value}"
                    ),
                }
            ), 400

        if file_id not in file_ids:
            file_ids.append(file_id)

    if not file_ids:
        return jsonify(
            {
                "success": False,
                "error": "No files were selected.",
            }
        ), 400

    planned_files = []
    required_tapes = []
    seen_tape_ids = set()
    total_bytes = 0

    for file_id in file_ids:
        row = get_file_by_id(
            file_id
        )

        if row is None:
            return jsonify(
                {
                    "success": False,
                    "error": (
                        f"File ID {file_id} "
                        "does not exist."
                    ),
                }
            ), 404

        total_bytes += int(
            row["size_bytes"] or 0
        )

        file_tapes = []

        if row["is_spanned"]:
            parts = get_file_parts(
                file_id
            )

            if not parts:
                return jsonify(
                    {
                        "success": False,
                        "error": (
                            f"Spanned file ID {file_id} "
                            "has no cataloged parts."
                        ),
                    }
                ), 409

            for part in parts:
                tape_id = part["tape_id"]

                if tape_id is None:
                    return jsonify(
                        {
                            "success": False,
                            "error": (
                                f"File ID {file_id} has "
                                "a part with no tape."
                            ),
                        }
                    ), 409

                tape_info = {
                    "tape_id": tape_id,
                    "label": (
                        part["tape_label"]
                        or part["ltfs_uuid"]
                        or f"Tape #{tape_id}"
                    ),
                    "ltfs_uuid": (
                        part["ltfs_uuid"]
                    ),
                }

                if tape_id not in [
                    item["tape_id"]
                    for item in file_tapes
                ]:
                    file_tapes.append(
                        tape_info
                    )

                if tape_id not in seen_tape_ids:
                    seen_tape_ids.add(
                        tape_id
                    )
                    required_tapes.append(
                        tape_info
                    )

        else:
            tape_id = row["tape_id"]

            if tape_id is None:
                return jsonify(
                    {
                        "success": False,
                        "error": (
                            f"File ID {file_id} "
                            "has no cataloged tape."
                        ),
                    }
                ), 409

            tape_info = {
                "tape_id": tape_id,
                "label": (
                    row["tape_label"]
                    or row["ltfs_uuid"]
                    or f"Tape #{tape_id}"
                ),
                "ltfs_uuid": (
                    row["ltfs_uuid"]
                ),
            }

            file_tapes.append(
                tape_info
            )

            if tape_id not in seen_tape_ids:
                seen_tape_ids.add(
                    tape_id
                )
                required_tapes.append(
                    tape_info
                )

        planned_files.append(
            {
                "file_id": row["id"],
                "filename": row["filename"],
                "relative_path": row["relative_path"],
                "size_bytes": int(
                    row["size_bytes"] or 0
                ),
                "is_spanned": bool(
                    row["is_spanned"]
                ),
                "required_tapes": file_tapes,
            }
        )

    return jsonify(
        {
            "success": True,
            "file_count": len(
                planned_files
            ),
            "total_bytes": total_bytes,
            "files": planned_files,
            "required_tapes": required_tapes,
        }
    )


@app.route(
    "/api/files/restore-active"
)
def files_restore_active_api():
    """
    Return the current or most recent unfinished selected-file restore.

    This endpoint never starts or resumes tape hardware activity.
    """

    initialize_database()

    operation = None

    #
    # Prefer a genuinely active in-memory selected restore.
    # Do not interpret its persisted "running" state as an
    # interruption while the worker is actually alive.
    #
    with OPERATION_STATE_LOCK:
        if ACTIVE_TAPE_OPERATION_ID:
            active = OPERATIONS.get(
                ACTIVE_TAPE_OPERATION_ID
            )

            if (
                active
                and active.get("type")
                == "restore_selected"
                and active.get("status")
                in {
                    "starting",
                    "running",
                    "waiting_for_tape",
                }
            ):
                operation = active

        if operation is not None:
            snapshot = _operation_snapshot(
                operation
            )

            return jsonify(
                {
                    "success": True,
                    "operation": snapshot,
                }
            )

    #
    # Nothing is actively running in memory. Look for a durable
    # unfinished selected restore left from an earlier process.
    #
    operation = (
        get_latest_resumable_restore_operation()
    )

    if operation is None:
        return jsonify(
            {
                "success": True,
                "operation": None,
            }
        )

    #
    # A persisted starting/running state with no matching active
    # in-memory worker means Flask/TapeBox restarted mid-restore.
    # Make it explicitly resumable instead of auto-starting I/O.
    #
    if operation.get("status") in {
        "starting",
        "running",
    }:
        operation["status"] = (
            "waiting_for_tape"
        )

        operation["message"] = (
            "Restore was interrupted. "
            "Continue restore when ready."
        )

        restart_message = (
            "TapeBox restarted during "
            "the restore."
        )

        if restart_message not in operation["messages"]:
            operation["messages"].append(
                restart_message
            )

        _persist_selected_restore(
            operation
        )

    with OPERATION_STATE_LOCK:
        OPERATIONS[
            operation["id"]
        ] = operation

        snapshot = _operation_snapshot(
            operation
        )

    return jsonify(
        {
            "success": True,
            "operation": snapshot,
        }
    )


@app.route(
    "/api/files/restore-start",
    methods=["POST"],
)
def files_restore_start_api():
    """
    Start or continue a restore of arbitrary selected catalog files.

    The configured TapeBox restore directory is used as the
    destination. Only one physical tape operation may run at once.
    """

    global ACTIVE_TAPE_OPERATION_ID

    initialize_database()

    payload = request.get_json(
        silent=True
    ) or {}

    raw_file_ids = payload.get(
        "file_ids",
        [],
    )

    if not isinstance(raw_file_ids, list):
        return jsonify(
            {
                "success": False,
                "error": "file_ids must be a list.",
            }
        ), 400

    file_ids = []

    for value in raw_file_ids:
        try:
            file_id = int(value)
        except (TypeError, ValueError):
            return jsonify(
                {
                    "success": False,
                    "error": (
                        f"Invalid file ID: {value}"
                    ),
                }
            ), 400

        if file_id <= 0:
            return jsonify(
                {
                    "success": False,
                    "error": (
                        f"Invalid file ID: {value}"
                    ),
                }
            ), 400

        if file_id not in file_ids:
            file_ids.append(
                file_id
            )

    if not file_ids:
        return jsonify(
            {
                "success": False,
                "error": "No files were selected.",
            }
        ), 400

    #
    # Validate that every requested catalog row exists before
    # claiming the tape operation lock.
    #
    for file_id in file_ids:
        if get_file_by_id(file_id) is None:
            return jsonify(
                {
                    "success": False,
                    "error": (
                        f"File ID {file_id} "
                        "does not exist."
                    ),
                }
            ), 404

    destination = get_setting(
        "restore_directory",
        "/mnt/tapebox/restored",
    )

    destination_path = Path(
        destination
    )

    if not destination_path.is_absolute():
        return jsonify(
            {
                "success": False,
                "error": (
                    "Configured restore directory must "
                    "be an absolute path."
                ),
            }
        ), 500

    #
    # Tape Inspector may own the physical drive independently of
    # the in-memory operation state, so its mounted filesystem is
    # authoritative.
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
            "type": "restore_selected",
            "job_id": None,
            "file_ids": list(file_ids),
            "destination": str(
                destination_path
            ),
            "status": "starting",
            "message": "Starting restore...",
            "messages": [
                "Starting restore..."
            ],
            "transfer": None,
            "result": None,
        }

        OPERATIONS[operation_id] = operation

        ACTIVE_TAPE_OPERATION_ID = (
            operation_id
        )

        _persist_selected_restore(
            operation
        )

    worker = threading.Thread(
        target=_selected_restore_worker,
        args=(
            operation_id,
            list(file_ids),
            str(destination_path),
        ),
        daemon=True,
        name=(
            f"tapebox-selected-restore-"
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

            _persist_selected_restore(
                operation
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
            "file_count": len(file_ids),
            "destination": str(
                destination_path
            ),
        }
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

    #
    # TapeBox private resumable-upload state.
    #
    if (
        relative.parts
        and relative.parts[0] == ".uploads"
    ):
        raise ValueError(
            "TapeBox internal staging paths are not accessible."
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
            # TapeBox internal resumable-upload state must never
            # appear as ordinary staging content.
            #
            if (
                target == root
                and entry.name == ".uploads"
            ):
                continue

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
    "/api/staging/upload",
    methods=["POST"],
)
def staging_upload_api():
    """
    Upload one or more files into the currently browsed
    TapeBox staging directory.

    Files are first written with a hidden temporary name and
    atomically renamed only after the upload completes.
    """

    initialize_default_settings()

    relative_path = request.form.get(
        "path",
        "",
    ).strip()

    try:
        root, target = _resolve_staging_path(
            relative_path
        )

        if not target.exists():
            raise ValueError(
                "Upload destination does not exist."
            )

        if not target.is_dir():
            raise ValueError(
                "Upload destination is not a folder."
            )

    except (OSError, ValueError) as exc:
        return jsonify({
            "success": False,
            "error": str(exc),
        }), 400

    uploads = request.files.getlist(
        "files"
    )

    if not uploads:
        return jsonify({
            "success": False,
            "error": "No files were selected.",
        }), 400

    uploaded = []

    for upload in uploads:
        original_name = (
            upload.filename or ""
        )

        #
        # Browsers may supply a fake or client-side path.
        # TapeBox accepts only the final filename component.
        #
        filename = Path(
            original_name.replace(
                "\\",
                "/",
            )
        ).name.strip()

        if (
            not filename
            or filename in (".", "..")
        ):
            return jsonify({
                "success": False,
                "error": "Invalid upload filename.",
            }), 400

        destination = (
            target / filename
        ).resolve()

        try:
            destination.relative_to(root)
        except ValueError:
            return jsonify({
                "success": False,
                "error": (
                    "Upload destination is outside "
                    "the staging area."
                ),
            }), 400

        if destination.exists():
            return jsonify({
                "success": False,
                "error": (
                    f"{filename} already exists "
                    "in this staging folder."
                ),
            }), 409

        temp_destination = (
            destination.parent
            / (
                ".tapebox-upload-"
                + uuid.uuid4().hex
                + "-"
                + filename
            )
        )

        try:
            upload.save(
                str(temp_destination)
            )

            #
            # Re-check immediately before final rename.
            #
            if destination.exists():
                temp_destination.unlink(
                    missing_ok=True
                )

                return jsonify({
                    "success": False,
                    "error": (
                        f"{filename} already exists "
                        "in this staging folder."
                    ),
                }), 409

            temp_destination.rename(
                destination
            )

            uploaded.append({
                "name": filename,
                "size_bytes": (
                    destination.stat().st_size
                ),
                "path": str(
                    destination.relative_to(root)
                ),
            })

        except Exception:
            try:
                temp_destination.unlink(
                    missing_ok=True
                )
            except OSError:
                pass

            raise

    return jsonify({
        "success": True,
        "uploaded": uploaded,
        "count": len(uploaded),
    })



#
# Resumable browser uploads.
#
# Upload state deliberately lives in the staging filesystem rather
# than the TapeBox SQLite catalog.
#

def _upload_state_root():
    initialize_default_settings()

    root, _ = _resolve_staging_path("")
    state_root = root / ".uploads"

    state_root.mkdir(
        parents=True,
        exist_ok=True,
    )

    return root, state_root


def _safe_upload_relative_path(value):
    value = str(value or "").replace("\\", "/").strip("/")

    if not value:
        raise ValueError("Upload path is required.")

    relative = Path(value)

    if relative.is_absolute():
        raise ValueError("Upload path must be relative.")

    if any(
        part in ("", ".", "..")
        for part in relative.parts
    ):
        raise ValueError("Invalid upload path.")

    return relative


def _upload_session_paths(upload_id):
    import re

    upload_id = str(upload_id or "").strip()

    if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
        raise ValueError("Invalid upload ID.")

    root, state_root = _upload_state_root()

    session_dir = state_root / upload_id
    metadata_path = session_dir / "upload.json"
    part_path = session_dir / "data.part"

    return (
        root,
        session_dir,
        metadata_path,
        part_path,
    )


def _write_upload_metadata(metadata_path, data):
    import json
    import os

    temp_path = metadata_path.with_suffix(
        ".json.tmp"
    )

    with open(
        temp_path,
        "w",
        encoding="utf-8",
    ) as handle:
        json.dump(
            data,
            handle,
            indent=2,
            sort_keys=True,
        )

        handle.flush()
        os.fsync(handle.fileno())

    temp_path.replace(metadata_path)


def _load_upload_metadata(upload_id):
    import json

    (
        root,
        session_dir,
        metadata_path,
        part_path,
    ) = _upload_session_paths(upload_id)

    if not metadata_path.exists():
        raise ValueError(
            "Upload session was not found."
        )

    with open(
        metadata_path,
        "r",
        encoding="utf-8",
    ) as handle:
        data = json.load(handle)

    expected = int(
        data.get("bytes_received", 0)
    )

    actual = (
        part_path.stat().st_size
        if part_path.exists()
        else 0
    )

    #
    # Metadata is committed only after the chunk data has been
    # flushed. If a crash happens between those two operations,
    # the .part file can be slightly ahead of upload.json.
    # Roll it back to the last committed byte boundary.
    #
    if actual > expected:
        with open(part_path, "r+b") as handle:
            handle.truncate(expected)

    if actual < expected:
        raise RuntimeError(
            "Upload data is shorter than its saved "
            "recovery state."
        )

    return (
        root,
        session_dir,
        metadata_path,
        part_path,
        data,
    )



def _find_resumable_upload(
    state_root,
    relative_path,
    total_size,
):
    import json

    relative_text = relative_path.as_posix()

    try:
        session_dirs = list(
            state_root.iterdir()
        )
    except OSError:
        return None

    for session_dir in session_dirs:
        if not session_dir.is_dir():
            continue

        metadata_path = (
            session_dir / "upload.json"
        )

        part_path = (
            session_dir / "data.part"
        )

        if not metadata_path.is_file():
            continue

        try:
            with open(
                metadata_path,
                "r",
                encoding="utf-8",
            ) as handle:
                metadata = json.load(handle)

            if (
                metadata.get("status")
                != "uploading"
            ):
                continue

            if (
                metadata.get("relative_path")
                != relative_text
            ):
                continue

            if (
                int(metadata.get("size", -1))
                != total_size
            ):
                continue

            expected = int(
                metadata.get(
                    "bytes_received",
                    0,
                )
            )

            actual = (
                part_path.stat().st_size
                if part_path.exists()
                else 0
            )

            #
            # Crash recovery:
            # data may have reached disk before upload.json.
            #
            if actual > expected:
                with open(
                    part_path,
                    "r+b",
                ) as handle:
                    handle.truncate(expected)

            if actual < expected:
                continue

            return metadata

        except (
            OSError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ):
            continue

    return None


@app.route(
    "/api/staging/uploads/batch",
    methods=["POST"],
)
def staging_batch_upload_api():
    import os

    root, _state_root = _upload_state_root()

    files = request.files.getlist("files")
    relative_paths = request.form.getlist(
        "paths"
    )

    if not files:
        return jsonify({
            "success": False,
            "error": "No files were supplied.",
        }), 400

    if len(files) != len(relative_paths):
        return jsonify({
            "success": False,
            "error": (
                "Batch file/path count mismatch."
            ),
        }), 400

    if len(files) > 250:
        return jsonify({
            "success": False,
            "error": (
                "Batch contains too many files."
            ),
        }), 400

    prepared = []

    try:
        #
        # Validate the entire batch before writing.
        #
        for uploaded, path_value in zip(
            files,
            relative_paths,
        ):
            relative_path = (
                _safe_upload_relative_path(
                    path_value
                )
            )

            destination = (
                root / relative_path
            ).resolve()

            try:
                destination.relative_to(root)
            except ValueError:
                raise ValueError(
                    "Upload destination is outside "
                    "the staging area."
                )

            uploaded.stream.seek(
                0,
                os.SEEK_END,
            )
            size = uploaded.stream.tell()
            uploaded.stream.seek(0)

            if destination.exists():
                if not destination.is_file():
                    raise ValueError(
                        f"{relative_path.as_posix()} "
                        "already exists and is not "
                        "a file."
                    )

                existing_size = (
                    destination.stat().st_size
                )

                if existing_size != size:
                    raise ValueError(
                        f"{relative_path.as_posix()} "
                        "already exists with a "
                        "different size."
                    )

                prepared.append((
                    uploaded,
                    relative_path,
                    destination,
                    size,
                    True,
                ))

                continue

            prepared.append((
                uploaded,
                relative_path,
                destination,
                size,
                False,
            ))

        uploaded_count = 0
        skipped_count = 0
        bytes_written = 0

        for (
            uploaded,
            relative_path,
            destination,
            size,
            already_exists,
        ) in prepared:
            if already_exists:
                skipped_count += 1
                continue

            destination.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            temp_path = (
                destination.parent
                / (
                    ".tapebox-upload-"
                    + destination.name
                    + ".part"
                )
            )

            try:
                with open(
                    temp_path,
                    "wb",
                ) as handle:
                    while True:
                        block = (
                            uploaded.stream.read(
                                4 * 1024 * 1024
                            )
                        )

                        if not block:
                            break

                        handle.write(block)
                        bytes_written += len(
                            block
                        )

                    handle.flush()

                temp_path.replace(
                    destination
                )

            except Exception:
                try:
                    temp_path.unlink(
                        missing_ok=True
                    )
                except OSError:
                    pass

                raise

            uploaded_count += 1

        #
        # One filesystem durability checkpoint
        # for the completed batch instead of
        # fsyncing every tiny file and metadata
        # record individually.
        #
        sync_fd = os.open(
            root,
            os.O_RDONLY,
        )

        try:
            os.fsync(sync_fd)
        finally:
            os.close(sync_fd)

        return jsonify({
            "success": True,
            "uploaded": uploaded_count,
            "skipped": skipped_count,
            "bytes_written": bytes_written,
            "files": len(files),
        })

    except (
        OSError,
        ValueError,
        RuntimeError,
    ) as exc:
        return jsonify({
            "success": False,
            "error": str(exc),
        }), 400


@app.route(
    "/api/staging/uploads/create",
    methods=["POST"],
)
def staging_resumable_create_api():
    import time
    import uuid

    data = request.get_json(silent=True) or {}

    relative_path = _safe_upload_relative_path(
        data.get("path")
    )

    try:
        total_size = int(
            data.get("size")
        )
    except (TypeError, ValueError):
        return jsonify({
            "success": False,
            "error": "Invalid file size.",
        }), 400

    if total_size < 0:
        return jsonify({
            "success": False,
            "error": "Invalid file size.",
        }), 400

    root, state_root = _upload_state_root()

    destination = (
        root / relative_path
    ).resolve()

    try:
        destination.relative_to(root)
    except ValueError:
        return jsonify({
            "success": False,
            "error": (
                "Upload destination is outside "
                "the staging area."
            ),
        }), 400

    if destination.exists():
        if not destination.is_file():
            return jsonify({
                "success": False,
                "error": (
                    f"{relative_path.as_posix()} "
                    "already exists and is not a file."
                ),
            }), 409

        existing_size = (
            destination.stat().st_size
        )

        if existing_size != total_size:
            return jsonify({
                "success": False,
                "error": (
                    f"{relative_path.as_posix()} "
                    "already exists with a different size."
                ),
            }), 409

        return jsonify({
            "success": True,
            "already_exists": True,
            "relative_path": (
                relative_path.as_posix()
            ),
            "size": existing_size,
        })

    existing = _find_resumable_upload(
        state_root,
        relative_path,
        total_size,
    )

    if existing is not None:
        return jsonify({
            "success": True,
            "resumed": True,
            "upload": existing,
        })

    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    upload_id = uuid.uuid4().hex
    session_dir = state_root / upload_id

    session_dir.mkdir(
        parents=False,
        exist_ok=False,
    )

    metadata_path = (
        session_dir / "upload.json"
    )

    part_path = (
        session_dir / "data.part"
    )

    part_path.touch()

    metadata = {
        "version": 1,
        "upload_id": upload_id,
        "relative_path": (
            relative_path.as_posix()
        ),
        "size": total_size,
        "bytes_received": 0,
        "status": "uploading",
        "sha256": None,
        "created_at": time.time(),
        "updated_at": time.time(),
    }

    _write_upload_metadata(
        metadata_path,
        metadata,
    )

    return jsonify({
        "success": True,
        "upload": metadata,
    })


@app.route(
    "/api/staging/uploads/<upload_id>",
    methods=["GET"],
)
def staging_resumable_status_api(upload_id):
    try:
        (
            root,
            session_dir,
            metadata_path,
            part_path,
            metadata,
        ) = _load_upload_metadata(upload_id)

    except (OSError, ValueError, RuntimeError) as exc:
        return jsonify({
            "success": False,
            "error": str(exc),
        }), 400

    return jsonify({
        "success": True,
        "upload": metadata,
    })


@app.route(
    "/api/staging/uploads/<upload_id>/chunk",
    methods=["POST"],
)
def staging_resumable_chunk_api(upload_id):
    import hashlib
    import os
    import time

    try:
        (
            root,
            session_dir,
            metadata_path,
            part_path,
            metadata,
        ) = _load_upload_metadata(upload_id)

        if metadata.get("status") != "uploading":
            raise ValueError(
                "Upload is not accepting chunks."
            )

        try:
            offset = int(
                request.headers.get(
                    "X-TapeBox-Offset",
                    "-1",
                )
            )
        except ValueError:
            raise ValueError(
                "Invalid upload offset."
            )

        committed = int(
            metadata.get(
                "bytes_received",
                0,
            )
        )

        if offset != committed:
            return jsonify({
                "success": False,
                "error": "Upload offset mismatch.",
                "expected_offset": committed,
            }), 409

        chunk = request.get_data(
            cache=False,
            as_text=False,
        )

        if not chunk:
            raise ValueError(
                "Empty upload chunk."
            )

        total_size = int(metadata["size"])

        if committed + len(chunk) > total_size:
            raise ValueError(
                "Chunk exceeds declared file size."
            )

        supplied_hash = (
            request.headers.get(
                "X-TapeBox-Chunk-SHA256",
                "",
            )
            .strip()
            .lower()
        )

        #
        # Always hash the received chunk on the server.
        #
        # Browsers served over plain LAN HTTP may not expose
        # crypto.subtle, so the client hash is optional.
        # When supplied, it must match.
        #
        actual_hash = hashlib.sha256(
            chunk
        ).hexdigest()

        if (
            supplied_hash
            and supplied_hash != actual_hash
        ):
            raise ValueError(
                "Chunk SHA-256 verification failed."
            )

        with open(part_path, "ab") as handle:
            handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())

        metadata["bytes_received"] = (
            committed + len(chunk)
        )
        metadata["updated_at"] = time.time()

        _write_upload_metadata(
            metadata_path,
            metadata,
        )

        return jsonify({
            "success": True,
            "bytes_received": (
                metadata["bytes_received"]
            ),
            "size": total_size,
        })

    except (OSError, ValueError, RuntimeError) as exc:
        return jsonify({
            "success": False,
            "error": str(exc),
        }), 400


@app.route(
    "/api/staging/uploads/<upload_id>/complete",
    methods=["POST"],
)
def staging_resumable_complete_api(upload_id):
    import hashlib
    import os
    import shutil
    import time

    try:
        (
            root,
            session_dir,
            metadata_path,
            part_path,
            metadata,
        ) = _load_upload_metadata(upload_id)

        total_size = int(
            metadata["size"]
        )

        received = int(
            metadata.get(
                "bytes_received",
                0,
            )
        )

        if received != total_size:
            raise ValueError(
                "Upload is not complete."
            )

        relative_path = (
            _safe_upload_relative_path(
                metadata["relative_path"]
            )
        )

        destination = (
            root / relative_path
        ).resolve()

        try:
            destination.relative_to(root)
        except ValueError:
            raise ValueError(
                "Upload destination is outside "
                "the staging area."
            )

        if destination.exists():
            raise ValueError(
                "Destination already exists."
            )

        metadata["status"] = "verifying"
        metadata["updated_at"] = time.time()

        _write_upload_metadata(
            metadata_path,
            metadata,
        )

        digest = hashlib.sha256()

        with open(part_path, "rb") as handle:
            while True:
                block = handle.read(
                    16 * 1024 * 1024
                )

                if not block:
                    break

                digest.update(block)

        whole_sha256 = digest.hexdigest()

        with open(part_path, "rb") as handle:
            os.fsync(handle.fileno())

        destination.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        part_path.replace(destination)

        metadata["status"] = "complete"
        metadata["sha256"] = whole_sha256
        metadata["bytes_received"] = total_size
        metadata["updated_at"] = time.time()

        _write_upload_metadata(
            metadata_path,
            metadata,
        )

        return jsonify({
            "success": True,
            "upload": metadata,
            "path": relative_path.as_posix(),
            "sha256": whole_sha256,
        })

    except (OSError, ValueError, RuntimeError) as exc:
        return jsonify({
            "success": False,
            "error": str(exc),
        }), 400


#
# Archive Planner
#
# Conservative usable capacities by LTO generation.
#
# These deliberately leave some headroom instead of
# planning all the way to the nominal native capacity.
#
ARCHIVE_PLANNER_CAPACITY_BYTES = {
    5: 1_400_000_000_000,
    6: 2_400_000_000_000,
    7: 5_800_000_000_000,
    8: 11_500_000_000_000,
    9: 17_500_000_000_000,
}

ARCHIVE_PLANNER_DEFAULT_GENERATION = 6


def _normalize_lto_generation(value):
    """
    Convert values such as 6, "6", and "LTO-6" to 6.
    """
    if value is None:
        return None

    text = str(value).strip().upper()

    if text.startswith("LTO-"):
        text = text[4:]

    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _planner_media():
    """
    Determine which LTO generation the planner should use.

    Prefer the currently loaded cartridge. If no usable
    generation can be detected, fall back to LTO-6.
    """
    generation = None
    source = "default"

    try:
        drives = discover_drives()

        if drives:
            nst_device = drives[0].get(
                "nst_device"
            )

            if nst_device:
                status = get_tape_status(
                    nst_device
                )

                if (
                    status.get("available")
                    and status.get("online")
                ):
                    generation = (
                        _normalize_lto_generation(
                            status.get("density")
                        )
                    )

                    if (
                        generation
                        in ARCHIVE_PLANNER_CAPACITY_BYTES
                    ):
                        source = "loaded_cartridge"

    except Exception:
        generation = None

    if (
        generation
        not in ARCHIVE_PLANNER_CAPACITY_BYTES
    ):
        generation = (
            ARCHIVE_PLANNER_DEFAULT_GENERATION
        )
        source = "default"

    return {
        "generation": generation,
        "generation_name": f"LTO-{generation}",
        "capacity_bytes": (
            ARCHIVE_PLANNER_CAPACITY_BYTES[
                generation
            ]
        ),
        "source": source,
    }


def _collect_planner_files(path):
    """
    Return regular files contained by a selected staging
    path.

    Directories are walked recursively. Symlinks are skipped
    so the planner cannot escape the configured staging tree.
    """

    path = Path(path)

    if path.is_symlink():
        return []

    if path.is_file():
        return [path]

    if not path.is_dir():
        return []

    files = []

    for candidate in sorted(
        path.rglob("*"),
        key=lambda item: str(item).lower(),
    ):
        if candidate.is_symlink():
            continue

        if candidate.is_file():
            files.append(candidate)

    return files


def _build_archive_plan(
    paths,
    generation=None,
):
    """
    Simulate TapeBox media usage.

    Normal files remain whole whenever they fit on a tape.
    A file larger than one tape's usable capacity is allowed
    to span cartridges.
    """

    media = _planner_media()

    requested_generation = (
        _normalize_lto_generation(
            generation
        )
    )

    if (
        requested_generation
        in ARCHIVE_PLANNER_CAPACITY_BYTES
    ):
        media = {
            "generation": requested_generation,
            "generation_name": (
                f"LTO-{requested_generation}"
            ),
            "capacity_bytes": (
                ARCHIVE_PLANNER_CAPACITY_BYTES[
                    requested_generation
                ]
            ),
            "source": "requested",
        }

    capacity = media["capacity_bytes"]

    files = []

    for path in paths:
        files.extend(
            _collect_planner_files(path)
        )

    #
    # A selection can contain both a directory and one of
    # its children. Do not count the same physical source
    # file twice.
    #
    unique_files = {}

    for file_path in files:
        try:
            resolved = file_path.resolve()
            stat_result = resolved.stat()
        except OSError:
            continue

        unique_files[str(resolved)] = {
            "path": resolved,
            "size": int(stat_result.st_size),
        }

    file_entries = list(
        unique_files.values()
    )

    total_bytes = sum(
        item["size"]
        for item in file_entries
    )

    spanning_files = [
        item
        for item in file_entries
        if item["size"] > capacity
    ]

    tapes = []

    def new_tape():
        tape = {
            "number": len(tapes) + 1,
            "used_bytes": 0,
            "files": 0,
            "parts": 0,
        }

        tapes.append(tape)

        return tape

    current_tape = None

    for item in file_entries:
        size = item["size"]

        #
        # Zero-byte files require catalog space but no
        # meaningful tape capacity.
        #
        if size == 0:
            if current_tape is None:
                current_tape = new_tape()

            current_tape["files"] += 1
            continue

        #
        # Normal files stay whole.
        #
        if size <= capacity:
            if current_tape is None:
                current_tape = new_tape()

            remaining = (
                capacity
                - current_tape["used_bytes"]
            )

            if size > remaining:
                current_tape = new_tape()

            current_tape["used_bytes"] += size
            current_tape["files"] += 1
            current_tape["parts"] += 1

            continue

        #
        # Oversized file: this file is allowed to span tapes.
        #
        bytes_remaining = size

        while bytes_remaining > 0:
            if current_tape is None:
                current_tape = new_tape()

            remaining = (
                capacity
                - current_tape["used_bytes"]
            )

            if remaining <= 0:
                current_tape = new_tape()
                remaining = capacity

            chunk = min(
                bytes_remaining,
                remaining,
            )

            current_tape["used_bytes"] += chunk
            current_tape["parts"] += 1

            bytes_remaining -= chunk

            if bytes_remaining > 0:
                current_tape = new_tape()

        current_tape["files"] += 1

    tape_count = len(tapes)

    if tape_count:
        final_free = max(
            0,
            capacity
            - tapes[-1]["used_bytes"],
        )
    else:
        final_free = capacity

    return {
        "generation": media["generation"],
        "generation_name": media["generation_name"],
        "media_source": media["source"],
        "capacity_bytes": capacity,
        "capacity_tb": capacity / 1_000_000_000_000,
        "file_count": len(file_entries),
        "total_bytes": total_bytes,
        "tape_count": tape_count,
        "spanning_required": bool(spanning_files),
        "spanning_file_count": len(spanning_files),
        "final_tape_free_bytes": final_free,
        "tapes": tapes,
    }


@app.route(
    "/api/staging/plan",
    methods=["POST"],
)
def staging_plan_api():
    """
    Calculate estimated tape requirements for selected
    staging files and folders.
    """

    initialize_database()
    initialize_default_settings()

    data = request.get_json(
        silent=True
    ) or {}

    relative_paths = data.get(
        "paths",
        [],
    )

    if not isinstance(
        relative_paths,
        list,
    ):
        return jsonify({
            "success": False,
            "error": (
                "Planner paths must be a list."
            ),
        }), 400

    if not relative_paths:
        return jsonify({
            "success": False,
            "error": (
                "Select at least one staging "
                "file or folder."
            ),
        }), 400

    targets = []

    try:
        for relative_path in relative_paths:
            relative_path = str(
                relative_path
            ).strip()

            if not relative_path:
                continue

            root, target = (
                _resolve_staging_path(
                    relative_path
                )
            )

            if target.is_symlink():
                raise ValueError(
                    "Symlinks cannot be planned."
                )

            if not target.exists():
                raise ValueError(
                    f"Staging item no longer exists: "
                    f"{relative_path}"
                )

            if not (
                target.is_file()
                or target.is_dir()
            ):
                raise ValueError(
                    f"Unsupported staging item: "
                    f"{relative_path}"
                )

            target.relative_to(root)

            targets.append(target)

    except (
        OSError,
        ValueError,
    ) as exc:
        return jsonify({
            "success": False,
            "error": str(exc),
        }), 400

    if not targets:
        return jsonify({
            "success": False,
            "error": (
                "No valid staging items were selected."
            ),
        }), 400

    plan = _build_archive_plan(
        targets
    )

    return jsonify({
        "success": True,
        "plan": plan,
    })



@app.route(
    "/api/staging/delete-selected",
    methods=["POST"],
)
def staging_delete_selected_api():
    """
    Permanently delete selected files and/or folders
    from the configured staging area.
    """

    import shutil

    initialize_default_settings()

    payload = request.get_json(
        silent=True
    ) or {}

    relative_paths = payload.get(
        "paths",
        [],
    )

    if not isinstance(
        relative_paths,
        list,
    ):
        return jsonify({
            "success": False,
            "error": (
                "Selected paths must be a list."
            ),
        }), 400

    relative_paths = [
        str(item).strip()
        for item in relative_paths
        if str(item).strip()
    ]

    if not relative_paths:
        return jsonify({
            "success": False,
            "error": (
                "Select at least one staging "
                "file or folder to delete."
            ),
        }), 400

    deleted = []

    try:
        root = _get_staging_root().resolve()

        #
        # Validate every requested path before
        # deleting anything.
        #
        targets = []

        for relative_path in relative_paths:
            relative = Path(
                relative_path
            )

            if relative.is_absolute():
                raise ValueError(
                    "Absolute paths are not allowed."
                )

            if ".." in relative.parts:
                raise ValueError(
                    "Parent path traversal is not allowed."
                )

            if not relative.parts:
                raise ValueError(
                    "The staging root cannot be deleted."
                )

            if relative.parts[0] == ".uploads":
                raise ValueError(
                    "TapeBox internal staging paths "
                    "cannot be deleted."
                )

            candidate = root / relative

            #
            # Resolve the parent separately so a
            # symlinked parent cannot escape staging.
            #
            resolved_parent = (
                candidate.parent.resolve()
            )

            try:
                resolved_parent.relative_to(root)
            except ValueError:
                raise ValueError(
                    "Path is outside the staging area."
                )

            #
            # A selected symlink should remove the
            # link itself, never its target.
            #
            if candidate.is_symlink():
                targets.append((
                    relative_path,
                    candidate,
                    "symlink",
                ))
                continue

            resolved = candidate.resolve()

            try:
                resolved.relative_to(root)
            except ValueError:
                raise ValueError(
                    "Path is outside the staging area."
                )

            if resolved == root:
                raise ValueError(
                    "The staging root cannot be deleted."
                )

            if not resolved.exists():
                raise ValueError(
                    "Staging item no longer exists: "
                    f"{relative_path}"
                )

            if resolved.is_dir():
                item_type = "directory"

            elif resolved.is_file():
                item_type = "file"

            else:
                raise ValueError(
                    "Unsupported staging item: "
                    f"{relative_path}"
                )

            targets.append((
                relative_path,
                resolved,
                item_type,
            ))

        #
        # Everything is valid. Now perform deletion.
        #
        for (
            relative_path,
            target,
            item_type,
        ) in targets:
            if item_type == "directory":
                shutil.rmtree(target)

            else:
                target.unlink()

            deleted.append(relative_path)

    except (
        OSError,
        ValueError,
    ) as exc:
        return jsonify({
            "success": False,
            "error": str(exc),
        }), 400

    return jsonify({
        "success": True,
        "deleted": deleted,
        "count": len(deleted),
    })


@app.route(
    "/api/staging/archive-selected",
    methods=["POST"],
)
def staging_archive_selected_api():
    """
    Start one resumable archive job containing multiple selected
    staging files and/or folders.
    """

    global ACTIVE_TAPE_OPERATION_ID

    initialize_database()
    initialize_default_settings()

    payload = request.get_json(
        silent=True
    ) or {}

    relative_paths = payload.get(
        "paths",
        []
    )

    if not isinstance(
        relative_paths,
        list,
    ):
        return jsonify(
            {
                "success": False,
                "error": (
                    "Selected paths must be a list."
                ),
            }
        ), 400

    relative_paths = [
        str(path).strip()
        for path in relative_paths
        if str(path).strip()
    ]

    if not relative_paths:
        return jsonify(
            {
                "success": False,
                "error": (
                    "At least one staging item "
                    "must be selected."
                ),
            }
        ), 400

    #
    # A mounted Inspector cartridge owns the physical tape drive.
    #
    inspector_mount = Path(
        "/mnt/tapebox/ltfs-inspect"
    )

    if _is_mounted(
        inspector_mount
    ):
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

    #
    # Resolve and validate everything before creating a snapshot.
    #
    selected_targets = []

    try:
        staging_root = Path(
            get_setting(
                "staging_directory",
                "/mnt/tapebox/staging",
            )
        ).expanduser().resolve()

        for relative_path in relative_paths:
            root, target = _resolve_staging_path(
                relative_path
            )

            candidate = (
                root
                / Path(relative_path)
            )

            if candidate.is_symlink():
                raise ValueError(
                    "Symbolic links cannot be archived."
                )

            if not target.exists():
                raise ValueError(
                    "Selected staging item "
                    "does not exist: "
                    + relative_path
                )

            if not (
                target.is_file()
                or target.is_dir()
            ):
                raise ValueError(
                    "Selected staging item is not "
                    "a regular file or directory: "
                    + relative_path
                )

            selected_targets.append(
                target
            )

    except (
        OSError,
        ValueError,
        RuntimeError,
    ) as exc:
        return jsonify(
            {
                "success": False,
                "error": str(exc),
            }
        ), 400

    #
    # Reserve the physical tape operation before making the
    # persistent hard-link selection snapshot.
    #
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

        #
        # Reserve ownership immediately so another request cannot
        # start while this request creates its snapshot.
        #
        ACTIVE_TAPE_OPERATION_ID = (
            operation_id
        )

    try:
        snapshot = (
            create_archive_selection_snapshot(
                staging_root,
                selected_targets,
            )
        )

        snapshot_path = Path(
            snapshot["snapshot_path"]
        )

    except Exception as exc:
        with OPERATION_STATE_LOCK:
            if (
                ACTIVE_TAPE_OPERATION_ID
                == operation_id
            ):
                ACTIVE_TAPE_OPERATION_ID = None

        return jsonify(
            {
                "success": False,
                "error": str(exc),
            }
        ), 400

    operation = {
        "id": operation_id,
        "type": "archive_staging_selection",
        "job_id": None,
        "destination": str(
            snapshot_path
        ),
        "status": "starting",
        "message": (
            "Starting selected archive..."
        ),
        "messages": [
            "Starting selected archive..."
        ],
        "transfer": None,
        "result": None,
        "selection": {
            "items": len(
                relative_paths
            ),
            "files": snapshot[
                "file_count"
            ],
            "bytes": snapshot[
                "total_bytes"
            ],
            "snapshot": str(
                snapshot_path
            ),
        },
    }

    with OPERATION_STATE_LOCK:
        OPERATIONS[
            operation_id
        ] = operation

    worker = threading.Thread(
        target=_archive_staging_worker,
        args=(
            operation_id,
            str(snapshot_path),
            True,
        ),
        daemon=True,
        name=(
            f"tapebox-archive-selected-"
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
                ACTIVE_TAPE_OPERATION_ID = None

        #
        # Worker never started, so no archive job can depend on
        # this snapshot.
        #
        try:
            shutil.rmtree(
                snapshot_path
            )
        except Exception:
            pass

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
            "operation": (
                _operation_snapshot(
                    operation
                )
            ),
            "selection": operation[
                "selection"
            ],
        }
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
                    item.relative_to(root)
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


@app.route(
    "/api/restored/delete-selected",
    methods=["POST"],
)
def restored_delete_selected_api():
    """
    Permanently delete selected files and/or folders
    from the configured restored-files area.
    """

    import shutil

    initialize_default_settings()

    payload = request.get_json(
        silent=True
    ) or {}

    relative_paths = payload.get(
        "paths",
        [],
    )

    if not isinstance(
        relative_paths,
        list,
    ):
        return jsonify({
            "success": False,
            "error": (
                "Selected paths must be a list."
            ),
        }), 400

    relative_paths = [
        str(item).strip()
        for item in relative_paths
        if str(item).strip()
    ]

    if not relative_paths:
        return jsonify({
            "success": False,
            "error": (
                "Select at least one restored "
                "file or folder to delete."
            ),
        }), 400

    deleted = []

    try:
        root = _get_restored_files_root().resolve()
        targets = []

        for relative_path in relative_paths:
            relative = Path(relative_path)

            if relative.is_absolute():
                raise ValueError(
                    "Absolute paths are not allowed."
                )

            if ".." in relative.parts:
                raise ValueError(
                    "Parent path traversal is not allowed."
                )

            if not relative.parts:
                raise ValueError(
                    "The restored-files root "
                    "cannot be deleted."
                )

            candidate = root / relative

            resolved_parent = candidate.parent.resolve()

            try:
                resolved_parent.relative_to(root)
            except ValueError:
                raise ValueError(
                    "Path is outside the restored "
                    "files area."
                )

            if candidate.is_symlink():
                targets.append((
                    relative_path,
                    candidate,
                    "symlink",
                ))
                continue

            resolved = candidate.resolve()

            try:
                resolved.relative_to(root)
            except ValueError:
                raise ValueError(
                    "Path is outside the restored "
                    "files area."
                )

            if resolved == root:
                raise ValueError(
                    "The restored-files root "
                    "cannot be deleted."
                )

            if not resolved.exists():
                raise ValueError(
                    "Restored item no longer exists: "
                    f"{relative_path}"
                )

            if resolved.is_dir():
                item_type = "directory"
            elif resolved.is_file():
                item_type = "file"
            else:
                raise ValueError(
                    "Unsupported restored item: "
                    f"{relative_path}"
                )

            targets.append((
                relative_path,
                resolved,
                item_type,
            ))

        for (
            relative_path,
            target,
            item_type,
        ) in targets:
            if item_type == "directory":
                shutil.rmtree(target)
            else:
                target.unlink()

            deleted.append(relative_path)

    except (
        OSError,
        ValueError,
    ) as exc:
        return jsonify({
            "success": False,
            "error": str(exc),
        }), 400

    return jsonify({
        "success": True,
        "deleted": deleted,
        "count": len(deleted),
    })


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


def _settings_create_directory(raw_parent, raw_name):
    """
    Create one directory inside an allowed Settings storage root.
    """

    if not raw_parent:
        raise ValueError(
            "Select a storage directory before creating a folder."
        )

    parent = Path(raw_parent)

    if not parent.is_absolute():
        raise ValueError(
            "Parent directory must be an absolute path."
        )

    parent = parent.resolve()

    allowed_root = None

    for root in SETTINGS_BROWSE_ROOTS:
        root_resolved = root.resolve()

        try:
            parent.relative_to(root_resolved)
            allowed_root = root_resolved
            break
        except ValueError:
            continue

    if allowed_root is None:
        raise ValueError(
            "That location is outside the allowed storage roots."
        )

    if not parent.exists():
        raise ValueError(
            "Parent directory does not exist."
        )

    if not parent.is_dir():
        raise ValueError(
            "Parent location is not a directory."
        )

    name = str(raw_name or "").strip()

    if not name:
        raise ValueError(
            "Folder name cannot be empty."
        )

    if name in {".", ".."}:
        raise ValueError(
            "Invalid folder name."
        )

    if "/" in name or "\\" in name:
        raise ValueError(
            "Folder name cannot contain path separators."
        )

    new_directory = (parent / name).resolve()

    try:
        new_directory.relative_to(allowed_root)
    except ValueError:
        raise ValueError(
            "New folder would be outside the allowed storage root."
        )

    if new_directory.exists():
        raise ValueError(
            "A file or folder with that name already exists."
        )

    new_directory.mkdir()

    return {
        "path": str(new_directory),
        "name": new_directory.name,
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
    "/api/settings/database/check",
    methods=["POST"],
)
def settings_database_check_api():
    """
    Run a read-only health check of the live TapeBox catalog.
    """

    result = check_catalog_health()

    status_code = (
        200
        if result.get("success")
        else 500
    )

    return jsonify(result), status_code


@app.route(
    "/api/settings/database/repair",
    methods=["POST"],
)
def settings_database_repair_api():
    """
    Safely rebuild and verify the live TapeBox catalog.
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
                    "repairing the database."
                ),
            }
        ), 409

    try:
        result = repair_catalog_database()

        status_code = (
            200
            if result.get("success")
            else 500
        )

        return jsonify(result), status_code

    except Exception as exc:
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



@app.route(
    "/api/settings/directories/create",
    methods=["POST"],
)
def settings_create_directory_api():
    """
    Create a directory from the Settings folder picker.
    """

    try:
        data = request.get_json(
            silent=True
        ) or {}

        result = _settings_create_directory(
            data.get("parent"),
            data.get("name"),
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



@app.route(
    "/api/tapes/register-existing",
    methods=["POST"],
)
def register_existing_tape_api():
    """
    Explicitly add a previously-inspected LTFS cartridge
    to the TapeBox catalog.

    This updates SQLite only. It does not mount, format,
    write to, or otherwise modify the physical cartridge.
    """

    initialize_database()

    data = request.get_json(
        silent=True
    ) or {}

    label = str(
        data.get("label") or ""
    ).strip()

    ltfs_uuid = str(
        data.get("uuid") or ""
    ).strip()

    if not label:
        return jsonify(
            {
                "success": False,
                "error": (
                    "LTFS cartridge label is required."
                ),
            }
        ), 400

    if not ltfs_uuid:
        return jsonify(
            {
                "success": False,
                "error": (
                    "LTFS cartridge UUID is required."
                ),
            }
        ), 400

    try:
        result = (
            register_existing_ltfs_tape(
                label=label,
                ltfs_uuid=ltfs_uuid,
                generation=data.get(
                    "generation"
                ),
                capacity_bytes=data.get(
                    "capacity_bytes"
                ),
                used_bytes=data.get(
                    "used_bytes"
                ),
                barcode=data.get(
                    "barcode"
                ),
                friendly_name=data.get(
                    "friendly_name"
                ),
                location=data.get(
                    "location"
                ),
                notes=data.get(
                    "notes"
                ),
            )
        )

    except ValueError as exc:
        return jsonify(
            {
                "success": False,
                "error": str(exc),
            }
        ), 400

    state = result.get("state")

    tape = result.get("tape")

    tape_data = (
        dict(tape)
        if tape is not None
        else None
    )

    if state == "conflict":
        return jsonify(
            {
                "success": False,
                "conflict": True,
                "state": state,
                "error": (
                    result.get("message")
                    or (
                        "Tape catalog identity "
                        "conflict."
                    )
                ),
                "tape": tape_data,
            }
        ), 409

    if state == "already_registered":
        return jsonify(
            {
                "success": True,
                "already_registered": True,
                "state": state,
                "tape": tape_data,
                "message": (
                    "This LTFS cartridge is "
                    "already registered."
                ),
            }
        )

    return jsonify(
        {
            "success": True,
            "already_registered": False,
            "state": state,
            "tape": tape_data,
            "message": (
                "LTFS cartridge added "
                "to the TapeBox catalog."
            ),
        }
    )



@app.route(
    "/api/tapes/format",
    methods=["POST"],
)
def format_tape_api():
    """
    Format the loaded cartridge as LTFS and register it.

    DESTRUCTIVE.

    Requires exact confirmation:
        FORMAT <LABEL>

    Example:
        FORMAT TAPE0003
    """

    global ACTIVE_TAPE_OPERATION_ID

    initialize_database()

    data = request.get_json(
        silent=True
    ) or {}

    label = str(
        data.get("label") or ""
    ).strip().upper()

    confirmation = str(
        data.get("confirmation") or ""
    ).strip()

    friendly_name = str(
        data.get("friendly_name") or ""
    ).strip() or None

    location = str(
        data.get("location") or ""
    ).strip() or None

    notes = str(
        data.get("notes") or ""
    ).strip() or None

    #
    # TapeBox labels are intentionally simple.
    #
    # mkltfs has a separate six-character tape serial
    # option, so do not confuse the TapeBox/LTFS volume
    # name with that optional serial.
    #
    if not label:
        return jsonify(
            {
                "success": False,
                "error": (
                    "Tape label is required."
                ),
            }
        ), 400

    if len(label) > 80:
        return jsonify(
            {
                "success": False,
                "error": (
                    "Tape label is too long."
                ),
            }
        ), 400

    expected_confirmation = (
        f"FORMAT {label}"
    )

    if confirmation != expected_confirmation:
        return jsonify(
            {
                "success": False,
                "confirmation_required": True,
                "expected_confirmation": (
                    expected_confirmation
                ),
                "error": (
                    "Destructive confirmation did "
                    "not match."
                ),
            }
        ), 400

    operation_owner = (
        f"format:{uuid.uuid4()}"
    )

    #
    # Reserve the one physical tape drive before doing
    # anything with the cartridge.
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
            operation_owner
        )

    try:
        inspector_mount = Path(
            "/mnt/tapebox/ltfs-inspect"
        )

        normal_mount = Path(
            "/mnt/tapebox/ltfs"
        )

        #
        # Never format a mounted cartridge.
        #
        if (
            _is_mounted(inspector_mount)
            or _is_mounted(normal_mount)
        ):
            return jsonify(
                {
                    "success": False,
                    "error": (
                        "An LTFS filesystem is "
                        "currently mounted. Unmount "
                        "the cartridge before "
                        "formatting."
                    ),
                }
            ), 409

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
                        "Tape cartridge is not "
                        "online and ready."
                    ),
                }
            ), 409

        #
        # Final read-only check immediately before the
        # destructive operation.
        #
        # IMPORTANT:
        # This first implementation intentionally refuses
        # to overwrite an existing LTFS cartridge.
        #
        before = inspect_ltfs(
            sg_device=sg_device,
        )

        if before.get("mounted"):
            return jsonify(
                {
                    "success": False,
                    "error": (
                        "Pre-format inspection did "
                        "not cleanly unmount LTFS. "
                        "Formatting was cancelled."
                    ),
                }
            ), 409

        if (
            before.get("ltfs")
            and before.get("uuid")
        ):
            return jsonify(
                {
                    "success": False,
                    "existing_ltfs": True,
                    "error": (
                        "This cartridge already "
                        "contains LTFS. TapeBox will "
                        "not overwrite an existing "
                        "LTFS cartridge through the "
                        "blank-tape format path."
                    ),
                    "cartridge": {
                        "label": before.get(
                            "label"
                        ),
                        "uuid": before.get(
                            "uuid"
                        ),
                    },
                }
            ), 409

        #
        # DESTRUCTIVE POINT.
        #
        # Do NOT use --force for the normal blank /
        # unpartitioned cartridge path.
        #
        result = format_ltfs(
            sg_device=sg_device,
            volume_name=label,
            force=False,
        )

        if result.get("returncode") != 0:
            return jsonify(
                {
                    "success": False,
                    "formatted": False,
                    "error": (
                        result.get("stderr")
                        or result.get("stdout")
                        or "mkltfs failed."
                    ),
                }
            ), 500

        #
        # Verify the resulting cartridge by mounting it
        # READ ONLY and obtaining its permanent LTFS UUID.
        #
        after = inspect_ltfs(
            sg_device=sg_device,
        )

        if after.get("mounted"):
            return jsonify(
                {
                    "success": False,
                    "formatted": True,
                    "registered": False,
                    "error": (
                        "Tape was formatted, but "
                        "post-format LTFS inspection "
                        "did not cleanly unmount. "
                        "The tape was NOT added to "
                        "the catalog."
                    ),
                }
            ), 500

        if not (
            after.get("ltfs")
            and after.get("uuid")
        ):
            return jsonify(
                {
                    "success": False,
                    "formatted": True,
                    "registered": False,
                    "error": (
                        "Tape was formatted, but "
                        "TapeBox could not verify "
                        "the new LTFS filesystem. "
                        "The tape was NOT added to "
                        "the catalog."
                    ),
                    "inspection_error": (
                        after.get("error")
                    ),
                }
            ), 500

        generation = (
            after.get("generation")
            or status.get("density")
        )

        #
        # Normalize values such as "LTO-6" to integer 6
        # for the existing TapeBox schema.
        #
        if isinstance(generation, str):
            generation_text = (
                generation.strip().upper()
            )

            if generation_text.startswith(
                "LTO-"
            ):
                generation_text = (
                    generation_text[4:]
                )

            try:
                generation = int(
                    generation_text
                )
            except ValueError:
                generation = None

        registration = (
            register_existing_ltfs_tape(
                label=(
                    after.get("label")
                    or label
                ),
                ltfs_uuid=after.get(
                    "uuid"
                ),
                generation=generation,
                capacity_bytes=after.get(
                    "capacity_bytes"
                ),
                used_bytes=after.get(
                    "used_bytes"
                ),
                barcode=after.get(
                    "barcode"
                ),
                friendly_name=(
                    friendly_name
                    or label
                ),
                location=location,
                notes=notes,
            )
        )

        state = registration.get(
            "state"
        )

        tape = registration.get(
            "tape"
        )

        if state == "conflict":
            return jsonify(
                {
                    "success": False,
                    "formatted": True,
                    "registered": False,
                    "error": (
                        registration.get(
                            "message"
                        )
                        or (
                            "Tape was formatted but "
                            "could not be registered "
                            "because of a catalog "
                            "identity conflict."
                        )
                    ),
                }
            ), 409

        return jsonify(
            {
                "success": True,
                "formatted": True,
                "registered": True,
                "state": state,
                "message": (
                    f"{label} was formatted as "
                    "LTFS, verified, and added "
                    "to the TapeBox catalog."
                ),
                "cartridge": {
                    "label": (
                        after.get("label")
                        or label
                    ),
                    "uuid": after.get(
                        "uuid"
                    ),
                    "generation": generation,
                    "capacity_bytes": (
                        after.get(
                            "capacity_bytes"
                        )
                    ),
                    "used_bytes": (
                        after.get(
                            "used_bytes"
                        )
                    ),
                    "free_bytes": (
                        after.get(
                            "free_bytes"
                        )
                    ),
                },
                "tape": (
                    dict(tape)
                    if tape is not None
                    else None
                ),
            }
        )

    finally:
        with OPERATION_STATE_LOCK:
            if (
                ACTIVE_TAPE_OPERATION_ID
                == operation_owner
            ):
                ACTIVE_TAPE_OPERATION_ID = None



@app.route(
    "/api/tapes/format-preview",
    methods=["POST"],
)
def format_tape_preview_api():
    """
    Inspect the currently loaded cartridge before formatting.

    This endpoint is NON-DESTRUCTIVE.

    It reserves the physical tape drive, checks cartridge
    readiness, and attempts a read-only LTFS inspection.

    No mkltfs command is executed here.
    """

    global ACTIVE_TAPE_OPERATION_ID

    operation_owner = "format_preview"

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
            operation_owner
        )

    try:
        inspector_mount = Path(
            "/mnt/tapebox/ltfs-inspect"
        )

        if _is_mounted(inspector_mount):
            return jsonify(
                {
                    "success": False,
                    "busy": True,
                    "error": (
                        "Tape Inspector currently has "
                        "an LTFS cartridge mounted. "
                        "Unmount it before preparing "
                        "a tape."
                    ),
                }
            ), 409

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
                        "Tape cartridge is not online "
                        "and ready."
                    ),
                }
            ), 409

        #
        # Try a READ-ONLY LTFS inspection.
        #
        # A blank/non-LTFS cartridge normally cannot be
        # mounted by LTFS, which is expected here.
        #
        info = inspect_ltfs(
            sg_device=sg_device,
        )

        ltfs_detected = bool(
            info.get("ltfs")
            and info.get("uuid")
            and not info.get("mounted")
        )

        existing_tape = None

        if ltfs_detected:
            existing = get_tape_by_uuid(
                info.get("uuid")
            )

            if existing:
                existing_tape = dict(
                    existing
                )

        if ltfs_detected:
            warning = (
                "This cartridge already contains an "
                "LTFS filesystem. Formatting it will "
                "destroy the existing LTFS volume and "
                "its contents."
            )

        else:
            warning = (
                "No readable LTFS filesystem was "
                "detected. The cartridge may be blank, "
                "scratch, non-LTFS, or unreadable. "
                "Formatting will overwrite the medium."
            )

        return jsonify(
            {
                "success": True,
                "destructive": True,
                "eligible_for_format": True,
                "ltfs_detected": ltfs_detected,
                "warning": warning,

                "drive": {
                    "description": (
                        drive.get("description")
                        or drive.get("name")
                        or "Tape Drive"
                    ),
                    "nst_device": nst_device,
                    "sg_device": sg_device,
                },

                "media": {
                    "generation": (
                        status.get("density")
                    ),
                    "online": bool(
                        status.get("online")
                    ),
                    "available": bool(
                        status.get("available")
                    ),
                },

                "existing_ltfs": (
                    {
                        "label": info.get(
                            "label"
                        ),
                        "uuid": info.get(
                            "uuid"
                        ),
                        "barcode": info.get(
                            "barcode"
                        ),
                        "format_version": info.get(
                            "format_version"
                        ),
                        "capacity_bytes": info.get(
                            "capacity_bytes"
                        ),
                        "used_bytes": info.get(
                            "used_bytes"
                        ),
                        "free_bytes": info.get(
                            "free_bytes"
                        ),
                        "cataloged": bool(
                            existing_tape
                        ),
                        "catalog_tape": (
                            existing_tape
                        ),
                    }
                    if ltfs_detected
                    else None
                ),

                #
                # Keep the failed read-only LTFS mount
                # information available for diagnostics,
                # but do not mistake it for a format error.
                #
                "inspection_error": (
                    None
                    if ltfs_detected
                    else info.get("error")
                ),
            }
        )

    finally:
        with OPERATION_STATE_LOCK:
            if (
                ACTIVE_TAPE_OPERATION_ID
                == operation_owner
            ):
                ACTIVE_TAPE_OPERATION_ID = None



@app.route(
    "/api/tapes/inspect-existing",
    methods=["POST"],
)
def inspect_existing_tape_api():
    """
    Inspect an inserted LTFS cartridge without modifying it.

    The tape drive is reserved only for the duration of the
    read-only LTFS inspection. inspect_ltfs() unmounts the
    cartridge before returning.
    """

    global ACTIVE_TAPE_OPERATION_ID

    operation_owner = (
        "catalog_inspect_existing"
    )

    #
    # Reserve the physical drive before discovery/mounting.
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
            operation_owner
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

        #
        # READ ONLY.
        #
        # inspect_ltfs() mounts the cartridge read-only,
        # reads its identity/capacity, and unmounts it
        # before returning.
        #
        info = inspect_ltfs(
            sg_device=sg_device,
        )

        if not info.get("ltfs"):
            return jsonify(
                {
                    "success": False,
                    "error": (
                        info.get("error")
                        or (
                            "Inserted cartridge could "
                            "not be identified as LTFS."
                        )
                    ),
                }
            ), 400

        #
        # Do not continue if inspection succeeded but the
        # temporary LTFS filesystem failed to unmount.
        #
        if info.get("mounted"):
            return jsonify(
                {
                    "success": False,
                    "error": (
                        "LTFS inspection did not "
                        "cleanly unmount."
                    ),
                }
            ), 500

        if info.get("error"):
            return jsonify(
                {
                    "success": False,
                    "error": info["error"],
                }
            ), 500

        ltfs_uuid = info.get("uuid")

        if not ltfs_uuid:
            return jsonify(
                {
                    "success": False,
                    "error": (
                        "LTFS cartridge UUID could "
                        "not be read."
                    ),
                }
            ), 400

        existing = get_tape_by_uuid(
            ltfs_uuid
        )

        tape_data = None

        if existing:
            tape_data = dict(existing)

        return jsonify(
            {
                "success": True,
                "registered": bool(existing),
                "existing_tape": tape_data,
                "cartridge": {
                    "label": info.get("label"),
                    "uuid": ltfs_uuid,
                    "volume_serial": (
                        info.get("volume_serial")
                    ),
                    "barcode": info.get(
                        "barcode"
                    ),
                    "format_version": (
                        info.get("format_version")
                    ),
                    "capacity_bytes": (
                        info.get("capacity_bytes")
                    ),
                    "used_bytes": (
                        info.get("used_bytes")
                    ),
                    "free_bytes": (
                        info.get("free_bytes")
                    ),
                    "generation": (
                        status.get("density")
                    ),
                },
                "drive": {
                    "description": (
                        drive.get("description")
                        or drive.get("name")
                        or "Tape Drive"
                    ),
                    "nst_device": nst_device,
                    "sg_device": sg_device,
                    "density": status.get(
                        "density"
                    ),
                },
            }
        )

    finally:
        #
        # inspect_ltfs() is a temporary inspection.
        # Unlike Tape Inspector, ownership is never retained.
        #
        with OPERATION_STATE_LOCK:
            if (
                ACTIVE_TAPE_OPERATION_ID
                == operation_owner
            ):
                ACTIVE_TAPE_OPERATION_ID = None



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
    "/api/inspector/unmount",
    methods=["POST"],
)
def inspector_unmount_api():
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
                    "before unmounting."
                ),
            }
        ), 409

    mount_path = Path(
        "/mnt/tapebox/ltfs-inspect"
    )

    #
    # If Inspector is already unmounted, make this
    # idempotent and simply release stale ownership.
    #
    if not _is_mounted(mount_path):
        with OPERATION_STATE_LOCK:
            if (
                ACTIVE_TAPE_OPERATION_ID
                == "inspector"
            ):
                ACTIVE_TAPE_OPERATION_ID = None

        return jsonify(
            {
                "success": True,
                "unmounted": True,
                "ejected": False,
                "already_unmounted": True,
                "message": (
                    "LTFS is already unmounted. "
                    "Cartridge remains loaded."
                ),
            }
        )

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
                    + str(error)
                ),
            }
        ), 500

    #
    # LTFS released the drive successfully.
    #
    with OPERATION_STATE_LOCK:
        if (
            ACTIVE_TAPE_OPERATION_ID
            == "inspector"
        ):
            ACTIVE_TAPE_OPERATION_ID = None

    return jsonify(
        {
            "success": True,
            "unmounted": True,
            "ejected": False,
            "message": (
                "LTFS unmounted cleanly. "
                "Cartridge remains loaded in the drive."
            ),
        }
    )



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
