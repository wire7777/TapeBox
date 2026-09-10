# TapeBox

**TapeBox is a simple web-based LTFS/LTO tape archive manager for Linux.**

TapeBox is designed for people who want to use LTO tape without needing an enterprise backup suite or complicated tape-library software.

It provides a straightforward workflow for staging files, planning tape usage, archiving to LTFS cartridges, restoring files, browsing existing LTFS media, and maintaining a searchable SQLite catalog.

> **Project status:** TapeBox is under active development. Back up important data and test your hardware/workflow before relying on it for irreplaceable archives.

## Features

- Web-based archive and restore interface
- Standard LTFS storage — no proprietary tape filesystem
- SSD/NVMe staging directory
- File and folder archive jobs
- Archive planner with LTO generation awareness
- Multi-tape archive support
- Whole files kept on one tape whenever possible
- Splitting/reassembly when a single file exceeds one tape
- SQLite file/tape catalog
- Search for which cartridge contains a file
- LTFS UUID-based cartridge identity
- Friendly tape names, locations, and notes
- Whole-job restore
- Live archive and restore progress
- Tape Inspector for arbitrary LTFS cartridges
- Read-only inspection of existing media
- Prepare/format new LTFS cartridges
- Add existing LTFS cartridges without reformatting
- Restored Files browser
- Database backup and restore
- Physical tape eject with clean-unmount safeguards

## Why LTFS?

TapeBox stores normal archive data on **standard LTFS** cartridges. It does not invent a proprietary tape filesystem.

This means ordinary files written by TapeBox can still be accessed by mounting the cartridge with compatible LTFS software outside TapeBox.

TapeBox adds the management layer around LTFS: searchable catalog information, archive jobs, tape identity, spanning information, restore automation, manifests, and the web interface.

## Tested Environment

Development and hardware testing have primarily used:

- Linux Mint / Ubuntu-based Linux
- HP Ultrium 6-SCSI LTO-6 SAS drive
- SAS HBA in IT/pass-through mode
- LTFS 2.4.8.4
- LTFS Format Specification 2.4.0
- LTO-5 and LTO-6 media
- Python 3
- Flask 3.0.2

Other hardware and Linux distributions may work but have not received the same level of testing.

## LTO Generations

The Archive Planner currently understands conservative planning capacities for:

| Generation | Planning Capacity |
| --- | ---: |
| LTO-5 | 1.40 TB |
| LTO-6 | 2.40 TB |
| LTO-7 | 5.80 TB |
| LTO-8 | 11.50 TB |
| LTO-9 | 17.50 TB |

When possible, TapeBox detects the generation of the loaded cartridge automatically.

Actual read/write compatibility is determined by your tape drive and media. TapeBox cannot make an LTO drive read or write media that the hardware itself does not support.

## Requirements

TapeBox currently targets modern Ubuntu/Linux Mint-style systems.

Linux packages commonly required:

```bash
sudo apt update

sudo apt install -y \
    python3 \
    python3-venv \
    python3-pip \
    git \
    lsscsi \
    mt-st \
    fuse3 \
    attr
```

TapeBox also requires a working **LTFS** installation.

These commands must be available in `PATH`:

```bash
command -v ltfs
command -v mkltfs
```

Verify them with:

```bash
ltfs --version
mkltfs --version
```

TapeBox has been tested with:

```text
LTFS version 2.4.8.4 (Prelim)
LTFS Format Specification version 2.4.0
```

LTFS packaging and installation varies between distributions and implementations, so TapeBox does **not** currently install LTFS automatically.

Install and verify LTFS before running the TapeBox installer.

The binaries may live in locations such as `/usr/bin` or `/usr/local/bin`; they simply need to be available in the service `PATH`.

## Hardware Detection

Before installing TapeBox, verify that Linux sees your tape drive:

```bash
lsscsi -g
```

A SAS tape drive will typically expose devices similar to:

```text
/dev/st0
/dev/nst0
/dev/sg0
```

Check cartridge/drive status with:

```bash
mt -f /dev/nst0 status
```

## Installation

Clone TapeBox into `/opt/tapebox`:

```bash
cd /opt

sudo git clone https://github.com/wire7777/TapeBox.git tapebox
sudo chown -R "$USER":"$USER" /opt/tapebox

cd /opt/tapebox
```

Run the installer:

```bash
sudo ./install.sh
```

The installer:

- checks required Linux commands
- verifies `ltfs` and `mkltfs`
- creates the `tapebox` group
- adds the installing user to that group
- creates TapeBox data and mount directories
- installs tape-device udev rules
- creates a Python virtual environment
- installs Python requirements
- initializes the SQLite database
- installs a systemd service
- enables TapeBox at boot

After installation, **log out and back in** so your new `tapebox` group membership is applied.

Then start TapeBox:

```bash
sudo systemctl start tapebox
```

Check it:

```bash
sudo systemctl status tapebox
```

Open the web interface:

```text
http://SERVER-IP:8080
```

Find your server address with:

```bash
hostname -I
```

TapeBox currently serves plain HTTP by default.

## Service Management

Start:

```bash
sudo systemctl start tapebox
```

Stop:

```bash
sudo systemctl stop tapebox
```

Restart:

```bash
sudo systemctl restart tapebox
```

Enable at boot:

```bash
sudo systemctl enable tapebox
```

Follow logs:

```bash
journalctl -u tapebox -f
```

Recent logs:

```bash
journalctl -u tapebox -n 200
```

## Tape Permissions

The installer creates udev rules that give the `tapebox` group access to Linux SCSI tape devices.

The rules are equivalent to:

```text
SUBSYSTEM=="scsi_generic", ATTRS{type}=="8", SYMLINK+="tapebox-changer", GROUP="tapebox", MODE="0660"
SUBSYSTEM=="scsi_generic", ATTRS{type}=="1", SYMLINK+="tapebox-drive-sg", GROUP="tapebox", MODE="0660"
SUBSYSTEM=="scsi_tape", KERNEL=="nst[0-9]", SYMLINK+="tapebox-drive-nst", GROUP="tapebox", MODE="0660"
SUBSYSTEM=="scsi_tape", KERNEL=="st*", GROUP="tapebox", MODE="0660"
```

Depending on the hardware, this can provide stable names such as:

```text
/dev/tapebox-drive-nst
/dev/tapebox-drive-sg
```

Check your groups with:

```bash
groups
```

If `tapebox` was just added, log out and back in before troubleshooting permissions.

## Data Locations

Default installation layout:

| Path | Purpose |
| --- | --- |
| `/opt/tapebox` | Application |
| `/opt/tapebox/venv` | Python virtual environment |
| `/var/lib/tapebox/catalog.db` | SQLite catalog |
| `/var/lib/tapebox/backups` | Catalog backups |
| `/var/lib/tapebox/manifests` | Manifest working data |
| `/var/lib/tapebox/state` | Persistent state |
| `/mnt/tapebox/staging` | Archive staging area |
| `/mnt/tapebox/restored` | Restored files |
| `/mnt/tapebox/ltfs` | Normal LTFS mount point |
| `/mnt/tapebox/ltfs-inspect` | Tape Inspector mount point |

The Restore Directory and Buffer/Staging Directory can also be configured from TapeBox Settings.

## Basic Workflow

A typical TapeBox archive looks like:

```text
Copy data to staging
        |
        v
Select files/folders in TapeBox
        |
        v
Archive Planner
        |
        v
Insert requested cartridge
        |
        v
Write to LTFS
        |
        v
Sync + clean unmount
        |
        v
Update catalog/manifests
        |
        v
Eject
```

Files can be placed in the staging directory using normal Linux/network methods such as SMB, NFS, SSH/SFTP, SCP, rsync, local copies, or TapeBox's web upload interface.

## Staging and Archive Planner

The default staging directory is:

```text
/mnt/tapebox/staging
```

Open **Staging** in the web interface and select the files or folders to archive.

TapeBox recursively calculates:

- total bytes
- file count
- estimated tape count
- tape generation
- estimated usage per cartridge
- estimated free space on the final cartridge
- whether any single file requires spanning

Normal files are kept whole whenever they fit on a cartridge.

TapeBox only needs to split a file when that **single logical file** is larger than the usable capacity of one cartridge.

## Multi-Tape Archives

A folder or archive job can span multiple cartridges.

For ordinary files, TapeBox moves to another tape instead of unnecessarily dividing the file.

If a single file itself is too large for one cartridge, TapeBox can store it as multiple parts and track those parts in the catalog.

During restore, TapeBox requests the required cartridges, reconstructs the logical file, and verifies the completed result.

## Preparing a New Tape

Insert a blank cartridge and open:

**Tapes → Prepare / Format Tape**

TapeBox first performs a non-destructive inspection.

For a blank/unpartitioned cartridge, enter the desired LTFS label and the exact confirmation requested by the interface, for example:

```text
FORMAT TAPE0001
```

The normal blank-tape workflow:

1. verifies the tape drive is available
2. checks that TapeBox does not already have an LTFS mount active
3. performs a final read-only inspection
4. refuses the blank-tape path if readable LTFS already exists
5. runs `mkltfs`
6. mounts the resulting LTFS filesystem read-only
7. verifies its UUID and metadata
8. registers the cartridge in SQLite
9. cleanly unmounts it
10. leaves the cartridge loaded

Formatting is destructive. Verify the physical cartridge before confirming.

## Existing LTFS Tapes

To catalog an existing LTFS cartridge without formatting it, use:

**Tapes → Add Existing LTFS Tape**

TapeBox performs a read-only inspection and displays information such as:

- LTFS label
- LTFS UUID
- generation
- capacity
- used/free space
- format information

Inspection alone does **not** automatically add a foreign cartridge to the TapeBox catalog.

Registration is an explicit action.

The LTFS UUID is treated as the permanent physical identity of a cataloged cartridge.

## Tape Inspector

Tape Inspector is intended for arbitrary LTFS cartridges, including media that is not registered with TapeBox.

It can:

- inspect a loaded cartridge
- identify its LTFS filesystem
- mount it read-only
- browse directories
- browse files
- copy selected content off the tape
- unmount without ejecting
- cleanly unmount and physically eject

Using Tape Inspector does not require adding the cartridge to the TapeBox catalog.

Only one TapeBox tape operation should own the physical drive at a time.

## Catalog and Search

TapeBox maintains a SQLite catalog at:

```text
/var/lib/tapebox/catalog.db
```

The catalog tracks information such as:

- tape labels
- LTFS UUIDs
- generation/capacity
- friendly names
- physical locations
- notes
- archive jobs
- files
- file parts
- tape associations

This lets the web interface determine which cartridge or cartridges are required for a restore.

## Tape Metadata

TapeBox supports TapeBox-only catalog metadata such as:

- Friendly Name
- Location
- Notes

Changing this information does not require rewriting the physical LTFS cartridge.

The LTFS UUID remains the permanent cartridge identity.

## Restore

TapeBox supports restoring ordinary files and archive jobs as well as logical files that span multiple cartridges.

The default destination is:

```text
/mnt/tapebox/restored
```

For a multi-tape restore, TapeBox determines the required tape sequence from the catalog and waits for the correct cartridge.

Live restore telemetry can show progress, bytes transferred, current tape, elapsed time, and operation state.

## Spanned Files

For a logical file that exceeds a cartridge's usable capacity, TapeBox tracks individual parts across tapes.

Restore uses local partial data while assembling the file.

Once all parts are present, TapeBox verifies the completed file and atomically finalizes it.

If a restore was interrupted after all tape parts had already reached local storage, TapeBox can finalize the completed local partial file without unnecessarily requesting the tapes again.

## Restored Files

The **Restored Files** page browses the configured restore directory and allows completed files to be accessed through the web interface.

The Restore Directory is configurable from Settings.

## Database Backup and Restore

TapeBox includes SQLite backup and restore functionality in Settings.

Default catalog:

```text
/var/lib/tapebox/catalog.db
```

Default backup directory:

```text
/var/lib/tapebox/backups
```

Keep independent catalog backups.

TapeBox also writes archive metadata/manifests associated with its LTFS workflow so the physical media carries recovery information in addition to the local SQLite catalog.

## Safe Unmount and Eject

TapeBox's archive workflow is intentionally conservative around LTFS finalization.

The basic write/finalization sequence is:

```text
write data
-> sync
-> clean LTFS unmount
-> commit successful catalog state
-> refresh tape manifest
-> clean unmount
-> eject
```

TapeBox does not intentionally force a physical eject when LTFS has failed to cleanly unmount.

Do not power off the tape drive, disconnect the SAS connection, kill TapeBox, or remove media while an archive, format, sync, or LTFS unmount is in progress.

## Manual Development Run

For development, TapeBox can be started directly:

```bash
cd /opt/tapebox

python3 -m tapebox.web \
    --host 0.0.0.0 \
    --port 8080
```

For a normal installed system, use the systemd service.

## Updating

Update the repository:

```bash
cd /opt/tapebox
git pull
```

If requirements have changed:

```bash
/opt/tapebox/venv/bin/pip install \
    -r /opt/tapebox/requirements.txt
```

Then restart:

```bash
sudo systemctl restart tapebox
```

## Troubleshooting

### Tape drive not detected

```bash
lsscsi -g
```

Then inspect the device nodes:

```bash
ls -l /dev/st* /dev/nst* /dev/sg*
```

### Check cartridge status

```bash
mt -f /dev/nst0 status
```

### LTFS not found

```bash
command -v ltfs
command -v mkltfs
```

Both commands must resolve to executable files.

### Permission denied

```bash
groups
```

Verify that your user is a member of `tapebox`.

After adding group membership, log out and back in.

### TapeBox service

```bash
sudo systemctl status tapebox
```

### Service logs

```bash
journalctl -u tapebox -n 200
```

or:

```bash
journalctl -u tapebox -f
```

## Current Limitations

TapeBox is still under active development.

Current limitations/cautions include:

- primarily tested on Linux Mint/Ubuntu
- primarily tested with a directly attached SAS tape drive
- LTFS must currently be installed separately
- robotic tape-library/changer operation is not the primary tested workflow
- the built-in web service currently uses HTTP by default
- built-in authentication/HTTPS deployment is not yet the default configuration
- additional LTO generations need broader real-hardware testing
- important archives should always have independent backups and verification

## Safety

LTO cartridges may contain irreplaceable data.

Before using **Prepare / Format Tape**, verify that the loaded cartridge is the cartridge you intend to erase.

Do not interrupt TapeBox while LTFS is writing, syncing, formatting, finalizing, mounting, or unmounting.

No archive system can protect against every hardware failure, damaged cartridge, unexpected power loss, operator mistake, or software defect. Maintain multiple copies of important data.

## Disclaimer / No Warranty

TapeBox is provided **"AS IS"**, without warranty of any kind, express or implied.

Use TapeBox entirely at your own risk. The authors and contributors are not responsible for data loss, damaged or overwritten tapes, failed archives or restores, hardware damage, loss of business, or any other direct or indirect damages resulting from the use of this software.

TapeBox can perform destructive operations, including formatting LTO cartridges. Always verify that the correct cartridge is loaded before confirming a format or other destructive operation.

Important data should never exist on only one tape or in only one location. Maintain independent backups and verify your archives.

TapeBox is an independent open-source project and is not affiliated with or endorsed by any LTO drive manufacturer, LTFS vendor, or the LTO Program.

## License

A project license has not yet been selected.

Before treating TapeBox as a generally redistributable open-source project, add an appropriate license file.

---

**TapeBox — simple LTFS/LTO archiving without enterprise backup-system complexity.**
