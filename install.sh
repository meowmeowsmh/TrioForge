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
# The terminal client needs these; they are NOT part of requirements.txt,
# which only covers the Flask web app.
PY="$ROOT/.venv-linux/bin/python"
[ -x "$PY" ] || PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$ROOT/.venv/bin/python3"
if [ -x "$PY" ]; then
    # A venv made by uv contains NO pip, and this script runs under `set -e`, so
    # `python -m pip` used to abort the whole install with a bare "No module named
    # pip" and no guidance. Try what the interpreter really has, verify the imports,
    # and never link a launcher that cannot run.
    if "$PY" -c "import rich, prompt_toolkit, textual" 2>/dev/null; then
        echo "  deps: already present (rich + prompt_toolkit + textual)"
        deps_ok=1
    else
        deps_ok=0
        if "$PY" -m pip --version >/dev/null 2>&1; then
            echo "  deps: pip install rich + prompt_toolkit + textual"
            "$PY" -m pip install -q --disable-pip-version-check rich prompt_toolkit textual || true
        else
            echo "  deps: this venv has no pip (uv venvs ship without one)"
        fi
        if ! "$PY" -c "import rich, prompt_toolkit, textual" 2>/dev/null \
           && command -v uv >/dev/null 2>&1; then
            echo "  deps: uv pip install rich + prompt_toolkit + textual"
            uv pip install --python "$PY" -q rich prompt_toolkit textual || true
        fi
        if ! "$PY" -c "import rich, prompt_toolkit, textual" 2>/dev/null; then
            echo "  deps: bootstrapping pip with ensurepip, then installing"
            "$PY" -m ensurepip --upgrade >/dev/null 2>&1 || true
            "$PY" -m pip install -q --disable-pip-version-check rich prompt_toolkit textual || true
        fi
        if "$PY" -c "import rich, prompt_toolkit, textual" 2>/dev/null; then
            deps_ok=1
            echo "  deps: installed"
        fi
    fi
    if [ "$deps_ok" -ne 1 ]; then
        echo "" >&2
        echo "ERROR: could not install rich / prompt_toolkit / textual into:" >&2
        echo "       $PY" >&2
        echo "" >&2
        echo "  Nothing was linked, on purpose: a 'trioforge' that exists and then dies" >&2
        echo "  with ModuleNotFoundError: textual is worse than no launcher at all." >&2
        echo "" >&2
        echo "  Install them by hand, then re-run this script:" >&2
        echo "      uv pip install --python \"$PY\" rich prompt_toolkit textual" >&2
        exit 1
    fi
else
    echo "  deps: no venv found yet — run ./run.sh once, or:"
    echo "        python3 -m pip install rich prompt_toolkit textual"
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
