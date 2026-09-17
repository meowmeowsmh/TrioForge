"""Create (or remove) a TrioForge shortcut with the app's own icon.

Per-user only: the Desktop and the Start Menu of the person running it, no admin
rights and nothing machine-wide. The shortcut runs start.vbs on Windows (server
hidden, TrioForge's own window) and launcher.py --window everywhere else.

    from shortcuts import install, remove, state
    install(project)          # Desktop + Start Menu (what a first run does)
    remove(project)           # take them away again
    state(project)            # what exists right now, for --status
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple
from uuid import uuid4

APP_NAME = "TrioForge"

# Each entry point owns its own shortcut, so whichever one you launch makes sure YOU
# have a way back to it - and never tramples the other's:
#   start-web.vbs -> "TrioForge"           (opens in the browser - the default)
#   start.vbs     -> "TrioForge (window)"  (opens in its own WebView2 window)
FLAVORS = {
    "web": {
        "suffix": "",
        "script": "start-web.vbs",
        "fallbacks": ("start-web.bat", "application.bat"),
        "desc": "TrioForge - opens in your browser (the server runs hidden)",
    },
    "window": {
        "suffix": " (window)",
        "script": "start.vbs",
        "fallbacks": ("application.bat", "start-web.vbs"),
        "desc": "TrioForge - opens in its own window (the server runs hidden)",
    },
}


def flavor_target(project: Path, flavor: str = "web") -> Path:
    """The script a flavour's shortcut should run."""
    spec = FLAVORS.get(flavor) or FLAVORS["web"]
    target = project / spec["script"]
    if target.is_file():
        return target
    for name in spec["fallbacks"]:
        alt = project / name
        if alt.is_file():
            return alt
    return target


try:
    from procutil import no_window_flags
except Exception:                                  # imported outside py/tools
    def no_window_flags() -> int:
        return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _icon(project: Path) -> Path:
    return project / "static" / "logo" / "triorforge.ico"


def _desktop_dir() -> Path:
    r"""The user's real Desktop folder, from the registry.

    `USERPROFILE\Desktop` is wrong on machines where the Desktop has been redirected
    (OneDrive, a moved folder, or a localised system) - and that is exactly the kind
    of machine where a shortcut written to the wrong place silently disappears. The
    registry entry is where Windows itself records where the Desktop really lives.
    """
    try:
        import winreg
    except Exception:
        winreg = None
    if winreg is not None:
        for key in (r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
                    r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                    val, _ = winreg.QueryValueEx(k, "Desktop")
                val = os.path.expandvars(val or "")
                if val and os.path.isdir(val):
                    return Path(val)
            except Exception:
                continue
    for candidate in (Path(os.environ.get("OneDrive", "")) / "Desktop",
                      Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"):
        if candidate.is_dir():
            return candidate
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"


def _windows_targets(project: Path, flavor: str = "web") -> List[Tuple[str, Path]]:
    """(label, .lnk path) for the places a user expects to find the app."""
    name = APP_NAME + ((FLAVORS.get(flavor) or FLAVORS["web"])["suffix"])
    out = []
    out.append(("Desktop", _desktop_dir() / (name + ".lnk")))
    appdata = os.environ.get("APPDATA")
    if appdata:
        out.append(("Start Menu",
                    Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
                    / (name + ".lnk")))
    return out


def _make_lnk(lnk: Path, target: Path, icon: Path, workdir: Path,
              desc: str = "") -> Tuple[bool, str]:
    r"""Create one Windows shortcut with the built-in WScript.Shell.

    Written as a throwaway .vbs and run with cscript, NOT through PowerShell
    `-Command` with string interpolation: passing the script on the command line
    broke on paths containing spaces, `&`, `$`, `%` or quotes (a friend's
    `C:\Users\John & Jane\...` clone path), and it silently failed under pythonw
    where nothing prints. WScript.Shell is present on every Windows install and a
    .vbs file has no command-line quoting problem - only double-quotes in paths,
    which Windows forbids anyway - so this is the path that "just works".
    """
    try:
        lnk.parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return False, "cannot create {}: {}".format(lnk.parent, exc)

    def q(value):
        return '"' + str(value).replace('"', '""') + '"'

    lines = [
        'Set sh = CreateObject("WScript.Shell")',
        "Set s = sh.CreateShortcut({})".format(q(lnk)),
        "s.TargetPath = {}".format(q(target)),
        "s.WorkingDirectory = {}".format(q(workdir)),
        "s.Description = {}".format(q(desc or APP_NAME)),
        "s.WindowStyle = 7",
    ]
    if icon and icon.is_file():
        lines.append("s.IconLocation = {}".format(q(icon)))
    lines.append("s.Save")
    script = lnk.parent / ".triorforge_{}.vbs".format(uuid4().hex)
    try:
        script.write_text("\r\n".join(lines) + "\r\n", encoding="ascii",
                          errors="replace")
        done = subprocess.run(
            ["cscript.exe", "//nologo", str(script)],
            capture_output=True, text=True, timeout=60,
            creationflags=no_window_flags())
        if done.returncode != 0:
            return False, (done.stderr or done.stdout or "cscript failed").strip()[:200]
    except Exception as exc:
        return False, "{}: {}".format(type(exc).__name__, exc)
    finally:
        try:
            script.unlink()
        except Exception:
            pass
    return (lnk.is_file(), "created" if lnk.is_file() else "not created")


def _linux_targets(project: Path, flavor: str = "web") -> List[Tuple[str, Path]]:
    """Linux (Mint, Ubuntu, …): an applications-menu entry plus one on the Desktop."""
    base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    desk = Path(os.environ.get("XDG_DESKTOP_DIR") or (Path.home() / "Desktop"))
    slug = "trioforge" if flavor == "web" else "trioforge-window"
    label = "TrioForge" if flavor == "web" else "TrioForge (window)"
    return [
        ("Start Menu", base / "applications" / (slug + ".desktop")),
        ("Desktop", desk / (label + ".desktop")),
    ]


def is_wsl() -> bool:
    """True inside Windows Subsystem for Linux - which has no desktop to put an icon on.

    WSL has no GUI of its own: writing a .desktop file there would not give anyone a
    clickable icon, and it would even create a bogus ~/Desktop directory. The app is
    reached through Windows (the browser, at the printed URL), so no shortcut is made.
    """
    if os.name == "nt" or sys.platform == "darwin":
        return False
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        import platform
        return "microsoft" in platform.uname().release.lower()
    except Exception:
        return False


def _mac_targets(project: Path, flavor: str = "web") -> List[Tuple[str, Path]]:
    """Where a Mac shortcut belongs.

    macOS has no .desktop files: the thing Finder will actually double-click is a
    `.command` file (it opens in Terminal and runs). That is what gets written.
    """
    label = "TrioForge" if flavor == "web" else "TrioForge (window)"
    desk = Path.home() / "Desktop"
    return [("Desktop", desk / (label + ".command"))]


def _make_command_file(path: Path, project: Path, flavor: str = "web") -> Tuple[bool, str]:
    """A double-clickable macOS launcher: run.sh, in the Terminal window Finder opens."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        flag = " --window" if flavor == "window" else ""
        body = (
            "#!/bin/sh\n"
            "# TrioForge - double-click this in Finder.\n"
            'cd "{}" || exit 1\n'
            "exec ./run.sh{}\n".format(project, flag)
        )
        path.write_text(body, encoding="utf-8")
        os.chmod(path, 0o755)
        return True, "created"
    except Exception as exc:
        return False, "{}: {}".format(type(exc).__name__, exc)


def targets(project: Path, flavor: str = "web") -> List[Tuple[str, Path]]:
    """(label, path) for every place this flavour's shortcut belongs - by platform.

    Empty on WSL, which has no desktop of its own.
    """
    if is_wsl():
        return []
    if os.name == "nt":
        return _windows_targets(project, flavor)
    if sys.platform == "darwin":
        return _mac_targets(project, flavor)
    return _linux_targets(project, flavor)


def _make_desktop_file(path: Path, project: Path, icon: Path, flavor: str = "web") -> Tuple[bool, str]:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        pythonw = project / ".venv" / "bin" / "python3"
        exe = str(pythonw if pythonw.is_file() else sys.executable)
        # "web" opens in the user's browser (no --window); "window" opens the desktop
        # window. Same server either way.
        extra = "--window --detach --no-browser" if flavor == "window" else "--detach"
        body = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name={name}\n"
            "Comment=Your own private AI workspace\n"
            "Exec={exe} {launcher} {project} {extra}\n"
            "Icon={icon}\n"
            "Terminal=false\n"
            "Categories=Utility;Office;\n"
        ).format(exe=exe, launcher=project / "py" / "tools" / "launcher.py",
                 project=project, icon=icon, extra=extra,
                 name="TrioForge" if flavor == "web" else "TrioForge (window)")
        path.write_text(body, encoding="utf-8")
        os.chmod(path, 0o755)
        return True, "created"
    except Exception as exc:
        return False, "{}: {}".format(type(exc).__name__, exc)


def install(project: Path, where: str = "both", flavor: str = "web") -> List[str]:
    """Create ONE flavour's shortcuts. Returns human-readable lines for the log.

    `flavor` is "web" (the default, opens in the browser) or "window" (the WebView2
    window). Each has its own name, so launching one never overwrites the other.
    """
    lines: List[str] = []
    icon = _icon(project)
    spec = FLAVORS.get(flavor) or FLAVORS["web"]
    found = targets(project, flavor)
    if not found:
        # WSL: no desktop of its own, so there is nothing to create (and nothing to
        # explain - the app is reached through Windows at the printed URL).
        return lines
    if os.name == "nt":
        target = flavor_target(project, flavor)
        if not target.is_file():
            lines.append("{} shortcut skipped: {} not found".format(flavor, spec["script"]))
            return lines
        for label, lnk in found:
            if where not in ("both", label.lower().replace(" ", "")):
                continue
            ok, msg = _make_lnk(lnk, target, icon, project, spec["desc"])
            lines.append("{} shortcut: {} ({})".format(label, lnk, msg) if ok
                         else "{} shortcut failed: {}".format(label, msg))
    elif sys.platform == "darwin":
        for label, path in found:
            if where not in ("both", label.lower().replace(" ", "")):
                continue
            ok, msg = _make_command_file(path, project, flavor)
            lines.append("{} launcher: {} ({})".format(label, path, msg) if ok
                         else "{} launcher failed: {}".format(label, msg))
    else:
        for label, path in found:
            if where not in ("both", label.lower().replace(" ", "")):
                continue
            ok, msg = _make_desktop_file(path, project, icon, flavor)
            lines.append("{} entry: {} ({})".format(label, path, msg) if ok
                         else "{} entry failed: {}".format(label, msg))
    return lines


def exists(project: Path, flavor: str = "web") -> bool:
    """True when THIS flavour's shortcut is already in place somewhere."""
    return any(path.is_file() for _label, path in targets(project, flavor))


def missing(project: Path, flavor: str = "web") -> List[str]:
    """Labels (e.g. 'Desktop') where this flavour's shortcut is NOT there yet.

    Checked per location, so deleting just the Desktop icon brings that one back
    without touching the Start Menu entry - and vice versa. Empty on WSL.
    """
    return [label for label, path in targets(project, flavor) if not path.is_file()]


def remove(project: Path, flavor: str = None) -> List[str]:
    lines: List[str] = []
    flavors = [flavor] if flavor else list(FLAVORS.keys())
    for fl in flavors:
        for label, path in targets(project, fl):
            try:
                if path.is_file():
                    path.unlink()
                    lines.append("{} ({}) shortcut removed: {}".format(label, fl, path))
                else:
                    lines.append("{} ({}) shortcut: not there".format(label, fl))
            except Exception as exc:
                lines.append("{} shortcut not removed: {}".format(label, exc))
    return lines


def state(project: Path, flavor: str = None) -> List[str]:
    """What currently exists - used by --status and the control panel."""
    flavors = [flavor] if flavor else list(FLAVORS.keys())
    lines: List[str] = []
    for fl in flavors:
        found = targets(project, fl)
        if not found:
            lines.append("({}): not applicable here".format(fl))
            continue
        for label, path in found:
            lines.append("{} ({}): {}".format(label, fl, "yes" if path.is_file() else "no"))
    return lines
