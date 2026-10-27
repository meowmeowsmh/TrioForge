"""Launch a program under a hard RAM cap (Windows Job Object).

Instead of killing processes to claw RAM back, this starts a program inside a
Windows Job Object whose memory is capped: when the program exceeds the cap,
Windows refuses it more memory (one tab may crash) but the rest of the machine
stays alive. That is the "cap, don't kill" answer to "Chrome ate all my RAM".

Usage:
    python py/tools/ram_cap.py 2048 "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
    python py/tools/ram_cap.py 2048 "C:\\path\\app.exe" -- arg1 arg2

The wrapper must keep running while the target runs (it holds the Job Object,
and the Job Object holds the cap), so it exits only when the target does. If the
wrapper is killed first, KILL_ON_JOB_CLOSE ends the target too - a capped app
never outlives its cap.
"""

from __future__ import annotations

import argparse
import ctypes
import subprocess
import sys
from ctypes import wintypes

JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9
CREATE_NO_WINDOW = 0x08000000


class _BASIC(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),      # ULONG_PTR
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IO(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _EXTENDED(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BASIC),
        ("IoInfo", _IO),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateJobObjectW.restype = wintypes.HANDLE
_k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
_k32.SetInformationJobObject.restype = wintypes.BOOL
_k32.SetInformationJobObject.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
_k32.AssignProcessToJobObject.restype = wintypes.BOOL
_k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_k32.CloseHandle.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [wintypes.HANDLE]


def _create_job(cap_bytes: int) -> int:
    """A Job Object whose combined process memory may not exceed ``cap_bytes``."""
    job = _k32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    info = _EXTENDED()
    info.BasicLimitInformation.LimitFlags = (
        JOB_OBJECT_LIMIT_PROCESS_MEMORY | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
    info.ProcessMemoryLimit = cap_bytes
    if not _k32.SetInformationJobObject(
            job, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info)):
        _k32.CloseHandle(job)
        raise ctypes.WinError(ctypes.get_last_error())
    return job


def launch(cmd: list[str], cap_mb: int) -> int:
    """Start ``cmd`` under a ``cap_mb`` MB RAM cap; block until it exits."""
    if cap_mb < 64:
        raise SystemExit("error: a cap below 64 MB is almost certainly a typo")
    job = _create_job(int(cap_mb) * 1024 * 1024)
    proc = subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW)
    if not _k32.AssignProcessToJobObject(job, wintypes.HANDLE(proc._handle)):
        _k32.CloseHandle(job)
        proc.kill()
        raise ctypes.WinError(ctypes.get_last_error())
    print(f"capped {proc.pid} ({cmd[0]}) to {cap_mb} MB — this window is the cap "
          f"and must stay open", file=sys.stderr)
    try:
        return proc.wait()
    finally:
        _k32.CloseHandle(job)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Launch a program under a hard RAM cap (Windows Job Object).")
    ap.add_argument("cap_mb", type=int, help="RAM cap in megabytes")
    ap.add_argument("exe", help="program to launch")
    ap.add_argument("args", nargs=argparse.REMAINDER,
                    help="arguments for the program (after a bare --)")
    ns = ap.parse_args(argv)
    cmd = [ns.exe]
    if ns.args and ns.args[0] == "--":
        ns.args = ns.args[1:]
    cmd += ns.args
    return launch(cmd, ns.cap_mb)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
