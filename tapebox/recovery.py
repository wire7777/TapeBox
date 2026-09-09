import json
import re
from pathlib import Path

from tapebox.database import (
    import_tape_manifest_records,
)

from tapebox.tape import (
    get_ltfs_virtual_attribute,
    mount_ltfs,
    _is_mounted,
    _unmount_ltfs,
)


IMPORT_MOUNTPOINT = Path(
    "/mnt/tapebox/ltfs-import"
)

DEFAULT_SG_DEVICE = "/dev/sg0"


def _generation_number(value):
    """
    Convert values such as "LTO-6" or 6 into integer 6.
    """

    if value is None:
        return None

    text = str(value).strip()

    if text.isdigit():
        return int(text)

    match = re.search(
        r"LTO-(\d+)",
        text,
        re.IGNORECASE,
    )

    if not match:
        return None

    return int(
        match.group(1)
    )


def import_loaded_tape(
    sg_device=DEFAULT_SG_DEVICE,
):
    """
    Recover TapeBox catalog records from the currently loaded tape.

    The cartridge is mounted read-only. Nothing on tape is modified.
    SQLite is updated only after metadata validation and clean unmount.
    """

    mountpoint = IMPORT_MOUNTPOINT

    mountpoint.mkdir(
        parents=True,
        exist_ok=True,
    )

    if _is_mounted(mountpoint):
        return {
            "success": False,
            "error": (
                f"Import mountpoint is already mounted: "
                f"{mountpoint}"
            ),
        }

    mount_result = mount_ltfs(
        sg_device,
        mountpoint,
        read_only=True,
        retries=5,
        retry_delay=1.5,
        timeout=120,
    )

    if not _is_mounted(mountpoint):
        return {
            "success": False,
            "error": (
                mount_result.get("stderr")
                or mount_result.get("stdout")
                or "Could not mount loaded tape read-only."
            ),
        }

    tape_metadata = None
    manifest = None
    loaded_uuid = None
    loaded_name = None
    read_error = None

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
                "Could not read LTFS volume UUID."
            )

        metadata_path = (
            mountpoint
            / ".tapebox"
            / "tape.json"
        )

        manifest_path = (
            mountpoint
            / ".tapebox"
            / "manifest.json"
        )

        if not metadata_path.is_file():
            raise RuntimeError(
                "This LTFS cartridge does not contain "
                "/.tapebox/tape.json."
            )

        if not manifest_path.is_file():
            raise RuntimeError(
                "This LTFS cartridge does not contain "
                "/.tapebox/manifest.json."
            )

        with metadata_path.open(
            "r",
            encoding="utf-8",
        ) as handle:
            tape_metadata = json.load(
                handle
            )

        with manifest_path.open(
            "r",
            encoding="utf-8",
        ) as handle:
            manifest = json.load(
                handle
            )

        if not isinstance(
            tape_metadata,
            dict,
        ):
            raise RuntimeError(
                "Invalid tape.json structure."
            )

        if not isinstance(
            manifest,
            dict,
        ):
            raise RuntimeError(
                "Invalid manifest.json structure."
            )

        if tape_metadata.get(
            "schema_version"
        ) != 1:
            raise RuntimeError(
                "Unsupported tape.json schema version."
            )

        if manifest.get(
            "schema_version"
        ) != 1:
            raise RuntimeError(
                "Unsupported manifest.json schema version."
            )

        metadata_uuid = tape_metadata.get(
            "ltfs_uuid"
        )

        if metadata_uuid != loaded_uuid:
            raise RuntimeError(
                "Tape identity mismatch: loaded LTFS UUID "
                f"is {loaded_uuid}, but tape.json says "
                f"{metadata_uuid}."
            )

        manifest_tape = manifest.get(
            "tape"
        )

        if not isinstance(
            manifest_tape,
            dict,
        ):
            raise RuntimeError(
                "manifest.json is missing tape identity."
            )

        manifest_uuid = manifest_tape.get(
            "ltfs_uuid"
        )

        if manifest_uuid != loaded_uuid:
            raise RuntimeError(
                "Tape identity mismatch: manifest UUID "
                f"is {manifest_uuid}, but loaded LTFS UUID "
                f"is {loaded_uuid}."
            )

        metadata_label = str(
            tape_metadata.get(
                "label",
                "",
            )
        ).strip().upper()

        manifest_label = str(
            manifest_tape.get(
                "label",
                "",
            )
        ).strip().upper()

        if not metadata_label:
            raise RuntimeError(
                "tape.json does not contain a TapeBox label."
            )

        if manifest_label != metadata_label:
            raise RuntimeError(
                "Tape label mismatch between tape.json and "
                "manifest.json."
            )

        if (
            loaded_name
            and tape_metadata.get("ltfs_volume_name")
            and str(
                tape_metadata["ltfs_volume_name"]
            ).strip()
            != str(loaded_name).strip()
        ):
            raise RuntimeError(
                "LTFS volume name does not match tape.json."
            )

        files = manifest.get(
            "files"
        )

        if not isinstance(
            files,
            list,
        ):
            raise RuntimeError(
                "manifest.json files field is not a list."
            )

        declared_count = manifest.get(
            "file_count"
        )

        if (
            declared_count is not None
            and int(declared_count) != len(files)
        ):
            raise RuntimeError(
                "manifest.json file_count does not match "
                "the files list."
            )

        required_fields = (
            "relative_path",
            "filename",
            "size_bytes",
            "tape_path",
        )

        for index, entry in enumerate(
            files,
            start=1,
        ):
            if not isinstance(
                entry,
                dict,
            ):
                raise RuntimeError(
                    f"Manifest file entry {index} is invalid."
                )

            missing = [
                field
                for field in required_fields
                if field not in entry
            ]

            if missing:
                raise RuntimeError(
                    f"Manifest file entry {index} is missing: "
                    + ", ".join(missing)
                )

            if not str(
                entry["tape_path"]
            ).startswith("/archive/"):
                raise RuntimeError(
                    f"Unsafe tape path in manifest entry "
                    f"{index}: {entry['tape_path']}"
                )

        #
        # Spanned parts were added after the original manifest
        # format. Missing spanned_parts means an older TapeBox
        # manifest with no oversized-file parts.
        #
        spanned_parts = manifest.get(
            "spanned_parts",
            [],
        )

        if not isinstance(
            spanned_parts,
            list,
        ):
            raise RuntimeError(
                "manifest.json spanned_parts field "
                "is not a list."
            )

        declared_part_count = manifest.get(
            "spanned_part_count"
        )

        if (
            declared_part_count is not None
            and int(declared_part_count)
            != len(spanned_parts)
        ):
            raise RuntimeError(
                "manifest.json spanned_part_count does "
                "not match the spanned_parts list."
            )

        required_part_fields = (
            "relative_path",
            "filename",
            "file_size_bytes",
            "file_sha256",
            "part_number",
            "part_size_bytes",
            "part_sha256",
            "tape_path",
        )

        for index, entry in enumerate(
            spanned_parts,
            start=1,
        ):
            if not isinstance(
                entry,
                dict,
            ):
                raise RuntimeError(
                    f"Manifest spanned part entry "
                    f"{index} is invalid."
                )

            missing = [
                field
                for field in required_part_fields
                if field not in entry
            ]

            if missing:
                raise RuntimeError(
                    f"Manifest spanned part entry "
                    f"{index} is missing: "
                    + ", ".join(missing)
                )

            if not str(
                entry["tape_path"]
            ).startswith("/archive/"):
                raise RuntimeError(
                    f"Unsafe tape path in spanned part "
                    f"{index}: {entry['tape_path']}"
                )

            if int(
                entry["file_size_bytes"]
            ) <= 0:
                raise RuntimeError(
                    f"Invalid whole-file size in spanned "
                    f"part {index}."
                )

            if int(
                entry["part_size_bytes"]
            ) <= 0:
                raise RuntimeError(
                    f"Invalid part size in spanned "
                    f"part {index}."
                )

            if int(
                entry["part_number"]
            ) <= 0:
                raise RuntimeError(
                    f"Invalid part number in spanned "
                    f"part {index}."
                )

            if int(
                entry["part_size_bytes"]
            ) > int(
                entry["file_size_bytes"]
            ):
                raise RuntimeError(
                    f"Spanned part {index} is larger "
                    "than its parent file."
                )

            if not str(
                entry["file_sha256"]
            ).strip():
                raise RuntimeError(
                    f"Spanned part {index} is missing "
                    "the whole-file SHA256."
                )

            if not str(
                entry["part_sha256"]
            ).strip():
                raise RuntimeError(
                    f"Spanned part {index} is missing "
                    "its part SHA256."
                )

    except Exception as exc:
        read_error = str(exc)

    unmounted, unmount_error = _unmount_ltfs(
        mountpoint
    )

    if not unmounted:
        return {
            "success": False,
            "error": (
                "Tape metadata was read, but the read-only "
                "LTFS mount could not be released cleanly: "
                f"{unmount_error}"
            ),
        }

    if read_error:
        return {
            "success": False,
            "error": read_error,
        }

    #
    # Tape is now cleanly unmounted.
    # Only now change SQLite.
    #
    try:
        result = import_tape_manifest_records(
            tape_metadata,
            manifest,
            generation=_generation_number(
                tape_metadata.get(
                    "generation"
                )
            ),
        )

    except Exception as exc:
        return {
            "success": False,
            "error": str(exc),
        }

    result["success"] = True
    result["volume_name"] = loaded_name

    return result
