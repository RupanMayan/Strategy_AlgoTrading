#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
#  Nifty Short Straddle — Ubuntu Setup & Service Installer
#  Creates a Python venv, installs dependencies, and sets up a systemd service.
#
#  Usage:
#    chmod +x setup_service.sh
#    sudo ./setup_service.sh
#
#  Commands after install:
#    sudo systemctl start   nifty-straddle   # start the strategy
#    sudo systemctl stop    nifty-straddle   # stop gracefully
#    sudo systemctl restart nifty-straddle   # restart
#    sudo systemctl status  nifty-straddle   # check status
#    journalctl -u nifty-straddle -f         # tail logs
# ══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

SERVICE_NAME="nifty-straddle"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/venv"

# ── Pre-flight checks ────────────────────────────────────────────────────────

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: This script must be run as root (use sudo)."
    exit 1
fi

read -rp "Linux user to run the service [$(logname)]: " RUN_USER
RUN_USER="${RUN_USER:-$(logname)}"

# ── Step 1: Install system dependencies ──────────────────────────────────────

echo ""
echo "[1/4] Installing system packages..."
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip > /dev/null

# ── Step 2: Create venv & install Python dependencies ────────────────────────

echo "[2/4] Setting up Python virtual environment at ${VENV_DIR}..."

if [[ -d "${VENV_DIR}" ]]; then
    read -rp "  Venv already exists. Recreate? [y/N]: " RECREATE
    if [[ "${RECREATE,,}" == "y" ]]; then
        rm -rf "${VENV_DIR}"
        python3 -m venv "${VENV_DIR}"
    fi
else
    python3 -m venv "${VENV_DIR}"
fi

PYTHON_BIN="${VENV_DIR}/bin/python"
PIP_BIN="${VENV_DIR}/bin/pip"

echo "  Installing dependencies from requirements.txt..."
"${PIP_BIN}" install --upgrade pip -q
"${PIP_BIN}" install -r "${SCRIPT_DIR}/requirements.txt" -q
echo "  Done."

# Fix ownership so the service user can access the venv
chown -R "${RUN_USER}:${RUN_USER}" "${VENV_DIR}"

# ── Step 3: Verify .env file ────────────────────────────────────────────────

echo "[3/4] Checking .env file..."
if [[ ! -f "${SCRIPT_DIR}/.env" ]]; then
    if [[ -f "${SCRIPT_DIR}/.env.example" ]]; then
        cp "${SCRIPT_DIR}/.env.example" "${SCRIPT_DIR}/.env"
        chown "${RUN_USER}:${RUN_USER}" "${SCRIPT_DIR}/.env"
        chmod 600 "${SCRIPT_DIR}/.env"
        echo "  Created .env from .env.example — edit it with your keys:"
        echo "    nano ${SCRIPT_DIR}/.env"
    else
        echo "  WARNING: No .env file found. Create one with your API keys before starting."
    fi
else
    chmod 600 "${SCRIPT_DIR}/.env"
    echo "  .env file found."
fi

# ── Step 4: Create systemd service ──────────────────────────────────────────

echo "[4/4] Creating systemd service..."

cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=Nifty Short Straddle — Algo Trading Strategy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${SCRIPT_DIR}
EnvironmentFile=${SCRIPT_DIR}/.env
ExecStart=${PYTHON_BIN} main.py
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"

echo ""
echo "════════════════════════════════════════════════════════════════"
echo "  Setup complete!"
echo ""
echo "  Venv:          ${VENV_DIR}"
echo "  Service:       ${SERVICE_NAME}"
echo ""
echo "  Start now:     sudo systemctl start ${SERVICE_NAME}"
echo "  Check status:  sudo systemctl status ${SERVICE_NAME}"
echo "  View logs:     journalctl -u ${SERVICE_NAME} -f"
echo "  Stop:          sudo systemctl stop ${SERVICE_NAME}"
echo "  Restart:       sudo systemctl restart ${SERVICE_NAME}"
echo "  Uninstall:     sudo systemctl disable ${SERVICE_NAME}"
echo "                 sudo rm ${SERVICE_FILE}"
echo "════════════════════════════════════════════════════════════════"
