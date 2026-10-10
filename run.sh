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
# Take the NEWEST python3.X on the machine, not whatever `python3` happens to
# alias to. On a distro that still points python3 at 3.12 while 3.14 is
# installed, the alias quietly decides the interpreter for the whole install;
# asking for the newest by name is what actually uses it.
#
# TRIOFORGE_PYTHON overrides the choice (TRIOFORGE_PYTHON=python3.12 ./run.sh).
PY="${TRIOFORGE_PYTHON:-}"

pick_python() {
    # Newest first. 3.15/3.16 are listed ahead of time on purpose: they become
    # the default the day they appear on PATH, with no edit needed here.
    for cand in python3.16 python3.15 python3.14 python3.13 python3.12 \
                python3.11 python3.10 python3 python; do
        if command -v "$cand" >/dev/null 2>&1; then
            echo "$cand"
            return 0
        fi
    done
    return 1
}

if [ -n "$PY" ]; then
    if ! command -v "$PY" >/dev/null 2>&1; then
        echo "[TrioForge] TRIOFORGE_PYTHON=$PY is not on PATH." >&2
        exit 1
    fi
else
    PY="$(pick_python)" || PY=""
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

    PY="$(pick_python)" || PY=""
    [ -z "$PY" ] && { echo "[TrioForge] Installed but not on PATH; open a NEW terminal and re-run."; exit 1; }
fi

# Enforce the floor pyproject.toml declares (requires-python >=3.10). Without
# this a 3.9 fails much later, inside a dependency, with a message that says
# nothing about Python being the reason.
if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    echo "[TrioForge] $PY is $("$PY" --version 2>&1 | awk '{print $2}'), but TrioForge needs Python 3.10 or newer." >&2
    echo "[TrioForge] Install a newer Python, or point TRIOFORGE_PYTHON at one." >&2
    exit 1
fi

echo ""
echo "[TrioForge] Using Python: $PY ($("$PY" --version 2>&1 | awk '{print $2}'))"

# ---- 2) Create the venv and install deps into it ---------------------------
# Use a Unix-specific venv folder: the Windows venv lives in .venv/Scripts,
# while a Linux/WSL venv uses .venv/bin — sharing the SAME .venv folder mixes
# the two layouts and breaks both. So we keep them apart.
VENV=".venv-linux"
if [ ! -x "$VENV/bin/python" ]; then
    echo "[TrioForge] Creating virtual environment ($VENV)..."
    "$PY" -m venv "$VENV" || { echo "[TrioForge] Could not create venv (install python3-venv)."; echo "[TrioForge] On Debian/Ubuntu: sudo apt install python3-venv"; exit 1; }
fi

# An EXISTING venv keeps its own interpreter — that is the whole point of one — so
# an install first created under 3.12 stays on 3.12 even once 3.14 is present. Say
# so, with the one command that changes it, or the newest-first choice above looks
# like it silently did nothing.
NEWEST="$(pick_python)" || NEWEST=""
NEWEST_VER=""
[ -n "$NEWEST" ] && NEWEST_VER="$("$NEWEST" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)"
VENV_VER="$("$VENV/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)"
if [ -n "$NEWEST_VER" ] && [ -n "$VENV_VER" ] && [ "$NEWEST_VER" != "$VENV_VER" ]; then
    echo "[TrioForge] Note: $VENV runs Python $VENV_VER, and $NEWEST ($NEWEST_VER) is also installed."
    echo "[TrioForge]       To rebuild it on $NEWEST_VER:  rm -rf $VENV && ./run.sh"
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
    # Record the manifest fingerprint so the launcher knows these deps are current
    # and only reinstalls when requirements actually change (e.g. after an update).
    "$VENV/bin/python" - <<'PY' 2>/dev/null || true
import pathlib, sys
sys.path.insert(0, str(pathlib.Path("py/tools").resolve()))
import updater
updater.write_deps_marker(pathlib.Path(".").resolve())
PY
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
# No --no-install here: the launcher reinstalls only when requirements/lock files
# changed since the last install — which is exactly what an auto-update needs.
#
# --background-update: the same flag TrioForge.bat (Windows) and autostart already
# pass. The launcher checks for and APPLIES updates as a hidden process, so the app is
# on screen immediately and the new code lands on the next launch — no prompt, no
# manual `git pull`. Without it, run.sh did the same update synchronously, blocking
# the start while git ran. Appended after "$@" so an explicit --no-update still wins.
exec "$VENV/bin/python" py/tools/launcher.py "$@" --background-update
