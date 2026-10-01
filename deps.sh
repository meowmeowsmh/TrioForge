#!/usr/bin/env bash
# =============================================================================
#  deps.sh — "make this checkout runnable", shared by ./forge and ./setup.sh.
#
#  SOURCED, never executed:
#      . "$ROOT/deps.sh"
#      forge_ensure_deps "$ROOT"
#
#  Why it exists: a fresh `git clone` has no venv and no packages, and a
#  `git pull` can add a dependency. Making people remember to re-run an
#  installer after every pull is how you get ModuleNotFoundError on first run.
#  forge calls this on every launch, and it does nothing unless something is
#  actually missing.
#
#  The up-to-date check is pure bash — one hash of the requirement files — so a
#  normal start pays ~10ms and not a python interpreter's worth of startup.
# =============================================================================

# The hash the app records in .deps_installed, reproduced exactly:
# py/tools/updater.deps_fingerprint() hashes each name, then its bytes, then NUL.
forge_deps_hash() {
    local root="$1" name
    for name in requirements.txt requirements-ml.txt pyproject.toml uv.lock; do
        printf '%s' "$name"
        [ -f "$root/$name" ] && cat "$root/$name"
        printf '\0'
    done | _forge_sha256
}

_forge_sha256() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum | cut -d' ' -f1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 | cut -d' ' -f1
    else
        # No hasher: claim the deps are current rather than reinstalling every
        # single launch. Wrong in the safe direction.
        printf 'unknown'
    fi
}

# The checkout's venv python, or nothing.
forge_venv_python() {
    local root="$1" p
    for p in "$root/.venv-linux/bin/python" \
             "$root/.venv/bin/python" \
             "$root/.venv/bin/python3"; do
        [ -x "$p" ] && { printf '%s' "$p"; return 0; }
    done
    return 1
}

# True when .deps_installed matches the current requirement files.
forge_deps_current() {
    local root="$1" marker="$1/.deps_installed" recorded
    [ -f "$marker" ] || return 1
    recorded="$(sed -n 's/^sha256:\(.*\)$/\1/p' "$marker" | head -1)"
    [ -n "$recorded" ] || return 1
    [ "$recorded" = "$(forge_deps_hash "$root")" ]
}

forge_mark_deps() {
    printf 'sha256:%s\n' "$(forge_deps_hash "$1")" > "$1/.deps_installed" 2>/dev/null || true
}

# Create .venv-linux when there is no venv at all. Echoes the python path.
forge_make_venv() {
    local root="$1" base
    base="$(command -v python3 || command -v python || true)"
    [ -n "$base" ] || return 1
    "$base" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' || return 1
    echo "forge: creating a virtual environment (.venv-linux)..." >&2
    "$base" -m venv "$root/.venv-linux" >/dev/null 2>&1 || return 1
    forge_venv_python "$root"
}

# pip install -r requirements.txt, degrading gracefully: pip -> uv -> ensurepip.
forge_pip_install() {
    local root="$1" py="$2"
    if "$py" -m pip --version >/dev/null 2>&1; then
        "$py" -m pip install -q --disable-pip-version-check -r "$root/requirements.txt"
        return $?
    fi
    if command -v uv >/dev/null 2>&1; then
        uv pip install --python "$py" -q -r "$root/requirements.txt"
        return $?
    fi
    echo "forge: this venv has no pip - bootstrapping it with ensurepip" >&2
    "$py" -m ensurepip --upgrade >/dev/null 2>&1 || true
    if "$py" -m pip --version >/dev/null 2>&1; then
        "$py" -m pip install -q --disable-pip-version-check -r "$root/requirements.txt"
        return $?
    fi
    return 1
}

# The whole job. Returns 0 when the checkout is ready to run.
#
#   TRIOFORGE_QUIET_DEPS=1   only speak up when installing
#   TRIOFORGE_SKIP_DEPS=1    do not call this at all (handled by the caller)
forge_ensure_deps() {
    local root="$1" py

    py="$(forge_venv_python "$root")" || py="$(forge_make_venv "$root")" || {
        echo "forge: no usable python3 (3.10+) found." >&2
        echo "       Install Python 3, then run ./setup.sh" >&2
        return 1
    }

    if forge_deps_current "$root"; then
        return 0                     # fast path: nothing has changed
    fi

    echo "forge: installing dependencies (first run, or requirements changed)..."
    if ! forge_pip_install "$root" "$py"; then
        echo "forge: dependency install FAILED." >&2
        echo "       Retry by hand:  \"$py\" -m pip install -r requirements.txt" >&2
        return 1
    fi
    if ! "$py" -c 'import rich, prompt_toolkit, textual' >/dev/null 2>&1; then
        echo "forge: install reported success but the imports are still missing." >&2
        echo "       Try:  ./setup.sh" >&2
        return 1
    fi
    forge_mark_deps "$root"
    echo "forge: dependencies ready"
    return 0
}
