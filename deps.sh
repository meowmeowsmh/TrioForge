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

# A python 3.10+ anywhere on PATH, or nothing.
#
# Every PATH entry is tried, not just `command -v python3`: an old python3 that
# shadows a good one earlier in PATH would otherwise look like "no python" and
# trigger an apt-get run that fixes nothing. python3 is preferred over python,
# and within each name the first that actually satisfies the version wins.
forge_find_python() {
    local name dir
    for name in python3 python; do
        local IFS=':'
        for dir in ${PATH:-}; do
            [ -n "$dir" ] || continue
            [ -x "$dir/$name" ] || continue
            if "$dir/$name" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
                printf '%s' "$dir/$name"
                return 0
            fi
        done
    done
    return 1
}

# Python 3.10+, INSTALLING it when it is missing or too old, so "no python" is
# not the end of the story. Echoes the interpreter. Mirrors run.sh, which has
# done this for the web app all along.
forge_ensure_python() {
    local base
    if base="$(forge_find_python)"; then
        printf '%s' "$base"
        return 0
    fi

    echo "forge: python 3.10+ not found — installing it (sudo may prompt)..." >&2
    if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update && sudo apt-get install -y python3 python3-pip python3-venv
    elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y python3 python3-pip
    elif command -v pacman >/dev/null 2>&1; then
        sudo pacman -Sy --noconfirm python python-pip
    elif command -v zypper >/dev/null 2>&1; then
        sudo zypper --non-interactive install python3 python3-pip
    elif command -v brew >/dev/null 2>&1; then
        brew install python
    else
        echo "forge: no package manager found (apt/dnf/pacman/zypper/brew)." >&2
        echo "       Install Python 3.10+ from https://www.python.org/downloads/" >&2
        return 1
    fi

    if base="$(forge_find_python)"; then
        printf '%s' "$base"
        return 0
    fi
    echo "forge: python is installed but not on PATH yet — open a NEW terminal." >&2
    return 1
}

# Create .venv-linux when there is no venv at all. Echoes the python path.
forge_make_venv() {
    local root="$1" base
    base="$(forge_ensure_python)" || return 1
    [ -n "$base" ] || return 1
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
    echo "forge: dependencies ready — both interfaces are installed:"
    echo "         terminal client   $FORGE_PROG"
    echo "         web app           ./run.sh    (then http://localhost:5003)"
    return 0
}
