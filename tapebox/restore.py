import hashlib
import time
import os
from pathlib import Path
from datetime import datetime, timezone

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



def _report_progress(
    progress,
    message,
    **details,
):
    """
    Send optional restore progress to the caller.

    Existing callbacks such as print continue receiving plain text.

    Callbacks that advertise:

        tapebox_structured_progress = True

    receive a dictionary containing the human-readable message plus
    structured fields used by the web UI.
    """

    if progress is None:
        return

    if getattr(
        progress,
        "tapebox_structured_progress",
        False,
    ):
        event = {
            "message": message,
            **details,
        }

        progress(event)
        return

    progress(message)


def _report_transfer_progress(
    progress,
    *,
    filename,
    bytes_written,
    bytes_total,
    started_at,
    part_number=None,
    parts_total=None,
    base_bytes=0,
):
    """
    Report measured restore throughput.

    bytes_written is the number copied during the current physical
    file/part operation.

    base_bytes represents bytes already assembled before this
    operation, which is useful for multi-tape spanned files.
    """

    now = time.monotonic()

    elapsed = max(
        now - started_at,
        0.000001,
    )

    speed_bps = (
        bytes_written
        / elapsed
    )

    logical_written = (
        int(base_bytes)
        + int(bytes_written)
    )

    logical_total = int(
        bytes_total
    )

    remaining = max(
        logical_total - logical_written,
        0,
    )

    eta_seconds = None

    if speed_bps > 0:
        eta_seconds = (
            remaining
            / speed_bps
        )

    percent = 0.0

    if logical_total > 0:
        percent = min(
            100.0,
            (
                logical_written
                / logical_total
            )
            * 100.0,
        )

    _report_progress(
        progress,
        f"Restoring {filename}: "
        f"{logical_written} / "
        f"{logical_total} bytes",
        type="transfer",
        filename=filename,
        part_number=part_number,
        parts_total=parts_total,
        bytes_written=logical_written,
        bytes_total=logical_total,
        bytes_this_operation=bytes_written,
        speed_bps=speed_bps,
        elapsed_seconds=elapsed,
        eta_seconds=eta_seconds,
        percent=percent,
    )


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
    progress=None,
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

                    transfer_started = (
                        time.monotonic()
                    )

                    base_bytes = (
                        part_start_offset
                    )

                    _report_progress(
                        progress,
                        (
                            f"Restoring "
                            f"{row['filename']} "
                            f"part "
                            f"{part['part_number']} "
                            f"of {len(parts)}..."
                        ),
                        type="transfer_start",
                        filename=row["filename"],
                        part_number=part["part_number"],
                        parts_total=len(parts),
                        bytes_written=base_bytes,
                        bytes_total=row["size_bytes"],
                        percent=(
                            (
                                base_bytes
                                / row["size_bytes"]
                            )
                            * 100.0
                            if row["size_bytes"]
                            else 0.0
                        ),
                    )

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

                            _report_transfer_progress(
                                progress,
                                filename=row[
                                    "filename"
                                ],
                                bytes_written=copied,
                                bytes_total=row[
                                    "size_bytes"
                                ],
                                started_at=(
                                    transfer_started
                                ),
                                part_number=part[
                                    "part_number"
                                ],
                                parts_total=len(
                                    parts
                                ),
                                base_bytes=(
                                    base_bytes
                                ),
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

            _restore_original_modified_time(
                final_destination,
                row["original_modified_at"],
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
            _restore_original_modified_time(
                final_destination,
                row["original_modified_at"],
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
                progress=progress,
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

            _restore_original_modified_time(
                final_destination,
                row["original_modified_at"],
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


def _restore_original_modified_time(
    path,
    original_modified_at,
):
    """
    Restore the cataloged original modification time.

    Creation/birth time is intentionally not changed because
    Linux does not provide a portable way to set it.

    Older catalog rows may have no original_modified_at; those
    restored files are left unchanged.
    """

    if not original_modified_at:
        return False

    value = str(
        original_modified_at
    ).strip()

    if not value:
        return False

    if value.endswith("Z"):
        value = value[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(
            value
        )
    except ValueError as exc:
        raise RuntimeError(
            "Catalog contains an invalid original "
            f"modified timestamp: {value}"
        ) from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(
            tzinfo=timezone.utc
        )

    path = Path(path)

    stat_result = path.stat()

    os.utime(
        path,
        (
            stat_result.st_atime,
            parsed.timestamp(),
        ),
    )

    return True


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
    Restore every cataloged file belonging to one archive job.
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

    return _restore_catalog_files(
        plan["files"],
        destination,
        progress=progress,
        job_id=job_id,
        source_path=plan["job"]["source_path"],
    )


def restore_selected_files(
    file_ids,
    destination,
    progress=None,
):
    """
    Restore an arbitrary selection of catalog file IDs.

    Files may come from different archive jobs. The same resumable
    tape, spanning, checksum, mount, unmount, and eject machinery
    used by archive-job restores performs the actual work.
    """

    normalized_ids = []

    for value in file_ids or []:
        try:
            file_id = int(value)
        except (TypeError, ValueError):
            return {
                "success": False,
                "error": (
                    f"Invalid file ID: {value}"
                ),
            }

        if file_id <= 0:
            return {
                "success": False,
                "error": (
                    f"Invalid file ID: {value}"
                ),
            }

        if file_id not in normalized_ids:
            normalized_ids.append(
                file_id
            )

    if not normalized_ids:
        return {
            "success": False,
            "error": "No files were selected.",
        }

    files = []

    for file_id in normalized_ids:
        row = get_file_by_id(
            file_id
        )

        if row is None:
            return {
                "success": False,
                "error": (
                    f"File ID {file_id} does not exist."
                ),
            }

        files.append(
            row
        )

    return _restore_catalog_files(
        files,
        destination,
        progress=progress,
        job_id=None,
        source_path="Selected catalog files",
    )


def _restore_catalog_files(
    files,
    destination,
    progress=None,
    job_id=None,
    source_path=None,
):
    """
    Restore a supplied collection of cataloged files using the
    currently loaded tape.

    This is the shared restore engine used by archive-job restores
    and arbitrary selected-file restores.

    The command is resumable:
      - already-restored files are SHA256 checked and skipped
      - files on the currently loaded tape are restored together
      - another invocation continues after the next tape is loaded
      - existing mismatched files are never overwritten
      - every newly restored file is SHA256 verified
    """

    files = list(files or [])

    if not files:
        return {
            "success": False,
            "error": (
                "Restore selection contains no cataloged files."
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

        _restore_original_modified_time(
            output,
            row["original_modified_at"],
        )

        completed_ids.add(
            row["id"]
        )

    if not remaining:
        return {
            "success": True,
            "completed": True,
            "already_restored": True,
            "job_id": job_id,
            "source_path": source_path,
            "destination": str(destination),
            "files_total": len(files),
            "files_completed": len(files),
            "files_restored_this_run": 0,
            "files_skipped": len(completed_ids),
            "bytes_restored_this_run": 0,
            "required_tapes": [],
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
    #
    # Crash-recovery finalization for spanned files.
    #
    # A restore may have successfully written and fsynced the final
    # physical tape part, then stopped before the complete logical
    # file was SHA256-verified and atomically renamed into place.
    #
    # If every cataloged part is already present in the local partial,
    # finish that work locally without requiring a tape drive.
    #
    locally_finalized_ids = set()

    for row in list(remaining):
        if not row["is_spanned"]:
            continue

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

        if not partial.exists():
            continue

        parts = get_file_parts(
            row["id"]
        )

        if not parts:
            return {
                "success": False,
                "error": (
                    "Spanned file has no physical parts "
                    f"in the catalog: file ID {row['id']}"
                ),
            }

        try:
            partial_state = (
                _validate_spanned_partial(
                    partial,
                    parts,
                )
            )
        except Exception as exc:
            return {
                "success": False,
                "error": str(exc),
            }

        completed_parts = int(
            partial_state.get(
                "parts_completed",
                0,
            )
        )

        if completed_parts < len(parts):
            continue

        output.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        finalize_result = (
            _restore_spanned_from_mounted_tape(
                row,
                output,
                "",
                None,
            )
        )

        if not finalize_result.get(
            "success"
        ):
            return {
                "success": False,
                "job_id": job_id,
                "source_path": source_path,
                "destination": str(destination),
                "error": finalize_result.get(
                    "error",
                    (
                        "Could not finalize completed "
                        "spanned restore partial."
                    ),
                ),
            }

        if finalize_result.get(
            "completed",
            False,
        ):
            locally_finalized_ids.add(
                row["id"]
            )

    if locally_finalized_ids:
        completed_ids |= (
            locally_finalized_ids
        )

        remaining = [
            row
            for row in remaining
            if row["id"]
            not in locally_finalized_ids
        ]

    #
    # If local crash recovery finished the final outstanding files,
    # there is no reason to discover, wait for, mount, or eject tape.
    #
    if not remaining:
        return {
            "success": True,
            "completed": True,
            "job_id": job_id,
            "source_path": source_path,
            "destination": str(destination),
            "files_total": len(files),
            "files_completed": len(files),
            "files_restored_this_run": len(
                locally_finalized_ids
            ),
            "files_skipped": (
                len(completed_ids)
                - len(locally_finalized_ids)
            ),
            "bytes_restored_this_run": 0,
            "required_tapes": [],
            "auto_ejected": False,
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

    _report_progress(
        progress,
        "Tape drive ready.",
        type="drive_ready",
    )

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

    _report_progress(
        progress,
        "Mounting LTFS read-only...",
        type="mounting",
    )

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

        _report_progress(
            progress,
            (
                f"Mounted "
                f"{loaded_name or loaded_uuid}."
            ),
            type="mounted",
            tape_label=(
                loaded_name or loaded_uuid
            ),
            tape_uuid=loaded_uuid,
        )

        tape_work = _build_archive_tape_work(
            remaining,
            destination,
        )

        loaded_work = tape_work.get(
            loaded_uuid
        )

        if not loaded_work:
            #
            # The mounted cartridge is not part of the remaining
            # restore plan.
            #
            # Build required-tape reporting from tape_work rather
            # than directly from the logical file rows.  Spanned
            # parent rows intentionally have no tape_label or
            # ltfs_uuid; their physical cartridge locations live
            # in file_parts and are represented by tape_work.
            #
            required = []

            for work in tape_work.values():
                label = (
                    work["tape_label"]
                    or work["ltfs_uuid"]
                )

                if (
                    label
                    and label not in required
                ):
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
                    "remaining files in this restore."
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

                transfer_started = (
                    time.monotonic()
                )

                _report_progress(
                    progress,
                    f"Restoring {row['filename']}...",
                    type="transfer_start",
                    filename=row["filename"],
                    part_number=None,
                    parts_total=None,
                    bytes_written=0,
                    bytes_total=row["size_bytes"],
                    percent=0.0,
                )

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

                                _report_transfer_progress(
                                    progress,
                                    filename=row[
                                        "filename"
                                    ],
                                    bytes_written=copied,
                                    bytes_total=row[
                                        "size_bytes"
                                    ],
                                    started_at=(
                                        transfer_started
                                    ),
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

                    _restore_original_modified_time(
                        output,
                        row["original_modified_at"],
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
                        progress=progress,
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
                "source_path": source_path,
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
        _report_progress(
            progress,
            "Unmounting LTFS...",
            type="unmounting",
        )

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
            _report_progress(
                progress,
                "LTFS unmounted cleanly.",
                type="unmounted",
            )

            _report_progress(
                progress,
                (
                    f"Ejecting "
                    f"{loaded_name or 'cartridge'}..."
                ),
                type="ejecting",
            )

            eject_result = eject_tape()

            result["auto_ejected"] = (
                eject_result.get(
                    "success",
                    False,
                )
            )

            if eject_result.get("success"):
                _report_progress(
                    progress,
                    "Cartridge ejected.",
                    type="ejected",
                )

            if not eject_result.get("success"):
                result["eject_warning"] = (
                    eject_result.get(
                        "error",
                        "Tape eject failed.",
                    )
                )

    return result
