#!/bin/sh
# TrioForge launcher for Linux / macOS / WSL — FULLY automatic first-run setup.
#
#  1. find python3 (or python), installing it via the OS package manager if missing
#  2. create the project venv (.venv-linux) if absent
#  3. install ALL core dependencies into it (flask, flask-compress, psutil,
#     frontmatter, providers, etc.) — no manual `pip install` at all
#  4. optionally install the heavy ML/embedding stack (torch + CUDA, ~2 GB)
#     with `--ml` or TRIOFORGE_ML=1 — only if you want semantic RAG
#  5. run the app with the venv python so flask & friends are always found
#
# GGUF models are NOT pip-installable: just drop them into models/,
# video_model/ or universal_models_to_text/ and they appear automatically.
#
# Usage: ./run.sh [--ml] [path] [--no-install] [--menu]

cd "$(dirname "$0")"

# ---- flags ---------------------------------------------------------------
INSTALL_ML=0
for arg in "$@"; do
    case "$arg" in
        --ml) INSTALL_ML=1 ;;
    esac
done
[ "$TRIOFORGE_ML" = "1" ] && INSTALL_ML=1

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
    "$PY" -m venv "$VENV" || { echo "[TrioForge] Could not create venv (install python3-venv)."; echo "[TrioForge] On Debian/Ubuntu: sudo apt install python3-venv"; exit 1; }
fi

# Install deps if ANY critical module can't be imported — not just flask.
# A fresh venv where someone installed only `flask` would otherwise skip this
# and then crash on `import flask_compress` / `import psutil` / `import frontmatter`.
REQUIRED_MODULES="flask flask_compress requests psutil frontmatter"
MISSING=0
for m in $REQUIRED_MODULES; do
    if ! "$VENV/bin/python" -c "import $m" >/dev/null 2>&1; then
        MISSING=1
        break
    fi
done

if [ "$MISSING" -eq 1 ]; then
    echo "[TrioForge] Installing core dependencies into $VENV (first run)..."
    "$VENV/bin/python" -m pip install --upgrade pip >/dev/null 2>&1
    "$VENV/bin/python" -m pip install -r requirements.txt || {
        echo "[TrioForge] Dependency install failed. Retry, or run:"
        echo "            $VENV/bin/python -m pip install -r requirements.txt"
        exit 1
    }
    echo "[TrioForge] Core dependencies installed."
fi

# Optional heavy ML/embedding stack (torch ~2 GB). Only if requested.
# NOTE: CUDA does not exist on macOS — Apple Silicon uses Metal (MPS), and PyPI's
# `torch` wheel for macOS ships MPS support automatically. So the wording and the
# accelerator differ per OS; the install command is the same either way.
if [ "$INSTALL_ML" = "1" ]; then
    if [ -f requirements-ml.txt ]; then
        if "$VENV/bin/python" -c "import sentence_transformers" >/dev/null 2>&1; then
            echo "[TrioForge] ML/embedding stack already installed."
        else
            if [ "$(uname -s)" = "Darwin" ]; then
                echo "[TrioForge] Installing ML/embedding stack (~2 GB, torch + Apple Metal/MPS)... this can take a while."
            else
                echo "[TrioForge] Installing ML/embedding stack (~2 GB, torch; CUDA on NVIDIA, CPU otherwise)... this can take a while."
            fi
            "$VENV/bin/python" -m pip install -r requirements-ml.txt || {
                echo "[TrioForge] ML install failed (optional). Semantic RAG will use keyword search."
            }
        fi
    else
        echo "[TrioForge] requirements-ml.txt not found — skipping."
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
echo "[TrioForge] (Models: drop .gguf files into models/, video_model/ or universal_models_to_text/)"
exec "$VENV/bin/python" py/tools/launcher.py --no-install "$@"
