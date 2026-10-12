#!/usr/bin/env bash
# ============================================================
#  bootstrap.sh — one-line installer for TrioForge (Linux / macOS / WSL).
#
#      curl -fsSL https://raw.githubusercontent.com/meowmeowsmh/TrioForge/main/bootstrap.sh | sh
#
#  Downloads the repo as an archive (no git), puts `forge`, `trioforge` and
#  `trioforge-web` on your PATH (so both clients work from ANY directory), then
#  hands off to ./run.sh for the web app (Python, venv, deps, llama.cpp).
#  Re-running the same line just launches the already-installed copy.
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
else
    echo "[trio] installing to $DIR (no git needed)"
    mkdir -p "$DIR"
    echo "[trio] downloading TrioForge..."
    curl -fsSL "$TARBALL" | tar xz --strip-components=1 -C "$DIR"
    cd "$DIR"
fi

# Put all three commands on the PATH, so they work from any directory rather than
# only this folder. install.sh only (re)creates symlinks and is safe to re-run.
#
# The guard checks EVERY name, not just `trioforge`: checking only that one meant
# an existing install never picked up `trioforge-web` when it was added, because
# `trioforge` was already on PATH and the installer was skipped.
# llama.cpp is skipped here - it is fetched automatically on first local use.
if [ -x "$DIR/install.sh" ]; then
    if ! command -v trioforge >/dev/null 2>&1 || ! command -v trioforge-web >/dev/null 2>&1; then
        echo "[trio] adding 'forge', 'trioforge' and 'trioforge-web' to your PATH"
        TRIOFORGE_SKIP_LLAMA=1 "$DIR/install.sh"
    fi
fi

echo "[trio] starting TrioForge -> http://localhost:5003"
exec ./run.sh "$@"
