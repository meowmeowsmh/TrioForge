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

APP_NAME = "TrioForge"
try:
    from procutil import no_window_flags
except Exception:                                  # imported outside py/tools
    def no_window_flags() -> int:
        return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _icon(project: Path) -> Path:
    return project / "static" / "logo" / "triorforge.ico"


def _windows_targets(project: Path) -> List[Tuple[str, Path]]:
    """(label, .lnk path) for the places a user expects to find the app."""
    out = []
    desktop = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
    if not desktop.is_dir():                        # OneDrive-redirected Desktop
        alt = Path(os.environ.get("OneDrive", "")) / "Desktop"
        if alt.is_dir():
            desktop = alt
    out.append(("Desktop", desktop / (APP_NAME + ".lnk")))
    appdata = os.environ.get("APPDATA")
    if appdata:
        out.append(("Start Menu",
                    Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
                    / (APP_NAME + ".lnk")))
    return out


def _make_lnk(lnk: Path, target: Path, icon: Path, workdir: Path) -> Tuple[bool, str]:
    """Create one Windows shortcut through PowerShell (no pywin32 needed)."""
    try:
        lnk.parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return False, "cannot create {}: {}".format(lnk.parent, exc)

    ps = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
        "$s.TargetPath = '{target}';"
        "$s.WorkingDirectory = '{workdir}';"
        "$s.Description = 'TrioForge - opens in your browser (the server runs hidden)';"
        "{icon}"
        "$s.WindowStyle = 7;"
        "$s.Save()"
    ).format(
        lnk=str(lnk).replace("'", "''"),
        target=str(target).replace("'", "''"),
        workdir=str(workdir).replace("'", "''"),
        icon=("$s.IconLocation = '{}';".format(str(icon).replace("'", "''"))
              if icon.is_file() else ""),
    )
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", ps],
            capture_output=True, text=True, timeout=60,
            creationflags=no_window_flags())
        if done.returncode != 0:
            return False, (done.stderr or done.stdout or "powershell failed").strip()[:200]
    except Exception as exc:
        return False, "{}: {}".format(type(exc).__name__, exc)
    return (lnk.is_file(), "created" if lnk.is_file() else "not created")


def _linux_targets(project: Path) -> List[Tuple[str, Path]]:
    base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    desk = Path(os.environ.get("XDG_DESKTOP_DIR") or (Path.home() / "Desktop"))
    return [
        ("Start Menu", base / "applications" / "trioforge.desktop"),
        ("Desktop", desk / "TrioForge.desktop"),
    ]


def _make_desktop_file(path: Path, project: Path, icon: Path) -> Tuple[bool, str]:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        pythonw = project / ".venv" / "bin" / "python3"
        exe = str(pythonw if pythonw.is_file() else sys.executable)
        body = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=TrioForge\n"
            "Comment=Your own private AI workspace\n"
            "Exec={exe} {launcher} {project} --window --detach --no-browser\n"
            "Icon={icon}\n"
            "Terminal=false\n"
            "Categories=Utility;Office;\n"
        ).format(exe=exe, launcher=project / "py" / "tools" / "launcher.py",
                 project=project, icon=icon)
        path.write_text(body, encoding="utf-8")
        os.chmod(path, 0o755)
        return True, "created"
    except Exception as exc:
        return False, "{}: {}".format(type(exc).__name__, exc)


def install(project: Path, where: str = "both") -> List[str]:
    """Create the shortcuts. Returns human-readable lines for the log."""
    lines: List[str] = []
    icon = _icon(project)
    if os.name == "nt":
        # The browser is the reliable daily driver: the WebView2 window is flaky on
        # some GPUs (blank window / freeze), while a browser tab always renders and
        # uses less memory. start-web.vbs is the shortcut target; start.vbs remains
        # available for whoever wants the app in a window of its own.
        target = project / "start-web.vbs"
        if not target.is_file():
            target = project / "start.vbs"
        if not target.is_file():
            target = project / "application.bat"
        for label, lnk in _windows_targets(project):
            if where not in ("both", label.lower().replace(" ", "")):
                continue
            ok, msg = _make_lnk(lnk, target, icon, project)
            lines.append("{} shortcut: {} ({})".format(label, lnk, msg) if ok
                         else "{} shortcut failed: {}".format(label, msg))
    else:
        for label, path in _linux_targets(project):
            if where not in ("both", label.lower().replace(" ", "")):
                continue
            ok, msg = _make_desktop_file(path, project, icon)
            lines.append("{} entry: {} ({})".format(label, path, msg) if ok
                         else "{} entry failed: {}".format(label, msg))
    return lines


def remove(project: Path) -> List[str]:
    lines: List[str] = []
    targets = _windows_targets(project) if os.name == "nt" else _linux_targets(project)
    for label, path in targets:
        try:
            if path.is_file():
                path.unlink()
                lines.append("{} shortcut removed: {}".format(label, path))
            else:
                lines.append("{} shortcut: not there".format(label))
        except Exception as exc:
            lines.append("{} shortcut not removed: {}".format(label, exc))
    return lines


def state(project: Path) -> List[str]:
    """What currently exists - used by --status and the control panel."""
    targets = _windows_targets(project) if os.name == "nt" else _linux_targets(project)
    return ["{}: {}".format(label, "yes" if path.is_file() else "no")
            for label, path in targets]
