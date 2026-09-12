"""TrioForge's own app window - the desktop edition.

This is a real window: TrioForge in the taskbar, TrioForge in the title bar, its
own icon, no tabs, no address bar, no browser. Inside it is the complete
application - chat, notes and the corkboard - because that is what app.py,
notes.py and cork_board.py already serve.

It renders through the WebView2 engine that ships with Windows 10/11 (the same
component Edge uses), embedded by pywebview. Nothing is downloaded from the
internet and nothing leaves the machine: the window points at the local server.

Why not native tkinter widgets? Because the interface is ~18,000 lines of HTML,
CSS and JavaScript (streaming chat, markdown, the corkboard's drag-and-link
canvas, the notes editor). tkinter has no HTML or JavaScript engine, so that
whole interface would have to be rebuilt by hand - and it would do less.

    python py/tools/app_window.py --url https://localhost:5003

Exit codes: 0 closed normally, 3 pywebview is not installed, 4 could not open.
"""

import argparse
import os
import sys
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _probe(url: str, timeout: float = 2.0) -> bool:
    """True if a TrioForge server answers on this base URL."""
    import json
    import ssl as _ssl
    import urllib.request
    ctx = _ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/ping", timeout=timeout,
                                    context=ctx) as resp:
            return json.loads(resp.read(200).decode("utf-8", "replace")).get("app") == "trioforge"
    except Exception:
        return False


def resolve_url(explicit: str, port: int) -> str:
    """Pick the URL to show, waiting for the server to come up.

    With no --url it discovers the scheme (https on Windows by default) by probing
    /api/ping and retries until the app answers, so the window opens cleanly even
    while the server is still installing dependencies on a first run.
    """
    import time
    if explicit:
        return explicit
    for _ in range(120):                      # up to ~3 minutes of first run
        for scheme in ("https", "http"):
            candidate = "{}://127.0.0.1:{}/".format(scheme, port)
            if _probe(candidate):
                return candidate
        time.sleep(1.5)
    return "http://127.0.0.1:{}/".format(port)


def user_data_dir() -> Path:
    """Where the embedded engine keeps cookies/cache/localStorage.

    Deliberately NOT inside the project: this is browser profile data (cookies,
    history, localStorage) and a project folder is one `git add -A` away from
    publishing it. Per-user application data is where it belongs.
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "TrioForge" / "webview"


def apply_icon_when_ready(window, ico: Path, timeout: float = 15.0) -> bool:
    """Wait for the native window, then put the icon on it.

    webview.start(func=...) runs before the GUI window exists, so the first attempt
    finds window.native = None. Poll for it, apply the icon twice (the form's handle
    is not always realised on the very first frame), and report what happened.
    """
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        if getattr(window, "native", None) is not None:
            break
        time.sleep(0.25)
    else:
        print("[icon] native window never appeared")
        return False

    ok = apply_window_icon(window, ico)
    time.sleep(1.0)
    apply_window_icon(window, ico)          # second pass: the handle is up by now
    return ok


def window_pid_file() -> Path:
    """Marks an app window as open, so the panel cannot open a second one.

    Written by THIS process (the real interpreter), not by whatever launched it:
    a venv's pythonw.exe is a shim that exits immediately, so a pid recorded by the
    launcher is dead within milliseconds and every attempt would open another
    window.
    """
    return user_data_dir().parent / "app_window.pid"


APP_ID = "TrioForge.Desktop"


def icon_path() -> Path:
    """The .ico shipped with the project (used for the window, taskbar and pins)."""
    return project_root() / "static" / "logo" / "triorforge.ico"


def register_app_id(ico: Path) -> None:
    """Tell Windows this program is 'TrioForge', and which icon to show for it.

    The window runs under pythonw.exe, so without this the taskbar and Alt-Tab show
    Python's logo. An AppUserModelID with a registered icon is how a script-hosted
    window gets its own identity.
    """
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              r"Software\Classes\AppUserModelId\%s" % APP_ID) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "TrioForge")
            if ico.is_file():
                winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(ico))
    except Exception:
        pass


def set_process_app_id() -> None:
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


def apply_window_icon(window, ico: Path) -> bool:
    """Put the TrioForge icon on the real window (title bar, taskbar, Alt-Tab).

    Two mechanisms, because a WinForms form and the native window keep separate
    icons: assigning form.Icon (pythonnet is already present for WebView2) and
    sending WM_SETICON to the form's window handle. WM_SETICON is what actually
    repaints the title bar and the taskbar button.
    """
    if not ico.is_file():
        print("[icon] .ico not found at", ico)
        return False

    native = getattr(window, "native", None)
    form = None
    try:
        form = native.TopLevelControl if native is not None and native.TopLevelControl else native
    except Exception:
        form = native
    if form is None:
        print("[icon] no native window yet")
        return False

    ok = False
    # 1) the WinForms property (covers Alt-Tab / task switchers)
    try:
        import clr
        try:
            clr.AddReference("System.Drawing")
        except Exception:
            pass
        from System.Drawing import Icon as DotNetIcon
        form.Icon = DotNetIcon(str(ico))
        form.Text = "TrioForge"
        print("[icon] form.Icon set")
        ok = True
    except Exception as exc:
        print("[icon] form.Icon failed: {}: {}".format(type(exc).__name__, exc))

    # 2) WM_SETICON on the handle (this is the one that repaints the title bar and
    #    the taskbar button)
    try:
        import ctypes
        raw = form.Handle
        # pythonnet hands back a System.IntPtr, which int() refuses.
        handle = raw.ToInt64() if hasattr(raw, "ToInt64") else int(raw)
        user32 = ctypes.windll.user32
        IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
        WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1
        for size, which in ((16, ICON_SMALL), (32, ICON_BIG), (48, ICON_BIG)):
            h = user32.LoadImageW(None, str(ico), IMAGE_ICON, size, size, LR_LOADFROMFILE)
            if h:
                user32.SendMessageW(ctypes.c_void_p(handle), WM_SETICON, which, h)
        # ask Windows to redraw the non-client area (title bar) straight away
        SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_FRAMECHANGED = 0x1, 0x2, 0x4, 0x20
        user32.SetWindowPos(ctypes.c_void_p(handle), 0, 0, 0, 0, 0,
                            SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_FRAMECHANGED)
        print("[icon] WM_SETICON sent to hwnd {}".format(handle))
        ok = True
    except Exception as exc:
        print("[icon] WM_SETICON failed: {}: {}".format(type(exc).__name__, exc))

    return ok


def is_local(url: str) -> bool:
    return any(h in url for h in ("localhost", "127.0.0.1", "[::1]"))


def main() -> int:
    parser = argparse.ArgumentParser(description="TrioForge app window")
    parser.add_argument("--url", default="", help="Explicit server URL (optional).")
    parser.add_argument("--port", type=int, default=5003,
                        help="Port to auto-detect the server on when --url is not given.")
    parser.add_argument("--title", default="TrioForge")
    parser.add_argument("--width", type=int, default=1320)
    parser.add_argument("--height", type=int, default=880)
    parser.add_argument("--no-persist", action="store_true",
                        help="Do not keep cookies/localStorage between runs (debugging).")
    args = parser.parse_args()

    try:
        import webview
    except Exception:
        print("pywebview is not installed - run: uv pip install pywebview")
        return 3

    url = resolve_url(args.url, args.port)

    # The app serves HTTPS with a locally generated (mkcert) certificate. WebView2
    # refuses an untrusted certificate and would show an error page instead of the
    # app, so for LOCAL addresses only we tell the embedded engine to accept it.
    # Never done for a remote URL.
    if url.startswith("https://") and is_local(url):
        existing = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "")
        if "ignore-certificate-errors" not in existing:
            os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
                existing + " --ignore-certificate-errors").strip()

    # Links to other sites should open in the real browser, not replace the app.
    try:
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    except Exception:
        pass

    storage = user_data_dir()
    kwargs = {"title": args.title, "url": url,
              "width": args.width, "height": args.height,
              "min_size": (900, 600), "text_select": True}

    try:
        window = webview.create_window(**kwargs)
    except TypeError as exc:
        # Older/newer pywebview disagree about some keyword; drop the optional ones.
        print("Window options not supported ({}); retrying with the basics.".format(exc))
        window = webview.create_window(title=args.title, url=url,
                                       width=args.width, height=args.height)
    except Exception as exc:
        print("Could not create the window: {}: {}".format(type(exc).__name__, exc))
        return 4

    def _on_loaded():
        # A certificate or connection failure lands on a blank/error page; make it
        # obvious rather than showing an empty window.
        try:
            title = window.evaluate_js("document.title") or ""
        except Exception:
            title = ""
        if not title.strip():
            try:
                window.load_html(
                    "<body style='background:#0b0d12;color:#e6edf3;font-family:Segoe UI;"
                    "display:flex;align-items:center;justify-content:center;height:100vh;"
                    "text-align:center'><div><h2>TrioForge is not reachable</h2>"
                    "<p style='color:#9aa4b2'>The local server did not answer at {}</p>"
                    "<p style='color:#6e7784'>Check that TrioForge is running (the control "
                    "panel shows its status), then press Reload.</p></div></body>".format(args.url))
            except Exception:
                pass

    try:
        window.events.loaded += _on_loaded
    except Exception:
        pass

    try:
        start_kwargs = {"gui": "edgechromium"}
        if not args.no_persist:
            # Keep localStorage/cookies: the app keeps theme and provider settings
            # there, and pywebview's default private mode wipes them every launch.
            start_kwargs["private_mode"] = False
            try:
                storage.mkdir(parents=True, exist_ok=True)
                start_kwargs["storage_path"] = str(storage)
            except Exception:
                pass
        try:
            pid_file = window_pid_file()
            pid_file.parent.mkdir(parents=True, exist_ok=True)
            pid_file.write_text("{} {}\n".format(os.getpid(), url), encoding="utf-8")
        except Exception:
            pid_file = None

        # Own identity + own icon before the window appears: the host process is
        # pythonw.exe, so without this the taskbar and Alt-Tab say "Python".
        ico = icon_path()
        register_app_id(ico)
        set_process_app_id()
        print("[icon] .ico = {} (exists: {})".format(ico, ico.is_file()))
        try:
            try:
                webview.start(**start_kwargs,
                              func=lambda: apply_icon_when_ready(window, ico),
                              icon=str(ico))
            except TypeError as exc:
                # An older/newer pywebview that does not accept one of these.
                print("[icon] start() rejected an argument ({}); retrying without them".format(exc))
                webview.start(**start_kwargs,
                              func=lambda: apply_icon_when_ready(window, ico))
        finally:
            try:
                if pid_file:
                    pid_file.unlink()
            except Exception:
                pass
    except Exception as exc:
        # Fall back to whatever backend pywebview finds (e.g. MSHTML on old boxes).
        print("[window] start failed: {}: {}".format(type(exc).__name__, exc))
        try:
            webview.start(private_mode=False)
        except Exception:
            print("Could not start the window: {}: {}".format(type(exc).__name__, exc))
            return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
