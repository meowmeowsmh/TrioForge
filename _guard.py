"""Run the guard's own identity check against a LIVE window process, step by step."""
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

ROOT = r"D:\TrioForge"
EXE = os.path.join(ROOT, ".venv", "Scripts", "TrioForge.exe")
ARGS = [EXE, "-u", os.path.join(ROOT, "py", "tools", "app_window.py"),
        "--port", "5003", "--title", "TrioForge"]
ENV = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "py"))
MARKER = os.path.join(os.environ["LOCALAPPDATA"], "TrioForge", "app_window.pid")

sys.path.insert(0, os.path.join(ROOT, "py"))
from tools import app_window as aw

user32 = ctypes.windll.user32
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)


def visible_window_pids():
    pids = []

    def cb(hwnd, _lp):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value == "TrioForge":
                pids.append(pid.value)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return pids


subprocess.run(["taskkill", "/IM", "TrioForge.exe", "/T", "/F"], capture_output=True)
time.sleep(2)
print("starting a fresh instance...")
subprocess.Popen(ARGS, cwd=ROOT, stdout=subprocess.DEVNULL,
                 stderr=subprocess.DEVNULL, env=ENV)

pid = None
for _ in range(120):
    time.sleep(0.25)
    p = visible_window_pids()
    if p:
        pid = p[0]
        break
if pid is None:
    print("no visible window appeared")
    raise SystemExit(1)
print("visible TrioForge window pid =", pid)

try:
    with open(MARKER, encoding="utf-8") as fh:
        print("marker file contents      :", repr(fh.read().strip()))
except Exception as e:
    print("marker unreadable:", e)

print("\n--- the guard's own functions ---")
print("pid_alive({})        -> {}".format(pid, aw.pid_alive(pid)))
print("_pid_is_our_app({})  -> {}".format(pid, aw._pid_is_our_app(pid)))

print("\n--- replicating the identity check by hand ---")
k32 = ctypes.windll.kernel32
handle = k32.OpenProcess(0x1000, False, int(pid))
print("OpenProcess(0x1000)      -> handle = {}".format(handle))
if handle:
    size = wintypes.DWORD(32768)
    buf = ctypes.create_unicode_buffer(size.value)
    ok = k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
    print("QueryFullProcessImageNameW -> {}".format(bool(ok)))
    print("  image path : {}".format(buf.value))
    print("  basename   : {}".format(os.path.basename(buf.value).lower()))
    print("  in allowlist (pythonw.exe/python.exe/triorforge.exe) -> {}".format(
        os.path.basename(buf.value).lower() in ("pythonw.exe", "python.exe", "triorforge.exe")))
    k32.CloseHandle(handle)
else:
    print("  OpenProcess FAILED -> _pid_is_our_app returns False -> guard takes over "
          "the slot and starts a SECOND instance")
