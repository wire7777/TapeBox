import hashlib
from pathlib import Path

from tapebox.database import (
    get_file_by_id,
    mark_file_verified,
)

from tapebox.tape import (
    discover_drives,
    get_ltfs_virtual_attribute,
    get_tape_status,
    run_command,
    _is_mounted,
    _unmount_ltfs,
)


VERIFY_MOUNTPOINT = Path("/mnt/tapebox/ltfs")
READ_BUFFER_SIZE = 16 * 1024 * 1024


def verify_file(file_id):
    """
    Read a file back from LTFS and compare its SHA256
    with the checksum stored in the TapeBox catalog.
    """

    row = get_file_by_id(file_id)

    if row is None:
        return {
            "success": False,
            "verified": False,
            "error": f"File ID {file_id} does not exist.",
        }

    if row["is_spanned"]:
        return {
            "success": False,
            "verified": False,
            "error": (
                "Spanned-file verification is not implemented yet."
            ),
        }

    expected_uuid = row["ltfs_uuid"]
    expected_checksum = row["checksum_sha256"]
    tape_path = row["tape_path"]

    if not expected_uuid:
        return {
            "success": False,
            "verified": False,
            "error": "Catalog entry has no LTFS tape UUID.",
        }

    if not expected_checksum:
        return {
            "success": False,
            "verified": False,
            "error": "Catalog entry has no SHA256 checksum.",
        }

    if not tape_path:
        return {
            "success": False,
            "verified": False,
            "error": "Catalog entry has no tape path.",
        }

    drives = discover_drives()

    if not drives:
        return {
            "success": False,
            "verified": False,
            "error": "No tape drive detected.",
        }

    if len(drives) > 1:
        return {
            "success": False,
            "verified": False,
            "error": (
                "More than one tape drive detected. "
                "Drive selection is not implemented yet."
            ),
        }

    drive = drives[0]

    nst_device = drive.get("nst_device")
    sg_device = drive.get("sg_device")

    if not nst_device or not sg_device:
        return {
            "success": False,
            "verified": False,
            "error": "Tape drive device mapping is incomplete.",
        }

    status = get_tape_status(nst_device)

    if not status.get("available"):
        return {
            "success": False,
            "verified": False,
            "error": (
                status.get("error")
                or "Tape cartridge is not available."
            ),
        }

    if not status.get("online"):
        return {
            "success": False,
            "verified": False,
            "error": "Tape cartridge is not online.",
        }

    VERIFY_MOUNTPOINT.mkdir(
        parents=True,
        exist_ok=True,
    )

    if _is_mounted(VERIFY_MOUNTPOINT):
        return {
            "success": False,
            "verified": False,
            "error": (
                "TapeBox LTFS mount point is already mounted: "
                f"{VERIFY_MOUNTPOINT}"
            ),
        }

    mount_result = run_command(
        [
            "ltfs",
            str(VERIFY_MOUNTPOINT),
            "-o",
            f"devname={sg_device}",
            "-o",
            "ro",
        ],
        timeout=120,
    )

    if not _is_mounted(VERIFY_MOUNTPOINT):
        return {
            "success": False,
            "verified": False,
            "error": (
                mount_result.get("stderr")
                or mount_result.get("stdout")
                or "LTFS read-only mount failed."
            ),
        }

    result = None

    try:
        loaded_uuid = get_ltfs_virtual_attribute(
            VERIFY_MOUNTPOINT,
            "ltfs.volumeUUID",
        )

        loaded_name = get_ltfs_virtual_attribute(
            VERIFY_MOUNTPOINT,
            "ltfs.volumeName",
        )

        if not loaded_uuid:
            raise RuntimeError(
                "Could not read LTFS volume UUID."
            )

        if loaded_uuid != expected_uuid:
            result = {
                "success": False,
                "verified": False,
                "wrong_tape": True,
                "error": (
                    f"Wrong tape loaded. Need {row['tape_label']} "
                    f"({expected_uuid}), but loaded "
                    f"{loaded_name or '-'} ({loaded_uuid})."
                ),
            }

        else:
            relative_tape_path = tape_path.lstrip("/")

            file_on_tape = (
                VERIFY_MOUNTPOINT
                / relative_tape_path
            )

            if not file_on_tape.exists():
                raise FileNotFoundError(
                    f"File is missing from tape: {tape_path}"
                )

            if not file_on_tape.is_file():
                raise RuntimeError(
                    f"Tape path is not a regular file: {tape_path}"
                )

            digest = hashlib.sha256()

            with open(file_on_tape, "rb") as handle:
                while True:
                    chunk = handle.read(
                        READ_BUFFER_SIZE
                    )

                    if not chunk:
                        break

                    digest.update(chunk)

            actual_checksum = digest.hexdigest()
            actual_size = file_on_tape.stat().st_size

            checksum_match = (
                actual_checksum
                == expected_checksum
            )

            size_match = (
                actual_size
                == row["size_bytes"]
            )

            verified = (
                checksum_match
                and size_match
            )

            result = {
                "success": True,
                "verified": verified,
                "file_id": row["id"],
                "filename": row["filename"],
                "tape_label": row["tape_label"],
                "tape_path": tape_path,
                "expected_checksum": expected_checksum,
                "actual_checksum": actual_checksum,
                "expected_size": row["size_bytes"],
                "actual_size": actual_size,
                "checksum_match": checksum_match,
                "size_match": size_match,
            }

    except Exception as exc:
        result = {
            "success": False,
            "verified": False,
            "error": str(exc),
        }

    finally:
        unmounted, unmount_error = _unmount_ltfs(
            VERIFY_MOUNTPOINT
        )

        if not unmounted:
            result = {
                "success": False,
                "verified": False,
                "error": (
                    "LTFS unmount failed after verification: "
                    f"{unmount_error}"
                ),
            }

    if (
        result.get("success")
        and result.get("verified")
    ):
        mark_file_verified(
            file_id
        )

    return result
