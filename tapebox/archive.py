
import hashlib
import os
from pathlib import Path

from tapebox.database import (
    get_tape_by_uuid,
    record_archived_file,
)

from tapebox.tape import (
    discover_drives,
    get_ltfs_virtual_attribute,
    get_tape_status,
    run_command,
    _is_mounted,
    _unmount_ltfs,
)


ARCHIVE_MOUNTPOINT = Path("/mnt/tapebox/ltfs")
COPY_BUFFER_SIZE = 16 * 1024 * 1024


def archive_file(source_path):
    """
    Archive one regular file to the currently loaded LTFS cartridge.

    Safety rules:
      - exactly one tape drive must be available
      - cartridge must be online
      - cartridge must not be write protected
      - LTFS is mounted only once
      - LTFS UUID is read immediately after mount
      - UUID must already exist in TapeBox SQLite catalog
      - no data is copied until UUID verification succeeds
      - existing destination files are never overwritten
      - database is updated only after successful LTFS unmount
    """

    source = Path(source_path).expanduser().resolve()

    if not source.exists():
        return {
            "success": False,
            "error": f"Source does not exist: {source}",
        }

    if not source.is_file():
        return {
            "success": False,
            "error": f"Source is not a regular file: {source}",
        }

    source_size = source.stat().st_size

    #
    # Detect drive.
    #

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

    #
    # Check basic cartridge state before LTFS mount.
    #

    tape_status = get_tape_status(nst_device)

    if not tape_status.get("available"):
        return {
            "success": False,
            "error": (
                tape_status.get("error")
                or "Tape cartridge is not available."
            ),
        }

    if not tape_status.get("online"):
        return {
            "success": False,
            "error": "Tape cartridge is not online.",
        }

    if tape_status.get("write_protected"):
        return {
            "success": False,
            "error": "Tape cartridge is write protected.",
        }

    #
    # Prepare TapeBox LTFS mountpoint.
    #

    ARCHIVE_MOUNTPOINT.mkdir(
        parents=True,
        exist_ok=True,
    )

    if _is_mounted(ARCHIVE_MOUNTPOINT):
        return {
            "success": False,
            "error": (
                "TapeBox archive mount point is already mounted: "
                f"{ARCHIVE_MOUNTPOINT}"
            ),
        }

    #
    # Mount LTFS ONCE.
    #
    # Important:
    # We deliberately do not perform a separate read-only inspection
    # first. Some drives/backends do not release quickly enough for
    # an immediate second LTFS open.
    #
    # Nothing is written until the UUID below has been verified
    # against the TapeBox catalog.
    #

    mount_result = run_command(
        [
            "ltfs",
            str(ARCHIVE_MOUNTPOINT),
            "-o",
            f"devname={sg_device}",
        ],
        timeout=120,
    )

    if not _is_mounted(ARCHIVE_MOUNTPOINT):
        message = (
            mount_result.get("stderr")
            or mount_result.get("stdout")
            or "LTFS mount failed."
        )

        return {
            "success": False,
            "error": message,
        }

    success = False
    error = None

    ltfs_uuid = None
    volume_name = None
    tape = None
    final_destination = None
    sha256 = None

    try:
        #
        # FIRST operation after mounting:
        # identify the physical cartridge.
        #

        ltfs_uuid = get_ltfs_virtual_attribute(
            ARCHIVE_MOUNTPOINT,
            "ltfs.volumeUUID",
        )

        volume_name = get_ltfs_virtual_attribute(
            ARCHIVE_MOUNTPOINT,
            "ltfs.volumeName",
        )

        if not ltfs_uuid:
            raise RuntimeError(
                "Could not read LTFS volume UUID. "
                "No data was written."
            )

        #
        # Permanent media identity must already be registered.
        #

        tape = get_tape_by_uuid(
            ltfs_uuid
        )

        if tape is None:
            raise RuntimeError(
                "Loaded cartridge is not registered in TapeBox. "
                f"LTFS UUID: {ltfs_uuid}. "
                "No data was written."
            )

        #
        # UUID passed. Writes are now permitted.
        #

        archive_root = (
            ARCHIVE_MOUNTPOINT
            / "archive"
        )

        archive_root.mkdir(
            parents=True,
            exist_ok=True,
        )

        destination = (
            archive_root
            / source.name
        )

        if destination.exists():
            raise FileExistsError(
                "Destination already exists on tape: "
                f"/archive/{source.name}"
            )

        temp_destination = (
            archive_root
            / f".tapebox-partial-{source.name}"
        )

        if temp_destination.exists():
            temp_destination.unlink()

        #
        # Stream source -> LTFS while calculating SHA256.
        #

        digest = hashlib.sha256()

        with open(source, "rb") as src:
            with open(
                temp_destination,
                "xb",
            ) as dst:

                while True:
                    chunk = src.read(
                        COPY_BUFFER_SIZE
                    )

                    if not chunk:
                        break

                    dst.write(chunk)
                    digest.update(chunk)

                dst.flush()
                os.fsync(dst.fileno())

        copied_size = (
            temp_destination.stat().st_size
        )

        if copied_size != source_size:
            raise RuntimeError(
                "Copied file size does not match source. "
                f"Source={source_size}, "
                f"copied={copied_size}"
            )

        sha256 = digest.hexdigest()

        #
        # Atomic-ish finalization inside LTFS filesystem.
        #

        temp_destination.rename(
            destination
        )

        final_destination = (
            f"/archive/{source.name}"
        )

        #
        # Flush filesystem buffers before unmount.
        #

        sync_result = run_command(
            ["sync"],
            timeout=120,
        )

        if sync_result.get("returncode") not in (
            None,
            0,
        ):
            raise RuntimeError(
                "System sync failed before LTFS unmount."
            )

        success = True

    except Exception as exc:
        error = str(exc)

    finally:
        #
        # LTFS must always be cleanly released.
        #

        unmounted, unmount_error = _unmount_ltfs(
            ARCHIVE_MOUNTPOINT
        )

        if not unmounted:
            success = False

            if error:
                error = (
                    f"{error}; additionally LTFS "
                    f"unmount failed: {unmount_error}"
                )
            else:
                error = (
                    "LTFS unmount failed: "
                    f"{unmount_error}"
                )

    if not success:
        return {
            "success": False,
            "error": (
                error
                or "Archive operation failed."
            ),
        }

    #
    # Do NOT catalog anything until LTFS has cleanly unmounted.
    #

    file_id = record_archived_file(
        original_path=str(source),
        relative_path=source.name,
        filename=source.name,
        size_bytes=source_size,
        sha256=sha256,
        tape_id=tape["id"],
        tape_path=final_destination,
    )

    return {
        "success": True,
        "file_id": file_id,
        "source": str(source),
        "filename": source.name,
        "size_bytes": source_size,
        "sha256": sha256,
        "tape_id": tape["id"],
        "tape_label": tape["label"],
        "volume_name": volume_name,
        "ltfs_uuid": ltfs_uuid,
        "tape_path": final_destination,
    }