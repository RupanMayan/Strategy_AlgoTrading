#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
#  Nifty Short Straddle — Update & Install Dependencies
#
#  Pulls the latest code from git and installs/upgrades Python dependencies.
#  Run this after every git pull to keep the environment in sync.
#
#  Usage:
#    chmod +x update.sh
#    ./update.sh                  # uses local venv/ by default
#    ./update.sh /path/to/venv   # specify a custom venv path
#
#  For systemd service, restart after update:
#    sudo systemctl restart nifty-straddle
# ══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="${1:-${SCRIPT_DIR}/venv}"

echo "══════════════════════════════════════════════════════════════"
echo "  Nifty Short Straddle — Update"
echo "══════════════════════════════════════════════════════════════"
echo ""

# ── Step 1: Pull latest code ──────────────────────────────────────────────

echo "[1/3] Pulling latest code..."
cd "${SCRIPT_DIR}"
if git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    git pull origin main
    echo "  Done."
else
    echo "  Not a git repo — skipping pull."
fi
echo ""

# ── Step 2: Create or verify venv ─────────────────────────────────────────

echo "[2/3] Checking virtual environment at ${VENV_DIR}..."

if [[ ! -d "${VENV_DIR}" ]]; then
    echo "  Venv not found — creating..."
    python3 -m venv "${VENV_DIR}"
    echo "  Created."
else
    echo "  Venv exists."
fi

PIP_BIN="${VENV_DIR}/bin/pip"

if [[ ! -f "${PIP_BIN}" ]]; then
    echo "  ERROR: ${PIP_BIN} not found. Delete ${VENV_DIR} and re-run."
    exit 1
fi
echo ""

# ── Step 3: Install / upgrade dependencies ────────────────────────────────

echo "[3/3] Installing dependencies from requirements.txt..."
"${PIP_BIN}" install --upgrade pip -q
"${PIP_BIN}" install --upgrade -r "${SCRIPT_DIR}/requirements.txt"
echo ""

# ── Summary ───────────────────────────────────────────────────────────────

echo "══════════════════════════════════════════════════════════════"
echo "  Update complete!"
echo ""
echo "  Venv:    ${VENV_DIR}"
echo "  Python:  $(${VENV_DIR}/bin/python --version)"
echo ""
echo "  Key packages:"
"${PIP_BIN}" show openalgo websockets APScheduler 2>/dev/null \
    | grep -E "^(Name|Version):" \
    | paste - - \
    | awk '{printf "    %-20s %s\n", $2, $4}'
echo ""
echo "  If running as a systemd service:"
echo "    sudo systemctl restart nifty-straddle"
echo "══════════════════════════════════════════════════════════════"
