#!/usr/bin/env bash
# ============================================================
#  install.sh — put the `forge` (and `trioforge`) command on your PATH.
#
#      ./install.sh
#
#  After this you can run `trioforge` from any directory. It links the
#  launcher into the first writable directory already on your PATH, and falls
#  back to ~/.local/bin. Works on Linux and macOS.
#
#  Safe to re-run: it only ever (re)creates two symlinks.
# ============================================================
set -euo pipefail

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "$SELF")" && pwd)"
LAUNCHER="$ROOT/forge"

echo "TrioForge terminal client"
echo "  repo: $ROOT"

# ---------------------------------------------------------------- pick a bin dir
# Prefer the conventional user bin dirs when they are already on PATH. Only
# dirs under $HOME are considered, so this never needs sudo.
BIN=""
for d in "$HOME/.local/bin" "$HOME/bin" "$HOME/.bin"; do
    case ":${PATH:-}:" in
        *":$d:"*) [ -d "$d" ] && [ -w "$d" ] && BIN="$d" && break ;;
    esac
done
if [ -z "$BIN" ]; then
    IFS=':' read -r -a PARTS <<< "${PATH:-}"
    for d in "${PARTS[@]}"; do
        # never drop launchers into a package manager's scratch dir
        case "$d" in
            *node_modules*|*/.npm/*) continue ;;
            "$HOME"/*) [ -d "$d" ] && [ -w "$d" ] && BIN="$d" && break ;;
        esac
    done
fi
[ -n "$BIN" ] || BIN="$HOME/.local/bin"
mkdir -p "$BIN"

# ---------------------------------------------------------------- deps
# ONE implementation, in deps.sh, shared with ./forge and ./setup.sh - this
# script used to carry its own copy of the pip/uv/ensurepip dance, which is how
# three installers end up disagreeing about what "installed" means.
# shellcheck source=deps.sh
. "$ROOT/deps.sh"

if [ -z "$(forge_venv_python "$ROOT" 2>/dev/null || true)" ]; then
    echo "  deps: no virtual environment yet."
    echo "        ./setup.sh creates one and installs everything (recommended),"
    echo "        or run ./run.sh once for the web app."
elif forge_ensure_deps "$ROOT"; then
    deps_ok=1
    echo "  deps: present (requirements.txt)"
else
    deps_ok=0
    echo "" >&2
    echo "ERROR: could not install the dependencies into:" >&2
    echo "       $(forge_venv_python "$ROOT")" >&2
    echo "" >&2
    echo "  Nothing was linked, on purpose: a 'trioforge' that exists and then dies" >&2
    echo "  with ModuleNotFoundError: textual is worse than no launcher at all." >&2
    echo "" >&2
    echo "  Fix it, then re-run this script:" >&2
    echo "      ./setup.sh" >&2
    exit 1
fi

# ---------------------------------------------------------------- link it
chmod +x "$LAUNCHER"
ln -sfn "$LAUNCHER" "$BIN/forge"
ln -sfn "$LAUNCHER" "$BIN/trioforge"
echo "  linked: $BIN/forge -> $LAUNCHER"
echo "          $BIN/trioforge -> $LAUNCHER"

# ---------------------------------------------------------------- llama.cpp
# So a local model works with no manual setup. It is also fetched on first use,
# so this is a convenience, not a requirement; TRIOFORGE_SKIP_LLAMA=1 skips it.
if [ "${TRIOFORGE_SKIP_LLAMA:-0}" = "1" ]; then
    echo "  llama: skipped (TRIOFORGE_SKIP_LLAMA=1)"
elif [ -n "$(find "$ROOT/tools/llama.cpp" -name 'llama-server*' -type f 2>/dev/null | head -1)" ]; then
    echo "  llama: already present under tools/llama.cpp"
elif [ -x "$PY" ]; then
    echo "  llama: downloading the prebuilt build for this machine (once)..."
    if PYTHONPATH="$ROOT/py" "$PY" -c \
        'import llama_installer as L; r=L.install_llamacpp(); print("        ", r["path"] if r["ok"] else "skipped: "+r["error"]); raise SystemExit(0 if r["ok"] else 1)'; then
        echo "  llama: ready"
    else
        echo "  llama: could not fetch it now — it will be fetched on first use"
    fi
else
    echo "  llama: will be fetched on first use"
fi

# ---------------------------------------------------------------- PATH check
case ":${PATH:-}:" in
    *":$BIN:"*) ;;
    *)
        echo
        echo "NOTE: $BIN is not on your PATH yet. Add it once:"
        case "${SHELL:-}" in
            */zsh)  PROFILE="$HOME/.zshrc" ;;
            */bash) PROFILE="$HOME/.bashrc" ;;
            *)      PROFILE="your shell profile" ;;
        esac
        echo "      echo 'export PATH=\"$BIN:\$PATH\"' >> $PROFILE"
        ;;
esac

echo
echo "Done. Try:  trioforge --version"
