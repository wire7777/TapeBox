import re
import shutil
import subprocess
import time
from pathlib import Path


def run_command(command, timeout=120):
    """
    Run a command and return stdout/stderr in a predictable structure.
    """
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        return {
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }

    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""

        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")

        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")

        return {
            "returncode": -2,
            "stdout": stdout.strip(),
            "stderr": (
                stderr.strip()
                or f"Command timed out after {timeout} seconds"
            ),
        }

    except Exception as exc:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": str(exc),
        }


def _is_transient_tape_error(text):
    """
    Return True for short-lived tape-device errors worth retrying.
    """
    text = (text or "").lower()

    markers = (
        "device or resource busy",
        "resource temporarily unavailable",
        "input/output error",
        "failed to open /dev/sg",
        "failed backend open call",
        "(16)",
    )

    return any(
        marker in text
        for marker in markers
    )


def mount_ltfs(
    sg_device,
    mount_path,
    read_only=False,
    retries=5,
    retry_delay=1.5,
    timeout=120,
):
    """
    Mount LTFS with a small retry window for transient device-busy
    conditions that can occur just after a previous LTFS operation.
    """
    mount_path = Path(mount_path)

    command = [
        "ltfs",
        str(mount_path),
        "-o",
        f"devname={sg_device}",
    ]

    if read_only:
        command.extend(
            [
                "-o",
                "ro",
            ]
        )

    last_result = {
        "returncode": -1,
        "stdout": "",
        "stderr": "LTFS mount failed.",
    }

    for attempt in range(1, retries + 1):
        last_result = run_command(
            command,
            timeout=timeout,
        )

        if _is_mounted(mount_path):
            return last_result

        text = (
            last_result.get("stdout", "")
            + "\n"
            + last_result.get("stderr", "")
        )

        if not _is_transient_tape_error(text):
            return last_result

        if attempt < retries:
            time.sleep(retry_delay)

    return last_result



def get_ltfs_devices():
    """
    Read LTFS-compatible tape devices.
    """
    result = run_command(
        [
            "ltfs",
            "-o",
            "device_list",
        ],
        timeout=10,
    )

    devices = []

    text = (
        result["stdout"]
        + "\n"
        + result["stderr"]
    )

    for line in text.splitlines():
        if "Device Name =" not in line:
            continue

        device_match = re.search(
            r"Device Name\s*=\s*(/dev/sg\d+)",
            line,
        )

        scsi_match = re.search(
            r"\(([^)]+)\)",
            line,
        )

        vendor_match = re.search(
            r"Vendor ID\s*=\s*(.*?)\s*,\s*Product ID",
            line,
        )

        product_match = re.search(
            r"Product ID\s*=\s*(.*?)\s*,\s*Serial Number",
            line,
        )

        serial_match = re.search(
            r"Serial Number\s*=\s*(.*?)\s*,\s*Product Name",
            line,
        )

        product_name_match = re.search(
            r"Product Name\s*=\s*\[(.*?)\]",
            line,
        )

        if not device_match:
            continue

        devices.append(
            {
                "device": device_match.group(1).strip(),
                "scsi_address": (
                    scsi_match.group(1).strip()
                    if scsi_match
                    else None
                ),
                "vendor": (
                    vendor_match.group(1).strip()
                    if vendor_match
                    else None
                ),
                "product": (
                    product_match.group(1).strip()
                    if product_match
                    else None
                ),
                "serial": (
                    serial_match.group(1).strip()
                    if serial_match
                    else None
                ),
                "product_name": (
                    product_name_match.group(1).strip()
                    if product_name_match
                    else None
                ),
            }
        )

    return devices


def discover_drives():
    """
    Discover Linux SCSI tape drives using lsscsi -g
    and enrich them with LTFS device information.
    """
    result = run_command(
        ["lsscsi", "-g"],
        timeout=10,
    )

    drives = []

    if result["returncode"] != 0:
        return drives

    for line in result["stdout"].splitlines():
        parts = line.split()

        if len(parts) < 6:
            continue

        if parts[1] != "tape":
            continue

        scsi_address = parts[0].strip("[]")

        st_device = None
        sg_device = None

        for part in parts:
            if re.fullmatch(r"/dev/st\d+", part):
                st_device = part

            elif re.fullmatch(r"/dev/sg\d+", part):
                sg_device = part

        if not st_device:
            continue

        try:
            st_index = parts.index(st_device)
        except ValueError:
            continue

        description = " ".join(parts[2:st_index])

        number_match = re.search(
            r"/dev/st(\d+)$",
            st_device,
        )

        if number_match:
            drive_number = number_match.group(1)
            nst_device = f"/dev/nst{drive_number}"
        else:
            nst_device = None

        drives.append(
            {
                "scsi_address": scsi_address,
                "st_device": st_device,
                "nst_device": nst_device,
                "sg_device": sg_device,
                "description": description,
                "raw": line,
            }
        )

    ltfs_devices = get_ltfs_devices()

    for drive in drives:
        for ltfs_device in ltfs_devices:
            if drive["sg_device"] == ltfs_device["device"]:
                drive["vendor"] = ltfs_device["vendor"]
                drive["product"] = ltfs_device["product"]
                drive["serial"] = ltfs_device["serial"]
                drive["product_name"] = ltfs_device["product_name"]
                break

    return drives


def get_tape_status(
    device="/dev/nst0",
    retries=10,
    retry_delay=1.0,
):
    """
    Read tape status using mt.

    Short-lived EBUSY/I/O errors are retried because some drives
    briefly remain unavailable after LTFS mount/unmount activity.

    A newly inserted cartridge may also return a successful mt
    status before the drive has finished loading it. In that case
    TapeBox waits for ONLINE instead of immediately reporting the
    cartridge as unavailable.

    If DR_OPEN is reported, no cartridge is loaded, so there is no
    reason to wait through the retry interval.
    """
    if not Path(device).exists():
        return {
            "device": device,
            "available": False,
            "error": "Device does not exist",
        }

    result = None

    for attempt in range(1, retries + 1):
        result = run_command(
            [
                "mt",
                "-f",
                device,
                "status",
            ],
            timeout=15,
        )

        if result["returncode"] == 0:
            text = result.get(
                "stdout",
                "",
            )

            #
            # Cartridge is fully loaded and ready.
            #
            if "ONLINE" in text:
                break

            #
            # DR_OPEN means the drive is empty/unloaded.
            # Do not make commands wait several seconds when
            # there is genuinely no cartridge inserted.
            #
            if "DR_OPEN" in text:
                break

            #
            # mt succeeded, but the cartridge has not yet
            # transitioned ONLINE. This commonly occurs for a
            # few seconds immediately after insertion.
            #
            if attempt < retries:
                time.sleep(
                    retry_delay
                )
                continue

            break

        error_text = (
            result.get("stdout", "")
            + "\n"
            + result.get("stderr", "")
        )

        if not _is_transient_tape_error(
            error_text
        ):
            break

        if attempt < retries:
            time.sleep(
                retry_delay
            )

    status = {
        "device": device,
        "available": (
            result["returncode"] == 0
        ),
        "raw": result.get(
            "stdout",
            "",
        ),
    }

    if result["returncode"] != 0:
        status["error"] = (
            result.get("stderr")
            or result.get("stdout")
            or "Unknown error"
        )
        return status

    text = result.get(
        "stdout",
        "",
    )

    density = re.search(
        r"Density code .*?\((.*?)\)",
        text,
    )

    if density:
        status["density"] = (
            density.group(1)
        )

    status["online"] = (
        "ONLINE" in text
    )
    status["write_protected"] = (
        "WR_PROT" in text
    )
    status["beginning_of_tape"] = (
        "BOT" in text
    )
    status["end_of_tape"] = (
        "EOT" in text
    )

    return status

def _is_mounted(mount_path):
    """
    Return True if the LTFS inspection mountpoint is active.
    """
    result = run_command(
        [
            "findmnt",
            "-n",
            str(mount_path),
        ],
        timeout=5,
    )

    return result["returncode"] == 0


def _wait_for_ltfs_release(
    mount_path,
    timeout=60.0,
    poll_interval=0.25,
):
    """
    Wait until the LTFS process for this mountpoint has exited.

    FUSE may remove the mountpoint before LTFS has completely
    released the underlying SCSI tape device.
    """
    mount_text = str(
        Path(mount_path)
    )

    deadline = (
        time.monotonic()
        + timeout
    )

    while time.monotonic() < deadline:
        result = run_command(
            [
                "ps",
                "-eo",
                "args=",
            ],
            timeout=10,
        )

        if result["returncode"] != 0:
            return False, (
                result["stderr"]
                or result["stdout"]
                or "Could not inspect LTFS processes"
            )

        ltfs_running = False

        for line in result["stdout"].splitlines():
            line = line.strip()

            if (
                line.startswith("ltfs ")
                and mount_text in line
            ):
                ltfs_running = True
                break

        if not ltfs_running:
            return True, None

        time.sleep(
            poll_interval
        )

    return False, (
        "LTFS filesystem is unmounted, but the LTFS "
        f"process for {mount_text} did not release "
        f"within {timeout:.0f} seconds."
    )


def _unmount_ltfs(mount_path):
    """
    Cleanly unmount an LTFS FUSE filesystem and wait until
    the LTFS process has released the tape device.
    """

    mount_path = Path(
        mount_path
    )

    #
    # The FUSE mount may already have disappeared while the
    # LTFS process is still completing its shutdown.
    #
    if not _is_mounted(mount_path):
        return _wait_for_ltfs_release(
            mount_path
        )

    result = run_command(
        [
            "fusermount3",
            "-u",
            str(mount_path),
        ],
        timeout=30,
    )

    if result["returncode"] != 0:
        result = run_command(
            [
                "umount",
                str(mount_path),
            ],
            timeout=30,
        )

        if result["returncode"] != 0:
            error = (
                result["stderr"]
                or result["stdout"]
                or "Unknown unmount error"
            )

            return False, error

    #
    # Do not report success merely because FUSE has removed
    # the mount. LTFS may still own /dev/sg0 for a short time.
    #
    return _wait_for_ltfs_release(
        mount_path
    )


def get_ltfs_virtual_attribute(
    mount_path,
    attribute,
):
    """
    Read an LTFS virtual attribute.

    Examples:
        ltfs.volumeUUID
        ltfs.volumeName
        ltfs.volumeSerial
    """
    result = run_command(
        [
            "attr",
            "-q",
            "-g",
            attribute,
            str(mount_path),
        ],
        timeout=10,
    )

    if result["returncode"] != 0:
        return None

    value = result["stdout"].strip()

    if not value:
        return None

    return value


def inspect_ltfs(
    sg_device="/dev/sg0",
    mountpoint="/mnt/tapebox/ltfs-inspect",
):
    """
    Mount the LTFS cartridge read-only,
    inspect it, then unmount automatically.
    """
    info = {
        "ltfs": False,
        "mounted": False,
        "label": None,
        "uuid": None,
        "volume_serial": None,
        "barcode": None,
        "format_version": None,
        "capacity_bytes": None,
        "used_bytes": None,
        "free_bytes": None,
        "error": None,
    }

    if not sg_device:
        info["error"] = "No SCSI generic device"
        return info

    if not Path(sg_device).exists():
        info["error"] = (
            f"Device does not exist: {sg_device}"
        )
        return info

    mount_path = Path(mountpoint)

    try:
        mount_path.mkdir(
            parents=True,
            exist_ok=True,
        )
    except OSError as exc:
        info["error"] = str(exc)
        return info

    if _is_mounted(mount_path):
        info["error"] = (
            "LTFS inspection mount point is already mounted: "
            f"{mount_path}"
        )
        return info

    mount_result = mount_ltfs(
        sg_device,
        mount_path,
        read_only=True,
        timeout=120,
    )

    text = (
        mount_result["stdout"]
        + "\n"
        + mount_result["stderr"]
    )

    if not _is_mounted(mount_path):
        info["error"] = (
            mount_result["stderr"]
            or mount_result["stdout"]
            or "LTFS mount failed"
        )
        return info

    info["mounted"] = True
    info["ltfs"] = True

    #
    # LTFS UUID
    #

    info["uuid"] = get_ltfs_virtual_attribute(
        mount_path,
        "ltfs.volumeUUID",
    )

    #
    # LTFS volume name
    #

    volume_name = get_ltfs_virtual_attribute(
        mount_path,
        "ltfs.volumeName",
    )

    if volume_name:
        info["label"] = volume_name

    #
    # Optional LTFS serial
    #

    info["volume_serial"] = get_ltfs_virtual_attribute(
        mount_path,
        "ltfs.volumeSerial",
    )

    #
    # Fallback label parsing
    #

    if not info["label"]:
        label_match = re.search(
            r"Tape attribute:\s*"
            r"Medium Label\s*=\s*"
            r"([^\r\n]+)",
            text,
        )

        if label_match:
            label = label_match.group(1).strip()

            if label.endswith("."):
                label = label[:-1]

            label = label.strip()

            if label:
                info["label"] = label

    #
    # Physical barcode
    #

    barcode_match = re.search(
        r"Tape attribute:\s*"
        r"Barcode\s*=\s*"
        r"([^\r\n]+)",
        text,
    )

    if barcode_match:
        barcode = barcode_match.group(1).strip()

        if barcode.endswith("."):
            barcode = barcode[:-1]

        barcode = barcode.strip()

        if barcode:
            info["barcode"] = barcode

    #
    # LTFS format version
    #

    format_match = re.search(
        r"Tape attribute:\s*"
        r"Application Format Version\s*=\s*"
        r"([0-9]+(?:\.[0-9]+)+)",
        text,
    )

    if format_match:
        info["format_version"] = format_match.group(1)

    #
    # Capacity / used / free
    #

    try:
        usage = shutil.disk_usage(
            mount_path
        )

        info["capacity_bytes"] = usage.total
        info["used_bytes"] = usage.used
        info["free_bytes"] = usage.free

    except OSError as exc:
        info["error"] = (
            f"Could not read LTFS capacity: {exc}"
        )

    #
    # Always unmount after inspection
    #

    success, error = _unmount_ltfs(
        mount_path
    )

    if success:
        info["mounted"] = False
    else:
        info["error"] = (
            "LTFS inspection succeeded, "
            "but automatic unmount failed: "
            f"{error}"
        )

    return info

def eject_tape(
    device="/dev/tapebox-drive-nst",
    timeout=120,
):
    """
    Rewind/unload and physically eject the loaded tape.

    The caller must ensure LTFS is fully unmounted before
    calling this function.
    """

    result = run_command(
        [
            "mt",
            "-f",
            device,
            "offline",
        ],
        timeout=timeout,
    )

    if result["returncode"] != 0:
        error_text = (
            result["stderr"]
            or result["stdout"]
            or "Unknown tape eject error"
        )

        return {
            "success": False,
            "device": device,
            "error": error_text,
        }

    return {
        "success": True,
        "device": device,
    }


def mount_ltfs_inspector(
    sg_device="/dev/sg0",
    mountpoint="/mnt/tapebox/ltfs-inspect",
):
    """
    Mount an LTFS cartridge read-only for interactive inspection.

    Unlike inspect_ltfs(), this intentionally leaves the
    filesystem mounted so the web UI can browse it.
    """
    mount_path = Path(mountpoint)

    info = {
        "success": False,
        "mounted": False,
        "label": None,
        "uuid": None,
        "volume_serial": None,
        "capacity_bytes": None,
        "used_bytes": None,
        "free_bytes": None,
        "items": [],
        "error": None,
    }

    if not sg_device:
        info["error"] = "No SCSI generic device."
        return info

    if not Path(sg_device).exists():
        info["error"] = (
            f"Device does not exist: {sg_device}"
        )
        return info

    try:
        mount_path.mkdir(
            parents=True,
            exist_ok=True,
        )
    except OSError as exc:
        info["error"] = str(exc)
        return info

    #
    # Reuse an Inspector mount if it already exists.
    #
    if not _is_mounted(mount_path):
        mount_result = mount_ltfs(
            sg_device,
            mount_path,
            read_only=True,
            timeout=120,
        )

        if not _is_mounted(mount_path):
            info["error"] = (
                mount_result.get("stderr")
                or mount_result.get("stdout")
                or "LTFS mount failed."
            )
            return info

    info["mounted"] = True

    #
    # Read LTFS identity directly from the mounted volume.
    #
    info["uuid"] = get_ltfs_virtual_attribute(
        mount_path,
        "ltfs.volumeUUID",
    )

    info["label"] = get_ltfs_virtual_attribute(
        mount_path,
        "ltfs.volumeName",
    )

    info["volume_serial"] = get_ltfs_virtual_attribute(
        mount_path,
        "ltfs.volumeSerial",
    )

    #
    # Capacity.
    #
    try:
        usage = shutil.disk_usage(
            mount_path
        )

        info["capacity_bytes"] = usage.total
        info["used_bytes"] = usage.used
        info["free_bytes"] = usage.free

    except OSError as exc:
        info["error"] = (
            f"Could not read LTFS capacity: {exc}"
        )
        return info

    #
    # Root directory only.
    #
    try:
        items = []

        for entry in mount_path.iterdir():
            try:
                stat = entry.stat()

                is_directory = entry.is_dir()

                items.append(
                    {
                        "name": entry.name,
                        "type": (
                            "directory"
                            if is_directory
                            else "file"
                        ),
                        "size": (
                            None
                            if is_directory
                            else stat.st_size
                        ),
                    }
                )

            except OSError as exc:
                items.append(
                    {
                        "name": entry.name,
                        "type": "unknown",
                        "size": None,
                        "error": str(exc),
                    }
                )

        items.sort(
            key=lambda item: (
                item["type"] != "directory",
                item["name"].casefold(),
            )
        )

        info["items"] = items

    except OSError as exc:
        info["error"] = (
            f"Could not read LTFS root directory: {exc}"
        )
        return info

    info["success"] = True

    return info


def browse_ltfs_inspector(
    relative_path="",
    mountpoint="/mnt/tapebox/ltfs-inspect",
):
    """
    Browse one directory on the currently mounted
    read-only Inspector LTFS filesystem.

    The requested path is strictly confined to the
    Inspector mount root.
    """
    mount_path = Path(mountpoint)

    result = {
        "success": False,
        "mounted": False,
        "path": "",
        "parent": None,
        "items": [],
        "error": None,
    }

    if not _is_mounted(mount_path):
        result["error"] = (
            "No LTFS cartridge is mounted in Tape Inspector."
        )
        return result

    result["mounted"] = True

    try:
        root = mount_path.resolve()

        requested = (
            root / str(relative_path).lstrip("/")
        ).resolve()

    except (OSError, RuntimeError) as exc:
        result["error"] = (
            f"Could not resolve requested path: {exc}"
        )
        return result

    #
    # SECURITY:
    # The resolved path must remain underneath the
    # Inspector LTFS mount.
    #
    try:
        relative = requested.relative_to(root)
    except ValueError:
        result["error"] = (
            "Requested path is outside the LTFS cartridge."
        )
        return result

    if not requested.exists():
        result["error"] = "Path does not exist."
        return result

    if not requested.is_dir():
        result["error"] = "Requested path is not a directory."
        return result

    relative_text = (
        ""
        if str(relative) == "."
        else relative.as_posix()
    )

    result["path"] = relative_text

    if relative_text:
        parent = relative.parent

        result["parent"] = (
            ""
            if str(parent) == "."
            else parent.as_posix()
        )

    try:
        items = []

        for entry in requested.iterdir():
            try:
                stat = entry.stat()
                is_directory = entry.is_dir()

                child_relative = (
                    entry.resolve()
                    .relative_to(root)
                    .as_posix()
                )

                items.append(
                    {
                        "name": entry.name,
                        "path": child_relative,
                        "type": (
                            "directory"
                            if is_directory
                            else "file"
                        ),
                        "size": (
                            None
                            if is_directory
                            else stat.st_size
                        ),
                    }
                )

            except (OSError, ValueError) as exc:
                items.append(
                    {
                        "name": entry.name,
                        "path": None,
                        "type": "unknown",
                        "size": None,
                        "error": str(exc),
                    }
                )

        items.sort(
            key=lambda item: (
                item["type"] != "directory",
                item["name"].casefold(),
            )
        )

        result["items"] = items
        result["success"] = True

    except OSError as exc:
        result["error"] = (
            f"Could not read LTFS directory: {exc}"
        )

    return result
