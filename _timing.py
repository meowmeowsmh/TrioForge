"""Measure a real cold start: how long until a VISIBLE TrioForge window exists?

Also times a second launch while the first is fully up (the 'impatient second
click' case), which the code intends to answer in about a second.
"""
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

user32 = ctypes.windll.user32
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)


def visible_trio_windows():
    out = []
    PIDS = {}

    def cb(hwnd, _lp):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        PIDS[hwnd] = pid.value
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if buf.value == "TrioForge":
            out.append(pid.value)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def kill_all():
    subprocess.run(["taskkill", "/IM", "TrioForge.exe", "/T", "/F"],
                   capture_output=True, text=True)
    time.sleep(2)


print("=== clearing any running instance ===")
kill_all()

print("\n=== COLD START (what happens on the first click) ===")
t0 = time.time()
fh = open(os.path.join(os.environ.get("TEMP", "."), "tf_cold.txt"), "w", encoding="utf-8")
child = subprocess.Popen(ARGS, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT, env=ENV)

first_seen = None
for i in range(120):
    time.sleep(0.25)
    pids = visible_trio_windows()
    if pids and first_seen is None:
        first_seen = time.time() - t0
        print("  visible TrioForge window after {:.2f}s (pid {})".format(first_seen, pids[0]))
        break
    if i % 8 == 0:
        print("    ...{:.1f}s - no visible window yet".format(time.time() - t0))

if first_seen is None:
    print("  NO visible window within 30s")
    first_seen = 30.0

print("\n=== SECOND CLICK while it is open ===")
t1 = time.time()
fh2 = open(os.path.join(os.environ.get("TEMP", "."), "tf_second3.txt"), "w", encoding="utf-8")
child2 = subprocess.Popen(ARGS, cwd=ROOT, stdout=fh2, stderr=subprocess.STDOUT, env=ENV)
try:
    rc = child2.wait(timeout=60)
    print("  second instance exited after {:.2f}s with code {}".format(
        time.time() - t1, rc))
except subprocess.TimeoutExpired:
    print("  second instance DID NOT EXIT within 60s  <-- this is the 'no response'")
    subprocess.run(["taskkill", "/PID", str(child2.pid), "/T", "/F"],
                   capture_output=True, text=True)

time.sleep(1)
print("  --- what the second instance printed ---")
print(open(os.path.join(os.environ.get("TEMP", "."), "tf_second3.txt"),
           encoding="utf-8", errors="replace").read().strip() or "(nothing)")

print("\n=== cold-start output ===")
print(open(os.path.join(os.environ.get("TEMP", "."), "tf_cold.txt"),
           encoding="utf-8", errors="replace").read().strip()[:1500] or "(nothing)")
