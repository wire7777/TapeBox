import argparse

from flask import Flask, render_template

from tapebox.database import (
    initialize_database,
    list_tapes,
    list_files,
    list_archive_jobs,
    get_tape_by_id,
    get_files_by_tape,
    search_files,
    get_file_parts,
)

from tapebox.tape import (
    discover_drives,
    get_tape_status,
)


app = Flask(__name__)


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
