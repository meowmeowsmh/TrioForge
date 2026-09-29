"""What this machine can actually run - Apple, Linux and Windows.

Deciding whether a model fits needs two numbers: how much GPU memory the model
may use, and how much system RAM is free. The RAM half is easy (psutil). The GPU
half is the hard part, because there is no portable API and every vendor is
different:

    Apple Silicon   unified memory: the GPU borrows system RAM (Metal), so there
                    is no separate VRAM number to read at all
    NVIDIA          NVML, via pynvml
    AMD             sysfs on Linux; the Windows driver hides it
    Intel           sysfs on Linux; the iGPU shares system RAM
    Windows         WMI's AdapterRAM is a 32-bit field, so it is wrong above 4 GB

So detection runs in an order that prefers the LOADER'S OWN answer:

    1. ``llama-server --list-devices``  - the binary we actually run reports every
       backend it was built with (Metal, Vulkan, ROCm, SYCL, CUDA) with total and
       free MiB per device. When it works, this is the truth, on every platform.
    2. vendor tools (NVML, Apple unified budget, DRM sysfs, system_profiler, WMI)
    3. nothing - the caller then falls back to RAM-only, which is what a machine
       genuinely without a usable GPU should do

Nothing here raises: a failed probe returns 0/None and is recorded in ``notes``,
so a wrong-looking recommendation can always be explained.
"""

from __future__ import annotations

import glob
import json
import os
import platform
import re
import subprocess
import sys
import time

GB = 1024 ** 3
_MIB = 1024 ** 2

# Asking llama.cpp for its device list costs a process spawn, and the answer is
# worth a few seconds of staleness: free VRAM moves, the hardware does not.
GPU_TTL = 5.0
_gpu_cache = {"at": 0.0, "value": None}

# A GGUF needs its file size, plus room for the KV cache and the compute buffers.
KV_OVERHEAD_GB = 1.5
FILE_OVERHEAD = 1.12

# Apple Silicon: the GPU may wire roughly this share of unified memory. Only used
# when the kernel does not publish a wired limit (iogpu.wired_limit_mb).
APPLE_UNIFIED_SHARE = 0.75


def _run(cmd, timeout=20):
    """Run a command, return stdout text or '' - never raise."""
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        done = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=timeout, creationflags=flags)
        return (done.stdout or b"").decode("utf-8", "replace")
    except Exception:
        return ""


# ------------------------------------------------------------------- system


_system_cache = None


def system():
    """OS, CPU and interpreter. Memoised: the CPU does not change, and on macOS
    probing it spawns sysctl - which a UI refreshing four times a second must not
    pay for."""
    global _system_cache
    if _system_cache is None:
        _system_cache = _probe_system()
    return _system_cache


def _probe_system():
    info = {
        "os": platform.system(),                       # Linux / Darwin / Windows
        "os_release": platform.release(),
        "machine": platform.machine(),                 # x86_64 / arm64 / AMD64
        "python": platform.python_version(),
        "cpu": platform.processor() or "",
        "cpu_count": os.cpu_count() or 0,
    }
    if info["os"] == "Darwin":
        info["cpu"] = _run(["sysctl", "-n", "machdep.cpu.brand_string"]).strip() \
            or info["cpu"] or platform.machine()
        info["apple_silicon"] = info["machine"] in ("arm64", "aarch64")
    elif info["os"] == "Linux":
        try:
            with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.lower().startswith("model name"):
                        info["cpu"] = line.split(":", 1)[1].strip()
                        break
        except OSError:
            pass
    elif info["os"] == "Windows":
        info["cpu"] = os.environ.get("PROCESSOR_IDENTIFIER", info["cpu"])
    return info


def ram():
    """Total and available system RAM, in bytes. available is what matters."""
    total = free = 0
    try:
        import psutil
        vm = psutil.virtual_memory()
        total, free = int(vm.total), int(vm.available)
    except Exception:
        pass
    if not total:
        # No psutil: get what we can, so the fit decision is not made blind.
        if os.name == "nt":
            out = _run(["wmic", "ComputerSystem", "get", "TotalPhysicalMemory"])
            m = re.search(r"(\d{6,})", out)
            total = int(m.group(1)) if m else 0
        elif sys.platform == "darwin":
            m = re.search(r"hw\.memsize:\s*(\d+)", _run(["sysctl", "hw.memsize"]))
            total = int(m.group(1)) if m else 0
        else:
            try:
                with open("/proc/meminfo", encoding="utf-8") as fh:
                    for line in fh:
                        if line.startswith("MemTotal:"):
                            total = int(line.split()[1]) * 1024
                        elif line.startswith("MemAvailable:"):
                            free = int(line.split()[1]) * 1024
            except OSError:
                pass
        free = free or total
    return {"ram_total": total, "ram_free": free}


# ------------------------------------------------------------------- the GPU

_DEVICE_RE = re.compile(
    r"\b([A-Za-z]+?)(\d+):\s+(.+?)\s*\((\d+)\s*MiB,\s*(\d+)\s*MiB free\)")

_VENDOR = (
    ("apple", ("metal", "mtl", "apple")),
    ("nvidia", ("cuda", "nvidia", "geforce", "quadro", "rtx", "tesla")),
    ("amd", ("rocm", "amd", "radeon", "vega", "navi", "gfx")),
    ("intel", ("sycl", "intel", "arc", "iris", "uhd")),
)


def _vendor_of(text):
    low = (text or "").lower()
    for vendor, needles in _VENDOR:
        if any(n in low for n in needles):
            return vendor
    return "unknown"


# An integrated GPU has no memory of its own - it carves its "VRAM" out of the
# same system RAM that `ram()` reports. Adding the two together (or adding an
# iGPU's share to a dGPU's) counts the same gigabytes twice and makes the fit
# verdict far too optimistic: on a hybrid laptop it claimed 15.8 GB of GPU
# memory where the discrete card has 8 GB and the rest was system RAM.
_SHARED_PATTERNS = (
    "radeon graphics", "radeon vega", "vega 8", "vega 11", "intel uhd",
    "intel iris", "intel hd", "raphael", "renoir", "cezanne", "barcelo",
    "lucienne", "phoenix", "rembrandt", "dg1", "uhd graphics",
)
_SHARED_RE = re.compile(r"radeon\s+\d{3}\s*m\b")     # Ryzen APUs: 610M, 780M


def _integrated(name):
    """True when this GPU's memory is really a slice of system RAM."""
    low = (name or "").lower()
    if any(p in low for p in _SHARED_PATTERNS):
        return True
    return _SHARED_RE.search(low) is not None


def _llamacpp_devices():
    """Devices as llama.cpp sees them - the loader's own view, all backends.

    Returns (devices, source_note). Each device is
    {"label", "name", "total", "free", "backend", "vendor"} with byte counts.
    """
    exe = None
    try:
        import llamacpp_service
        exe = llamacpp_service._resolve_llama_server_exe(
            os.environ.get("LLAMA_SERVER", "")) or None
    except Exception:
        exe = None
    if not exe:
        try:
            import llama_installer
            exe = llama_installer.find_installed()
        except Exception:
            exe = None
    if not exe or not os.path.exists(exe):
        return [], "llama.cpp binary not found"

    text = _run([exe, "--list-devices"], timeout=30)
    devices = []
    for line in text.splitlines():
        m = _DEVICE_RE.search(line)
        if not m:
            continue
        backend, index, name = m.group(1), m.group(2), m.group(3).strip()
        total, free = int(m.group(4)) * _MIB, int(m.group(5)) * _MIB
        devices.append({
            "label": backend + index,
            "name": name,
            "total": total,
            "free": free,
            "backend": backend,
            "vendor": _vendor_of(backend + " " + name),
        })
    if not devices:
        return [], "llama.cpp reported no devices"
    return devices, "llama.cpp --list-devices"


def _nvidia():
    try:
        import warnings
        warnings.filterwarnings("ignore", message=".*pynvml package is deprecated.*")
        import pynvml
        pynvml.nvmlInit()
        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            try:
                name = pynvml.nvmlDeviceGetName(handle)
                if isinstance(name, bytes):
                    name = name.decode("utf-8", "replace")
            except Exception:
                name = "NVIDIA GPU"
            return {"name": name, "total": int(mem.total), "free": int(mem.free)}
        finally:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass
    except Exception:
        return None


def _apple_unified(total_ram):
    """Apple Silicon: there is no separate VRAM, so budget a slice of RAM."""
    if not total_ram:
        return None
    budget = 0
    limit = _run(["sysctl", "-n", "iogpu.wired_limit_mb"]).strip()
    if limit.isdigit() and int(limit) > 0:
        budget = int(limit) * _MIB
    note = "wired limit"
    if not budget:
        budget = int(total_ram * APPLE_UNIFIED_SHARE)
        note = "~{:.0f}% of unified memory (estimate)".format(APPLE_UNIFIED_SHARE * 100)
    name = _run(["sysctl", "-n", "machdep.cpu.brand_string"]).strip() or "Apple GPU"
    return {"name": name, "total": budget, "free": budget, "note": note}


def _apple_discrete():
    """Intel Macs with a discrete GPU: system_profiler reports its VRAM."""
    text = _run(["system_profiler", "SPDisplaysDataType"], timeout=30)
    if not text:
        return None
    name = ""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("Chipset Model:"):
            name = s.split(":", 1)[1].strip()
        m = re.match(r"VRAM \(Total\):\s*(\d+)\s*(GB|MB)", s)
        if m:
            size = int(m.group(1)) * (GB if m.group(2) == "GB" else _MIB)
            return {"name": name or "Discrete GPU", "total": size, "free": size}
    return None


def _linux_drm():
    """AMD/Intel on Linux: the DRM sysfs nodes carry VRAM total and used."""
    best = None
    for total_path in sorted(glob.glob("/sys/class/drm/card*/device/mem_info_vram_total")):
        try:
            with open(total_path, encoding="utf-8") as fh:
                total = int(fh.read().strip())
        except (OSError, ValueError):
            continue
        if total <= 0:
            continue
        used = 0
        used_path = total_path.replace("_total", "_used")
        try:
            with open(used_path, encoding="utf-8") as fh:
                used = int(fh.read().strip())
        except (OSError, ValueError):
            used = 0
        if best is None or total > best["total"]:
            best = {"name": "GPU (DRM)", "total": total,
                    "free": max(0, total - used), "path": total_path}
    return best


def _windows_wmi():
    """AMD/Intel on Windows. AdapterRAM is 32-bit, so treat it as a floor only."""
    out = _run(["powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_VideoController | "
                "Select-Object Name,AdapterRAM | ConvertTo-Json -Compress"], timeout=40)
    if not out.strip():
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    rows = data if isinstance(data, list) else [data]
    best = None
    for row in rows:
        size = row.get("AdapterRAM") or 0
        try:
            size = int(size)
        except (TypeError, ValueError):
            size = 0
        if size <= 0:
            continue
        if best is None or size > best["total"]:
            best = {"name": row.get("Name") or "GPU", "total": size, "free": size}
    if best:
        best["approximate"] = True
    return best


def gpu(refresh=False):
    """The best description of the GPU memory this machine can offer.

    Returns a dict; ``source`` says where the number came from so a surprising
    recommendation can be explained, and ``notes`` records what was tried.
    Cached for GPU_TTL seconds - probing spawns a process.
    """
    now = time.time()
    if not refresh and _gpu_cache["value"] is not None \
            and now - _gpu_cache["at"] < GPU_TTL:
        return _gpu_cache["value"]
    result = _probe_gpu()
    _gpu_cache["at"] = time.time()
    _gpu_cache["value"] = result
    return result


def _probe_gpu():
    notes = []
    devices, why = _llamacpp_devices()
    if devices:
        # Judge on ONE device - never the sum. On a hybrid laptop the iGPU's
        # "VRAM" is system RAM, so adding it to the dGPU's invents memory that
        # does not exist (measured here: AMD 610M 7.8 GB + RTX 5060 8.0 GB was
        # reported as 15.8 GB, which would have called a 12 GB model "gpu").
        # Prefer a dedicated card, then the largest one.
        dedicated = [d for d in devices if not _integrated(d["name"])]
        primary = max(dedicated or devices, key=lambda d: d["total"])
        shared = _integrated(primary["name"]) or primary["vendor"] == "apple"
        if len(devices) > 1:
            notes.append(
                "{} GPUs present ({}); fit is judged on {} alone - the others are "
                "not added, because an integrated GPU shares system RAM".format(
                    len(devices), ", ".join(d["name"] for d in devices),
                    primary["name"]))
        if shared:
            notes.append("{} shares memory with the CPU (unified)".format(primary["name"]))
        return {
            "name": primary["name"],
            "vendor": primary["vendor"],
            "backend": primary["backend"],
            "total": primary["total"],
            "free": primary["free"],
            "unified": shared,
            "multi": len(devices) > 1,
            "devices": devices,
            "source": "llama.cpp --list-devices",
            "approximate": False,
            "notes": notes,
        }
    notes.append(why)

    info = system()
    alt = None
    if info["os"] == "Darwin" and info.get("apple_silicon"):
        alt = _apple_unified(ram()["ram_total"])
        if alt:
            alt["vendor"], alt["backend"], alt["unified"] = "apple", "Metal", True
            notes.append("unified memory: " + alt.pop("note", ""))
    if alt is None:
        nv = _nvidia()
        if nv:
            alt = dict(nv, vendor="nvidia", backend="CUDA", unified=False)
    if alt is None and info["os"] == "Darwin":
        alt = _apple_discrete()
        if alt:
            alt.update(vendor="unknown", backend="Metal", unified=False)
    if alt is None and info["os"] == "Linux":
        alt = _linux_drm()
        if alt:
            alt.update(vendor=_vendor_of(alt.get("name", "")), backend="DRM", unified=False)
    if alt is None and info["os"] == "Windows":
        alt = _windows_wmi()
        if alt:
            alt.update(vendor=_vendor_of(alt.get("name", "")), backend="WMI", unified=False)
    if alt is None:
        notes.append("no GPU memory could be read; models will be judged on RAM only")
        return {"name": "", "vendor": "none", "backend": "", "total": 0, "free": 0,
                "unified": False, "multi": False, "devices": [],
                "source": "none", "approximate": True, "notes": notes}

    # An APU found through sysfs/WMI is shared memory too, so the RAM figure must
    # not be added on top of it.
    alt.setdefault("unified", _integrated(alt.get("name", "")))
    alt.setdefault("multi", False)
    alt.setdefault("devices", [])
    alt.setdefault("approximate", not alt.get("total"))
    alt["source"] = alt.get("backend", "unknown") + " probe"
    alt["notes"] = notes
    return alt


# ------------------------------------------------------------------- the report


def specs(refresh=False):
    """Everything a fit decision may use, plus where each number came from."""
    sysinfo, memory, g = system(), ram(), gpu()
    return {
        "os": sysinfo["os"],
        "os_release": sysinfo["os_release"],
        "machine": sysinfo["machine"],
        "cpu": sysinfo["cpu"],
        "cpu_count": sysinfo["cpu_count"],
        "python": sysinfo["python"],
        "apple_silicon": bool(sysinfo.get("apple_silicon")),

        "ram_total_gb": round(memory["ram_total"] / GB, 2),
        "ram_free_gb": round(memory["ram_free"] / GB, 2),

        "vram_total_gb": round(g["total"] / GB, 2),
        "vram_free_gb": round(g["free"] / GB, 2),

        "gpu_name": g["name"],
        "gpu_vendor": g["vendor"],
        "gpu_backend": g["backend"],
        "gpu_unified": g["unified"],
        "gpu_multi": g["multi"],
        "gpu_source": g["source"],
        "gpu_approximate": g["approximate"],
        "gpu_devices": [
            {"label": d["label"], "name": d["name"], "vendor": d["vendor"],
             "total_gb": round(d["total"] / GB, 2), "free_gb": round(d["free"] / GB, 2)}
            for d in g["devices"]
        ],
        "notes": g["notes"],
    }


def fit(size_gb, spec=None):
    """How well a model of ``size_gb`` will run here.

    gpu     - the whole model and its cache fit in GPU memory (fast)
    split   - some layers on the GPU, the rest in RAM (usable, slower)
    cpu     - fits in system RAM only (slow)
    too_big - will not fit at all without more memory
    """
    spec = spec or specs()
    need = (size_gb or 0) * FILE_OVERHEAD + KV_OVERHEAD_GB

    # Compare against TOTAL, not free: choosing a model replaces whatever is
    # loaded, which gives that memory back.
    vram = max(spec.get("vram_total_gb") or 0, spec.get("vram_free_gb") or 0)
    ram = spec.get("ram_free_gb") or 0

    if spec.get("gpu_unified"):
        # Apple Silicon: GPU memory IS system RAM, so adding them would count the
        # same gigabytes twice and call an oversized model "gpu". One pool, one
        # budget: the GPU share first, then plain RAM on the CPU.
        if vram and need <= vram:
            return "gpu"
        if need <= (spec.get("ram_total_gb") or ram):
            return "cpu"
        return "too_big"

    if vram and need <= vram:
        return "gpu"
    # "split" needs a GPU to split onto. Testing vram + ram before ram made the
    # "cpu" branch unreachable, so a GPU-less machine was told its models would
    # run "partly on GPU" when there was no GPU at all.
    if vram and need <= vram + ram:
        return "split"
    if need <= ram:
        return "cpu"
    return "too_big"


FIT_LABEL = {
    "gpu": "Fast - fits entirely in your GPU memory",
    "split": "OK - runs partly on GPU, partly in RAM (slower)",
    "cpu": "Slow - RAM only, no GPU offload",
    "too_big": "Too big for your memory right now",
}


def fit_label(verdict, spec=None):
    """A label that names the actual hardware, so it is not a generic promise."""
    label = FIT_LABEL.get(verdict, verdict)
    spec = spec or specs()
    if verdict == "gpu" and spec.get("gpu_unified"):
        return "Fast - fits in unified memory (Metal)"
    if verdict == "gpu" and spec.get("gpu_name"):
        return "Fast - fits entirely in {}'s {} GB".format(
            spec["gpu_name"], spec["vram_total_gb"])
    return label


def summary(spec=None):
    """A few lines for a CLI/UI, in the order a person would ask."""
    spec = spec or specs()
    lines = [
        "system   {} ({}) · {} cores".format(
            spec.get("os", "?"), spec.get("machine", "?"), spec.get("cpu_count", 0)),
        "cpu      {}".format(spec.get("cpu") or "unknown"),
        "ram      {:.1f} GB free of {:.1f} GB".format(
            spec.get("ram_free_gb") or 0, spec.get("ram_total_gb") or 0),
    ]
    if spec.get("gpu_name"):
        kind = "unified" if spec.get("gpu_unified") else "dedicated"
        lines.append("gpu      {}  ({} · {} GB, {} GB free{} )".format(
            spec["gpu_name"], spec.get("gpu_backend") or "?",
            spec.get("vram_total_gb") or 0, spec.get("vram_free_gb") or 0,
            ", estimated" if spec.get("gpu_approximate") else ""))
        lines.append("         {} memory, read from {}".format(
            kind, spec.get("gpu_source") or "?"))
    else:
        lines.append("gpu      none detected - models run on the CPU")
    for note in spec.get("notes") or []:
        lines.append("note     {}".format(note))
    lines.append("verdict  a 7B at Q4_K_M (~4.6 GB) would be: {}".format(
        fit_label(fit(4.6, spec), spec)))
    return lines
