import argparse
import threading
import uuid
from pathlib import Path

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
)

from tapebox.restore import (
    restore_archive_job,
)

from tapebox.tape import (
    discover_drives,
    get_tape_status,
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
        default_destination=(
            "/mnt/tapebox/restored"
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
    drives = discover_drives()

    if not drives:
        return jsonify(
            {
                "success": True,
                "detected": False,
                "online": False,
                "available": False,
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
                "online": False,
                "available": False,
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

    return jsonify(
        {
            "success": True,
            "detected": True,
            "online": bool(
                status.get("online")
            ),
            "available": bool(
                status.get("available")
            ),
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
