import argparse
import re
import sqlite3

from tapebox.archive import archive_file

from tapebox.tape import (
    discover_drives,
    get_tape_status,
    inspect_ltfs,
)

from tapebox.database import (
    DB_PATH,
    initialize_database,
    add_tape,
    list_tapes,
    reconcile_loaded_tape,
    list_files,
)


def format_bytes(value):
    if value is None:
        return "-"

    value = int(value)

    tb = value / 1_000_000_000_000

    if tb >= 0.1:
        return f"{tb:.2f} TB"

    gb = value / 1_000_000_000

    if gb >= 0.1:
        return f"{gb:.2f} GB"

    mb = value / 1_000_000

    return f"{mb:.2f} MB"


def generation_number(value):
    """
    Convert strings such as:

        LTO-6

    into:

        6
    """

    if not value:
        return None

    match = re.search(
        r"LTO-(\d+)",
        str(value),
        re.IGNORECASE,
    )

    if not match:
        return None

    return int(match.group(1))


def cmd_init(args):
    initialize_database()

    print("TapeBox database initialized")
    print(f"Database: {DB_PATH}")


def cmd_tape_add(args):
    initialize_database()

    label = args.label.upper()

    try:
        add_tape(label)

    except sqlite3.IntegrityError:
        print(
            f"Tape already exists: {label}"
        )
        return

    print(
        f"Added tape: {label}"
    )


def cmd_tape_list(args):
    initialize_database()

    tapes = list_tapes()

    if not tapes:
        print("No tapes in catalog.")
        return

    print()

    print(
        f"{'ID':<5}"
        f"{'LABEL':<14}"
        f"{'GEN':<8}"
        f"{'STATUS':<14}"
        f"{'USED':<14}"
        f"{'UUID'}"
    )

    print("-" * 92)

    for tape in tapes:

        generation = (
            f"LTO-{tape['generation']}"
            if tape["generation"] is not None
            else "-"
        )

        used = format_bytes(
            tape["used_bytes"] or 0
        )

        uuid = (
            tape["ltfs_uuid"]
            or "-"
        )

        print(
            f"{tape['id']:<5}"
            f"{tape['label']:<14}"
            f"{generation:<8}"
            f"{tape['status']:<14}"
            f"{used:<14}"
            f"{uuid}"
        )

    print()


def cmd_tape_status(args):
    """
    Display loaded cartridge, LTFS, and catalog status.
    """

    initialize_database()

    drives = discover_drives()

    if not drives:
        print(
            "No tape drives detected."
        )
        return

    drive = drives[0]

    device = drive.get(
        "nst_device"
    )

    sg_device = drive.get(
        "sg_device"
    )

    print()
    print("TapeBox Cartridge")
    print("-" * 40)

    print(
        f"{'Drive':<16}"
        f"{drive.get('description', 'Unknown')}"
    )

    print(
        f"{'Serial':<16}"
        f"{drive.get('serial', '-')}"
    )

    print(
        f"{'Device':<16}"
        f"{device or '-'}"
    )

    print(
        f"{'SG Device':<16}"
        f"{sg_device or '-'}"
    )

    print()

    if not device:
        print(
            f"{'Cartridge':<16}"
            f"UNKNOWN"
        )

        print()
        return

    status = get_tape_status(
        device
    )

    if not status.get(
        "available"
    ):
        error = status.get(
            "error",
            "Unknown error",
        )

        error_lower = (
            error.lower()
        )

        no_media_messages = (
            "no medium",
            "no media",
            "medium not present",
            "no tape",
        )

        if any(
            message in error_lower
            for message in no_media_messages
        ):
            print(
                f"{'Cartridge':<16}"
                f"NO CARTRIDGE"
            )

        else:
            print(
                f"{'Cartridge':<16}"
                f"ERROR"
            )

            print(
                f"{'Error':<16}"
                f"{error}"
            )

        print()
        return

    cartridge = (
        "LOADED"
        if status.get("online")
        else "NOT READY"
    )

    density = status.get(
        "density",
        "Unknown",
    )

    print(
        f"{'Cartridge':<16}"
        f"{cartridge}"
    )

    print(
        f"{'Generation':<16}"
        f"{density}"
    )

    print(
        f"{'Write Protect':<16}"
        f"{'YES' if status.get('write_protected') else 'NO'}"
    )

    print(
        f"{'At BOT':<16}"
        f"{'YES' if status.get('beginning_of_tape') else 'NO'}"
    )

    print(
        f"{'At EOT':<16}"
        f"{'YES' if status.get('end_of_tape') else 'NO'}"
    )

    print()

    ltfs_info = inspect_ltfs(
        sg_device=sg_device,
    )

    print(
        f"{'LTFS':<16}"
        f"{'YES' if ltfs_info.get('ltfs') else 'NO'}"
    )

    print(
        f"{'Volume':<16}"
        f"{ltfs_info.get('label') or '-'}"
    )

    print(
        f"{'UUID':<16}"
        f"{ltfs_info.get('uuid') or '-'}"
    )

    print(
        f"{'Barcode':<16}"
        f"{ltfs_info.get('barcode') or '-'}"
    )

    print(
        f"{'LTFS Format':<16}"
        f"{ltfs_info.get('format_version') or '-'}"
    )

    print(
        f"{'Capacity':<16}"
        f"{format_bytes(ltfs_info.get('capacity_bytes'))}"
    )

    print(
        f"{'Used':<16}"
        f"{format_bytes(ltfs_info.get('used_bytes'))}"
    )

    print(
        f"{'Free':<16}"
        f"{format_bytes(ltfs_info.get('free_bytes'))}"
    )

    if ltfs_info.get(
        "error"
    ):
        print()

        print(
            f"{'LTFS Error':<16}"
            f"{ltfs_info['error']}"
        )

    if ltfs_info.get(
        "ltfs"
    ):
        catalog = reconcile_loaded_tape(
            label=ltfs_info.get(
                "label"
            ),
            ltfs_uuid=ltfs_info.get(
                "uuid"
            ),
            generation=generation_number(
                density
            ),
            capacity_bytes=ltfs_info.get(
                "capacity_bytes"
            ),
            used_bytes=ltfs_info.get(
                "used_bytes"
            ),
            barcode=ltfs_info.get(
                "barcode"
            ),
        )

        print()

        if (
            catalog["state"]
            == "registered"
        ):
            tape = catalog[
                "tape"
            ]

            print(
                f"{'Catalog':<16}"
                f"REGISTERED"
            )

            print(
                f"{'TapeBox ID':<16}"
                f"{tape['label']}"
            )

            print(
                f"{'Database ID':<16}"
                f"{tape['id']}"
            )

        elif (
            catalog["state"]
            == "conflict"
        ):
            print(
                f"{'Catalog':<16}"
                f"CONFLICT"
            )

            print(
                f"{'Error':<16}"
                f"{catalog.get('message', '-')}"
            )

        else:
            print(
                f"{'Catalog':<16}"
                f"UNREGISTERED"
            )

            print(
                f"{'TapeBox ID':<16}"
                f"-"
            )

    print()


def cmd_drive_list(args):
    drives = discover_drives()

    if not drives:
        print(
            "No tape drives detected."
        )
        return

    print()

    for index, drive in enumerate(
        drives,
        start=1,
    ):
        print(
            f"Tape Drive {index}"
        )

        print(
            f"  SCSI:       "
            f"{drive['scsi_address']}"
        )

        print(
            f"  Device:     "
            f"{drive['st_device']}"
        )

        print(
            f"  No-rewind:  "
            f"{drive['nst_device']}"
        )

        print(
            f"  Generic:    "
            f"{drive['sg_device']}"
        )

        print(
            f"  Drive:      "
            f"{drive['description']}"
        )

        print(
            f"  Vendor:     "
            f"{drive.get('vendor', '-')}"
        )

        print(
            f"  Model:      "
            f"{drive.get('product', '-')}"
        )

        print(
            f"  Serial:     "
            f"{drive.get('serial', '-')}"
        )

        print()


def cmd_drive_status(args):
    drives = discover_drives()

    if not drives:
        print(
            "No tape drives detected."
        )
        return

    for drive in drives:

        device = drive[
            "nst_device"
        ]

        print()

        print(
            f"Drive: "
            f"{drive['description']}"
        )

        print(
            f"Device: "
            f"{device}"
        )

        if drive.get(
            "serial"
        ):
            print(
                f"Serial: "
                f"{drive['serial']}"
            )

        status = get_tape_status(
            device
        )

        if not status[
            "available"
        ]:
            print(
                "Status: ERROR"
            )

            print(
                status.get(
                    "error",
                    "Unknown error",
                )
            )

            continue

        print(
            "Status:",
            (
                "ONLINE"
                if status.get("online")
                else "OFFLINE"
            ),
        )

        print(
            "Media:",
            status.get(
                "density",
                "Unknown",
            ),
        )

        print(
            "Write protected:",
            (
                "YES"
                if status.get(
                    "write_protected"
                )
                else "NO"
            ),
        )

        print(
            "Beginning of tape:",
            (
                "YES"
                if status.get(
                    "beginning_of_tape"
                )
                else "NO"
            ),
        )

        print(
            "End of tape:",
            (
                "YES"
                if status.get(
                    "end_of_tape"
                )
                else "NO"
            ),
        )

        print()


def cmd_archive_add(args):
    initialize_database()

    result = archive_file(
        args.path
    )

    if not result[
        "success"
    ]:
        print()
        print("Archive FAILED")
        print("-" * 40)
        print(
            result.get(
                "error",
                "Unknown error",
            )
        )
        print()
        return

    print()
    print("Archive Complete")
    print("-" * 40)

    print(
        f"{'File':<16}"
        f"{result['filename']}"
    )

    print(
        f"{'Size':<16}"
        f"{format_bytes(result['size_bytes'])}"
    )

    print(
        f"{'Tape':<16}"
        f"{result['tape_label']}"
    )

    print(
        f"{'Tape Path':<16}"
        f"{result['tape_path']}"
    )

    print(
        f"{'SHA256':<16}"
        f"{result['sha256']}"
    )

    print(
        f"{'Database ID':<16}"
        f"{result['file_id']}"
    )

    print()


def cmd_file_list(args):
    initialize_database()

    rows = list_files()

    if not rows:
        print(
            "No archived files."
        )
        return

    print()

    print(
        f"{'ID':<6}"
        f"{'FILE':<32}"
        f"{'SIZE':<14}"
        f"{'TAPE':<14}"
        f"{'TAPE PATH'}"
    )

    print("-" * 90)

    for row in rows:
        print(
            f"{row['id']:<6}"
            f"{row['filename'][:30]:<32}"
            f"{format_bytes(row['size_bytes']):<14}"
            f"{(row['tape_label'] or '-'):<14}"
            f"{row['tape_path'] or '-'}"
        )

    print()


def build_parser():
    parser = argparse.ArgumentParser(
        prog="tapebox",
        description=(
            "TapeBox LTFS archive manager"
        ),
    )

    subparsers = (
        parser.add_subparsers(
            dest="command",
            required=True,
        )
    )

    #
    # init
    #

    init_parser = (
        subparsers.add_parser(
            "init",
            help=(
                "Initialize TapeBox database"
            ),
        )
    )

    init_parser.set_defaults(
        func=cmd_init,
    )

    #
    # tape
    #

    tape_parser = (
        subparsers.add_parser(
            "tape",
            help="Tape management",
        )
    )

    tape_sub = (
        tape_parser.add_subparsers(
            dest="tape_command",
            required=True,
        )
    )

    tape_add = (
        tape_sub.add_parser(
            "add",
            help=(
                "Add tape to catalog"
            ),
        )
    )

    tape_add.add_argument(
        "label",
        help=(
            "Friendly tape label "
            "such as TAPE0001"
        ),
    )

    tape_add.set_defaults(
        func=cmd_tape_add,
    )

    tape_list = (
        tape_sub.add_parser(
            "list",
            help="List tapes",
        )
    )

    tape_list.set_defaults(
        func=cmd_tape_list,
    )

    tape_status = (
        tape_sub.add_parser(
            "status",
            help=(
                "Show loaded cartridge status"
            ),
        )
    )

    tape_status.set_defaults(
        func=cmd_tape_status,
    )

    #
    # drive
    #

    drive_parser = (
        subparsers.add_parser(
            "drive",
            help="Tape drive management",
        )
    )

    drive_sub = (
        drive_parser.add_subparsers(
            dest="drive_command",
            required=True,
        )
    )

    drive_list = (
        drive_sub.add_parser(
            "list",
            help="Detect tape drives",
        )
    )

    drive_list.set_defaults(
        func=cmd_drive_list,
    )

    drive_status = (
        drive_sub.add_parser(
            "status",
            help=(
                "Show tape drive status"
            ),
        )
    )

    drive_status.set_defaults(
        func=cmd_drive_status,
    )

    #
    # archive
    #

    archive_parser = (
        subparsers.add_parser(
            "archive",
            help="Archive files to tape",
        )
    )

    archive_sub = (
        archive_parser.add_subparsers(
            dest="archive_command",
            required=True,
        )
    )

    archive_add = (
        archive_sub.add_parser(
            "add",
            help=(
                "Archive one file to the loaded tape"
            ),
        )
    )

    archive_add.add_argument(
        "path",
        help="Path to file to archive",
    )

    archive_add.set_defaults(
        func=cmd_archive_add,
    )

    #
    # file
    #

    file_parser = (
        subparsers.add_parser(
            "file",
            help="Archived file catalog",
        )
    )

    file_sub = (
        file_parser.add_subparsers(
            dest="file_command",
            required=True,
        )
    )

    file_list = (
        file_sub.add_parser(
            "list",
            help="List archived files",
        )
    )

    file_list.set_defaults(
        func=cmd_file_list,
    )

    return parser


def main():
    parser = build_parser()

    args = parser.parse_args()

    args.func(args)

