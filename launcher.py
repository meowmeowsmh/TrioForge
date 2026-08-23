#!/usr/bin/env python3
"""
TrioForge — cross-platform launcher.

One entry point that runs on Windows, Linux, macOS, and WSL.
`application.bat` and `run.sh` are thin wrappers that call this script
and open an interactive menu so the user can pick their environment.

Usage:
    python launcher.py [path] [--menu] [--install] [--no-install]

Options:
    path         optional path to the TrioForge project folder
    --menu       show the interactive environment menu
    --install    force (re)install dependencies
    --no-install skip dependency installation
    --unix       (internal) run natively; used when launched from WSL/bash
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterator, List, Optional

# Core files that must exist together for a folder to be a TrioForge project.
REQUIRED_FILES = (
    "app.py",
    "cork_board.py",
    "notes.py",
    "llm_providers.py",
    "zoompicleftandright.py",
    "common.py",
)
DEPS_MARKER = ".deps_installed"

BANNER = r""" _____     _       _____                    
|_   _| __(_) ___ |  ___|__  _ __ __ _  ___ 
  | || '__| |/ _ \| |_ / _ \| '__/ _` |/ _ \
  | || |  | | (_) |  _| (_) | | | (_| |  __/
  |_||_|  |_|\___/|_|  \___/|_|  \__, |\___|
                                 |___/      """


def is_project(path: Optional[Path]) -> bool:
    """Return True if `path` contains all the core TrioForge files."""
    return bool(path) and all((path / name).is_file() for name in REQUIRED_FILES)


def _search_roots() -> List[Path]:
    """Candidate folders to search when the project isn't in the obvious spots."""
    roots: List[Path] = [Path.home()]
    if os.name == "nt":
        drive = Path(__file__).resolve().drive
        if drive:
            roots.append(Path(drive + os.sep))
    else:
        roots += [Path("/home"), Path("/opt"), Path("/srv"), Path("/mnt"), Path("/media")]
    return roots


def _bounded_rglob(root: Path, max_depth: int = 5) -> Iterator[Path]:
    """Yield `app.py` files up to `max_depth` levels deep, skipping unreadable dirs."""
    stack = [(root, 0)]
    while stack:
        path, depth = stack.pop()
        if depth > max_depth or not path.is_dir():
            continue
        try:
            entries = sorted(path.iterdir(), key=lambda p: p.name.lower())
        except (PermissionError, OSError):
            continue
        for entry in entries:
            if entry.is_dir():
                stack.append((entry, depth + 1))
            elif entry.name == "app.py":
                yield entry


def find_project(explicit: Optional[str]) -> Optional[Path]:
    """Locate the project: explicit path, current folder, TRIOFORGE_HOME, then a search."""
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if is_project(path):
            return path
        print("Specified path is not a TrioForge project: {}".format(explicit))
        return None

    candidates = [Path.cwd()]
    home = os.environ.get("TRIOFORGE_HOME")
    if home:
        candidates.append(Path(home))
    for candidate in candidates:
        if is_project(candidate):
            return candidate

    print("Project not found in the current folder; searching common locations...")
    for root in _search_roots():
        for app in _bounded_rglob(root):
            if is_project(app.parent):
                return app.parent
    return None


def install_deps(project: Path) -> None:
    """Install requirements.txt once, then write the marker file."""
    req = project / "requirements.txt"
    if not req.is_file():
        print("No requirements.txt found; skipping dependency install.")
        return
    print("Installing dependencies (this may take a while on first run)...")
    result = subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(req)])
    if result.returncode != 0:
        print("Dependency installation failed.")
        print("Install manually with: pip install -r requirements.txt")
        sys.exit(1)
    (project / DEPS_MARKER).touch()
    print("Dependencies installed.")


def run_app(project: Path) -> None:
    """Start the Flask app using the same Python interpreter as this script."""
    print("Project folder: {}".format(project))
    print("Starting TrioForge... open https://localhost:5001 in your browser.")
    result = subprocess.run([sys.executable, "app.py"], cwd=str(project))
    sys.exit(result.returncode)


def prepare_and_run(project: Path, args) -> None:
    """Install deps if needed, then start the app."""
    marker = project / DEPS_MARKER
    if args.install:
        install_deps(project)
    elif not args.no_install and not marker.exists():
        install_deps(project)
    run_app(project)


def run_native(args) -> int:
    """Locate the project and run it with this machine's Python."""
    project = find_project(args.path)
    if project is None:
        print("Could not locate the TrioForge project.")
        print("Run from inside the project folder, pass its path as an argument,")
        print("or set the TRIOFORGE_HOME environment variable.")
        return 1
    prepare_and_run(project, args)
    return 0


def _win_to_wsl_path(p: str) -> str:
    """Convert a Windows path (C:\\foo\\bar) to a WSL path (/mnt/c/foo/bar)."""
    p = p.replace("\\", "/")
    drive = p[0].lower()
    return "/mnt/{}/{}".format(drive, p[3:])


def _run_via_wsl(project: Path, args) -> None:
    """Best-effort launch of the Linux side through Windows Subsystem for Linux."""
    wsl = shutil.which("wsl")
    if not wsl:
        print("WSL is not installed. Install WSL (https://learn.microsoft.com/wsl),")
        print("or choose 'Run on Windows' instead — the app is the same either way.")
        return
    launcher = Path(__file__).resolve()
    linux_launcher = _win_to_wsl_path(str(launcher))
    linux_cwd = _win_to_wsl_path(str(project))
    print("Launching via WSL... (make sure Python and the project dependencies")
    print("are installed inside WSL, or run ./run.sh there.)")
    subprocess.run(
        [wsl, "bash", "-lc",
         "cd '{}' && python3 '{}' --unix".format(linux_cwd, linux_launcher)],
    )


def run_unix(args) -> int:
    """'Linux / macOS / WSL' mode: native on Unix, or via WSL from native Windows."""
    if os.name == "nt":
        project = find_project(args.path)
        if project is None:
            print("Could not locate the TrioForge project.")
            return 1
        _run_via_wsl(project, args)
        return 0
    return run_native(args)


# ── Banner (your exact block-character art, kept verbatim) ──
BANNER_WIDE_ROWS = [
    "▐▄▄▄▄▄▌▐▄▄▄▄▄▌ ▐▄▄▌ ▐▄▄▄▄▄▌  ▐▄▄▄▄▄▌ ▐▄▄▄▄▄▌ ▐▄▄▄▄▄▄▌  ▐▄▄▄▄▄▌  ▐▄▄▄▄▄▌     ▐▄▄▄▄▄▌▐▄▄▌ ▐▄▄▌▐▄▄▄▄▄▄▌▐▄▄▌ ▐▄▄▌▐▄▄▄▄▄▄▌ ▐▄▄▌ ▐▄▄▄▄▄▄▌▐▄▄▄▄▄▄▌▐▄▄▌ ▐▄▄▄▄▄▌    ▐▄▄▄▄▄▄▌  ▐▄▄▄▄▄▌ ▐▄▄▄▄▄▄▌",
    "  ▐██▌  ▐██▌ ▐██▌▐██▌▐██▌ ▐██▌▐██▌    ▐██▌ ▐██▌▐██▌ ▐██▌▐██▌     ▐██▌        ▐██▌    ▐██▌ ▐██▌  ▐██▌  ▐██▌ ▐██▌▐██▌ ▐██▌▐██▌▐██▌       ▐██▌  ▐██▌▐██▌        ▐██▌ ▐██▌▐██▌ ▐██▌  ▐██▌  ",
    "  ▐██▌  ▐██████▌ ▐██▌▐██▌ ▐██▌▐████▌  ▐██▌ ▐██▌▐██████▌ ▐██▌▐███▌▐████▌      ▐████▌  ▐██▌ ▐██▌  ▐██▌  ▐██▌ ▐██▌▐██████▌ ▐██▌ ▐█████▌   ▐██▌  ▐██▌▐██▌        ▐██████▌ ▐██▌ ▐██▌  ▐██▌  ",
    "  ▐▀▀▌  ▐▀▀▌ ▐▀▀▌▐▀▀▌▐▀▀▌ ▐▀▀ ▐▀▀▌    ▐▀▀▌ ▐▀▀ ▐▀▀▌ ▐▀▀▌▐▀▀▌ ▐▀▀ ▐▀▀▌        ▐▀▀▌    ▐▀▀▌ ▐▀▀   ▐▀▀▌  ▐▀▀▌ ▐▀▀ ▐▀▀▌ ▐▀▀▌▐▀▀▌     ▐▀▀▌  ▐▀▀▌  ▐▀▀▌▐▀▀▌        ▐▀▀▌ ▐▀▀ ▐▀▀▌ ▐▀▀   ▐▀▀▌  ",
    "  ▐▄▄▌  ▐▄▄▌ ▐▄▄▌▐▄▄▌ ▐▄▄▄▄▄▌ ▐▄▄▌     ▐▄▄▄▄▄▌ ▐▄▄▌ ▐▄▄▌ ▐▄▄▄▄▄▄▌ ▐▄▄▄▄▄▌    ▐▄▄▌     ▐▄▄▄▄▄▌   ▐▄▄▌   ▐▄▄▄▄▄▌ ▐▄▄▌ ▐▄▄▌▐▄▄▌▐▄▄▄▄▄▄▌   ▐▄▄▌  ▐▄▄▌ ▐▄▄▄▄▄▌    ▐▄▄▄▄▄▄▌  ▐▄▄▄▄▄▌   ▐▄▄▌  ",
]


def _colorize_banner_wide() -> str:
    """Return the banner with ANSI color (white borders, cyan fills)."""
    border, fill, reset = '\x1b[97m', '\x1b[96m', '\x1b[0m'
    out = []
    for row in BANNER_WIDE_ROWS:
        out.append(''.join(
            (border + ch + reset) if ch in ('▐', '▌')
            else (fill + ch + reset) if ch in ('▄', '█', '▀')
            else ch
            for ch in row
        ))
    return '\n'.join(out)



def _supports_color() -> bool:
    """True when the terminal is interactive and ANSI color is enabled."""
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


def menu_loop(args) -> int:
    """Interactive environment menu."""
    if not getattr(args, "no_banner", False):
        if _supports_color():
            print(_colorize_banner_wide())
        else:
            print('\n'.join(BANNER_WIDE_ROWS))
        print()
    while True:
        print("How do you want to run TrioForge?")
        print("  1) Run on Windows (native)")
        print("  2) Run on Linux / macOS / WSL")
        print("  3) Auto-detect environment")
        print("  4) Quit")
        try:
            choice = input("Enter your choice (1-4): ").strip()
        except EOFError:
            break
        if choice == "1":
            run_native(args)
        elif choice == "2":
            run_unix(args)
        elif choice == "3":
            run_native(args)
        elif choice == "4":
            break
        else:
            print("Invalid choice. Please enter 1, 2, 3, or 4.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="TrioForge cross-platform launcher.")
    parser.add_argument("path", nargs="?", default=None,
                        help="Path to the TrioForge project folder (optional).")
    parser.add_argument("--menu", action="store_true",
                        help="Show the interactive environment menu.")
    parser.add_argument("--no-banner", action="store_true",
                        help="Skip printing the banner (the .bat already shows it).")
    parser.add_argument("--install", action="store_true",
                        help="Force (re)install dependencies.")
    parser.add_argument("--no-install", action="store_true",
                        help="Skip dependency installation.")
    parser.add_argument("--unix", action="store_true",
                        help="(internal) Run natively; used when launched via WSL/bash.")
    args = parser.parse_args()

    if args.menu:
        return menu_loop(args)
    return run_native(args)


if __name__ == "__main__":
    sys.exit(main())
