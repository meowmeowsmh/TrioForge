#!/bin/sh
# TrioForge launcher for Linux / macOS / WSL.
# Usage: ./run.sh [path] [--no-install]
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
    PY=python3
else
    PY=python
fi

exec "$PY" py/tools/launcher.py "$@"
