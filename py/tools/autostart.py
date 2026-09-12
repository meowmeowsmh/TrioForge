"""TrioForge — "start automatically when I log in".

Opt-in, per user, no admin rights on any platform:

* **Windows**  — a value in ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``
  pointing at ``pythonw.exe`` (no console window).
* **Linux**    — ``~/.config/autostart/trioforge.desktop`` (XDG autostart).
* **macOS**    — ``~/Library/LaunchAgents/com.trioforge.plist`` (launchd).

Docker does not need any of this: the compose file already ships
``restart: unless-stopped``, so the container comes back after a reboot.

The started instance runs the launcher in ``--autostart`` mode, which means
"quiet": no menu, no banner, and no browser tab opening at login.
"""

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

APP_NAME = "TrioForge"
WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
MAC_LABEL = "com.trioforge.launcher"


def _launcher_path() -> Path:
    """The launcher the OS should run — NOT this module (this one is imported by it)."""
    return Path(__file__).resolve().with_name("launcher.py")


def _python_for_autostart() -> str:
    """Prefer pythonw.exe on Windows so logging in does not flash a console."""
    if os.name == "nt":
        exe = Path(sys.executable)
        pythonw = exe.with_name("pythonw.exe")
        if pythonw.is_file():
            return str(pythonw)
    return sys.executable


def _extra_args() -> List[str]:
    """What the login entry should use.

    --window opens TrioForge in its own window, the same thing start.vbs does, so
    "start at login" gives you the app rather than a silent server you cannot see.
    --no-browser keeps a browser tab from opening as well.
    """
    return ["--autostart", "--window", "--no-browser"]


def _command(project: Path) -> List[str]:
    """The command the OS should run at login."""
    return [_python_for_autostart(), str(_launcher_path()), str(project)] + _extra_args()


def _windows_command_line(project: Path) -> str:
    return '"{}" "{}" "{}" {}'.format(
        _python_for_autostart(), _launcher_path(), project, " ".join(_extra_args()))


# ── Windows ──────────────────────────────────────────────────────────────────
def _win_install(project: Path) -> Tuple[bool, str]:
    import winreg                                     # noqa: WPS433 (Windows only)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, WIN_RUN_KEY) as key:
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _windows_command_line(project))
    return True, "added to HKCU Run: {}".format(_windows_command_line(project))


def _win_remove() -> Tuple[bool, str]:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, APP_NAME)
        return True, "removed from HKCU Run"
    except FileNotFoundError:
        return False, "was not enabled"
    except OSError as exc:
        return False, "could not remove the Run entry: {}".format(exc)


def _win_status() -> Tuple[bool, str]:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
        return True, "enabled -> {}".format(value)
    except FileNotFoundError:
        return False, "not enabled"
    except OSError as exc:
        return False, "unknown ({})".format(exc)


# ── Linux ────────────────────────────────────────────────────────────────────
def _linux_desktop_file() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "autostart" / "trioforge.desktop"


def _linux_install(project: Path) -> Tuple[bool, str]:
    path = _linux_desktop_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = " ".join('"{}"'.format(part) for part in _command(project))
    path.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name={name}\n"
        "Comment=Self-hosted AI workspace (chat, notes, corkboard)\n"
        "Exec={cmd}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n".format(name=APP_NAME, cmd=cmd),
        encoding="utf-8",
    )
    return True, "wrote {}".format(path)


def _linux_remove() -> Tuple[bool, str]:
    path = _linux_desktop_file()
    if path.exists():
        path.unlink()
        return True, "removed {}".format(path)
    return False, "was not enabled"


def _linux_status() -> Tuple[bool, str]:
    path = _linux_desktop_file()
    return (path.exists(), "enabled ({})".format(path) if path.exists() else "not enabled")


# ── macOS ────────────────────────────────────────────────────────────────────
def _mac_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / (MAC_LABEL + ".plist")


def _mac_install(project: Path) -> Tuple[bool, str]:
    path = _mac_plist()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": MAC_LABEL,
        "ProgramArguments": _command(project),
        "RunAtLoad": True,
        "KeepAlive": False,
        "WorkingDirectory": str(project),
        "StandardOutPath": str(project / "logs" / "launcher.out.log"),
        "StandardErrorPath": str(project / "logs" / "launcher.err.log"),
    }
    try:
        (project / "logs").mkdir(exist_ok=True)
    except Exception:
        pass
    with path.open("wb") as handle:
        plistlib.dump(payload, handle)
    # load it now so the user does not have to log out to test it
    subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
    subprocess.run(["launchctl", "load", str(path)], capture_output=True)
    return True, "wrote {} and loaded it".format(path)


def _mac_remove() -> Tuple[bool, str]:
    path = _mac_plist()
    if path.exists():
        subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
        path.unlink()
        return True, "removed {}".format(path)
    return False, "was not enabled"


def _mac_status() -> Tuple[bool, str]:
    path = _mac_plist()
    return (path.exists(), "enabled ({})".format(path) if path.exists() else "not enabled")


# ── public API ───────────────────────────────────────────────────────────────
def _impls():
    if os.name == "nt":
        return _win_install, _win_remove, _win_status
    if sys.platform == "darwin":
        return _mac_install, _mac_remove, _mac_status
    return _linux_install, _linux_remove, _linux_status


def install(project: Path) -> Tuple[bool, str]:
    install_fn, _, _ = _impls()
    try:
        return install_fn(project)
    except Exception as exc:
        return False, "could not enable auto-start: {}: {}".format(type(exc).__name__, exc)


def remove() -> Tuple[bool, str]:
    _, remove_fn, _ = _impls()
    try:
        return remove_fn()
    except Exception as exc:
        return False, "could not disable auto-start: {}: {}".format(type(exc).__name__, exc)


def state() -> Tuple[bool, str]:
    _, _, status_fn = _impls()
    try:
        return status_fn()
    except Exception as exc:
        return False, "unknown ({})".format(exc)


def supported() -> bool:
    return os.name == "nt" or sys.platform in ("darwin", "linux") or sys.platform.startswith("linux")
