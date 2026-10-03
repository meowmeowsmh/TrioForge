"""Make this checkout runnable before forge.cmd starts the terminal client.

The Windows twin of deps.sh's forge_ensure_deps. Without it, forge.cmd ran
whatever Python it found: after a `git pull` that added a dependency (textual,
duckdb, ...) it died with ModuleNotFoundError on every machine except the one
whose venv happened to have been updated by hand.

Does nothing (one hash of the requirement files) unless the project has no
.venv or .deps_installed no longer matches requirements.txt & co. Then it
creates .venv and installs into it with the same code TrioForge.bat uses.

Exit status 0 means .venv is ready.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import launcher  # noqa: E402 - stdlib-only, safe to import
import updater   # noqa: E402

TUI_IMPORTS = "import rich, prompt_toolkit, textual, httpx"


def _runs(python: str) -> bool:
    try:
        return subprocess.run([python, "-c", "pass"], capture_output=True,
                              timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def main() -> int:
    project = Path(__file__).resolve().parents[2]
    venv = launcher.project_venv_python(project)
    if venv and not _runs(venv):
        # A .venv copied from another PC (a zip of the folder) points at a
        # Python that only exists there. Set it aside and build a fresh one.
        broken = project / ".venv-broken-{}".format(int(time.time()))
        print("forge: .venv does not run on this machine (copied from another "
              "PC?) - moving it to {} and rebuilding.".format(broken.name),
              file=sys.stderr)
        (project / ".venv").rename(broken)
        venv = None
    if venv and not updater.deps_changed(project):
        return 0
    print("forge: installing dependencies (first run, or requirements changed)...",
          file=sys.stderr)
    launcher.install_deps(project)          # exits non-zero itself on failure
    venv = launcher.project_venv_python(project)
    if not venv:
        print("forge: could not create .venv - run TrioForge.bat once.", file=sys.stderr)
        return 1
    if subprocess.run([venv, "-c", TUI_IMPORTS]).returncode != 0:
        print("forge: install finished but the terminal client's packages are "
              "still missing.\n       Try: \"{}\" -m pip install -r requirements.txt"
              .format(venv), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
