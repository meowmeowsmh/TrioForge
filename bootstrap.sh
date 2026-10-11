#!/usr/bin/env bash
# ============================================================
#  bootstrap.sh — one-line installer for TrioForge (Linux / macOS / WSL).
#
#      curl -fsSL https://raw.githubusercontent.com/meowmeowsmh/TrioForge/main/bootstrap.sh | sh
#
#  Clones the repo (once), then hands off to the real entry points:
#  ./run.sh (web app) or ./forge (terminal client). Python, the venv,
#  dependencies and llama.cpp are all handled by those, exactly as the README
#  describes. Requires git; no sudo, no admin.
#
#  TRIOFORGE_DIR=...   install somewhere other than ~/TrioForge
# ============================================================
set -euo pipefail

REPO="https://github.com/meowmeowsmh/TrioForge.git"
DIR="${TRIOFORGE_DIR:-$HOME/TrioForge}"

if [ -d "$DIR/.git" ]; then
    echo "[trio] already installed at $DIR - updating"
    git -C "$DIR" pull --ff-only --quiet 2>/dev/null \
        || echo "[trio] update skipped (local changes or no network)"
else
    echo "[trio] installing to $DIR"
    git clone --depth 1 "$REPO" "$DIR"
fi

cd "$DIR"
echo "[trio] starting TrioForge (web app, then http://localhost:5003)"
exec ./run.sh "$@"
