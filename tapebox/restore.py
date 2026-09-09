import hashlib
import os
from pathlib import Path

from tapebox.database import get_file_by_id

from tapebox.tape import (
    discover_drives,
    get_ltfs_virtual_attribute,
    get_tape_status,
    mount_ltfs,
    _is_mounted,
    _unmount_ltfs,
)


RESTORE_MOUNTPOINT = Path("/mnt/tapebox/ltfs")
READ_BUFFER_SIZE = 16 * 1024 * 1024


def restore_file(file_id, destination):
    """
    Restore one non-spanned file from LTFS.

    Safety:
      - verifies the loaded cartridge by LTFS UUID
      - never overwrites an existing destination
      - copies to a temporary partial file
      - verifies size and SHA256 before final rename
      - removes partial output after failure
      - mounts the tape read-only
    """

    row = get_file_by_id(file_id)

    if row is None:
        return {
            "success": False,
            "error": f"File ID {file_id} does not exist.",
        }

    if row["is_spanned"]:
        return {
            "success": False,
            "error": (
                "Spanned-file restore is not implemented yet."
            ),
        }

    expected_uuid = row["ltfs_uuid"]
    expected_checksum = row["checksum_sha256"]
    tape_path = row["tape_path"]

    if not expected_uuid:
        return {
            "success": False,
            "error": "Catalog entry has no LTFS tape UUID.",
        }

    if not expected_checksum:
        return {
            "success": False,
            "error": "Catalog entry has no SHA256 checksum.",
        }

    if not tape_path:
        return {
            "success": False,
            "error": "Catalog entry has no tape path.",
        }

    destination = Path(destination)

    #
    # If destination is an existing directory, restore using
    # the catalog filename inside that directory.
    #
    if destination.exists() and destination.is_dir():
        final_destination = (
            destination / row["filename"]
        )
    else:
        final_destination = destination

    if final_destination.exists():
        return {
            "success": False,
            "error": (
                "Destination already exists. "
                "TapeBox will not overwrite it: "
                f"{final_destination}"
            ),
        }

    parent = final_destination.parent

    if not parent.exists():
        return {
            "success": False,
            "error": (
                f"Destination directory does not exist: {parent}"
            ),
        }

    if not parent.is_dir():
        return {
            "success": False,
            "error": (
                f"Destination parent is not a directory: {parent}"
            ),
        }

    partial_destination = (
        parent
        / (
            ".tapebox-partial-"
            + final_destination.name
        )
    )

    if partial_destination.exists():
        return {
            "success": False,
            "error": (
                "A previous partial restore exists: "
                f"{partial_destination}"
            ),
        }

    drives = discover_drives()

    if not drives:
        return {
            "success": False,
            "error": "No tape drive detected.",
        }

    if len(drives) > 1:
        return {
            "success": False,
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
            "error": "Tape drive device mapping is incomplete.",
        }

    status = get_tape_status(
        nst_device
    )

    if not status.get("available"):
        return {
            "success": False,
            "error": (
                status.get("error")
                or "Tape cartridge is not available."
            ),
        }

    if not status.get("online"):
        return {
            "success": False,
            "error": "Tape cartridge is not online.",
        }

    RESTORE_MOUNTPOINT.mkdir(
        parents=True,
        exist_ok=True,
    )

    if _is_mounted(RESTORE_MOUNTPOINT):
        return {
            "success": False,
            "error": (
                "TapeBox LTFS mount point is already mounted: "
                f"{RESTORE_MOUNTPOINT}"
            ),
        }

    mount_result = mount_ltfs(
        sg_device,
        RESTORE_MOUNTPOINT,
        read_only=True,
        retries=5,
        retry_delay=1.5,
        timeout=120,
    )

    if not _is_mounted(RESTORE_MOUNTPOINT):
        return {
            "success": False,
            "error": (
                mount_result.get("stderr")
                or mount_result.get("stdout")
                or "LTFS read-only mount failed."
            ),
        }

    result = None

    try:
        loaded_uuid = get_ltfs_virtual_attribute(
            RESTORE_MOUNTPOINT,
            "ltfs.volumeUUID",
        )

        loaded_name = get_ltfs_virtual_attribute(
            RESTORE_MOUNTPOINT,
            "ltfs.volumeName",
        )

        if not loaded_uuid:
            raise RuntimeError(
                "Could not read LTFS volume UUID."
            )

        if loaded_uuid != expected_uuid:
            result = {
                "success": False,
                "wrong_tape": True,
                "required_tape": row["tape_label"],
                "required_uuid": expected_uuid,
                "loaded_tape": loaded_name,
                "loaded_uuid": loaded_uuid,
                "error": (
                    f"Wrong tape loaded. Need "
                    f"{row['tape_label']} ({expected_uuid}), "
                    f"but loaded {loaded_name or '-'} "
                    f"({loaded_uuid})."
                ),
            }

        else:
            relative_tape_path = (
                tape_path.lstrip("/")
            )

            source = (
                RESTORE_MOUNTPOINT
                / relative_tape_path
            )

            if not source.exists():
                raise FileNotFoundError(
                    f"File is missing from tape: {tape_path}"
                )

            if not source.is_file():
                raise RuntimeError(
                    "Tape path is not a regular file: "
                    f"{tape_path}"
                )

            digest = hashlib.sha256()
            bytes_written = 0

            with open(source, "rb") as src:
                with open(
                    partial_destination,
                    "xb",
                ) as dst:

                    while True:
                        chunk = src.read(
                            READ_BUFFER_SIZE
                        )

                        if not chunk:
                            break

                        dst.write(chunk)
                        digest.update(chunk)
                        bytes_written += len(chunk)

                    dst.flush()
                    os.fsync(
                        dst.fileno()
                    )

            actual_checksum = (
                digest.hexdigest()
            )

            size_match = (
                bytes_written
                == row["size_bytes"]
            )

            checksum_match = (
                actual_checksum
                == expected_checksum
            )

            if not size_match:
                raise RuntimeError(
                    "Restored file size mismatch. "
                    f"Expected {row['size_bytes']} bytes, "
                    f"received {bytes_written} bytes."
                )

            if not checksum_match:
                raise RuntimeError(
                    "Restored file SHA256 mismatch. "
                    "The restored copy will not be accepted."
                )

            os.replace(
                partial_destination,
                final_destination,
            )

            #
            # Flush the destination directory so the rename
            # is committed before reporting success.
            #
            directory_fd = os.open(
                parent,
                os.O_RDONLY,
            )

            try:
                os.fsync(
                    directory_fd
                )
            finally:
                os.close(
                    directory_fd
                )

            result = {
                "success": True,
                "file_id": row["id"],
                "filename": row["filename"],
                "tape_label": row["tape_label"],
                "tape_path": tape_path,
                "destination": str(
                    final_destination
                ),
                "size_bytes": bytes_written,
                "sha256": actual_checksum,
                "verified": True,
            }

    except Exception as exc:
        result = {
            "success": False,
            "error": str(exc),
        }

    finally:
        unmounted, unmount_error = (
            _unmount_ltfs(
                RESTORE_MOUNTPOINT
            )
        )

        if not unmounted:
            result = {
                "success": False,
                "error": (
                    "Restore operation finished, but LTFS "
                    "could not be released cleanly: "
                    f"{unmount_error}"
                ),
            }

        #
        # A partial restore must never be left looking like
        # a valid restored file.
        #
        if (
            not result.get("success")
            and partial_destination.exists()
        ):
            try:
                partial_destination.unlink()
            except OSError:
                pass

    return result
