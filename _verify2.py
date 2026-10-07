"""Verify the derived launcher name matches reality, and the guard now says True."""
import importlib
import os
import sys

ROOT = r"D:\TrioForge"
sys.path.insert(0, os.path.join(ROOT, "py"))
from tools import app_window as aw

importlib.reload(aw)

real = os.path.join(ROOT, ".venv", "Scripts", "TrioForge.exe")
real_name = os.path.basename(real).lower()

print("APP_NAME        :", repr(aw.APP_NAME))
print("_OUR_EXE_NAMES  :", aw._OUR_EXE_NAMES)
print("  with lengths  :", [(n, len(n)) for n in aw._OUR_EXE_NAMES])
print()
print("real launcher   :", real, "(exists:", os.path.isfile(real), ")")
print("real basename   :", repr(real_name), "len", len(real_name))
print()
print("real name in allow-list ->", real_name in aw._OUR_EXE_NAMES)
assert real_name in aw._OUR_EXE_NAMES, "the allow-list still does not match the shipped launcher"

# No typo can hide: every non-python entry must be the product name + .exe.
for n in aw._OUR_EXE_NAMES:
    if n not in ("python.exe", "pythonw.exe"):
        assert n == aw.APP_NAME.lower() + ".exe", n
        assert len(n) == len(aw.APP_NAME) + 4, (n, len(n))

print("\n--- live check against the running window ---")
MARKER = os.path.join(os.environ["LOCALAPPDATA"], "TrioForge", "app_window.pid")
try:
    with open(MARKER, encoding="utf-8") as fh:
        pid = int(fh.read().strip().split()[0])
except Exception as e:
    print("no marker:", e)
    pid = 0

if pid:
    print("marker pid          :", pid)
    print("pid_alive           :", aw.pid_alive(pid))
    print("_pid_is_our_app     :", aw._pid_is_our_app(pid))
    assert aw._pid_is_our_app(pid) is True, "guard STILL fails to recognise our own window"
    print("\nPASS: the guard recognises the live TrioForge window")
else:
    print("(no live window to check - start one first)")
