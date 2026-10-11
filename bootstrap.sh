#!/usr/bin/env bash
# ============================================================
#  bootstrap.sh — one-line installer for TrioForge (Linux / macOS / WSL).
#
#      curl -fsSL https://raw.githubusercontent.com/meowmeowsmh/TrioForge/main/bootstrap.sh | sh
#
#  Downloads the repo as an archive (no git), then hands off to ./run.sh,
#  which does the rest (Python, venv, dependencies, llama.cpp). Re-running the
#  same line just launches the already-installed copy — no cd, no git pull.
#
#  TRIOFORGE_DIR=...   install somewhere other than ~/TrioForge
# ============================================================
set -euo pipefail

REPO="https://github.com/meowmeowsmh/TrioForge"
TARBALL="${REPO}/archive/refs/heads/main.tar.gz"
DIR="${TRIOFORGE_DIR:-$HOME/TrioForge}"

if [ -f "$DIR/run.sh" ]; then
    echo "[trio] already installed at $DIR - launching"
    cd "$DIR"
    exec ./run.sh "$@"
fi

echo "[trio] installing to $DIR (no git needed)"
mkdir -p "$DIR"
echo "[trio] downloading TrioForge..."
curl -fsSL "$TARBALL" | tar xz --strip-components=1 -C "$DIR"

cd "$DIR"
echo "[trio] starting TrioForge -> http://localhost:5003"
exec ./run.sh "$@"
