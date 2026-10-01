#!/usr/bin/env bash
# ============================================================
#  setup.sh — the one command to run after `git clone`.
#
#      git clone <repo> TrioForge && cd TrioForge && ./setup.sh
#
#  It creates the venv, installs EVERY dependency (the Flask web app and the
#  `forge` terminal client), puts `forge` / `trioforge` on your PATH, and
#  pre-fetches llama.cpp so a local model works with no further setup.
#
#  Safe to re-run - it installs only what is missing. Afterwards a plain
#  `git pull` is enough: ./forge re-installs by itself when a requirement
#  changes, so you never have to come back here.
#
#  Env:
#    TRIOFORGE_SKIP_LLAMA=1   skip the llama.cpp download
#    TRIOFORGE_SKIP_LINK=1    do not touch your PATH
# ============================================================
set -euo pipefail

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "$SELF")" && pwd)"
cd "$ROOT"

echo "TrioForge setup"
echo "  repo: $ROOT"
echo

# ---------------------------------------------------------------- python
PY_BASE="$(command -v python3 || command -v python || true)"
if [ -z "$PY_BASE" ]; then
    echo "ERROR: python3 was not found." >&2
    case "$(uname -s)" in
        Linux)  echo "       sudo apt install python3 python3-venv python3-pip" >&2 ;;
        Darwin) echo "       brew install python" >&2 ;;
    esac
    exit 1
fi
if ! "$PY_BASE" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "ERROR: python 3.10+ is required; found $("$PY_BASE" -V 2>&1)." >&2
    exit 1
fi
echo "  python: $("$PY_BASE" -V 2>&1 | cut -d' ' -f1-2)"

# ---------------------------------------------------------------- dependencies
# Same code path ./forge uses on every launch, so the two cannot disagree.
# shellcheck source=deps.sh
. "$ROOT/deps.sh"
forge_ensure_deps "$ROOT"

# ---------------------------------------------------------------- PATH + llama
if [ "${TRIOFORGE_SKIP_LINK:-0}" != "1" ] && [ -x "$ROOT/install.sh" ]; then
    echo
    "$ROOT/install.sh"
fi

echo
echo "Done. Run:  trioforge"
