#!/bin/sh
# TrioForge launcher for Linux / macOS / WSL — auto-installs Python3 + deps if missing.
#
#  1. find python3 (or python), installing the latest via the OS package manager
#     if neither exists
#  2. create the project venv (.venv) if absent, and install requirements into it
#  3. run the launcher WITH the venv python so `flask` and friends are always found
#
# No manual downloading required. Usage: ./run.sh [path] [--no-install] [--menu]

cd "$(dirname "$0")"

# ---- 1) Locate or install Python 3 ----------------------------------------
PY=""

if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
fi

if [ -z "$PY" ]; then
    echo ""
    echo "[TrioForge] Python 3 was not found. Installing the latest automatically..."
    echo ""

    if command -v apt-get >/dev/null 2>&1; then
        echo "[TrioForge] apt-get detected — installing python3 (sudo may prompt)..."
        sudo apt-get update && sudo apt-get install -y python3 python3-pip python3-venv
    elif command -v dnf >/dev/null 2>&1; then
        echo "[TrioForge] dnf detected — installing python3 (sudo may prompt)..."
        sudo dnf install -y python3 python3-pip
    elif command -v pacman >/dev/null 2>&1; then
        echo "[TrioForge] pacman detected — installing python (sudo may prompt)..."
        sudo pacman -Sy --noconfirm python python-pip
    elif command -v brew >/dev/null 2>&1; then
        echo "[TrioForge] Homebrew detected — installing python..."
        brew install python
    else
        echo "[TrioForge] No package manager detected (apt/dnf/pacman/brew)."
        echo "[TrioForge] Install Python 3 from https://www.python.org/downloads/ then re-run."
        exit 1
    fi

    if command -v python3 >/dev/null 2>&1; then
        PY=python3
    elif command -v python >/dev/null 2>&1; then
        PY=python
    fi
    [ -z "$PY" ] && { echo "[TrioForge] Installed but not on PATH; open a NEW terminal and re-run."; exit 1; }
fi

echo ""
echo "[TrioForge] Using Python: $PY"

# ---- 2) Create the venv and install deps into it ---------------------------
# Use a Unix-specific venv folder: the Windows venv lives in .venv/Scripts,
# while a Linux/WSL venv uses .venv/bin — sharing the SAME .venv folder mixes
# the two layouts and breaks both. So we keep them apart.
VENV=".venv-linux"
if [ ! -x "$VENV/bin/python" ]; then
    echo "[TrioForge] Creating virtual environment ($VENV)..."
    "$PY" -m venv "$VENV" || { echo "[TrioForge] Could not create venv (install python3-venv)."; exit 1; }
fi

# Install deps if flask isn't actually importable — a real check, not a stale
# marker file (a marker from a previous Windows run would otherwise skip this
# and leave a freshly-created venv empty).
if [ -f requirements.txt ]; then
    if ! "$VENV/bin/python" -c "import flask" >/dev/null 2>&1; then
        echo "[TrioForge] Installing dependencies into $VENV (first run)..."
        "$VENV/bin/python" -m pip install --upgrade pip >/dev/null 2>&1
        "$VENV/bin/python" -m pip install -r requirements.txt || {
            echo "[TrioForge] Dependency install failed. Retry, or run:"
            echo "            $VENV/bin/python -m pip install -r requirements.txt"
            exit 1
        }
        echo "[TrioForge] Dependencies installed."
    fi
fi

# ---- 3) Launch with the venv python (always has flask) ---------------------
# Port: default 5003 (avoids colliding with a stale Docker container/wslrelay on
# 5001). Override with TRIOFORGE_PORT=xxxx ./run.sh
if [ -z "$TRIOFORGE_PORT" ]; then
    export TRIOFORGE_PORT=5003
fi
echo "[TrioForge] Port: $TRIOFORGE_PORT"
echo "[TrioForge] Launching TrioForge..."
exec "$VENV/bin/python" py/tools/launcher.py --no-install "$@"
