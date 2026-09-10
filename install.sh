#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/tapebox"
DATA_DIR="/var/lib/tapebox"
LOG_DIR="/var/log/tapebox"
MOUNT_ROOT="/mnt/tapebox"

SERVICE_NAME="tapebox"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
UDEV_FILE="/etc/udev/rules.d/99-tapebox.rules"

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
    echo "  sudo apt install -y python3 python3-venv python3-pip git lsscsi mt-st fuse3 attr"
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

if [[ -n "$INSTALL_USER" && "$INSTALL_USER" != "root" ]]; then
    usermod -aG tapebox "$INSTALL_USER"
    echo "  Added $INSTALL_USER to tapebox group"
fi

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

chgrp -R tapebox \
    "$DATA_DIR" \
    "$LOG_DIR" \
    "$MOUNT_ROOT"

chmod -R g+rwX \
    "$DATA_DIR" \
    "$LOG_DIR" \
    "$MOUNT_ROOT"

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
echo "[6/9] Creating Python virtual environment..."

python3 -m venv "$APP_DIR/venv"

"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo
echo "[7/9] Creating systemd service..."

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
ExecStart=$APP_DIR/venv/bin/python -m tapebox.web --host 0.0.0.0 --port 8080
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF_SERVICE

systemctl daemon-reload

echo
echo "[8/9] Initializing TapeBox..."

cd "$APP_DIR"

"$APP_DIR/venv/bin/python" - <<'PY'
from tapebox.database import initialize_database

initialize_database()

print("TapeBox database initialized.")
PY

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
