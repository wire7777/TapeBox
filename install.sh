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

LTFS_REPO="https://github.com/LinearTapeFileSystem/ltfs.git"
LTFS_BRANCH="release/v2.4.8.4"
LTFS_EXPECTED_VERSION="2.4.8.4"
LTFS_BUILD_ROOT="/tmp/tapebox-ltfs-build"

if [[ $EUID -ne 0 ]]; then
    echo "Run this installer with sudo:"
    echo "  sudo ./install.sh"
    exit 1
fi

INSTALL_USER="${SUDO_USER:-}"
RUN_USER="${INSTALL_USER:-root}"

if [[ "$RUN_USER" == "root" ]]; then
    echo
    echo "ERROR:"
    echo "Could not determine the non-root install user."
    echo
    echo "Run the installer from your normal user account:"
    echo "  sudo ./install.sh"
    exit 1
fi

echo
echo "========================================"
echo " TapeBox Installer"
echo "========================================"
echo
echo "Application user: $RUN_USER"
echo

echo "[1/10] Installing TapeBox system dependencies..."

BASE_PACKAGES=(
    python3
    python3-venv
    python3-pip
    sqlite3
    git
    sudo
    curl
    ca-certificates
    attr
    fuse3
    lsscsi
    mt-st
    sg3-utils
)

LTFS_BUILD_PACKAGES=(
    build-essential
    autoconf
    automake
    libtool
    pkg-config
    libfuse-dev
    libxml2-dev
    libsnmp-dev
    uuid-dev
    libicu-dev
    icu-devtools
)

ALL_PACKAGES=(
    "${BASE_PACKAGES[@]}"
    "${LTFS_BUILD_PACKAGES[@]}"
)

MISSING_PACKAGES=()

for pkg in "${ALL_PACKAGES[@]}"; do
    if dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null \
        | grep -q '^install ok installed$'; then
        printf "  %-22s OK\n" "$pkg"
    else
        printf "  %-22s MISSING\n" "$pkg"
        MISSING_PACKAGES+=("$pkg")
    fi
done

if (( ${#MISSING_PACKAGES[@]} > 0 )); then
    echo
    echo "Installing missing packages:"
    printf '  %s\n' "${MISSING_PACKAGES[@]}"
    echo

    apt-get update
    DEBIAN_FRONTEND=noninteractive \
        apt-get install -y "${MISSING_PACKAGES[@]}"
else
    echo
    echo "All required system packages are already installed."
fi

echo
echo "Verifying required commands..."

required_commands=(
    python3
    git
    lsscsi
    mt
    fusermount3
    rescan-scsi-bus.sh
    visudo
    sqlite3
    curl
    pkg-config
)

for cmd in "${required_commands[@]}"; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "ERROR: Required command '$cmd' is missing."
        exit 1
    fi

    echo "  OK: $cmd -> $(command -v "$cmd")"
done

echo
echo "[2/10] Checking LTFS..."

ltfs_version_ok() {
    command -v ltfs >/dev/null 2>&1 &&
    command -v mkltfs >/dev/null 2>&1 &&
    command -v ltfsck >/dev/null 2>&1 &&
    ltfs --version 2>&1 \
        | grep -Fq "LTFS version ${LTFS_EXPECTED_VERSION}"
}

if ltfs_version_ok; then
    echo "  LTFS $LTFS_EXPECTED_VERSION is already installed."
else
    echo
    echo "LTFS $LTFS_EXPECTED_VERSION is not installed."
    echo "TapeBox will build and install it automatically."
    echo

    rm -rf "$LTFS_BUILD_ROOT"
    mkdir -p "$LTFS_BUILD_ROOT"
    cd "$LTFS_BUILD_ROOT"

    echo "  Cloning LTFS $LTFS_BRANCH..."

    git clone \
        --branch "$LTFS_BRANCH" \
        --single-branch \
        "$LTFS_REPO" \
        ltfs

    cd ltfs

    echo "  Initializing LTFS submodules..."

    git submodule update --init --recursive

    echo "  Preparing ICU pkg-config compatibility file..."

    ICU_PC_DIR="$(
        pkg-config --variable=pcfiledir icu-uc 2>/dev/null || true
    )"

    if [[ -z "$ICU_PC_DIR" ]]; then
        echo "ERROR: pkg-config could not locate icu-uc."
        exit 1
    fi

    if [[ ! -f "$ICU_PC_DIR/icu-uc.pc" ]]; then
        echo "ERROR: ICU pkg-config file was not found:"
        echo "  $ICU_PC_DIR/icu-uc.pc"
        exit 1
    fi

    cp "$ICU_PC_DIR/icu-uc.pc" ./icu.pc

    echo "  Running autogen..."

    ./autogen.sh

    echo "  Configuring LTFS..."

    PKG_CONFIG_PATH="$PWD:${PKG_CONFIG_PATH:-}" \
        ./configure

    echo "  Compiling LTFS..."

    make -j"$(nproc)"

    echo "  Installing LTFS..."

    make install

    echo "  Refreshing shared library cache..."

    ldconfig
    hash -r

    echo
    echo "Verifying LTFS installation..."

    if ! ltfs_version_ok; then
        echo "ERROR: LTFS installation verification failed."
        exit 1
    fi

    echo "  LTFS installed successfully."

    cd "$APP_DIR"
    rm -rf "$LTFS_BUILD_ROOT"
fi

echo
echo "  ltfs:   $(command -v ltfs)"
echo "  mkltfs: $(command -v mkltfs)"
echo "  ltfsck: $(command -v ltfsck)"
echo

ltfs --version 2>&1 | head -3 || true

echo
echo "[3/10] Creating TapeBox group..."

if ! getent group tapebox >/dev/null 2>&1; then
    groupadd --system tapebox
    echo "  Created group: tapebox"
else
    echo "  Group already exists: tapebox"
fi

usermod -aG tapebox "$RUN_USER"

echo "  Added $RUN_USER to tapebox group"

echo
echo "[4/10] Creating TapeBox directories..."

mkdir -p \
    "$DATA_DIR/backups" \
    "$DATA_DIR/manifests" \
    "$DATA_DIR/state" \
    "$LOG_DIR" \
    "$MOUNT_ROOT/staging" \
    "$MOUNT_ROOT/ltfs" \
    "$MOUNT_ROOT/ltfs-inspect" \
    "$MOUNT_ROOT/restored"

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
echo "[5/10] Installing hardware access rules..."

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
echo "Installing restricted SCSI rescan sudo rule..."

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
echo "[6/10] Creating Python virtual environment..."

rm -rf "$APP_DIR/venv"

python3 -m venv "$APP_DIR/venv"

"$APP_DIR/venv/bin/python" -m pip install --upgrade pip
"$APP_DIR/venv/bin/python" -m pip install \
    -r "$APP_DIR/requirements.txt"

echo
echo "Verifying Python packages..."

"$APP_DIR/venv/bin/python" - <<'PY'
from importlib.metadata import version
import gunicorn

print("  Flask:", version("flask"))
print("  Gunicorn:", gunicorn.__version__)
PY

echo
echo "Installing TapeBox updater command..."

cat > /usr/local/bin/tapebox-updater <<EOF_UPDATER
#!/bin/sh
cd "$APP_DIR" || exit 1
exec "$APP_DIR/venv/bin/python" -m tapebox.updater_cli "\$@"
EOF_UPDATER

chown root:root /usr/local/bin/tapebox-updater
chmod 0755 /usr/local/bin/tapebox-updater

echo "  Installed: /usr/local/bin/tapebox-updater"

echo
echo "[7/10] Creating systemd service..."

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

echo "  Installed: $SERVICE_FILE"

echo
echo "[8/10] Initializing TapeBox database..."

cd "$APP_DIR"

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
    "$APP_DIR/venv/bin/python" \
    -c 'from tapebox.database import initialize_database; initialize_database(); print("TapeBox database initialized.")'

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
echo "[9/10] Enabling and starting TapeBox..."

systemctl enable tapebox
systemctl restart tapebox

echo
echo "Waiting for TapeBox to start..."

TAPEBOX_READY=0

for attempt in {1..20}; do
    if curl \
        --silent \
        --fail \
        --max-time 3 \
        http://127.0.0.1:8080/ \
        >/dev/null 2>&1
    then
        TAPEBOX_READY=1
        break
    fi

    sleep 1
done

if [[ "$TAPEBOX_READY" -ne 1 ]]; then
    echo
    echo "ERROR: TapeBox did not answer on port 8080."
    echo
    systemctl --no-pager --full status tapebox || true
    echo
    journalctl -u tapebox -n 80 --no-pager || true
    exit 1
fi

echo "  TapeBox web server is responding."

echo
echo "[10/10] Final verification..."

echo
echo "LTFS:"
ltfs --version 2>&1 | head -2 || true

echo
echo "TapeBox service:"
systemctl is-enabled tapebox
systemctl is-active tapebox

echo
echo "TapeBox updater:"
/usr/local/bin/tapebox-updater --help >/dev/null
echo "  /usr/local/bin/tapebox-updater -> OK"

echo
echo "HTTP:"
HTTP_STATUS="$(
    curl \
        --silent \
        --output /dev/null \
        --write-out '%{http_code}' \
        http://127.0.0.1:8080/
)"

echo "  http://127.0.0.1:8080/ -> $HTTP_STATUS"

if [[ "$HTTP_STATUS" != "200" ]]; then
    echo "ERROR: TapeBox returned HTTP $HTTP_STATUS."
    exit 1
fi

echo
echo "Database:"
ls -l "$DATA_DIR/catalog.db"

echo
echo "========================================"
echo " TapeBox installation successful"
echo "========================================"
echo
echo "Web interface:"
echo
echo "  http://SERVER-IP:8080"
echo
echo "Service commands:"
echo
echo "  sudo systemctl status tapebox"
echo "  sudo systemctl restart tapebox"
echo "  sudo journalctl -u tapebox -f"
echo
echo "Updater commands:"
echo
echo "  tapebox-updater status"
echo "  tapebox-updater check"
echo "  tapebox-updater update vX.Y.Z"
echo "  tapebox-updater rollback"
echo
echo "IMPORTANT:"
echo "Log out and back in before manually accessing tape"
echo "devices so your new tapebox group membership applies."
echo
