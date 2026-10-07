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
import atexit
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
    # 720 x 0.25s keeps the same ~3 minute budget as the previous 120 x 1.5s, but a
    # server that is already answering is noticed in a quarter of a second instead of
    # after a sleep. This is the wait the app window does BEFORE it creates itself, so
    # it is on the critical path for how long the window takes to appear: the server
    # needs ~5s to import, and the old 1.5s polling step could add up to that again
    # purely as sleep. Measured: fastfetch and the window both wait on this loop.
    for _ in range(720):                      # up to ~3 minutes of first run
        for scheme in ("https", "http"):
            candidate = "{}://127.0.0.1:{}/".format(scheme, port)
            if _probe(candidate):
                return candidate
        time.sleep(0.25)
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


def _wait_for_visible_window(timeout: float, window=None) -> int:
    """Wait for a real, VISIBLE top-level window; nudge a hidden form to show itself.

    ``window.native`` is NOT proof of a window: pywebview creates the WinForms form
    first and only calls ``Show()`` once WebView2 finishes initialising. When that
    initialisation wedges, the form object exists for ever while nothing is ever drawn
    - and every guard that trusted ``native`` declared the app healthy while the user
    stared at an empty desktop. That is the "double-click does nothing, Task Manager
    fills up with TrioForge" report: an invisible process per click, none of which
    could be seen, focused or closed. So: ask for a visible window, and if only the
    form exists, ask the form to show itself.
    """
    import time
    deadline = time.time() + timeout
    nudged = False
    while True:
        hwnd = _find_own_window_hwnd()
        if hwnd:
            return hwnd
        if time.time() >= deadline:
            return 0
        if window is not None and not nudged and getattr(window, "native", None) is not None:
            nudged = True
            try:
                window.show()
                print("[window] the form existed but was never shown - asked it to show")
            except Exception as exc:
                print("[window] could not show the window: {}: {}".format(
                    type(exc).__name__, exc))
        time.sleep(0.25)


def apply_icon_when_ready(window, ico: Path, url: str = "", timeout: float = 24.0,
                          width: int = 1320, height: int = 880) -> bool:
    """Wait for the VISIBLE window, then put the icon on it and start watching it.

    webview.start(func=...) runs before the GUI window exists, so the first attempt
    finds window.native = None. This is also the only moment we know the window is
    really on screen, so it is where the hang watchdog and the blank watchdog get
    their proof: both need a handle that exists, and the fallback watchdog needs the
    honest answer to "did a window appear at all" - which is why readiness is a
    visible window here and not merely a native form object.
    """
    global _window_ready
    hwnd = _wait_for_visible_window(timeout, window)
    if not hwnd:
        print("[icon] no visible window after {}s - leaving it to the fallback "
              "watchdog".format(int(timeout)))
        return False

    _window_ready = True
    try:
        import threading as _th

        # The visible top-level handle, straight from the OS - no pythonnet IntPtr to
        # mis-convert (that silent failure once made the watchdog check nothing at all).
        def _handle():
            return hwnd

        _th.Thread(target=hang_watchdog, args=(_handle, url), daemon=True).start()
        print("[window] watching for hangs (a frozen window is handed to your browser)")
        _th.Thread(target=blank_page_watchdog, args=(_handle, url), daemon=True).start()
        print("[window] watching for a blank window (a page that never paints is "
              "handed to your browser)")
        _th.Thread(target=size_watchdog, args=(window, _handle, width, height),
                   daemon=True).start()
        print("[window] watching for a collapsed window (restored automatically)")
    except Exception as _exc:
        print("[window] hang watchdog not started:", _exc)

    # Icon work assigns WinForms properties from a non-UI thread, which marshals onto
    # the UI thread - and blocks for ever if that thread is stuck in WebView2 init.
    # Readiness must never depend on it, so it gets its own thread.
    def _icons():
        import time
        apply_window_icon(window, ico)
        time.sleep(1.0)
        apply_window_icon(window, ico)      # second pass: handle realised by now

    threading.Thread(target=_icons, daemon=True).start()
    return True


def window_pid_file() -> Path:
    """Marks an app window as open, so the panel cannot open a second one.

    Written by THIS process (the real interpreter), not by whatever launched it:
    a venv's pythonw.exe is a shim that exits immediately, so a pid recorded by the
    launcher is dead within milliseconds and every attempt would open another
    window.
    """
    return user_data_dir().parent / "app_window.pid"


#: Product name - matches py/tools/autostart.py and shortcuts.py.
APP_NAME = "TrioForge"

#: Image names that count as "our app" when checking whether the pid in the window
#: marker is really a TrioForge window. The launcher ships as TrioForge.exe; a dev
#: run may be pythonw.exe or python.exe. sys.executable's own name is also added at
#: call time, so a renamed or custom-built launcher is still recognised.
#:
#: The launcher name is DERIVED, never written out. A hard-coded "triorforge.exe"
#: carried an extra 'r' (14 characters where the real file is 13), so the SHIPPED
#: launcher never matched its own marker: _pid_is_our_app() always returned False,
#: the single-instance guard never fired, and every second click deleted the marker
#: and started a whole second instance. The two then fought over port 5003 and the
#: one locked WebView2 profile, so neither window painted - the reported "I launch
#: it and it does not respond". Building it from APP_NAME makes that typo
#: impossible, and a regression test pins it against the real file on disk.
_OUR_EXE_NAMES = ("pythonw.exe", "python.exe", APP_NAME.lower() + ".exe")


def _pid_is_our_app(pid: int) -> bool:
    """True when `pid` really is a TrioForge/pywebview process, not a reused pid.

    The marker outlives a killed window and Windows reuses pids, so the number alone
    is not proof. Returns True when the check cannot be made - a failure here must
    never stop the app from starting.
    """
    if os.name != "nt" or not pid:
        return True
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1000, False, int(pid))     # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            size = wintypes.DWORD(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value).lower()
                ours = set(_OUR_EXE_NAMES)
                try:
                    ours.add(os.path.basename(sys.executable).lower())
                except Exception:
                    pass
                return name in ours
        finally:
            k32.CloseHandle(handle)
    except Exception:
        pass
    return True


def claim_window_slot(url: str = "") -> tuple:
    """Claim the single app-window slot atomically. -> (path_or_None, already_open)

    This is the fix for "double-click does nothing and Task Manager fills up with
    TrioForge". Two separate faults made one click lethal:

    1. The marker used to be written just before ``webview.start()`` - after
       ``_spawn_server()`` and ``create_window()``, seconds later. The launcher's
       "a window is already open" guard reads this marker, so in that gap every
       impatient double-click started ANOTHER complete window process.
    2. Even written early, a plain ``write_text`` is not a claim: six clicks landing
       inside the same second all saw an empty slot and all wrote the file. Six
       processes then raced for port 5003 and fought over one locked WebView2 profile
       folder - so none of them ever painted, which is exactly what was reported.

    So the write is an ``O_CREAT|O_EXCL`` create (only one process can win it), and a
    loser that finds a LIVE owner backs off instead of joining the stampede. A marker
    left by a killed window is stale: it is taken over, not obeyed.
    """
    marker = window_pid_file()
    payload = "{} {} {}\n".format(os.getpid(), url or "", int(time.time()))
    for _ in range(2):
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            pid = 0
            try:
                pid = int(marker.read_text(encoding="utf-8").strip().split()[0])
            except Exception:
                pid = 0
            if pid and pid != os.getpid() and pid_alive(pid) and _pid_is_our_app(pid):
                return None, True              # a live window owns it: back off
            try:
                marker.unlink()                # stale from a killed window: take over
            except Exception:
                return None, False             # cannot clear it; start anyway
            continue
        except Exception:
            return None, False                 # no usable marker; never block the app
        try:
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
        return marker, False
    return None, True


def focus_existing_window(pid: int) -> bool:
    """Raise the window that already owns the slot, so a click is never a no-op."""
    if os.name != "nt" or not pid:
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        found = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def callback(hwnd, _lparam):
            owner = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == int(pid) and user32.IsWindowVisible(hwnd):
                found.append(hwnd)
            return True

        user32.EnumWindows(WNDENUMPROC(callback), 0)
        if not found:
            return False
        hwnd = found[0]
        user32.ShowWindow(hwnd, 9)             # SW_RESTORE: un-minimise
        user32.ShowWindow(hwnd, 5)             # SW_SHOW
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def hang_marker() -> Path:
    """Records that the window hung, so the next start can avoid the same cause."""
    return user_data_dir().parent / "window_hung.txt"


# How long a recorded hang keeps the app on software rendering. Long enough to survive
# the retry right after a bad launch, short enough that a driver fix is not punished
# for ever.
HANG_MEMORY_SECONDS = 24 * 3600


def hardware_gpu_allowed() -> bool:
    """False after a recent hang: the next start uses software rendering.

    A hung WebView2 window is almost always its renderer or GPU process stalling - and
    this machine's GPU driver has already failed a Vulkan allocation for llama.cpp, so
    it is the prime suspect. The software path is SwiftShader (``--use-angle=swiftshader``),
    NOT ``--disable-gpu``: measured on the machine that hit this, ``--disable-gpu``
    produced a window that never painted at all (blank white, or black), while
    SwiftShader drew the whole interface correctly. Stability beats decoration - but
    only if the page is actually drawn.

    The memory EXPIRES. It used to be permanent, so a single bad night left the app on
    CPU rendering for ever: the marker on this machine was eighteen days old and still
    docking the GPU. A driver problem that has been fixed (by an update, a reboot, or
    just one unlucky process) must not punish every later launch.
    """
    if os.environ.get("TRIOFORGE_WINDOW_HARDWARE", "").strip() in ("1", "true", "on"):
        return True
    if os.environ.get("TRIOFORGE_WINDOW_SOFTWARE", "").strip() in ("1", "true", "on"):
        return False
    marker = hang_marker()
    if not marker.is_file():
        return True
    try:
        if time.time() - marker.stat().st_mtime > HANG_MEMORY_SECONDS:
            return True
    except Exception:
        return True
    return False


def clear_hang_marker() -> None:
    """Forget the last hang: a window that painted proves the current path works."""
    try:
        hang_marker().unlink()
        print("[window] the window painted normally - hardware rendering allowed again")
    except Exception:
        pass


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


def size_watchdog(window, get_handle, width: int, height: int, interval: float = 3.0) -> None:
    """Restore the window when WebView2 collapses it to a sliver.

    On this machine the embedded window sometimes shrinks to ~160x28 AFTER it has
    already painted - a WebView2/GPU quirk, not a user action. A 160x28 window is
    invisible in practice, which is the "double-click, nothing appears" report that
    survived every other fix. A user minimisation (IsIconic) is left alone, so this
    never fights a deliberate Minimise.
    """
    import time
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    while True:
        time.sleep(interval)
        if not _window_ready:
            continue
        try:
            hwnd = get_handle()
        except Exception:
            continue
        if not hwnd:
            continue
        try:
            r = wintypes.RECT()
            if not user32.GetWindowRect(wintypes.HWND(int(hwnd)), ctypes.byref(r)):
                continue
            w, h = r.right - r.left, r.bottom - r.top
            if w >= 300 and h >= 300:
                continue
            if bool(user32.IsIconic(wintypes.HWND(int(hwnd)))):
                continue                       # the user minimised it - respect that
            # Collapsed by the engine, not by the user: put the size back.
            try:
                window.resize(int(width), int(height))
                try:
                    window.show()
                except Exception:
                    pass
                print("[window] window collapsed to {}x{} - restored {}x{}".format(
                    w, h, width, height))
            except Exception:
                try:
                    SWP_NOZORDER = 0x0004
                    user32.SetWindowPos(wintypes.HWND(int(hwnd)), 0, 0, 0,
                                        int(width), int(height), SWP_NOZORDER)
                    print("[window] Win32 resize to {}x{}".format(width, height))
                except Exception as exc:
                    print("[window] could not restore size: {}: {}".format(
                        type(exc).__name__, exc))
        except Exception as exc:
            print("[window] size check failed: {}: {}".format(type(exc).__name__, exc))


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
    if frac < 0.95 and hardware_gpu_allowed():
        # A window on the hardware path that actually painted is the proof the driver
        # is fine again - hand the GPU back instead of staying on SwiftShader for ever.
        clear_hang_marker()
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

# Set once a real, VISIBLE window is on screen - not when the form object merely
# exists. Every watchdog keys off this, so it must never be a guess: a form that was
# never shown used to satisfy it, which is how invisible TrioForge processes piled up
# while the app looked like it would not launch at all.
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


def claim_app_identity() -> None:
    """Register TrioForge with the shell and stamp THIS process as TrioForge.

    Call this before spawning anything. Task Manager groups processes by their
    AppUserModelID, and a child inherits the AUMID of the parent that created it - so a
    process started before this call shows up as its own anonymous entry instead of
    nesting under TrioForge. That is exactly what happened: the window spawned the
    server at line ~724 and only claimed the identity at line ~861, so the server (and
    anything it spawns in turn) appeared as a second, unrelated entry.

    Idempotent and silent: safe to call from every entry point, and never raises.
    """
    try:
        register_app_id(icon_path())
    except Exception:
        pass
    set_process_app_id()


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

    # Claim the TrioForge identity FIRST, before this process spawns anything. Children
    # inherit the parent's AppUserModelID, so a server started before this call lands in
    # Task Manager as a separate anonymous entry instead of nesting under TrioForge.
    claim_app_identity()

    # Then claim the WINDOW SLOT, before any slow work. This marker is what the
    # launcher's "a window is already open" guard reads, so it must exist the moment
    # this process does: it used to be written just before webview.start() - AFTER
    # _spawn_server() and create_window(), i.e. several seconds later. In that gap the
    # guard saw no marker at all, so every impatient double-click started ANOTHER
    # complete window process: six clicks meant six processes, six servers racing for
    # port 5003, and six WebView2 instances fighting over one locked profile folder -
    # so none of them ever painted, which is the "double-click does nothing, Task
    # Manager fills up" report. The claim is also exclusive (O_CREAT|O_EXCL), so even
    # clicks landing in the same second cannot all win it.
    pid_file, already_open = claim_window_slot(args.url or "")
    if already_open:
        prior = 0
        try:
            prior = int(window_pid_file().read_text(encoding="utf-8").strip().split()[0])
        except Exception:
            prior = 0
        print("[window] TrioForge is already opening or open (pid {}) - raising it "
              "instead of starting a second window".format(prior or "?"))
        focus_existing_window(prior)
        return 0
    if pid_file is not None:
        def _drop_pid_file():
            try:
                pid_file.unlink()
            except Exception:
                pass

        atexit.register(_drop_pid_file)

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
        # The pid file was already claimed at the top of main(); see there for why
        # it cannot wait until this point.

        # Own identity + own icon before the window appears: the host process is
        # pythonw.exe, so without this the taskbar and Alt-Tab say "Python".
        ico = icon_path()
        register_app_id(ico)
        set_process_app_id()
        print("[icon] .ico = {} (exists: {})".format(ico, ico.is_file()))
        # If the window never shows, the user still gets the app (in a browser). The
        # timeout sits just past the visible-window wait above, so a slow-but-real
        # start wins the race and only a window that truly never appeared falls back.
        threading.Thread(target=fallback_watchdog, args=(url, 30.0, storage), daemon=True).start()
        try:
            try:
                webview.start(**start_kwargs,
                              func=lambda: apply_icon_when_ready(
                                  window, ico, url,
                                  width=args.width, height=args.height),
                              icon=str(ico))
            except TypeError as exc:
                # An older/newer pywebview that does not accept one of these.
                print("[icon] start() rejected an argument ({}); retrying without them".format(exc))
                webview.start(**start_kwargs,
                              func=lambda: apply_icon_when_ready(
                                  window, ico, url,
                                  width=args.width, height=args.height))
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
