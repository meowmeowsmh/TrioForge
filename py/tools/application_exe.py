"""application.exe â€” the double-clickable entry point for Windows.

Named after ``application.bat`` so the Windows entry point is one word everywhere:
``application.bat`` (from a clone), ``application.exe`` (downloaded).

This is deliberately a *bootstrap*, not a bundle: it does not contain Flask, the
providers or the app. It only

1. finds (or clones) a TrioForge checkout,
2. updates it,
3. hands over to ``py/tools/launcher.py``, which sets up Python/deps as usual.

That keeps the executable a couple of megabytes instead of gigabytes, and means
the app itself keeps auto-updating exactly like a plain ``git clone`` does â€” the
maintainer pushes, everybody's next launch is the new version.

Built by .github/workflows/build-windows-exe.yml (PyInstaller, one file).

Usage:
    application.exe                 # update and run
    application.exe --update        # update only
    application.exe --install-autostart
    application.exe --dir D:\\TrioForge
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

GITHUB_URL = "https://github.com/meowmeowsmh/TrioForge.git"
REQUIRED_FILES = ("py/app.py", "py/tools/launcher.py", "templates/index.html")


def default_dir() -> Path:
    """Where to keep the app: %LOCALAPPDATA%\\TrioForge on Windows, else ~/.trioforge."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "TrioForge"
    return Path.home() / ".trioforge"


def is_project(path: Path) -> bool:
    return all((path / name).is_file() for name in REQUIRED_FILES)


def find_existing() -> Path:
    """Reuse a checkout the user already has (cwd, then the usual spot)."""
    for candidate in (Path.cwd(), default_dir()):
        if is_project(candidate):
            return candidate
    return default_dir()


def run(cmd, cwd=None) -> int:
    print("> {}".format(" ".join(str(c) for c in cmd)))
    try:
        return subprocess.call([str(c) for c in cmd], cwd=str(cwd) if cwd else None)
    except FileNotFoundError:
        print("Not found: {}".format(cmd[0]))
        return 127


def clone(target: Path) -> bool:
    target.parent.mkdir(parents=True, exist_ok=True)
    if run(["git", "clone", "--depth", "1", GITHUB_URL, str(target)]) != 0:
        print()
        print("Could not clone TrioForge. Install git (https://git-scm.com/downloads)")
        print("or download the ZIP from https://github.com/meowmeowsmh/TrioForge")
        print("and unzip it to: {}".format(target))
        return False
    return True


def python_command(project: Path) -> list:
    """How to invoke a REAL Python interpreter (never this exe).

    Inside a PyInstaller build `sys.executable` is application.exe itself, so using
    it here would relaunch the bootstrap with launcher arguments - a loop, not a
    launcher. Prefer the project venv, then a python on PATH, then `py -3`.
    """
    for rel in ("Scripts/python.exe", "bin/python3", "bin/python"):
        candidate = project / ".venv" / rel
        if candidate.is_file():
            return [str(candidate)]
    if not getattr(sys, "frozen", False):
        return [sys.executable]                 # running as a script: good enough
    for name in ("python", "python3"):
        found = shutil.which(name)
        if found:
            return [found]
    launcher = shutil.which("py")
    if launcher:
        return [launcher, "-3"]                 # the Windows py launcher picks 3.x
    return []


def try_install_python() -> bool:
    """Last resort on Windows: install Python with winget (same as the .bat)."""
    if os.name != "nt":
        return False
    winget = shutil.which("winget")
    if not winget:
        return False
    print("  no Python found - installing it with winget (a few minutes, one time)")
    for package in ("Python.Python.3", "Python.Python.3.13"):
        code = run([winget, "install", "--id", package, "-e", "--source", "winget",
                    "--accept-package-agreements", "--accept-source-agreements"])
        if code == 0:
            return True
    return False


def log_path(project: Path) -> Path:
    """Where to keep a copy of everything the launcher printed."""
    try:
        logs = project / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        return logs / "bootstrap.log"
    except Exception:
        return Path(tempfile.gettempdir()) / "trioforge-bootstrap.log"


def run_launcher(cmd: list, cwd: Path, logfile: Path) -> int:
    """Run the launcher, teeing its output to a log file so failures survive.

    Printing must never be able to kill the run: a frozen console on Windows is
    cp1252, and the launcher's output contains arrows and box-drawing characters,
    so an unguarded print() raises UnicodeEncodeError and takes the bootstrap with
    it (which is exactly what happened the first time this was tried).
    """
    try:
        with logfile.open("a", encoding="utf-8", errors="replace") as fh:
            fh.write("\n=== {} ===\n".format(datetime.now().isoformat(timespec="seconds")))
            fh.write("> {}\n".format(" ".join(str(c) for c in cmd)))
            fh.flush()
            proc = subprocess.Popen([str(c) for c in cmd], cwd=str(cwd),
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, text=True,
                                    encoding="utf-8", errors="replace", bufsize=1)
            for line in proc.stdout:
                fh.write(line)
                try:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                except Exception:
                    safe = line.encode("ascii", "replace").decode("ascii")
                    try:
                        sys.stdout.write(safe)
                        sys.stdout.flush()
                    except Exception:
                        pass
            return proc.wait()
    except Exception as exc:
        print("Could not run the launcher: {}: {}".format(type(exc).__name__, exc))
        return 1


def wait_for_key() -> None:
    """Keep the window open so a double-click user can actually READ the error.

    Without this the console closes the instant anything fails, which is exactly
    how "it did not open" happens with no visible reason.
    """
    try:
        if sys.stdin and sys.stdin.isatty():
            print()
            input("Press Enter to close this window...")
    except Exception:
        pass


def gui_interpreter(interpreter: list) -> list:
    """Prefer pythonw.exe so the control panel runs without a console window."""
    first = str(interpreter[0])
    if os.name == "nt":
        candidate = Path(first).with_name("pythonw.exe")
        if candidate.is_file():
            return [str(candidate)] + [str(a) for a in interpreter[1:]]
    return interpreter


def main() -> int:
    # A frozen Windows console is cp1252; the launcher prints arrows/box-drawing
    # characters, so switch our own streams to UTF-8 up front and never let a
    # print() failure abort the bootstrap.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="TrioForge bootstrap launcher")
    parser.add_argument("--dir", default=None, help="Where TrioForge lives / should live.")
    parser.add_argument("--update", action="store_true", help="Update and exit.")
    parser.add_argument("--install-autostart", action="store_true",
                        help="Start TrioForge automatically when you log in.")
    parser.add_argument("--remove-autostart", action="store_true",
                        help="Stop starting TrioForge at login.")
    parser.add_argument("--status", action="store_true", help="Show version/git/deps state.")
    parser.add_argument("--no-pause", action="store_true",
                        help="Never wait for a keypress (for scripts and CI).")
    parser.add_argument("rest", nargs=argparse.REMAINDER,
                        help="Anything else is passed straight to launcher.py.")
    args = parser.parse_args()

    # Maintenance/inspection runs should exit straight away; a plain double-click
    # (or an interactive failure) keeps the window so the reason stays readable.
    non_interactive = bool(args.update or args.status or args.install_autostart
                           or args.remove_autostart or args.no_pause
                           or (not sys.stdout.isatty()))
    project = Path(args.dir).expanduser().resolve() if args.dir else find_existing()
    print("TrioForge bootstrap")
    print("  app folder: {}".format(project))

    if not is_project(project):
        print("  no checkout there yet - cloning {} (shallow)".format(GITHUB_URL))
        if not clone(project):
            if not non_interactive:
                wait_for_key()
            return 1

    launcher = project / "py" / "tools" / "launcher.py"
    interpreter = python_command(project)
    if not interpreter:
        print("  python    : not found")
        if try_install_python():
            interpreter = python_command(project)
        if not interpreter:
            print()
            print("Python is required. Install it (https://www.python.org/downloads/) or")
            print("run application.bat, which installs it for you. Then run application.exe again.")
            if not non_interactive:
                wait_for_key()
            return 1
    print("  python    : {}".format(" ".join(interpreter)))

    passthrough = []
    if args.update:
        passthrough.append("--update")
    if args.install_autostart:
        passthrough.append("--install-autostart")
    if args.remove_autostart:
        passthrough.append("--remove-autostart")
    if args.status:
        passthrough.append("--status")
    passthrough += [a for a in args.rest if a]

    # With no flags this is a double-click: pop up the control panel (the small
    # window that hosts the app), the way Ollama's desktop app does. Maintenance
    # flags stay plain console commands so scripts and CI keep working.
    maintenance = bool(args.update or args.status or args.install_autostart
                       or args.remove_autostart or args.no_pause)
    if not maintenance and not passthrough:
        gui = project / "py" / "tools" / "launcher_gui.py"
        if gui.is_file():
            gui_cmd = gui_interpreter(interpreter) + [str(gui), str(project)]
            try:
                flags = 0
                if os.name == "nt":
                    flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
                subprocess.Popen([str(c) for c in gui_cmd], cwd=str(project),
                                 creationflags=flags,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 stdin=subprocess.DEVNULL)
                print("  control   : TrioForge is starting - the control panel window opens")
                print("              in a moment. It hosts the app and shows what it is doing.")
                print("              Log: {}".format(log_path(project)))
                return 0
            except Exception as exc:
                print("  control   : could not open the panel ({});".format(exc))
                print("              falling back to the console launcher.")

    # The launcher owns updates, dependency install, auto-start and running the
    # app, so the exe stays a thin, always-current shim.
    logfile = log_path(project)
    print("  log       : {}".format(logfile))
    code = run_launcher(interpreter + [str(launcher), str(project), "--no-banner"] + passthrough,
                        cwd=project, logfile=logfile)
    if code != 0:
        print()
        print("TrioForge did not start (exit {}). The full output is in:".format(code))
        print("  {}".format(logfile))
        if not non_interactive:
            wait_for_key()
    return code


if __name__ == "__main__":
    sys.exit(main())
