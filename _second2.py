"""Catch what the second launch actually does - output to FILES so a hang loses nothing."""
import os
import subprocess
import sys
import time

ROOT = r"D:\TrioForge"
EXE = os.path.join(ROOT, ".venv", "Scripts", "TrioForge.exe")
MARKER = os.path.join(os.environ["LOCALAPPDATA"], "TrioForge", "app_window.pid")
OUT = os.path.join(os.environ.get("TEMP", "."), "tf_second_out.txt")
ERR = os.path.join(os.environ.get("TEMP", "."), "tf_second_err.txt")


def marker():
    try:
        with open(MARKER, encoding="utf-8") as fh:
            return fh.read().strip()
    except FileNotFoundError:
        return "(absent)"
    except Exception as e:
        return "(unreadable: {})".format(e)


def procs():
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"Name='TrioForge.exe'\" | "
         "ForEach-Object { \"$($_.ProcessId) $($_.CommandLine)\" }"],
        capture_output=True, text=True)
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


print("=== BEFORE ===")
print("marker:", marker())
for p in procs():
    print("  ", p[:120])

for f in (OUT, ERR):
    if os.path.exists(f):
        os.remove(f)

print("\n=== starting a second instance (background, output to files) ===")
fh_out = open(OUT, "w", encoding="utf-8")
fh_err = open(ERR, "w", encoding="utf-8")
child = subprocess.Popen(
    [EXE, "-u", os.path.join(ROOT, "py", "tools", "app_window.py"),
     "--port", "5003", "--title", "TrioForge"],
    cwd=ROOT, stdout=fh_out, stderr=fh_err,
    env=dict(os.environ, PYTHONPATH=os.path.join(ROOT, "py")))

for i in range(1, 9):
    time.sleep(3)
    alive = child.poll() is None
    print("  [{}s] second instance alive={}  marker={!r}".format(i * 3, alive, marker()))
    if not alive:
        print("       -> exited with code", child.returncode)
        break

print("\n=== AFTER ===")
for p in procs():
    print("  ", p[:120])

print("\n=== stdout from the second instance ===")
print(open(OUT, encoding="utf-8", errors="replace").read().strip() or "(nothing)")
print("=== stderr ===")
print(open(ERR, encoding="utf-8", errors="replace").read().strip() or "(nothing)")

if child.poll() is None:
    print("\n*** second instance is STILL RUNNING - killing it to leave things clean")
    subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                   capture_output=True, text=True)
