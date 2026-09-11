#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/tapebox"
DATA_DIR="/var/lib/tapebox"
LOG_DIR="/var/log/tapebox"
MOUNT_ROOT="/mnt/tapebox"

SERVICE_NAME="tapebox"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
UDEV_FILE="/etc/udev/rules.d/99-tapebox.rules"
SUDOERS_FILE="/etc/sudoers.d/tapebox-rescan"

if [[ $EUID -ne 0 ]]; then
    echo "Run this installer with sudo:"
    echo "  sudo ./install.sh"
    exit 1
fi

echo
echo "========================================"
echo " TapeBox Installer"
echo "========================================"
echo

echo "[1/9] Checking required commands..."

required_commands=(
    python3
    git
    lsscsi
    mt
    fusermount3
    rescan-scsi-bus.sh
    visudo
)

missing=0

for cmd in "${required_commands[@]}"; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "  MISSING: $cmd"
        missing=1
    else
        echo "  OK: $cmd -> $(command -v "$cmd")"
    fi
done

if [[ $missing -ne 0 ]]; then
    echo
    echo "Install the missing Linux packages first."
    echo
    echo "On Ubuntu / Linux Mint, typically:"
    echo
    echo "  sudo apt update"
    echo "  sudo apt install -y python3 python3-venv python3-pip git lsscsi mt-st fuse3 attr sg3-utils sudo"
    echo
    exit 1
fi

echo
echo "[2/9] Checking LTFS..."

if ! command -v ltfs >/dev/null 2>&1; then
    echo
    echo "ERROR: ltfs was not found in PATH."
    echo
    echo "TapeBox requires a working LTFS installation."
    echo "Install LTFS first, then run this installer again."
    exit 1
fi

if ! command -v mkltfs >/dev/null 2>&1; then
    echo
    echo "ERROR: mkltfs was not found in PATH."
    echo
    echo "TapeBox requires mkltfs to prepare new cartridges."
    echo "Install LTFS first, then run this installer again."
    exit 1
fi

echo "  ltfs:   $(command -v ltfs)"
echo "  mkltfs: $(command -v mkltfs)"

ltfs --version 2>&1 | head -3 || true

echo
echo "[3/9] Creating TapeBox group..."

if ! getent group tapebox >/dev/null 2>&1; then
    groupadd --system tapebox
    echo "  Created group: tapebox"
else
    echo "  Group already exists: tapebox"
fi

INSTALL_USER="${SUDO_USER:-}"
RUN_USER="${INSTALL_USER:-root}"

if [[ "$RUN_USER" == "root" ]]; then
    echo
    echo "WARNING:"
    echo "Could not determine the non-root install user."
    echo "The service would otherwise run as root."
    echo
    echo "Re-run this installer using sudo from your normal user account."
    exit 1
fi

usermod -aG tapebox "$RUN_USER"
echo "  Added $RUN_USER to tapebox group"

echo
echo "[4/9] Creating directories..."

mkdir -p \
    "$DATA_DIR/backups" \
    "$DATA_DIR/manifests" \
    "$DATA_DIR/state" \
    "$LOG_DIR" \
    "$MOUNT_ROOT/staging" \
    "$MOUNT_ROOT/ltfs" \
    "$MOUNT_ROOT/ltfs-inspect" \
    "$MOUNT_ROOT/restored"

# TapeBox application data is private to the service user.
for dir in \
    "$DATA_DIR" \
    "$DATA_DIR/backups" \
    "$DATA_DIR/manifests" \
    "$DATA_DIR/state" \
    "$LOG_DIR"
do
    chown "$RUN_USER":tapebox "$dir"
    chmod 0750 "$dir"
done

# Tape working directories may also be used by members of
# the tapebox group, so keep these group-writable.
for dir in \
    "$MOUNT_ROOT" \
    "$MOUNT_ROOT/staging" \
    "$MOUNT_ROOT/ltfs" \
    "$MOUNT_ROOT/ltfs-inspect" \
    "$MOUNT_ROOT/restored"
do
    chown "$RUN_USER":tapebox "$dir"
    chmod 0770 "$dir"
done

echo
echo "[5/9] Installing udev rules..."

cat > "$UDEV_FILE" <<'RULES'
SUBSYSTEM=="scsi_generic", ATTRS{type}=="8", SYMLINK+="tapebox-changer", GROUP="tapebox", MODE="0660"
SUBSYSTEM=="scsi_generic", ATTRS{type}=="1", SYMLINK+="tapebox-drive-sg", GROUP="tapebox", MODE="0660"
SUBSYSTEM=="scsi_tape", KERNEL=="nst[0-9]", SYMLINK+="tapebox-drive-nst", GROUP="tapebox", MODE="0660"
SUBSYSTEM=="scsi_tape", KERNEL=="st*", GROUP="tapebox", MODE="0660"
RULES

udevadm control --reload-rules
udevadm trigger

echo "  Installed: $UDEV_FILE"

echo
echo "  Installing restricted SCSI rescan sudo rule..."

cat > "$SUDOERS_FILE" <<EOF_SUDOERS
$RUN_USER ALL=(root) NOPASSWD: /usr/bin/rescan-scsi-bus.sh
EOF_SUDOERS

chmod 0440 "$SUDOERS_FILE"

if ! visudo -cf "$SUDOERS_FILE"; then
    echo "ERROR: Invalid sudoers configuration."
    rm -f "$SUDOERS_FILE"
    exit 1
fi

echo "  Installed: $SUDOERS_FILE"

echo
echo "[6/9] Creating Python virtual environment..."

python3 -m venv "$APP_DIR/venv"

"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo
echo "[7/9] Creating systemd service..."

cat > "$SERVICE_FILE" <<EOF_SERVICE
[Unit]
Description=TapeBox LTFS Archive Manager
After=network.target

[Service]
Type=simple
User=$RUN_USER
Group=tapebox
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
Environment=PATH=/usr/local/bin:/usr/bin:/bin
ExecStart=$APP_DIR/venv/bin/gunicorn --workers 1 --threads 4 --timeout 300 --bind 0.0.0.0:8080 tapebox.web:app
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF_SERVICE

systemctl daemon-reload

echo
echo "[8/9] Initializing TapeBox..."

cd "$APP_DIR"

# Repair ownership from older installs before SQLite is opened.
for db_file in \
    "$DATA_DIR/catalog.db" \
    "$DATA_DIR/catalog.db-wal" \
    "$DATA_DIR/catalog.db-shm"
do
    if [[ -e "$db_file" ]]; then
        chown "$RUN_USER":tapebox "$db_file"
        chmod 0640 "$db_file"
    fi
done

sudo -u "$RUN_USER" -g tapebox \
    "$APP_DIR/venv/bin/python" -c 'from tapebox.database import initialize_database; initialize_database(); print("TapeBox database initialized.")'

# Keep database files private after initialization as well.
for db_file in \
    "$DATA_DIR/catalog.db" \
    "$DATA_DIR/catalog.db-wal" \
    "$DATA_DIR/catalog.db-shm"
do
    if [[ -e "$db_file" ]]; then
        chown "$RUN_USER":tapebox "$db_file"
        chmod 0640 "$db_file"
    fi
done

echo
echo "[9/9] Enabling service..."

systemctl enable tapebox

echo
echo "========================================"
echo " Installation complete"
echo "========================================"
echo
echo "Start TapeBox:"
echo
echo "  sudo systemctl start tapebox"
echo
echo "Check status:"
echo
echo "  sudo systemctl status tapebox"
echo
echo "Web interface:"
echo
echo "  http://SERVER-IP:8080"
echo
echo "IMPORTANT:"
echo "Log out and back in before using TapeBox manually so the"
echo "new tapebox group membership is applied."
echo
