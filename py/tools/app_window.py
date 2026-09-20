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
import threading
import time
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


def apply_icon_when_ready(window, ico: Path, url: str = "", timeout: float = 15.0) -> bool:
    """Wait for the native window, then put the icon on it and start watching it.

    webview.start(func=...) runs before the GUI window exists, so the first attempt
    finds window.native = None. Poll for it, apply the icon twice (the form's handle
    is not always realised on the very first frame), and report what happened. This is
    also the moment we know the window is real, so it is where the hang watchdog and
    the fallback watchdog get their proof - both need `url` and the real handle.
    """
    global _window_ready
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        if getattr(window, "native", None) is not None:
            break
        time.sleep(0.25)
    else:
        print("[icon] native window never appeared")
        return False

    _window_ready = True
    try:
        import threading as _th

        def _handle():
            form = window.native.TopLevelControl or window.native
            raw = form.Handle
            # pythonnet hands back a System.IntPtr, and int() refuses it. The watchdog
            # used to swallow that failure, so it silently checked nothing at all -
            # which is why a window could sit at "not responding" for a minute.
            return int(raw.ToInt64()) if hasattr(raw, "ToInt64") else int(raw)

        _th.Thread(target=hang_watchdog, args=(_handle, url), daemon=True).start()
        print("[window] watching for hangs (a frozen window is handed to your browser)")
        _th.Thread(target=blank_page_watchdog, args=(_handle, url), daemon=True).start()
        print("[window] watching for a blank window (a page that never paints is "
              "handed to your browser)")
    except Exception as _exc:
        print("[window] hang watchdog not started:", _exc)
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


def hang_marker() -> Path:
    """Records that the window hung, so the next start can avoid the same cause."""
    return user_data_dir().parent / "window_hung.txt"


def hardware_gpu_allowed() -> bool:
    """False once the window has hung: the next start uses software rendering.

    A hung WebView2 window is almost always its renderer or GPU process stalling - and
    this machine's GPU driver has already failed a Vulkan allocation for llama.cpp, so
    it is the prime suspect. The software path is SwiftShader (``--use-angle=swiftshader``),
    NOT ``--disable-gpu``: measured on the machine that hit this, ``--disable-gpu``
    produced a window that never painted at all (blank white, or black), while
    SwiftShader drew the whole interface correctly. Stability beats decoration - but
    only if the page is actually drawn.
    """
    if os.environ.get("TRIOFORGE_WINDOW_HARDWARE", "").strip() in ("1", "true", "on"):
        return True
    if os.environ.get("TRIOFORGE_WINDOW_SOFTWARE", "").strip() in ("1", "true", "on"):
        return False
    return not hang_marker().is_file()


def note_hang(reason: str) -> None:
    """Remember a hang (with a count) so the next launch can do something about it."""
    try:
        marker = hang_marker()
        count = 0
        if marker.is_file():
            try:
                count = int(marker.read_text(encoding="utf-8").strip().split()[0])
            except Exception:
                count = 0
        marker.write_text("{} {}\n".format(count + 1, reason), encoding="utf-8")
        print("[window] noted a hang ({}): {}".format(count + 1, reason))
    except Exception:
        pass


def hang_watchdog(get_handle, url: str, hung_seconds: int = 20, interval: float = 5.0,
                  is_hung=None) -> None:
    """Watch the real window; when Windows says it stopped responding, get out.

    "Not responding" means the window is no longer pumping messages. The app is still
    perfectly usable in a browser, so instead of leaving a frozen window on screen we
    mark the hang (the next start then uses software rendering) and open the browser.

    is_hung is injectable so this can be exercised without a genuinely frozen window.
    """
    import time
    if is_hung is None:
        import ctypes
        user32 = ctypes.windll.user32

        def is_hung(hwnd):
            return bool(user32.IsHungAppWindow(ctypes.c_void_p(int(hwnd))))
    hung = 0.0
    while True:
        time.sleep(interval)
        if not _window_ready:
            continue
        try:
            hwnd = get_handle()
        except Exception:
            hwnd = None
        if not hwnd:
            continue
        try:
            if is_hung(hwnd):
                hung += interval
                print("[window] not responding for {}s".format(int(hung)))
                if hung >= hung_seconds:
                    note_hang("IsHungAppWindow for {}s".format(int(hung)))
                    print("[window] the window is not responding - opening your browser instead "
                          "(the server is fine). The next start will use software rendering.")
                    try:
                        import webbrowser
                        webbrowser.open(url)
                    except Exception as exc:
                        print("[window] could not open a browser:", exc)
                    time.sleep(5)
                    os._exit(1)
            else:
                hung = 0.0
        except Exception as exc:
            # Never swallow this silently: a broken check looks exactly like a healthy
            # window, which is how the int(IntPtr) bug hid for a whole debugging round.
            global _hang_check_error_logged
            if not _hang_check_error_logged:
                _hang_check_error_logged = True
                print("[window] hang check failed: {}: {}".format(type(exc).__name__, exc))
            hung = 0.0


def _window_fraction_blank(hwnd: int) -> float:
    """Fraction of the window that is near-white or near-black (0.0..1.0).

    A WebView2 window whose compositor failed still runs the page (its JS keeps
    talking to the server) but shows a solid white or black window. Looking at the
    actual pixels is the only reliable way to tell "rendered" from "broken" - the DOM
    is fine either way. Returns 1.0 on any error (treat as blank).
    """
    import ctypes
    from ctypes import wintypes
    try:
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        r = wintypes.RECT()
        if not user32.GetWindowRect(wintypes.HWND(int(hwnd)), ctypes.byref(r)):
            return 1.0
        w, h = r.right - r.left, r.bottom - r.top
        if w < 200 or h < 200:
            return 0.0                    # minimised / not measurable
        hdc = user32.GetWindowDC(wintypes.HWND(int(hwnd)))
        mdc = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        gdi32.SelectObject(mdc, bmp)
        user32.PrintWindow(wintypes.HWND(int(hwnd)), mdc, 0x00000002)   # PW_RENDERFULLCONTENT

        class BMIH(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        bi = BMIH()
        bi.biSize = ctypes.sizeof(BMIH)
        bi.biWidth, bi.biHeight, bi.biPlanes, bi.biBitCount, bi.biCompression = w, -h, 1, 32, 0
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
        raw = buf.raw

        blank = total = 0
        for i in range(0, len(raw), 4 * 53):          # sample, don't scan every pixel
            b, g, r = raw[i], raw[i + 1], raw[i + 2]
            total += 1
            if (r > 240 and g > 240 and b > 240) or (r < 14 and g < 14 and b < 14):
                blank += 1
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(wintypes.HWND(int(hwnd)), hdc)
        return blank / float(total) if total else 1.0
    except Exception as exc:
        print("[window] blank check failed: {}: {}".format(type(exc).__name__, exc))
        return 1.0


def _find_own_window_hwnd() -> int:
    """The handle of this process's own visible window (0 when not up yet)."""
    import ctypes
    from ctypes import wintypes
    pid = os.getpid()
    user32 = ctypes.windll.user32
    user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                                      wintypes.LPARAM), wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    found = []

    def cb(hwnd, _lp):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            r = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            if r.right - r.left > 300 and r.bottom - r.top > 300:
                found.append(hwnd)
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                          wintypes.LPARAM)(cb), 0)
    return int(found[0]) if found else 0


def blank_page_watchdog(get_handle, url: str, timeout: float = 22.0) -> None:
    """When the page never paints (solid white/black window), open the browser.

    A stuck WebView2 compositor still runs the page - the JS keeps calling the server
    - but the window shows nothing, which looks exactly like a broken app. Sample the
    window's pixels after it has had time to draw; if it is blank, hand the app to the
    browser (where it always renders) and remember the hang.
    """
    import time
    deadline = time.time() + timeout
    hwnd = 0
    while time.time() < deadline:
        hwnd = _find_own_window_hwnd()
        if hwnd:
            break
        time.sleep(1.0)
    if not hwnd:
        try:
            hwnd = get_handle()
        except Exception:
            hwnd = 0
    if not hwnd:
        return
    time.sleep(12.0)                               # let the page draw first
    frac = _window_fraction_blank(hwnd)
    print("[window] blank-pixel fraction: {:.0f}%".format(frac * 100))
    if frac >= 0.95:
        note_hang("window painted blank (white/black) for ~{}s".format(int(timeout)))
        print("[window] the window is blank (the page runs but nothing painted) - "
              "opening your browser instead.")
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception as exc:
            print("[window] could not open a browser:", exc)
        time.sleep(4)
        os._exit(1)


def pid_alive(pid: int) -> bool:
    """Is that process still running? (Never signals it.)"""
    if not pid or pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return False
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return bool(ok) and code.value == 259        # STILL_ACTIVE
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def rotate_profile_if_stale(storage: Path) -> bool:
    """Drop a WebView2 profile left behind by a killed or crashed window.

    If the previous run never removed its pid file it was killed rather than closed,
    and then the embedded engine's profile can be locked or half-written - which made
    the next window hang forever with no error at all (seen after a test killed the
    window). Rotating it costs only cache and cookies: app settings live on the
    server side. Returns True when a rotation happened.
    """
    try:
        marker = window_pid_file()
        if not marker.is_file():
            return False
        try:
            pid = int(marker.read_text(encoding="utf-8").strip().split()[0])
        except Exception:
            pid = 0
        if pid and pid_alive(pid):
            return False                      # a real window is running: leave it alone
        marker.unlink()
    except Exception:
        return False
    return rotate_profile(storage)


def _carry_settings_over(old: Path, new: Path) -> None:
    """Copy the app's own settings into a fresh profile.

    The profile folder holds the engine's cache and cookies AND the app's UI settings
    (localStorage: theme, sidebar state, provider and per-view AI choices). Rotating
    the profile must not throw those away with the cache, or the user comes back to a
    default-looking app. Cache and cookies are disposable; this is not.
    """
    import shutil
    for rel in ("EBWebView/Default/Local Storage",
                "EBWebView/Default/Session Storage",
                "EBWebView/Default/Preferences"):
        src = old / rel
        if not src.exists():
            continue
        dst = new / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.is_dir():
                shutil.rmtree(dst, ignore_errors=True)
            elif dst.exists():
                dst.unlink()
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
            print("[window] carried {} over to the fresh profile".format(rel))
        except Exception as exc:
            print("[window] could not carry {} over: {}".format(rel, exc))


def rotate_profile(storage: Path) -> bool:
    """Move a broken profile aside and start a fresh one that keeps your settings.

    Only used when the window genuinely failed to appear: a locked or half-written
    profile is then the most likely cause and nothing else helps. Cache and cookies
    go; the app's settings ride along.
    """
    if not storage.exists():
        return False
    try:
        import time as _time
        stale = storage.with_name(storage.name + "-stale-" + str(int(_time.time())))
        storage.rename(stale)
        print("[window] old profile moved to {}".format(stale.name))
    except Exception as exc:
        print("[window] could not rotate the profile ({}); trying anyway".format(exc))
        return False
    _carry_settings_over(stale, storage)
    # Keep only the newest rotated profile, so repeated crashes cannot fill the disk.
    try:
        import shutil
        for old in sorted(storage.parent.glob(storage.name + "-stale-*"))[:-1]:
            shutil.rmtree(old, ignore_errors=True)
    except Exception:
        pass
    return True


APP_ID = "TrioForge.Desktop"

# Set once the native window really exists (the icon callback is the proof).
_window_ready = False
_hang_check_error_logged = False


def fallback_watchdog(url: str, timeout: float = 25.0, storage: Path = None) -> None:
    """If the embedded window never appears: retry once with a fresh profile, then
    fall back to the browser.

    WebView2 is the least reliable part of this app by nature: it loads a private copy
    of Edge's runtime, keeps a profile folder that an unclean shutdown can corrupt,
    and runs its own GPU process on the same driver. Any of those can leave a window
    that never shows. A corrupt or locked profile is the usual cause, so the first
    response is a clean retry with a fresh profile (settings carried over); only if
    that fails too does the app open in a browser, where none of this applies.
    """
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _window_ready:
            return
        time.sleep(0.5)
    if _window_ready:
        return
    print("[window] the embedded window did not appear within {}s".format(int(timeout)))

    retried = os.environ.get("TRIOFORGE_WINDOW_RETRIED") == "1"
    if storage is not None and not retried:
        rotate_profile(storage)
        os.environ["TRIOFORGE_WINDOW_RETRIED"] = "1"
        print("[window] retrying once with a fresh profile (your settings are kept)")
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as exc:
            print("[window] retry failed:", exc)

    print("[window] opening your browser instead (the server is fine)")
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception as exc:
        print("[window] could not open a browser either:", exc)
    time.sleep(5)          # give the browser a moment to appear
    # Leave nothing hung behind: no window, no hidden process. Skipping the normal
    # cleanup is deliberate - the leftover marker marks this run as failed.
    os._exit(1)


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


def _tell_server_to_shutdown(url: str) -> None:
    """Ask the local server to stop itself and its services (best-effort).

    Closing the app window must not leave the server, a loaded llama.cpp model or
    the voice agent running - otherwise "closed" only hides the window while a
    Python process and gigabytes of model stay resident. The server owns those
    services, so we POST /api/shutdown and let it tear everything down.
    """
    import json
    import ssl as _ssl
    import urllib.request
    base = (url or "http://127.0.0.1:5003").rstrip("/")
    ctx = _ssl._create_unverified_context() if base.startswith("https://") else None
    try:
        req = urllib.request.Request(
            base + "/api/shutdown", data=b"{}",
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
            resp.read(100)
        print("[window] told the server to shut down (closing the app stops its services).")
    except Exception as exc:
        print("[window] could not reach the server to shut it down: {}".format(exc))


def _server_up(port: int) -> bool:
    """True when a TrioForge server already answers on the port (either scheme)."""
    for scheme in ("https", "http"):
        if _probe("{}://127.0.0.1:{}/".format(scheme, port)):
            return True
    return False


def _spawn_server(port: int):
    """Start the app's server as a CHILD of this window process.

    The window OWNS the server: running it as a child (instead of the launcher
    starting a detached, unrelated process) is what lets the taskbar and Task
    Manager show one TrioForge app with the server nested under it, and it makes
    "close the window" a clean teardown of the whole stack.
    """
    import subprocess
    script = project_root() / "py" / "app.py"
    env = dict(os.environ)
    env["TRIOFORGE_NO_BROWSER"] = "1"     # the window IS the app; no browser tab
    env["TRIOFORGE_PORT"] = str(port)
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    # Keep the server's own output instead of discarding it. A windowless launch has
    # no console, so DEVNULL meant every traceback and warning vanished - which is
    # exactly why a real failure could only be guessed at from the UI.
    out = subprocess.DEVNULL
    try:
        import time as _time
        log_path = project_root() / "logs" / "server-console.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        out = open(str(log_path), "a", encoding="utf-8", errors="replace")
        out.write("\n===== server started {} =====\n".format(
            _time.strftime("%Y-%m-%d %H:%M:%S")))
        out.flush()
    except Exception:
        out = subprocess.DEVNULL
    try:
        child = subprocess.Popen(
            [sys.executable, str(script)],
            cwd=str(project_root()), env=env, creationflags=flags,
            stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        print("[window] server started as a child process (pid {})".format(child.pid))
        return child
    except Exception as exc:
        print("[window] could not start the server: {}".format(exc))
        return None


def _stop_child(child, timeout: float = 6.0) -> None:
    """Wait for the server child to exit, then force it if it lingers."""
    if child is None or child.poll() is not None:
        return
    try:
        child.wait(timeout=timeout)
    except Exception:
        try:
            child.terminate()
            child.wait(timeout=5)
        except Exception:
            try:
                child.kill()
            except Exception:
                pass


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

    # The window OWNS the server (see _spawn_server). Start it first, then wait for
    # it to come up. With an explicit --url we point at a remote server and start
    # nothing.
    server_child = None
    if not args.url and not _server_up(args.port):
        server_child = _spawn_server(args.port)

    url = resolve_url(args.url, args.port)

    # The app serves HTTPS with a locally generated (mkcert) certificate. WebView2
    # refuses an untrusted certificate and would show an error page instead of the
    # app, so for LOCAL addresses only we tell the embedded engine to accept it.
    # Never done for a remote URL.
    browser_args = []
    if not hardware_gpu_allowed():
        # SwiftShader, not --disable-gpu: --disable-gpu left this machine with a
        # window that never painted (blank white/black). SwiftShader still draws the
        # whole interface, it just rasterises on the CPU.
        browser_args.append("--use-angle=swiftshader")
        print("[window] software rendering via SwiftShader (a previous window stopped "
              "responding; the GPU driver is the suspect)")
    if url.startswith("https://") and is_local(url):
        browser_args.append("--ignore-certificate-errors")

    # Trim the embedded engine. WebView2 shares Edge's runtime, so by default a single
    # window drags in 14 processes and ~1 GB of memory - Edge's shopping, autofill,
    # sync, collections, PDF and update services, none of which this app uses. A
    # browser tab costs ~150-300 MB because it reuses an engine that is already
    # running; these flags are how the app window gets closer to that.
    browser_args += [
        "--disable-features=msEdgeAutofill,msEdgeCommerce,msEdgeShoppingAssistant,"
        "msEdgeCollections,msEdgeSidebar,msEdgeIdentityFeature,msEdgeSyncFeature,"
        "msEdgeTranslate,msEdgePDF,msEdgeReadAloud,msEdgeWebView2DisablePopups,"
        "AutofillServerCommunication,EdgeCollections,EdgeShoppingAssistant,"
        "OptimizationGuideModelDownloading,OptimizationHints,"
        "CalculateNativeWinOcclusion,MediaRouter,Translate",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-domain-reliability",
        "--disable-sync",
        "--disable-extensions",
        "--disable-default-apps",
        "--disable-client-side-phishing-detection",
        "--disable-breakpad",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-service-autorun",
        "--renderer-process-limit=1",
        "--process-per-site",
    ]
    existing = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "").strip()
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
        (existing + " " if existing else "") + " ".join(browser_args)).strip()

    # Links to other sites should open in the real browser, not replace the app.
    try:
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    except Exception:
        pass

    # Let the browser-engine downloads actually happen.
    #
    # pywebview's WebView2 backend wires up DownloadStarting and then cancels every
    # download unless this setting is on (platforms/edgechromium.py:
    # `on_download_starting` -> `if not webview_settings['ALLOW_DOWNLOADS']`). It
    # defaults to off, so the chat export buttons ("All (MD)", "All (JSON)") did
    # nothing in the app window while working perfectly in a browser: the server sent
    # the file with Content-Disposition and the embedded engine silently dropped it.
    #
    # This is the whole difference between "web can" and "the app cannot".
    try:
        webview.settings["ALLOW_DOWNLOADS"] = True
    except Exception:
        pass

    storage = user_data_dir()
    # NO proactive rotation here. It used to run whenever the previous window had not
    # exited cleanly, and it threw the profile away - including the app's UI settings
    # (theme, sidebar, provider, per-view choices), so a crash meant coming back to a
    # default-looking app. A rotation is now a response to a window that actually
    # failed to appear, and it carries those settings over.
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
            pid_file.write_text("{} {} {}\n".format(os.getpid(), url, int(time.time())),
                                encoding="utf-8")
        except Exception:
            pid_file = None

        # Own identity + own icon before the window appears: the host process is
        # pythonw.exe, so without this the taskbar and Alt-Tab say "Python".
        ico = icon_path()
        register_app_id(ico)
        set_process_app_id()
        print("[icon] .ico = {} (exists: {})".format(ico, ico.is_file()))
        # If the window never shows, the user still gets the app (in a browser).
        threading.Thread(target=fallback_watchdog, args=(url, 25.0, storage), daemon=True).start()
        try:
            try:
                webview.start(**start_kwargs,
                              func=lambda: apply_icon_when_ready(window, ico, url),
                              icon=str(ico))
            except TypeError as exc:
                # An older/newer pywebview that does not accept one of these.
                print("[icon] start() rejected an argument ({}); retrying without them".format(exc))
                webview.start(**start_kwargs,
                              func=lambda: apply_icon_when_ready(window, ico, url))
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
    # Closing the window ends the session - but ONLY for a server THIS window started.
    # If one was already running when we opened (server_child is None), it belongs to
    # somebody else: the browser path (start-web), the control panel, or another
    # window. Tearing that down killed the server out from under whoever was using it
    # (the recurring "web vs WebView2" conflict).
    if server_child is not None:
        _tell_server_to_shutdown(url)
        _stop_child(server_child)
    else:
        print("[window] leaving the already-running server alone (this window did not start it)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
