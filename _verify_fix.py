"""Verify the fix: the guard must recognise our own launcher, and a second click
must exit in about a second instead of starting another instance."""
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
TMP = os.environ.get("TEMP", ".")

sys.path.insert(0, os.path.join(ROOT, "py"))
from tools import app_window as aw

user32 = ctypes.windll.user32
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)


def window_pids():
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


def marker():
    try:
        with open(MARKER, encoding="utf-8") as fh:
            return fh.read().strip()
    except FileNotFoundError:
        return "(absent)"


subprocess.run(["taskkill", "/IM", "TrioForge.exe", "/T", "/F"], capture_output=True)
time.sleep(2)

print("=== 1. the allow-list now matches the shipped launcher ===")
print("   _OUR_EXE_NAMES =", aw._OUR_EXE_NAMES)
print("   TrioForge.exe lowercased in allow-list ->",
      "triorforge.exe" in aw._OUR_EXE_NAMES)

print("\n=== 2. cold start ===")
t0 = time.time()
subprocess.Popen(ARGS, cwd=ROOT, stdout=subprocess.DEVNULL,
                 stderr=subprocess.DEVNULL, env=ENV)
pid = None
for _ in range(120):
    time.sleep(0.25)
    p = window_pids()
    if p:
        pid = p[0]
        break
print("   visible window after {:.2f}s (pid {})".format(time.time() - t0, pid))
print("   marker:", marker())

print("\n=== 3. the guard's identity check against the live window ===")
print("   pid_alive({})       -> {}".format(pid, aw.pid_alive(pid)))
print("   _pid_is_our_app({}) -> {}".format(pid, aw._pid_is_our_app(pid)))

print("\n=== 4. SECOND CLICK while it is open ===")
out = os.path.join(TMP, "tf_fix_second.txt")
fh = open(out, "w", encoding="utf-8")
t1 = time.time()
child = subprocess.Popen(ARGS, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT, env=ENV)
try:
    rc = child.wait(timeout=45)
    print("   second instance exited after {:.2f}s (code {})".format(time.time() - t1, rc))
except subprocess.TimeoutExpired:
    print("   STILL not exiting after 45s - fix did not work")
    subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], capture_output=True)
time.sleep(0.5)
print("   it printed:")
print("   " + (open(out, encoding="utf-8", errors="replace").read().strip() or "(nothing)")
      .replace("\n", "\n   "))

print("\n=== 5. the first window must still be alive and visible ===")
alive = window_pids()
print("   visible TrioForge windows now:", alive)
print("   original pid {} still visible: {}".format(pid, pid in alive))
print("   marker now:", marker())
