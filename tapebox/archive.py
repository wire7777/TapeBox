
import hashlib
import json
import os
from pathlib import Path

from tapebox.database import (
    add_file_part,
    create_archive_job,
    create_spanned_file,
    get_archive_job,
    get_archive_job_files,
    get_file_parts,
    get_file_parts_by_tape,
    get_files_by_tape,
    get_next_file_part_number,
    get_spanned_file_by_job_path,
    get_spanned_file_written_bytes,
    get_tape_by_uuid,
    record_archived_file,
    record_spanned_file_part,
    update_archive_job,
)

from tapebox.tape import (
    discover_drives,
    eject_tape,
    get_ltfs_virtual_attribute,
    get_tape_status,
    mount_ltfs,
    run_command,
    _is_mounted,
    _unmount_ltfs,
)


ARCHIVE_MOUNTPOINT = Path("/mnt/tapebox/ltfs")
COPY_BUFFER_SIZE = 16 * 1024 * 1024

# Leave some breathing room for LTFS metadata/index updates.
TAPE_FREE_RESERVE_BYTES = 4 * 1024 * 1024 * 1024


def _write_tape_metadata(
    mountpoint,
    tape,
    loaded_uuid,
    loaded_name=None,
):
    """
    Write TapeBox cartridge identity metadata atomically.

    The metadata lives at:
        /.tapebox/tape.json

    A temporary file is written and fsynced before rename so an
    interrupted write cannot leave a partially written tape.json.
    """
    mountpoint = Path(mountpoint)

    metadata_dir = (
        mountpoint
        / ".tapebox"
    )

    metadata_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    metadata_path = (
        metadata_dir
        / "tape.json"
    )

    temporary_path = (
        metadata_dir
        / "tape.json.tmp"
    )

    metadata = {
        "schema_version": 1,
        "created_by": "TapeBox",
        "label": tape["label"],
        "ltfs_uuid": loaded_uuid,
        "ltfs_volume_name": (
            loaded_name
            or tape["label"]
        ),
    }

    # Include generation when it exists in the catalog.
    try:
        generation = tape["generation"]
    except (KeyError, IndexError):
        generation = None

    if generation:
        generation_text = str(generation)

        if generation_text.isdigit():
            generation_text = (
                f"LTO-{generation_text}"
            )

        metadata["generation"] = generation_text

    try:
        with temporary_path.open(
            "w",
            encoding="utf-8",
        ) as handle:
            json.dump(
                metadata,
                handle,
                indent=2,
                sort_keys=True,
            )

            handle.write("\n")
            handle.flush()
            os.fsync(
                handle.fileno()
            )

        temporary_path.replace(
            metadata_path
        )

        # Flush the directory entry containing the rename.
        directory_fd = os.open(
            metadata_dir,
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

    finally:
        if temporary_path.exists():
            temporary_path.unlink()

    return metadata_path




def _write_tape_manifest(
    mountpoint,
    tape,
):
    """
    Rebuild /.tapebox/manifest.json from the SQLite catalog for
    this cartridge.

    This intentionally rewrites the complete manifest so older
    cataloged files are automatically backfilled.
    """
    mountpoint = Path(mountpoint)

    metadata_dir = (
        mountpoint
        / ".tapebox"
    )

    metadata_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    manifest_path = (
        metadata_dir
        / "manifest.json"
    )

    temporary_path = (
        metadata_dir
        / "manifest.json.tmp"
    )

    rows = get_files_by_tape(
        tape["id"]
    )

    files = []

    for row in rows:
        files.append(
            {
                "archive_job_id": row["archive_job_id"],
                "relative_path": row["relative_path"],
                "filename": row["filename"],
                "size_bytes": row["size_bytes"],
                "sha256": row["checksum_sha256"],
                "tape_path": row["tape_path"],
                "archived_at": row["archived_at"],
                "verified_at": row["verified_at"],
                "is_spanned": bool(
                    row["is_spanned"]
                ),
            }
        )

    part_rows = get_file_parts_by_tape(
        tape["id"]
    )

    spanned_parts = []

    for row in part_rows:
        spanned_parts.append(
            {
                "archive_job_id": row[
                    "archive_job_id"
                ],
                "relative_path": row[
                    "relative_path"
                ],
                "filename": row[
                    "filename"
                ],
                "file_size_bytes": row[
                    "file_size_bytes"
                ],
                "file_sha256": row[
                    "file_checksum_sha256"
                ],
                "part_number": row[
                    "part_number"
                ],
                "part_size_bytes": row[
                    "size_bytes"
                ],
                "part_sha256": row[
                    "checksum_sha256"
                ],
                "tape_path": row[
                    "tape_path"
                ],
                "archived_at": row[
                    "archived_at"
                ],
                "verified_at": row[
                    "verified_at"
                ],
                "is_spanned": True,
            }
        )

    manifest = {
        "schema_version": 1,
        "created_by": "TapeBox",
        "tape": {
            "label": tape["label"],
            "ltfs_uuid": tape["ltfs_uuid"],
        },
        "file_count": len(files),
        "files": files,
        "spanned_part_count": len(
            spanned_parts
        ),
        "spanned_parts": spanned_parts,
    }

    try:
        with temporary_path.open(
            "w",
            encoding="utf-8",
        ) as handle:
            json.dump(
                manifest,
                handle,
                indent=2,
                sort_keys=True,
            )

            handle.write("\n")
            handle.flush()

            os.fsync(
                handle.fileno()
            )

        temporary_path.replace(
            manifest_path
        )

        directory_fd = os.open(
            metadata_dir,
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

    finally:
        if temporary_path.exists():
            temporary_path.unlink()

    return manifest_path



def refresh_tape_manifest(
    tape,
    mountpoint=ARCHIVE_MOUNTPOINT,
):
    """
    Remount a registered TapeBox cartridge, verify its LTFS UUID,
    rebuild tape.json and manifest.json from SQLite, then unmount.

    This is intended to run after SQLite has been updated so the
    on-tape manifest includes the newest catalog records.
    """
    mountpoint = Path(mountpoint)

    if _is_mounted(mountpoint):
        return {
            "success": False,
            "error": (
                f"Manifest mountpoint is already mounted: "
                f"{mountpoint}"
            ),
        }

    mountpoint.mkdir(
        parents=True,
        exist_ok=True,
    )

    result = None

    # Give the drive a moment to finish releasing after the previous
    # LTFS unmount. mount_ltfs() also contains its own retry logic.
    import time
    time.sleep(1.0)

    mount_result = mount_ltfs(
        "/dev/sg0",
        mountpoint,
        timeout=120,
    )

    if not _is_mounted(mountpoint):
        return {
            "success": False,
            "error": (
                mount_result.get("stderr")
                or mount_result.get("stdout")
                or "Could not remount LTFS for manifest update."
            ),
        }

    try:
        loaded_uuid = get_ltfs_virtual_attribute(
            mountpoint,
            "ltfs.volumeUUID",
        )

        loaded_name = get_ltfs_virtual_attribute(
            mountpoint,
            "ltfs.volumeName",
        )

        if not loaded_uuid:
            raise RuntimeError(
                "Could not read LTFS UUID during manifest refresh."
            )

        expected_uuid = tape["ltfs_uuid"]

        if loaded_uuid != expected_uuid:
            raise RuntimeError(
                "Wrong cartridge loaded during manifest refresh. "
                f"Expected UUID {expected_uuid}, "
                f"found {loaded_uuid}."
            )

        _write_tape_metadata(
            mountpoint,
            tape,
            loaded_uuid,
            loaded_name,
        )

        manifest_path = _write_tape_manifest(
            mountpoint,
            tape,
        )

        run_command(
            ["sync"],
            timeout=60,
        )

        result = {
            "success": True,
            "manifest_path": str(
                manifest_path
            ),
            "tape": tape["label"],
            "ltfs_uuid": loaded_uuid,
        }

    except Exception as exc:
        result = {
            "success": False,
            "error": str(exc),
        }

    finally:
        unmounted, unmount_error = _unmount_ltfs(
            mountpoint
        )

        if not unmounted:
            result = {
                "success": False,
                "error": (
                    "Manifest update finished but LTFS could not "
                    f"be unmounted cleanly: {unmount_error}"
                ),
            }

    return result


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

    mount_result = mount_ltfs(
        sg_device,
        ARCHIVE_MOUNTPOINT,
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

        _write_tape_metadata(
            ARCHIVE_MOUNTPOINT,
            tape,
            tape["ltfs_uuid"],
            tape["label"],
        )

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

    manifest_result = refresh_tape_manifest(
        tape
    )

    return {
        "success": True,
        "file_id": file_id,
        "manifest_updated": manifest_result.get(
            "success",
            False,
        ),
        "manifest_warning": (
            None
            if manifest_result.get("success")
            else manifest_result.get("error")
        ),
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

def archive_folder(source_path):
    """
    Archive one complete directory tree to the loaded LTFS tape.

    This first folder implementation requires the complete folder to fit
    on the currently loaded tape. Multi-tape continuation will be added
    separately.
    """

    import shutil

    source = Path(source_path).expanduser().resolve()

    if not source.exists():
        return {
            "success": False,
            "error": f"Source does not exist: {source}",
        }

    if not source.is_dir():
        return {
            "success": False,
            "error": f"Source is not a directory: {source}",
        }

    files = []

    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            return {
                "success": False,
                "error": (
                    "Symbolic links are not supported yet: "
                    f"{path}"
                ),
            }

        if path.is_file():
            files.append(path)

    if not files:
        return {
            "success": False,
            "error": "Folder contains no regular files.",
        }

    total_bytes = sum(
        path.stat().st_size
        for path in files
    )

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

    status = get_tape_status(nst_device)

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

    if status.get("write_protected"):
        return {
            "success": False,
            "error": "Loaded tape is write protected.",
        }

    ARCHIVE_MOUNTPOINT.mkdir(
        parents=True,
        exist_ok=True,
    )

    if _is_mounted(ARCHIVE_MOUNTPOINT):
        return {
            "success": False,
            "error": (
                "TapeBox LTFS mount point is already mounted: "
                f"{ARCHIVE_MOUNTPOINT}"
            ),
        }

    mount_result = mount_ltfs(
        sg_device,
        ARCHIVE_MOUNTPOINT,
        timeout=120,
    )

    if not _is_mounted(ARCHIVE_MOUNTPOINT):
        return {
            "success": False,
            "error": (
                mount_result.get("stderr")
                or mount_result.get("stdout")
                or "LTFS mount failed."
            ),
        }

    result = None
    catalog_records = []

    try:
        loaded_uuid = get_ltfs_virtual_attribute(
            ARCHIVE_MOUNTPOINT,
            "ltfs.volumeUUID",
        )

        loaded_name = get_ltfs_virtual_attribute(
            ARCHIVE_MOUNTPOINT,
            "ltfs.volumeName",
        )

        if not loaded_uuid:
            raise RuntimeError(
                "Could not read LTFS volume UUID."
            )

        tape = get_tape_by_uuid(
            loaded_uuid
        )

        if tape is None:
            raise RuntimeError(
                "Loaded tape is not registered in TapeBox. "
                f"Volume={loaded_name or '-'} "
                f"UUID={loaded_uuid}"
            )

        usage = shutil.disk_usage(
            ARCHIVE_MOUNTPOINT
        )

        if total_bytes > usage.free:
            raise RuntimeError(
                "Folder does not fit on the loaded tape. "
                f"Need {total_bytes} bytes, "
                f"but only {usage.free} bytes are free. "
                "Multi-tape folder continuation is not implemented yet."
            )

        _write_tape_metadata(
            ARCHIVE_MOUNTPOINT,
            tape,
            tape["ltfs_uuid"],
            tape["label"],
        )

        archive_root = (
            ARCHIVE_MOUNTPOINT
            / "archive"
        )

        archive_root.mkdir(
            parents=True,
            exist_ok=True,
        )

        destination_root = (
            archive_root
            / source.name
        )

        if destination_root.exists():
            raise FileExistsError(
                "Destination folder already exists on tape: "
                f"/archive/{source.name}"
            )

        destination_root.mkdir(
            parents=True,
            exist_ok=False,
        )

        copied_bytes = 0

        for index, source_file in enumerate(
            files,
            start=1,
        ):
            relative = source_file.relative_to(
                source
            )

            destination = (
                destination_root
                / relative
            )

            destination.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            temp_destination = (
                destination.parent
                / (
                    ".tapebox-partial-"
                    + destination.name
                )
            )

            if temp_destination.exists():
                temp_destination.unlink()

            digest = hashlib.sha256()
            source_size = source_file.stat().st_size

            with open(source_file, "rb") as src:
                with open(temp_destination, "wb") as dst:
                    while True:
                        chunk = src.read(
                            COPY_BUFFER_SIZE
                        )

                        if not chunk:
                            break

                        dst.write(chunk)
                        digest.update(chunk)
                        copied_bytes += len(chunk)

                    dst.flush()
                    os.fsync(
                        dst.fileno()
                    )

            copied_size = (
                temp_destination.stat().st_size
            )

            if copied_size != source_size:
                raise RuntimeError(
                    "Copied size mismatch for "
                    f"{source_file}: "
                    f"source={source_size}, "
                    f"tape={copied_size}"
                )

            temp_destination.rename(
                destination
            )

            tape_path = (
                "/archive/"
                + source.name
                + "/"
                + relative.as_posix()
            )

            catalog_records.append(
                {
                    "original_path": str(
                        source_file
                    ),
                    "relative_path": (
                        source.name
                        + "/"
                        + relative.as_posix()
                    ),
                    "filename": source_file.name,
                    "size_bytes": source_size,
                    "sha256": digest.hexdigest(),
                    "tape_id": tape["id"],
                    "tape_path": tape_path,
                }
            )

            print(
                f"[{index}/{len(files)}] "
                f"{relative} "
                f"({source_size} bytes)"
            )

        sync_result = run_command(
            ["sync"],
            timeout=120,
        )

        if sync_result.get("returncode") not in (
            None,
            0,
        ):
            raise RuntimeError(
                sync_result.get("stderr")
                or "sync failed"
            )

        result = {
            "success": True,
            "folder": source.name,
            "source_path": str(source),
            "file_count": len(files),
            "size_bytes": total_bytes,
            "bytes_written": copied_bytes,
            "tape_label": tape["label"],
            "tape_uuid": loaded_uuid,
            "tape_id": tape["id"],
            "tape_path": (
                f"/archive/{source.name}"
            ),
        }

    except Exception as exc:
        result = {
            "success": False,
            "error": str(exc),
        }

    finally:
        unmounted, unmount_error = _unmount_ltfs(
            ARCHIVE_MOUNTPOINT
        )

        if not unmounted:
            result = {
                "success": False,
                "error": (
                    "LTFS unmount failed after folder archive: "
                    f"{unmount_error}"
                ),
            }

    if not result.get("success"):
        return result

    database_ids = []

    try:
        for record in catalog_records:
            file_id = record_archived_file(
                record["original_path"],
                record["relative_path"],
                record["filename"],
                record["size_bytes"],
                record["sha256"],
                record["tape_id"],
                record["tape_path"],
            )

            database_ids.append(
                file_id
            )

    except Exception as exc:
        return {
            "success": False,
            "tape_write_succeeded": True,
            "error": (
                "Folder was written to tape, but catalog "
                f"update failed: {exc}"
            ),
        }

    result["database_ids"] = database_ids

    manifest_result = refresh_tape_manifest(
        tape
    )

    result["manifest_updated"] = (
        manifest_result.get(
            "success",
            False,
        )
    )

    if not manifest_result.get("success"):
        result["manifest_warning"] = (
            manifest_result.get("error")
        )

    return result


def archive_path(source_path):
    """
    Archive either one regular file or one directory tree.
    """

    source = Path(
        source_path
    ).expanduser()

    if source.is_dir():
        return archive_folder_job(
            source
        )

    return archive_file(
        source
    )


def _collect_archive_folder_files(source):
    """
    Return sorted regular files for a folder archive.
    Symlinks are deliberately rejected for now.
    """

    files = []

    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(
                "Symbolic links are not supported yet: "
                f"{path}"
            )

        if path.is_file():
            files.append(path)

    if not files:
        raise RuntimeError(
            "Folder contains no regular files."
        )

    return files


def archive_folder_job(source_path, job_id=None):
    """
    Archive a folder using a resumable archive job.

    Whole normal files stay on one tape.

    If the next file will not fit on the currently loaded
    cartridge, the job is placed into waiting_for_tape state
    and can later be continued with resume_archive_job().
    """

    import shutil

    source = Path(
        source_path
    ).expanduser().resolve()

    if not source.exists():
        return {
            "success": False,
            "error": f"Source does not exist: {source}",
        }

    if not source.is_dir():
        return {
            "success": False,
            "error": f"Source is not a directory: {source}",
        }

    try:
        files = _collect_archive_folder_files(
            source
        )
    except Exception as exc:
        return {
            "success": False,
            "error": str(exc),
        }

    total_bytes = sum(
        path.stat().st_size
        for path in files
    )

    if job_id is None:
        job_id = create_archive_job(
            str(source),
            len(files),
            total_bytes,
        )

        job = get_archive_job(
            job_id
        )

        previous_job_status = "new"

    else:
        job = get_archive_job(
            job_id
        )

        if job is None:
            return {
                "success": False,
                "error": (
                    f"Archive job {job_id} does not exist."
                ),
            }

        if job["status"] == "completed":
            return {
                "success": True,
                "completed": True,
                "job_id": job_id,
                "folder": source.name,
                "source_path": str(source),
                "file_count": job["total_files"],
                "size_bytes": job["total_bytes"],
                "bytes_written": job["bytes_written"],
                "message": "Archive job is already complete.",
            }

        previous_job_status = job["status"]

        stored_source = Path(
            job["source_path"]
        ).expanduser().resolve()

        if stored_source != source:
            return {
                "success": False,
                "error": (
                    "Archive job source mismatch. "
                    f"Expected {stored_source}, got {source}."
                ),
            }

        update_archive_job(
            job_id,
            status="running",
            error="",
        )

    completed_rows = get_archive_job_files(
        job_id
    )

    # A normal file is complete when its catalog row exists.
    #
    # A spanned file is complete only when the sum of its safely
    # cataloged physical parts equals the original file size.
    #
    # bytes_written must reflect bytes actually committed to tape,
    # not the full logical size of an incomplete spanned parent.
    completed_paths = set()
    previous_bytes = 0

    for row in completed_rows:
        if row["is_spanned"]:
            written_bytes = get_spanned_file_written_bytes(
                row["id"]
            )

            previous_bytes += written_bytes

            if written_bytes >= row["size_bytes"]:
                completed_paths.add(
                    row["relative_path"]
                )

        else:
            previous_bytes += row["size_bytes"]

            completed_paths.add(
                row["relative_path"]
            )

    pending_files = []

    for source_file in files:
        relative = source_file.relative_to(
            source
        )

        catalog_relative = (
            source.name
            + "/"
            + relative.as_posix()
        )

        if catalog_relative not in completed_paths:
            pending_files.append(
                source_file
            )

    if not pending_files:
        update_archive_job(
            job_id,
            status="completed",
            bytes_written=previous_bytes,
            completed=True,
            error="",
        )

        return {
            "success": True,
            "completed": True,
            "job_id": job_id,
            "folder": source.name,
            "source_path": str(source),
            "file_count": len(files),
            "files_completed": len(files),
            "size_bytes": total_bytes,
            "bytes_written": previous_bytes,
        }

    drives = discover_drives()

    if not drives:
        update_archive_job(
            job_id,
            status="waiting_for_tape",
            bytes_written=previous_bytes,
            error="No tape drive detected.",
        )

        return {
            "success": False,
            "job_id": job_id,
            "error": "No tape drive detected.",
        }

    if len(drives) > 1:
        return {
            "success": False,
            "job_id": job_id,
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
            "job_id": job_id,
            "error": (
                "Tape drive device mapping is incomplete."
            ),
        }

    status = get_tape_status(
        nst_device
    )

    if not status.get("available"):
        update_archive_job(
            job_id,
            status="waiting_for_tape",
            bytes_written=previous_bytes,
            error=(
                status.get("error")
                or "Tape cartridge is not available."
            ),
        )

        return {
            "success": False,
            "job_id": job_id,
            "needs_next_tape": True,
            "error": (
                status.get("error")
                or "Tape cartridge is not available."
            ),
        }

    if not status.get("online"):
        return {
            "success": False,
            "job_id": job_id,
            "needs_next_tape": True,
            "error": "Tape cartridge is not online.",
        }

    if status.get("write_protected"):
        return {
            "success": False,
            "job_id": job_id,
            "error": "Loaded tape is write protected.",
        }

    ARCHIVE_MOUNTPOINT.mkdir(
        parents=True,
        exist_ok=True,
    )

    if _is_mounted(
        ARCHIVE_MOUNTPOINT
    ):
        return {
            "success": False,
            "job_id": job_id,
            "error": (
                "TapeBox LTFS mount point is already mounted: "
                f"{ARCHIVE_MOUNTPOINT}"
            ),
        }

    mount_result = mount_ltfs(
        sg_device,
        ARCHIVE_MOUNTPOINT,
        timeout=120,
    )

    if not _is_mounted(
        ARCHIVE_MOUNTPOINT
    ):
        return {
            "success": False,
            "job_id": job_id,
            "error": (
                mount_result.get("stderr")
                or mount_result.get("stdout")
                or "LTFS mount failed."
            ),
        }

    result = None
    catalog_records = []
    spanned_part_records = []
    copied_this_tape = 0
    needs_next_tape = False
    next_file = None
    tape = None
    loaded_uuid = None

    try:
        loaded_uuid = get_ltfs_virtual_attribute(
            ARCHIVE_MOUNTPOINT,
            "ltfs.volumeUUID",
        )

        loaded_name = get_ltfs_virtual_attribute(
            ARCHIVE_MOUNTPOINT,
            "ltfs.volumeName",
        )

        if not loaded_uuid:
            raise RuntimeError(
                "Could not read LTFS volume UUID."
            )

        tape = get_tape_by_uuid(
            loaded_uuid
        )

        if tape is None:
            raise RuntimeError(
                "Loaded tape is not registered in TapeBox. "
                f"Volume={loaded_name or '-'} "
                f"UUID={loaded_uuid}"
            )

        # Never reuse a cartridge that already contains files
        # from this archive job. The catalog is the authority here,
        # not the current job status.
        used_tape_ids = set()

        for row in completed_rows:
            if row["is_spanned"]:
                for part in get_file_parts(
                    row["id"]
                ):
                    if part["tape_id"] is not None:
                        used_tape_ids.add(
                            part["tape_id"]
                        )

            elif row["tape_id"] is not None:
                used_tape_ids.add(
                    row["tape_id"]
                )

        if tape["id"] in used_tape_ids:
            result = {
                "success": False,
                "needs_next_tape": True,
                "same_tape": True,
                "error": (
                    "This archive job requires a different "
                    "cartridge. The loaded tape "
                    f"{tape['label']} has already been used by "
                    f"job {job_id}."
                ),
            }

            raise RuntimeError(
                "__TAPEBOX_NEED_DIFFERENT_TAPE__"
            )

        _write_tape_metadata(
            ARCHIVE_MOUNTPOINT,
            tape,
            tape["ltfs_uuid"],
            tape["label"],
        )

        archive_root = (
            ARCHIVE_MOUNTPOINT
            / "archive"
        )

        archive_root.mkdir(
            parents=True,
            exist_ok=True,
        )

        destination_root = (
            archive_root
            / source.name
        )

        destination_root.mkdir(
            parents=True,
            exist_ok=True,
        )

        for source_file in pending_files:
            relative = source_file.relative_to(
                source
            )

            source_size = source_file.stat().st_size

            usage = shutil.disk_usage(
                ARCHIVE_MOUNTPOINT
            )

            test_usable = os.environ.get(
                "TAPEBOX_TEST_USABLE_BYTES"
            )

            if test_usable is not None:
                try:
                    test_capacity = int(
                        test_usable
                    )
                except ValueError:
                    raise RuntimeError(
                        "TAPEBOX_TEST_USABLE_BYTES "
                        "must be an integer."
                    )

                if test_capacity <= 0:
                    raise RuntimeError(
                        "TAPEBOX_TEST_USABLE_BYTES "
                        "must be greater than zero."
                    )

                # The test value represents already-usable tape
                # capacity. Do not subtract the real 4 GiB reserve
                # from a tiny simulated cartridge.
                maximum_single_file = (
                    test_capacity
                )

                usable_free = max(
                    0,
                    test_capacity
                    - copied_this_tape,
                )

            else:
                maximum_single_file = max(
                    0,
                    usage.total
                    - TAPE_FREE_RESERVE_BYTES,
                )

                usable_free = max(
                    0,
                    usage.free
                    - TAPE_FREE_RESERVE_BYTES,
                )

            catalog_relative = (
                source.name
                + "/"
                + relative.as_posix()
            )

            existing_spanned = (
                get_spanned_file_by_job_path(
                    job_id,
                    catalog_relative,
                )
            )

            is_spanned = (
                existing_spanned is not None
                or source_size > maximum_single_file
            )

            if is_spanned:
                if usable_free <= 0:
                    needs_next_tape = True
                    next_file = relative.as_posix()
                    break

                if existing_spanned is not None:
                    spanned_file_id = (
                        existing_spanned["id"]
                    )

                    source_offset = (
                        get_spanned_file_written_bytes(
                            spanned_file_id
                        )
                    )

                    part_number = (
                        get_next_file_part_number(
                            spanned_file_id
                        )
                    )

                    whole_file_sha256 = (
                        existing_spanned[
                            "checksum_sha256"
                        ]
                    )

                    if not whole_file_sha256:
                        raise RuntimeError(
                            "Existing spanned file is missing "
                            "its whole-file SHA256."
                        )

                else:
                    spanned_file_id = None
                    source_offset = 0
                    part_number = 1

                    print(
                        f"Hashing oversized file before "
                        f"spanning: {relative}"
                    )

                    whole_file_sha256 = (
                        _sha256_file(
                            source_file
                        )
                    )

                if source_offset >= source_size:
                    raise RuntimeError(
                        "Spanned-file catalog already contains "
                        "at least the complete source size, but "
                        "the file was still considered pending: "
                        f"{relative}"
                    )

                remaining_bytes = (
                    source_size
                    - source_offset
                )

                part_size = min(
                    remaining_bytes,
                    usable_free,
                )

                base_destination = (
                    destination_root
                    / relative
                )

                destination = (
                    base_destination.parent
                    / (
                        base_destination.name
                        + ".tapebox-part"
                        + f"{part_number:04d}"
                    )
                )

                destination.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                if destination.exists():
                    raise RuntimeError(
                        "Spanned destination part already "
                        "exists on tape but is not safely "
                        "cataloged for this resume operation: "
                        f"{destination}. "
                        "Tape/catalog reconciliation is required."
                    )

                temp_destination = (
                    destination.parent
                    / (
                        ".tapebox-partial-"
                        + destination.name
                    )
                )

                if temp_destination.exists():
                    temp_destination.unlink()

                part_result = _copy_file_part(
                    source_file,
                    temp_destination,
                    source_offset,
                    part_size,
                )

                temp_destination.rename(
                    destination
                )

                tape_path = (
                    "/"
                    + destination.relative_to(
                        ARCHIVE_MOUNTPOINT
                    ).as_posix()
                )

                spanned_part_records.append(
                    {
                        "original_path": str(
                            source_file
                        ),
                        "relative_path": (
                            catalog_relative
                        ),
                        "filename": (
                            source_file.name
                        ),
                        "file_size_bytes": (
                            source_size
                        ),
                        "file_sha256": (
                            whole_file_sha256
                        ),
                        "part_number": (
                            part_number
                        ),
                        "tape_id": tape["id"],
                        "tape_path": tape_path,
                        "part_size_bytes": (
                            part_result[
                                "size_bytes"
                            ]
                        ),
                        "part_sha256": (
                            part_result[
                                "sha256"
                            ]
                        ),
                    }
                )

                copied_this_tape += (
                    part_result[
                        "size_bytes"
                    ]
                )

                written_total = (
                    source_offset
                    + part_result[
                        "size_bytes"
                    ]
                )

                print(
                    f"[SPAN] {relative} "
                    f"part {part_number} "
                    f"({part_result['size_bytes']} bytes) "
                    f"offset {source_offset} "
                    f"-> {tape['label']}"
                )

                if written_total < source_size:
                    needs_next_tape = True
                    next_file = (
                        relative.as_posix()
                    )
                    break

                # Final part of this spanned file fits on the
                # current cartridge. Continue with the next file.
                continue

            if source_size > usable_free:
                needs_next_tape = True
                next_file = relative.as_posix()
                break

            destination = (
                destination_root
                / relative
            )

            destination.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            if destination.exists():
                raise RuntimeError(
                    "Destination already exists on tape but "
                    "is not recorded as completed for this job: "
                    f"/archive/{source.name}/"
                    f"{relative.as_posix()}. "
                    "Tape/catalog reconciliation is required."
                )

            temp_destination = (
                destination.parent
                / (
                    ".tapebox-partial-"
                    + destination.name
                )
            )

            if temp_destination.exists():
                temp_destination.unlink()

            digest = hashlib.sha256()

            with open(
                source_file,
                "rb",
            ) as src:
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

                        dst.write(
                            chunk
                        )

                        digest.update(
                            chunk
                        )

                    dst.flush()
                    os.fsync(
                        dst.fileno()
                    )

            copied_size = (
                temp_destination.stat().st_size
            )

            if copied_size != source_size:
                raise RuntimeError(
                    "Copied size mismatch for "
                    f"{source_file}: "
                    f"source={source_size}, "
                    f"tape={copied_size}"
                )

            temp_destination.rename(
                destination
            )

            tape_path = (
                "/archive/"
                + source.name
                + "/"
                + relative.as_posix()
            )

            catalog_records.append(
                {
                    "original_path": str(
                        source_file
                    ),
                    "relative_path": (
                        source.name
                        + "/"
                        + relative.as_posix()
                    ),
                    "filename": source_file.name,
                    "size_bytes": source_size,
                    "sha256": digest.hexdigest(),
                    "tape_id": tape["id"],
                    "tape_path": tape_path,
                }
            )

            copied_this_tape += (
                source_size
            )

            print(
                f"[{len(completed_rows) + len(catalog_records)}"
                f"/{len(files)}] "
                f"{relative} "
                f"({source_size} bytes) "
                f"-> {tape['label']}"
            )

        if catalog_records or spanned_part_records:
            sync_result = run_command(
                ["sync"],
                timeout=120,
            )

            if sync_result.get(
                "returncode"
            ) not in (
                None,
                0,
            ):
                raise RuntimeError(
                    sync_result.get("stderr")
                    or "sync failed"
                )

        result = {
            "success": True,
        }

    except Exception as exc:
        if str(exc) != "__TAPEBOX_NEED_DIFFERENT_TAPE__":
            result = {
                "success": False,
                "error": str(exc),
            }

    finally:
        unmounted, unmount_error = _unmount_ltfs(
            ARCHIVE_MOUNTPOINT
        )

        if not unmounted:
            result = {
                "success": False,
                "error": (
                    "LTFS unmount failed after archive job: "
                    f"{unmount_error}"
                ),
            }

    if not result.get("success"):
        # If the job was already waiting for another cartridge,
        # a transient failure such as EBUSY must not destroy that
        # state. Otherwise a retry could incorrectly reuse the
        # cartridge that was just filled.
        if (
            result.get("needs_next_tape")
            or previous_job_status == "waiting_for_tape"
        ):
            failure_status = "waiting_for_tape"
        else:
            failure_status = "error"

        update_archive_job(
            job_id,
            status=failure_status,
            bytes_written=previous_bytes,
            error=result.get(
                "error",
                "Unknown archive error",
            ),
        )

        result["job_id"] = job_id

        return result

    database_ids = []

    try:
        for record in catalog_records:
            file_id = record_archived_file(
                record["original_path"],
                record["relative_path"],
                record["filename"],
                record["size_bytes"],
                record["sha256"],
                record["tape_id"],
                record["tape_path"],
                archive_job_id=job_id,
            )

            database_ids.append(
                file_id
            )

        for record in spanned_part_records:
            file_id = record_spanned_file_part(
                original_path=record[
                    "original_path"
                ],
                relative_path=record[
                    "relative_path"
                ],
                filename=record[
                    "filename"
                ],
                file_size_bytes=record[
                    "file_size_bytes"
                ],
                file_sha256=record[
                    "file_sha256"
                ],
                archive_job_id=job_id,
                part_number=record[
                    "part_number"
                ],
                tape_id=record[
                    "tape_id"
                ],
                tape_path=record[
                    "tape_path"
                ],
                part_size_bytes=record[
                    "part_size_bytes"
                ],
                part_sha256=record[
                    "part_sha256"
                ],
            )

            if file_id not in database_ids:
                database_ids.append(
                    file_id
                )

    except Exception as exc:
        update_archive_job(
            job_id,
            status="error",
            bytes_written=previous_bytes,
            error=(
                "Tape write succeeded but catalog update failed: "
                f"{exc}"
            ),
        )

        return {
            "success": False,
            "job_id": job_id,
            "tape_write_succeeded": True,
            "error": (
                "Files were written to tape, but catalog "
                f"update failed: {exc}"
            ),
        }

    #
    # SQLite is now authoritative for everything successfully written
    # to this cartridge. Rebuild the on-tape recovery manifest only
    # after those catalog records exist.
    #
    # Ordering:
    #
    #   tape write
    #   -> sync
    #   -> clean LTFS unmount
    #   -> SQLite catalog commit
    #   -> remount and refresh manifest
    #
    if catalog_records or spanned_part_records:
        manifest_result = refresh_tape_manifest(
            tape
        )
    else:
        manifest_result = {
            "success": True,
            "skipped": True,
        }

    completed_rows = get_archive_job_files(
        job_id
    )

    completed_bytes = 0
    completed_count = 0

    for row in completed_rows:
        if row["is_spanned"]:
            written_bytes = (
                get_spanned_file_written_bytes(
                    row["id"]
                )
            )

            completed_bytes += (
                written_bytes
            )

            if written_bytes >= row["size_bytes"]:
                completed_count += 1

        else:
            completed_bytes += (
                row["size_bytes"]
            )

            completed_count += 1

    all_complete = (
        completed_count
        == len(files)
    )

    if all_complete:
        update_archive_job(
            job_id,
            status="completed",
            bytes_written=completed_bytes,
            completed=True,
            error="",
        )

    else:
        update_archive_job(
            job_id,
            status="waiting_for_tape",
            bytes_written=completed_bytes,
            error="",
        )

    #
    # Safe auto-eject point:
    #   tape write
    #   -> clean LTFS unmount
    #   -> SQLite commit
    #   -> manifest refresh
    #   -> eject
    #
    eject_result = None

    if manifest_result.get("success"):
        eject_result = eject_tape()

    return {
        "success": True,
        "completed": all_complete,
        "needs_next_tape": (
            not all_complete
        ),
        "auto_ejected": (
            bool(
                eject_result
                and eject_result.get("success")
            )
        ),
        "eject_warning": (
            None
            if (
                eject_result is None
                or eject_result.get("success")
            )
            else eject_result.get("error")
        ),
        "job_id": job_id,
        "manifest_updated": manifest_result.get(
            "success",
            False,
        ),
        "manifest_warning": (
            None
            if manifest_result.get("success")
            else manifest_result.get("error")
        ),
        "folder": source.name,
        "source_path": str(source),
        "file_count": len(files),
        "files_completed": completed_count,
        "files_this_tape": len(
            catalog_records
        ),
        "size_bytes": total_bytes,
        "bytes_written": completed_bytes,
        "bytes_this_tape": copied_this_tape,
        "tape_label": (
            tape["label"]
            if tape
            else None
        ),
        "tape_uuid": loaded_uuid,
        "next_file": next_file,
        "database_ids": database_ids,
    }


def resume_archive_job(job_id):
    """
    Resume an existing folder archive job.
    """

    job = get_archive_job(
        job_id
    )

    if job is None:
        return {
            "success": False,
            "error": (
                f"Archive job {job_id} does not exist."
            ),
        }

    return archive_folder_job(
        job["source_path"],
        job_id=job_id,
    )


def _copy_file_part(
    source_path,
    destination_path,
    source_offset,
    part_size,
):
    """
    Copy exactly part_size bytes from source_path beginning at
    source_offset.

    Returns:
        {
            "size_bytes": ...,
            "sha256": ...
        }

    This is the low-level writer used for true oversized-file spanning.
    """

    source_path = Path(source_path)
    destination_path = Path(destination_path)

    if source_offset < 0:
        raise ValueError(
            "source_offset cannot be negative"
        )

    if part_size <= 0:
        raise ValueError(
            "part_size must be greater than zero"
        )

    source_size = source_path.stat().st_size

    if source_offset >= source_size:
        raise RuntimeError(
            "Source offset is beyond the end of the file."
        )

    available = (
        source_size
        - source_offset
    )

    bytes_to_copy = min(
        part_size,
        available,
    )

    digest = hashlib.sha256()

    copied = 0

    destination_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with open(
        source_path,
        "rb",
    ) as src:

        src.seek(
            source_offset
        )

        with open(
            destination_path,
            "xb",
        ) as dst:

            while copied < bytes_to_copy:

                remaining = (
                    bytes_to_copy
                    - copied
                )

                chunk = src.read(
                    min(
                        COPY_BUFFER_SIZE,
                        remaining,
                    )
                )

                if not chunk:
                    raise RuntimeError(
                        "Unexpected end of source file "
                        "while writing spanned part."
                    )

                dst.write(
                    chunk
                )

                digest.update(
                    chunk
                )

                copied += len(
                    chunk
                )

            dst.flush()

            os.fsync(
                dst.fileno()
            )

    actual_size = (
        destination_path.stat().st_size
    )

    if actual_size != copied:
        raise RuntimeError(
            "Spanned part size mismatch after write. "
            f"Expected {copied}, found {actual_size}."
        )

    return {
        "size_bytes": copied,
        "sha256": digest.hexdigest(),
    }


def _sha256_file(path):
    """
    Calculate the SHA256 of one complete source file.
    """
    digest = hashlib.sha256()

    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(
                COPY_BUFFER_SIZE
            )

            if not chunk:
                break

            digest.update(chunk)

    return digest.hexdigest()
