import hashlib
import os
from pathlib import Path

from tapebox.database import (
    get_file_by_id,
    get_file_parts,
)

from tapebox.tape import (
    discover_drives,
    eject_tape,
    get_ltfs_virtual_attribute,
    get_tape_status,
    mount_ltfs,
    _is_mounted,
    _unmount_ltfs,
)


RESTORE_MOUNTPOINT = Path("/mnt/tapebox/ltfs")
READ_BUFFER_SIZE = 16 * 1024 * 1024



def _report_progress(progress, message):
    """
    Send an optional progress message to the caller.

    Core restore code remains UI-agnostic:
      - CLI may pass print
      - web/API callers may pass their own callback
      - callers may omit progress entirely
    """
    if progress is not None:
        progress(message)


def _validate_spanned_partial(
    partial_path,
    parts,
):
    """
    Validate that an existing partial restore consists of an exact,
    complete prefix of the cataloged tape parts.

    Returns:
        {
            "parts_completed": ...,
            "bytes_completed": ...
        }

    A partial ending in the middle of a part is rejected.
    Every completed part is SHA256 checked independently.
    """

    partial_path = Path(
        partial_path
    )

    if not partial_path.exists():
        return {
            "parts_completed": 0,
            "bytes_completed": 0,
        }

    if not partial_path.is_file():
        raise RuntimeError(
            "Spanned restore partial exists but is not "
            f"a regular file: {partial_path}"
        )

    actual_size = partial_path.stat().st_size

    if actual_size == 0:
        return {
            "parts_completed": 0,
            "bytes_completed": 0,
        }

    expected_boundary = 0
    matching_parts = 0

    for part in parts:
        expected_boundary += int(
            part["size_bytes"]
        )

        if actual_size == expected_boundary:
            matching_parts = int(
                part["part_number"]
            )
            break

        if actual_size < expected_boundary:
            raise RuntimeError(
                "Existing spanned restore partial ends "
                "in the middle of a tape part. "
                "TapeBox will not resume it automatically."
            )

    if matching_parts == 0:
        raise RuntimeError(
            "Existing spanned restore partial size does not "
            "match a valid tape-part boundary."
        )

    #
    # Verify each already assembled part independently.
    #
    with open(partial_path, "rb") as handle:
        for part in parts[:matching_parts]:
            digest = hashlib.sha256()
            remaining = int(
                part["size_bytes"]
            )

            while remaining:
                chunk = handle.read(
                    min(
                        READ_BUFFER_SIZE,
                        remaining,
                    )
                )

                if not chunk:
                    raise RuntimeError(
                        "Existing spanned restore partial "
                        "ended unexpectedly during verification."
                    )

                digest.update(chunk)
                remaining -= len(chunk)

            if (
                digest.hexdigest()
                != part["checksum_sha256"]
            ):
                raise RuntimeError(
                    "Existing spanned restore partial failed "
                    "SHA256 verification for part "
                    f"{part['part_number']}."
                )

    return {
        "parts_completed": matching_parts,
        "bytes_completed": actual_size,
    }


def _restore_spanned_from_mounted_tape(
    row,
    destination,
    loaded_uuid,
    loaded_name=None,
):
    """
    Restore the next eligible physical part(s) of one spanned
    logical file from an LTFS cartridge that is already mounted.

    This helper does NOT:
      - discover the drive
      - mount LTFS
      - unmount LTFS
      - eject the cartridge

    The caller owns the tape session.

    Safety:
      - validates the catalog part sequence
      - validates an existing partial at exact part boundaries
      - verifies every part size and SHA256
      - fsyncs every verified part
      - rolls a failed part back to the previous verified boundary
      - verifies complete-file size and SHA256 before final rename
    """

    parts = get_file_parts(
        row["id"]
    )

    if not parts:
        return {
            "success": False,
            "error": (
                "Spanned file has no cataloged tape parts."
            ),
        }

    expected_number = 1
    total_part_bytes = 0

    for part in parts:
        if (
            int(part["part_number"])
            != expected_number
        ):
            return {
                "success": False,
                "error": (
                    "Spanned file has a missing or "
                    "out-of-order tape part. "
                    f"Expected part {expected_number}, "
                    f"found {part['part_number']}."
                ),
            }

        if not part["ltfs_uuid"]:
            return {
                "success": False,
                "error": (
                    "Spanned tape part has no LTFS UUID: "
                    f"part {part['part_number']}."
                ),
            }

        if not part["tape_path"]:
            return {
                "success": False,
                "error": (
                    "Spanned tape part has no tape path: "
                    f"part {part['part_number']}."
                ),
            }

        if not part["checksum_sha256"]:
            return {
                "success": False,
                "error": (
                    "Spanned tape part has no SHA256: "
                    f"part {part['part_number']}."
                ),
            }

        total_part_bytes += int(
            part["size_bytes"]
        )

        expected_number += 1

    if total_part_bytes != int(
        row["size_bytes"]
    ):
        return {
            "success": False,
            "error": (
                "Spanned file is incomplete in the catalog. "
                f"Expected {row['size_bytes']} bytes, "
                f"but parts total {total_part_bytes} bytes."
            ),
        }

    if not row["checksum_sha256"]:
        return {
            "success": False,
            "error": (
                "Spanned file has no complete-file SHA256."
            ),
        }

    destination = Path(
        destination
    )

    if (
        destination.exists()
        and destination.is_dir()
    ):
        final_destination = (
            destination
            / row["filename"]
        )
    else:
        final_destination = destination

    parent = final_destination.parent

    if not parent.exists():
        return {
            "success": False,
            "error": (
                "Destination directory does not exist: "
                f"{parent}"
            ),
        }

    if not parent.is_dir():
        return {
            "success": False,
            "error": (
                "Destination parent is not a directory: "
                f"{parent}"
            ),
        }

    #
    # Existing final files are accepted only when they match.
    #
    if final_destination.exists():
        if not final_destination.is_file():
            return {
                "success": False,
                "error": (
                    "Destination exists but is not a regular "
                    f"file: {final_destination}"
                ),
            }

        checksum, size = _sha256_file(
            final_destination
        )

        if (
            size == row["size_bytes"]
            and checksum
            == row["checksum_sha256"]
        ):
            return {
                "success": True,
                "completed": True,
                "file_id": row["id"],
                "filename": row["filename"],
                "destination": str(
                    final_destination
                ),
                "size_bytes": size,
                "sha256": checksum,
                "already_restored": True,
                "bytes_written_this_run": 0,
            }

        return {
            "success": False,
            "error": (
                "Destination already exists but does not "
                "match the TapeBox catalog. It will not "
                f"be overwritten: {final_destination}"
            ),
        }

    partial_destination = (
        parent
        / (
            ".tapebox-partial-"
            + final_destination.name
        )
    )

    try:
        partial_state = (
            _validate_spanned_partial(
                partial_destination,
                parts,
            )
        )
    except Exception as exc:
        return {
            "success": False,
            "error": str(exc),
        }

    completed_parts = int(
        partial_state[
            "parts_completed"
        ]
    )

    #
    # Crash-recovery case:
    # every part is already safely present in the partial.
    #
    if completed_parts >= len(parts):
        checksum, size = _sha256_file(
            partial_destination
        )

        if (
            size != row["size_bytes"]
            or checksum
            != row["checksum_sha256"]
        ):
            return {
                "success": False,
                "error": (
                    "Complete spanned partial failed final "
                    "size or SHA256 verification."
                ),
            }

        os.replace(
            partial_destination,
            final_destination,
        )

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

        return {
            "success": True,
            "completed": True,
            "file_id": row["id"],
            "filename": row["filename"],
            "destination": str(
                final_destination
            ),
            "size_bytes": size,
            "sha256": checksum,
            "verified": True,
            "bytes_written_this_run": 0,
        }

    next_part = parts[
        completed_parts
    ]

    if (
        loaded_uuid
        != next_part["ltfs_uuid"]
    ):
        return {
            "success": False,
            "wrong_tape": True,
            "required_tape": (
                next_part["tape_label"]
            ),
            "required_uuid": (
                next_part["ltfs_uuid"]
            ),
            "loaded_tape": (
                loaded_name or "-"
            ),
            "loaded_uuid": loaded_uuid,
            "part_number": (
                next_part["part_number"]
            ),
            "error": (
                "Wrong tape loaded. Need "
                f"{next_part['tape_label']} "
                f"for part "
                f"{next_part['part_number']}."
            ),
        }

    #
    # Consume every consecutive part belonging to this cartridge.
    #
    parts_this_tape = []

    for part in parts[
        completed_parts:
    ]:
        if (
            part["ltfs_uuid"]
            != loaded_uuid
        ):
            break

        parts_this_tape.append(
            part
        )

    bytes_written_this_run = 0

    try:
        mode = (
            "ab"
            if partial_destination.exists()
            else "xb"
        )

        with open(
            partial_destination,
            mode,
        ) as dst:

            for part in parts_this_tape:
                #
                # Exact boundary of the last completely verified part.
                #
                part_start_offset = dst.tell()

                try:
                    source = (
                        RESTORE_MOUNTPOINT
                        / part[
                            "tape_path"
                        ].lstrip("/")
                    )

                    if not source.exists():
                        raise FileNotFoundError(
                            "Tape part is missing: "
                            f"{part['tape_path']}"
                        )

                    if not source.is_file():
                        raise RuntimeError(
                            "Tape part path is not a "
                            "regular file: "
                            f"{part['tape_path']}"
                        )

                    digest = hashlib.sha256()
                    copied = 0

                    with open(
                        source,
                        "rb",
                    ) as src:
                        while True:
                            chunk = src.read(
                                READ_BUFFER_SIZE
                            )

                            if not chunk:
                                break

                            dst.write(
                                chunk
                            )

                            digest.update(
                                chunk
                            )

                            copied += len(
                                chunk
                            )

                    if (
                        copied
                        != part["size_bytes"]
                    ):
                        raise RuntimeError(
                            "Restored tape-part size "
                            "mismatch for part "
                            f"{part['part_number']}."
                        )

                    if (
                        digest.hexdigest()
                        != part[
                            "checksum_sha256"
                        ]
                    ):
                        raise RuntimeError(
                            "Restored tape-part SHA256 "
                            "mismatch for part "
                            f"{part['part_number']}."
                        )

                    #
                    # Make this verified boundary durable.
                    #
                    dst.flush()

                    os.fsync(
                        dst.fileno()
                    )

                    bytes_written_this_run += (
                        copied
                    )

                except Exception:
                    #
                    # Never preserve an incomplete or failed part.
                    #
                    dst.seek(
                        part_start_offset
                    )

                    dst.truncate(
                        part_start_offset
                    )

                    dst.flush()

                    os.fsync(
                        dst.fileno()
                    )

                    raise

        completed_parts += len(
            parts_this_tape
        )

        #
        # All parts assembled: verify entire logical file before
        # exposing the final filename.
        #
        if completed_parts == len(parts):
            checksum, size = _sha256_file(
                partial_destination
            )

            if (
                size != row["size_bytes"]
            ):
                raise RuntimeError(
                    "Restored complete spanned file "
                    "size mismatch. "
                    f"Expected {row['size_bytes']} "
                    f"bytes, received {size}."
                )

            if (
                checksum
                != row["checksum_sha256"]
            ):
                raise RuntimeError(
                    "Restored complete spanned file "
                    "SHA256 mismatch."
                )

            os.replace(
                partial_destination,
                final_destination,
            )

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

            return {
                "success": True,
                "completed": True,
                "file_id": row["id"],
                "filename": row["filename"],
                "destination": str(
                    final_destination
                ),
                "parts_total": len(parts),
                "parts_completed": len(
                    parts
                ),
                "size_bytes": size,
                "sha256": checksum,
                "verified": True,
                "bytes_written_this_run": (
                    bytes_written_this_run
                ),
            }

        required = parts[
            completed_parts
        ]

        return {
            "success": True,
            "completed": False,
            "needs_next_tape": True,
            "file_id": row["id"],
            "filename": row["filename"],
            "destination": str(
                final_destination
            ),
            "partial_destination": str(
                partial_destination
            ),
            "parts_total": len(parts),
            "parts_completed": (
                completed_parts
            ),
            "bytes_completed": (
                partial_destination.stat().st_size
            ),
            "bytes_written_this_run": (
                bytes_written_this_run
            ),
            "next_part": required[
                "part_number"
            ],
            "required_tape": required[
                "tape_label"
            ],
            "required_uuid": required[
                "ltfs_uuid"
            ],
        }

    except Exception as exc:
        return {
            "success": False,
            "error": str(exc),
            "partial_preserved": (
                partial_destination.exists()
            ),
            "bytes_written_this_run": (
                bytes_written_this_run
            ),
        }


def _restore_spanned_file(
    row,
    destination,
    progress=None,
):
    """
    Restore one logical file whose physical contents span multiple
    LTFS cartridges.

    One invocation consumes whichever next required cartridge is
    currently loaded. The partial assembled output is intentionally
    preserved between successful tape swaps.

    The final file is only renamed into place after:
      - every part size is verified
      - every part SHA256 is verified
      - the complete file size is verified
      - the complete file SHA256 is verified
    """

    parts = get_file_parts(
        row["id"]
    )

    if not parts:
        return {
            "success": False,
            "error": (
                "Spanned file has no cataloged tape parts."
            ),
        }

    #
    # Parts must form an exact sequence beginning at 1.
    #
    expected_number = 1
    total_part_bytes = 0

    for part in parts:
        if (
            int(part["part_number"])
            != expected_number
        ):
            return {
                "success": False,
                "error": (
                    "Spanned file has a missing or "
                    "out-of-order tape part. "
                    f"Expected part {expected_number}, "
                    f"found {part['part_number']}."
                ),
            }

        if not part["ltfs_uuid"]:
            return {
                "success": False,
                "error": (
                    "Spanned tape part has no LTFS UUID: "
                    f"part {part['part_number']}."
                ),
            }

        if not part["tape_path"]:
            return {
                "success": False,
                "error": (
                    "Spanned tape part has no tape path: "
                    f"part {part['part_number']}."
                ),
            }

        if not part["checksum_sha256"]:
            return {
                "success": False,
                "error": (
                    "Spanned tape part has no SHA256: "
                    f"part {part['part_number']}."
                ),
            }

        total_part_bytes += int(
            part["size_bytes"]
        )

        expected_number += 1

    if total_part_bytes != int(
        row["size_bytes"]
    ):
        return {
            "success": False,
            "error": (
                "Spanned file is incomplete in the catalog. "
                f"Expected {row['size_bytes']} bytes, "
                f"but parts total {total_part_bytes} bytes."
            ),
        }

    if not row["checksum_sha256"]:
        return {
            "success": False,
            "error": (
                "Spanned file has no complete-file SHA256."
            ),
        }

    destination = Path(
        destination
    )

    if (
        destination.exists()
        and destination.is_dir()
    ):
        final_destination = (
            destination
            / row["filename"]
        )
    else:
        final_destination = destination

    parent = final_destination.parent

    if not parent.exists():
        return {
            "success": False,
            "error": (
                "Destination directory does not exist: "
                f"{parent}"
            ),
        }

    if not parent.is_dir():
        return {
            "success": False,
            "error": (
                "Destination parent is not a directory: "
                f"{parent}"
            ),
        }

    #
    # A completed destination is accepted only when it matches the
    # complete logical file.
    #
    if final_destination.exists():
        if not final_destination.is_file():
            return {
                "success": False,
                "error": (
                    "Destination exists but is not a regular "
                    f"file: {final_destination}"
                ),
            }

        checksum, size = _sha256_file(
            final_destination
        )

        if (
            size == row["size_bytes"]
            and checksum
            == row["checksum_sha256"]
        ):
            return {
                "success": True,
                "completed": True,
                "file_id": row["id"],
                "filename": row["filename"],
                "destination": str(
                    final_destination
                ),
                "size_bytes": size,
                "sha256": checksum,
                "already_restored": True,
            }

        return {
            "success": False,
            "error": (
                "Destination already exists but does not "
                "match the TapeBox catalog. It will not "
                f"be overwritten: {final_destination}"
            ),
        }

    partial_destination = (
        parent
        / (
            ".tapebox-partial-"
            + final_destination.name
        )
    )

    try:
        partial_state = (
            _validate_spanned_partial(
                partial_destination,
                parts,
            )
        )
    except Exception as exc:
        return {
            "success": False,
            "error": str(exc),
        }

    completed_parts = int(
        partial_state[
            "parts_completed"
        ]
    )

    if completed_parts >= len(parts):
        #
        # This should only happen after a crash between completing
        # the final part and the final full-file verification.
        #
        checksum, size = _sha256_file(
            partial_destination
        )

        if (
            size != row["size_bytes"]
            or checksum
            != row["checksum_sha256"]
        ):
            return {
                "success": False,
                "error": (
                    "Complete spanned partial failed final "
                    "size or SHA256 verification."
                ),
            }

        os.replace(
            partial_destination,
            final_destination,
        )

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

        return {
            "success": True,
            "completed": True,
            "file_id": row["id"],
            "filename": row["filename"],
            "destination": str(
                final_destination
            ),
            "size_bytes": size,
            "sha256": checksum,
            "verified": True,
        }

    next_part = parts[
        completed_parts
    ]

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

    nst_device = drive.get(
        "nst_device"
    )

    sg_device = drive.get(
        "sg_device"
    )

    if not nst_device or not sg_device:
        return {
            "success": False,
            "error": (
                "Tape drive device mapping is incomplete."
            ),
        }

    _report_progress(
        progress,
        "Waiting for tape drive to become ready...",
    )

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
            "error": (
                "Tape cartridge is not online."
            ),
        }

    RESTORE_MOUNTPOINT.mkdir(
        parents=True,
        exist_ok=True,
    )

    if _is_mounted(
        RESTORE_MOUNTPOINT
    ):
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

    if not _is_mounted(
        RESTORE_MOUNTPOINT
    ):
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
        loaded_uuid = (
            get_ltfs_virtual_attribute(
                RESTORE_MOUNTPOINT,
                "ltfs.volumeUUID",
            )
        )

        loaded_name = (
            get_ltfs_virtual_attribute(
                RESTORE_MOUNTPOINT,
                "ltfs.volumeName",
            )
        )

        if not loaded_uuid:
            raise RuntimeError(
                "Could not read LTFS volume UUID."
            )

        result = (
            _restore_spanned_from_mounted_tape(
                row,
                final_destination,
                loaded_uuid,
                loaded_name,
            )
        )

    except Exception as exc:
        result = {
            "success": False,
            "error": str(exc),
            "partial_preserved": (
                partial_destination.exists()
            ),
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
                    "Spanned restore operation finished, "
                    "but LTFS could not be released cleanly: "
                    f"{unmount_error}"
                ),
                "partial_preserved": (
                    partial_destination.exists()
                ),
                "auto_ejected": False,
            }

        elif (
            result
            and (
                result.get("success")
                or result.get("wrong_tape")
            )
        ):
            #
            # LTFS has been cleanly released. It is now safe
            # to unload the cartridge.
            #
            eject_result = eject_tape()

            result["auto_ejected"] = (
                eject_result.get(
                    "success",
                    False,
                )
            )

            if not eject_result.get("success"):
                result["eject_warning"] = (
                    eject_result.get(
                        "error",
                        "Tape eject failed.",
                    )
                )

    return result


def restore_file(
    file_id,
    destination,
    progress=None,
):
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
        return _restore_spanned_file(
            row,
            destination,
            progress=progress,
        )

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

    _report_progress(
        progress,
        "Waiting for tape drive to become ready...",
    )

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


def _sha256_file(path):
    """
    Return SHA256 and size for a local file.
    """

    digest = hashlib.sha256()
    size = 0

    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(
                READ_BUFFER_SIZE
            )

            if not chunk:
                break

            digest.update(chunk)
            size += len(chunk)

    return digest.hexdigest(), size


def _build_archive_tape_work(
    remaining,
    destination,
):
    """
    Build the next tape-work plan for an archive-job restore.

    Normal files contribute one tape item.

    Spanned files contribute only their next unfinished physical
    part, based on the validated local partial-file boundary.

    Returned structure:

        {
            "<ltfs_uuid>": {
                "tape_label": "...",
                "ltfs_uuid": "...",
                "items": [...]
            }
        }
    """

    destination = Path(destination)
    tape_work = {}

    for row in remaining:
        if not row["is_spanned"]:
            ltfs_uuid = row["ltfs_uuid"]

            entry = tape_work.setdefault(
                ltfs_uuid,
                {
                    "tape_label": (
                        row["tape_label"]
                        or ltfs_uuid
                    ),
                    "ltfs_uuid": ltfs_uuid,
                    "items": [],
                },
            )

            entry["items"].append(
                {
                    "kind": "file",
                    "file_id": row["id"],
                    "relative_path": (
                        row["relative_path"]
                    ),
                    "tape_path": (
                        row["tape_path"]
                    ),
                    "size_bytes": (
                        row["size_bytes"]
                    ),
                }
            )

            continue

        parts = get_file_parts(
            row["id"]
        )

        if not parts:
            raise RuntimeError(
                "Spanned file has no physical "
                f"parts: file ID {row['id']}."
            )

        output = (
            destination
            / row["relative_path"]
        )

        partial = (
            output.parent
            / (
                ".tapebox-partial-"
                + output.name
            )
        )

        partial_state = (
            _validate_spanned_partial(
                partial,
                parts,
            )
        )

        completed_parts = int(
            partial_state[
                "parts_completed"
            ]
        )

        if completed_parts >= len(parts):
            #
            # All physical parts are already present in the partial.
            # Final verification/rename is handled by the existing
            # spanned restore engine, so no cartridge is needed here.
            #
            continue

        part = parts[
            completed_parts
        ]

        ltfs_uuid = part[
            "ltfs_uuid"
        ]

        entry = tape_work.setdefault(
            ltfs_uuid,
            {
                "tape_label": (
                    part["tape_label"]
                    or ltfs_uuid
                ),
                "ltfs_uuid": ltfs_uuid,
                "items": [],
            },
        )

        entry["items"].append(
            {
                "kind": "part",
                "file_id": row["id"],
                "relative_path": (
                    row["relative_path"]
                ),
                "part_number": (
                    part["part_number"]
                ),
                "tape_path": (
                    part["tape_path"]
                ),
                "size_bytes": (
                    part["size_bytes"]
                ),
            }
        )

    return tape_work


def restore_archive_job(
    job_id,
    destination,
    progress=None,
):
    """
    Restore all possible files from an archive job using the
    currently loaded tape.

    The command is resumable:
      - already-restored files are SHA256 checked and skipped
      - files on the currently loaded tape are restored together
      - another invocation continues after the next tape is loaded
      - existing mismatched files are never overwritten
      - every newly restored file is SHA256 verified
    """

    from tapebox.database import get_archive_restore_plan

    plan = get_archive_restore_plan(
        job_id
    )

    if plan is None:
        return {
            "success": False,
            "error": (
                f"Archive job {job_id} does not exist."
            ),
        }

    files = plan["files"]
    job = plan["job"]

    if not files:
        return {
            "success": False,
            "error": (
                f"Archive job {job_id} contains no cataloged files."
            ),
        }

    for row in files:
        #
        # Every logical file requires a complete-file SHA256.
        #
        if not row["checksum_sha256"]:
            return {
                "success": False,
                "error": (
                    f"File ID {row['id']} has no SHA256 checksum."
                ),
            }

        #
        # A normal file lives directly on one tape.
        #
        # A true spanned file intentionally has no parent
        # tape_id/tape_path; its physical locations live in
        # file_parts instead.
        #
        if not row["is_spanned"]:
            if not row["ltfs_uuid"]:
                return {
                    "success": False,
                    "error": (
                        f"File ID {row['id']} has no LTFS UUID."
                    ),
                }

            if not row["tape_path"]:
                return {
                    "success": False,
                    "error": (
                        f"File ID {row['id']} has no tape path."
                    ),
                }

    destination = Path(destination)

    destination.mkdir(
        parents=True,
        exist_ok=True,
    )

    if not destination.is_dir():
        return {
            "success": False,
            "error": (
                f"Restore destination is not a directory: "
                f"{destination}"
            ),
        }

    #
    # First inspect anything already restored.
    #
    completed_ids = set()
    remaining = []

    for row in files:
        output = (
            destination
            / row["relative_path"]
        )

        if not output.exists():
            remaining.append(row)
            continue

        if not output.is_file():
            return {
                "success": False,
                "error": (
                    "Restore destination already exists but "
                    "is not a regular file: "
                    f"{output}"
                ),
            }

        checksum, size = _sha256_file(
            output
        )

        if (
            size != row["size_bytes"]
            or checksum != row["checksum_sha256"]
        ):
            return {
                "success": False,
                "error": (
                    "Existing restore destination does not "
                    "match the TapeBox catalog and will not "
                    f"be overwritten: {output}"
                ),
            }

        completed_ids.add(
            row["id"]
        )

    if not remaining:
        return {
            "success": True,
            "completed": True,
            "job_id": job_id,
            "source_path": job["source_path"],
            "destination": str(destination),
            "files_total": len(files),
            "files_completed": len(files),
            "files_restored_this_run": 0,
            "files_skipped": len(completed_ids),
            "bytes_restored_this_run": 0,
        }

    #
    # True spanned files already have a tested, resumable restore
    # engine in restore_file() / _restore_spanned_file().
    #
    # Handle one unfinished spanned logical file per invocation.
    # That engine:
    #   - validates the existing partial
    #   - verifies each physical part
    #   - resumes at exact part boundaries
    #   - verifies the complete logical SHA256
    #   - safely unmounts LTFS
    #   - auto-ejects the cartridge
    #
    # Once all spanned logical files are complete, this function
    # falls through to the existing bulk normal-file restore path.
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

    nst_device = drive.get(
        "nst_device"
    )

    sg_device = drive.get(
        "sg_device"
    )

    if not nst_device or not sg_device:
        return {
            "success": False,
            "error": (
                "Tape drive device mapping is incomplete."
            ),
        }

    _report_progress(
        progress,
        "Waiting for tape drive to become ready...",
    )

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
            "error": (
                "Tape cartridge is not online."
            ),
        }

    RESTORE_MOUNTPOINT.mkdir(
        parents=True,
        exist_ok=True,
    )

    if _is_mounted(
        RESTORE_MOUNTPOINT
    ):
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

    if not _is_mounted(
        RESTORE_MOUNTPOINT
    ):
        return {
            "success": False,
            "error": (
                mount_result.get("stderr")
                or mount_result.get("stdout")
                or "LTFS read-only mount failed."
            ),
        }

    result = None

    restored_this_run = 0
    bytes_restored = 0
    restored_ids = set()

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

        tape_work = _build_archive_tape_work(
            remaining,
            destination,
        )

        loaded_work = tape_work.get(
            loaded_uuid
        )

        if not loaded_work:
            required = []

            for row in remaining:
                label = (
                    row["tape_label"]
                    or row["ltfs_uuid"]
                )

                if label not in required:
                    required.append(label)

            result = {
                "success": False,
                "wrong_tape": True,
                "loaded_tape": (
                    loaded_name or "-"
                ),
                "loaded_uuid": loaded_uuid,
                "required_tapes": required,
                "error": (
                    "Loaded tape is not required for the "
                    "remaining files in this restore job."
                ),
            }

        else:
            #
            # Restore all normal files belonging to this cartridge.
            #
            loaded_normal_files = [
                row
                for row in remaining
                if (
                    not row["is_spanned"]
                    and row["ltfs_uuid"] == loaded_uuid
                )
            ]

            for row in loaded_normal_files:
                relative_tape_path = (
                    row["tape_path"].lstrip("/")
                )

                source = (
                    RESTORE_MOUNTPOINT
                    / relative_tape_path
                )

                output = (
                    destination
                    / row["relative_path"]
                )

                output.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                if not source.exists():
                    raise FileNotFoundError(
                        "File is missing from tape: "
                        f"{row['tape_path']}"
                    )

                if not source.is_file():
                    raise RuntimeError(
                        "Tape path is not a regular file: "
                        f"{row['tape_path']}"
                    )

                partial = (
                    output.parent
                    / (
                        ".tapebox-partial-"
                        + output.name
                    )
                )

                if partial.exists():
                    partial.unlink()

                digest = hashlib.sha256()
                copied = 0

                try:
                    with open(
                        source,
                        "rb",
                    ) as src:
                        with open(
                            partial,
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

                                copied += len(
                                    chunk
                                )

                            dst.flush()
                            os.fsync(
                                dst.fileno()
                            )

                    actual_checksum = (
                        digest.hexdigest()
                    )

                    if copied != row["size_bytes"]:
                        raise RuntimeError(
                            "Restored file size mismatch for "
                            f"{row['relative_path']}. "
                            f"Expected {row['size_bytes']} bytes, "
                            f"received {copied} bytes."
                        )

                    if (
                        actual_checksum
                        != row["checksum_sha256"]
                    ):
                        raise RuntimeError(
                            "Restored file SHA256 mismatch for "
                            f"{row['relative_path']}."
                        )

                    os.replace(
                        partial,
                        output,
                    )

                    directory_fd = os.open(
                        output.parent,
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

                    restored_this_run += 1
                    bytes_restored += copied

                    restored_ids.add(
                        row["id"]
                    )

                except Exception:
                    if partial.exists():
                        try:
                            partial.unlink()
                        except OSError:
                            pass

                    raise

            #
            # Restore every spanned logical file whose NEXT required
            # physical part is on this same loaded cartridge.
            #
            # The mounted helper may consume several consecutive parts
            # from this tape while preserving exact rollback boundaries.
            #
            loaded_spanned_ids = []

            for item in loaded_work["items"]:
                if item["kind"] != "part":
                    continue

                file_id = item["file_id"]

                if file_id not in loaded_spanned_ids:
                    loaded_spanned_ids.append(
                        file_id
                    )

            for file_id in loaded_spanned_ids:
                row = next(
                    item
                    for item in remaining
                    if item["id"] == file_id
                )

                output = (
                    destination
                    / row["relative_path"]
                )

                output.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                span_result = (
                    _restore_spanned_from_mounted_tape(
                        row,
                        output,
                        loaded_uuid,
                        loaded_name,
                    )
                )

                if not span_result.get("success"):
                    raise RuntimeError(
                        span_result.get(
                            "error",
                            "Spanned restore failed.",
                        )
                    )

                bytes_restored += int(
                    span_result.get(
                        "bytes_written_this_run",
                        0,
                    )
                )

                if span_result.get(
                    "completed",
                    False,
                ):
                    restored_this_run += 1

                    restored_ids.add(
                        row["id"]
                    )

            now_completed = (
                completed_ids
                | restored_ids
            )

            still_remaining = [
                row
                for row in files
                if row["id"] not in now_completed
            ]

            #
            # Build required-tape reporting from the same planner.
            # This correctly handles spanned parent rows, which do not
            # have their own tape_id / tape_label / ltfs_uuid.
            #
            required_tapes = []

            if still_remaining:
                next_work = (
                    _build_archive_tape_work(
                        still_remaining,
                        destination,
                    )
                )

                for work in next_work.values():
                    label = (
                        work["tape_label"]
                        or work["ltfs_uuid"]
                    )

                    if (
                        label
                        and label
                        not in required_tapes
                    ):
                        required_tapes.append(
                            label
                        )

            result = {
                "success": True,
                "completed": (
                    len(still_remaining) == 0
                ),
                "job_id": job_id,
                "source_path": job["source_path"],
                "destination": str(destination),
                "loaded_tape": (
                    loaded_name or "-"
                ),
                "loaded_uuid": loaded_uuid,
                "files_total": len(files),
                "files_completed": len(
                    now_completed
                ),
                "files_restored_this_run": (
                    restored_this_run
                ),
                "files_skipped": len(
                    completed_ids
                ),
                "bytes_restored_this_run": (
                    bytes_restored
                ),
                "required_tapes": (
                    required_tapes
                ),
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
                    "LTFS unmount failed after restore: "
                    f"{unmount_error}"
                ),
                "auto_ejected": False,
            }

        elif (
            result
            and (
                result.get("success")
                or result.get("wrong_tape")
            )
        ):
            #
            # The bulk normal-file restore has finished using this
            # cartridge and LTFS released it cleanly.
            #
            eject_result = eject_tape()

            result["auto_ejected"] = (
                eject_result.get(
                    "success",
                    False,
                )
            )

            if not eject_result.get("success"):
                result["eject_warning"] = (
                    eject_result.get(
                        "error",
                        "Tape eject failed.",
                    )
                )

    return result
