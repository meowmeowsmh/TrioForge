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
    """Physical memory in bytes: total, used, available, and the raw free.

    ``available`` is the number that matters, and the one a task manager shows.
    Linux counts the page cache as *used*, so the raw ``free`` figure is nearly
    always tiny and looks alarming - "2.4 GB free of 14.7 GB" on a perfectly
    healthy idle desktop is normal, because ~8 GB of it is reclaimable cache.

    This function used to publish ``available`` under the name "free", so
    TrioForge printed "10.4 GB free" while `free -h` said 2.4 GiB free and the
    desktop's task manager showed a third number again. All three were correct
    and none of them agreed, which is exactly what made the RAM look wrong.
    """
    info = {"total": 0, "available": 0, "used": 0, "free": 0, "percent": 0.0}
    try:
        import psutil
        vm = psutil.virtual_memory()
        info = {"total": int(vm.total), "available": int(vm.available),
                "used": int(vm.used), "free": int(vm.free),
                "percent": round(float(vm.percent), 1)}
    except Exception:
        pass
    total, free = info["total"], info["available"]
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
    info["total"], info["available"] = total, free
    if not info["used"] and total:
        info["used"] = max(0, total - free)
    if not info["free"]:
        info["free"] = info["available"]
    if not info["percent"] and total:
        info["percent"] = round(info["used"] * 100.0 / total, 1)
    return info


def firmware_ram():
    """RAM the firmware says exists, or 0 when it cannot be read without root.

    Linux exposes the firmware's own memory map in /sys/firmware/memmap. Its
    "System RAM" total is always a little larger than MemTotal, because the
    kernel, the ACPI tables and - on an APU - the integrated GPU's frame buffer
    are carved out of it. That gap is the entire reason a laptop sold as "16 GB"
    reports 14.7 GB, and it is worth naming rather than leaving as a mystery:
    Windows Task Manager reports the *installed* size, so it reads higher, and
    neither number is wrong.
    """
    if not sys.platform.startswith("linux"):
        return 0
    total = 0
    try:
        for entry in glob.glob("/sys/firmware/memmap/*/"):
            try:
                with open(entry + "type", encoding="utf-8") as fh:
                    if fh.read().strip() != "System RAM":
                        continue
                with open(entry + "start", encoding="utf-8") as fh:
                    start = int(fh.read().strip(), 16)
                with open(entry + "end", encoding="utf-8") as fh:
                    end = int(fh.read().strip(), 16)
            except (OSError, ValueError):
                continue
            total += end - start + 1
    except Exception:
        return 0
    return total


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
    # Normalise "(TM)"/"(R)" and punctuation first: "AMD Radeon(TM) 610M" must
    # match the APU patterns. Before this, the "(TM)" broke the match, the iGPU
    # was treated as a dedicated card, and its 8 GB of *system* RAM was shown as
    # VRAM while the real RTX 5060 was ignored ("Radeon · 8 GB vram · 2 GPUs").
    low = re.sub(r"\([^)]*\)", "", (name or "").lower())
    low = re.sub(r"[^a-z0-9]+", " ", low)
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


def _nvidia_devices():
    """Every NVIDIA GPU NVML can see, or [] when the driver does not answer.

    NVML is the accurate source for the marketing name and live free VRAM. When
    the loaded kernel module and the userspace library do not match (a version
    skew after a driver update), nvmlInit raises DriverNotLoaded even though the
    card still works - so callers must not read [] as "no NVIDIA card".
    """
    try:
        import warnings
        warnings.filterwarnings("ignore", message=".*pynvml package is deprecated.*")
        import pynvml
        pynvml.nvmlInit()
        try:
            out = []
            for index in range(pynvml.nvmlDeviceGetCount()):
                try:
                    handle = pynvml.nvmlDeviceGetHandleByIndex(index)
                    mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
                    try:
                        name = pynvml.nvmlDeviceGetName(handle)
                        if isinstance(name, bytes):
                            name = name.decode("utf-8", "replace")
                    except Exception:
                        name = "NVIDIA GPU"
                    out.append({"name": name, "total": int(mem.total),
                                "free": int(mem.free)})
                except Exception:
                    continue
            return out
        finally:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass
    except Exception:
        return []


def _nvidia():
    """First NVIDIA GPU via NVML, or None - kept for the non-Linux fallbacks."""
    devices = _nvidia_devices()
    return devices[0] if devices else None


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


def _read(path):
    """Read a sysfs text file to a stripped string, '' on any failure."""
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except (OSError, ValueError):
        return ""


def _pci_bar_vram(device_dir):
    """NVIDIA VRAM: the largest 64-bit prefetchable BAR in ``resource``.

    The proprietary NVIDIA driver does not publish ``mem_info_vram_*`` (that node
    is amdgpu/i915), so the card's VRAM must come from its PCIe BAR - the same
    number ``lspci -v`` prints next to "Memory at ... (64-bit, prefetchable)".
    """
    best = 0
    try:
        with open(device_dir + "resource", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                try:
                    start, end, flags = int(parts[0], 16), int(parts[1], 16), int(parts[2], 16)
                except ValueError:
                    continue
                # IORESOURCE_MEM=0x200, PREFETCH=0x2000, MEM_64=0x100000.
                if (flags & 0x200) and (flags & 0x2000) and (flags & 0x100000):
                    best = max(best, end - start + 1)
    except OSError:
        return 0
    return best


def _nvidia_name_from_proc(slot):
    """The marketing name the NVIDIA driver itself reports, or ''.

    /proc/driver/nvidia/gpus/<pci-slot>/information carries "Model: NVIDIA ..."
    and stays readable even when nvidia-smi/NVML fail from a driver version skew.
    """
    try:
        path = "/proc/driver/nvidia/gpus/{}/information".format(slot)
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("Model:"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


# Only the bare cardN directory is a card; cardN-HDMI-A-1 & friends also carry a
# ``device`` symlink back to the same PCI device and must not be counted again.
_CARD_RE = re.compile(r"^card\d+$")


def _linux_drm_devices():
    """Every GPU the Linux DRM layer knows about, with a name and VRAM.

    One entry per /sys/class/drm/cardN. amdgpu/i915 publish VRAM via
    ``mem_info_vram_total``; NVIDIA's driver reports its marketing name under
    /proc/driver/nvidia and its VRAM as a PCI BAR, which this reads instead.
    Returns a list of {"label","name","vendor","total","free","backend",
    "integrated","approx"}; a card whose memory cannot be sized is skipped.
    """
    devices = []
    for device_dir in sorted(glob.glob("/sys/class/drm/card*/device/")):
        card = os.path.basename(os.path.dirname(device_dir.rstrip("/")))
        if not _CARD_RE.match(card):
            continue
        vendor_id = _read(device_dir + "vendor").lower()
        vendor = _PCI_VENDOR.get(vendor_id)
        if not vendor:
            continue                       # virtual/unknown card (e.g. virtio-gpu)
        slot = os.path.basename(os.path.realpath(device_dir.rstrip("/")))

        vram_total = _read(device_dir + "mem_info_vram_total")
        vram_used = _read(device_dir + "mem_info_vram_used")
        gtt_total = _read(device_dir + "mem_info_gtt_total")
        total = int(vram_total) if vram_total.isdigit() else 0
        used = int(vram_used) if vram_used.isdigit() else 0

        if total <= 0 and vendor == "nvidia":
            total = _pci_bar_vram(device_dir)
            used = 0                       # free is unknown without NVML
        if total <= 0:
            continue

        # Name: the driver's own answer first, then the generic sysfs label.
        name = _nvidia_name_from_proc(slot) if vendor == "nvidia" else ""
        if not name:
            name = _read(device_dir + "product_name")
        if not name and vendor == "amd":
            # An AMD APU's iGPU is "Radeon Graphics" in the CPU brand string and
            # shares system RAM, so its GTT (system-RAM backing) dwarfs the small
            # visible-VRAM window.
            name = "AMD Radeon Graphics" if (gtt_total.isdigit()
                                             and int(gtt_total) > total) else ""
        if not name:
            name = _read(device_dir + "label")
        if not name:
            name = {"nvidia": "NVIDIA GPU", "amd": "AMD GPU",
                    "intel": "Intel GPU"}.get(vendor, "GPU")

        # Integrated = the "VRAM" is really a slice of system RAM. The name says
        # "Radeon Graphics"/"Raphael" for AMD APUs, and GTT (system RAM backing)
        # far larger than the VRAM window is the sysfs-level confirmation.
        integrated = _integrated(name)
        if vendor == "amd" and gtt_total.isdigit() and int(gtt_total) > total:
            integrated = True

        devices.append({
            "label": "drm-{}".format(slot),
            "name": name,
            "vendor": vendor,
            "total": total,
            "free": max(0, total - used),
            "backend": "DRM",
            "integrated": integrated,
            "approx": used == 0,
        })
    return devices


def _linux_gpus(llama_devices):
    """Complete Linux GPU inventory: DRM sysfs, overlaid with llama.cpp + NVML.

    ``llama_devices`` may be [] or may list only what the current build's backend
    can reach. A hybrid laptop whose NVIDIA driver is version-skewed is invisible
    to Vulkan, so llama.cpp reports only the AMD iGPU and the discrete card would
    silently vanish ("1 GPU" where there are 2). The DRM sysfs list is the
    physical inventory, so it is the base; llama.cpp's and NVML's answers override
    name/free/backend where they can actually see a card.
    """
    base = _linux_drm_devices()
    if not base:
        return list(llama_devices)         # container/WSL: no DRM nodes to read

    def _overlay(target, src):
        if src.get("name"):
            target["name"] = src["name"]
        if src.get("total"):
            target["total"] = src["total"]
        if src.get("free") is not None:
            target["free"] = src["free"]
        target["backend"] = src.get("backend", target["backend"])
        if src.get("label"):
            target["label"] = src["label"]
        target["approx"] = False

    # NVML first: the accurate NVIDIA name and live free figure, when the driver
    # answers. Match to NVIDIA cards by order.
    nv_iter = iter(_nvidia_devices())
    for d in base:
        if d["vendor"] == "nvidia":
            nv = next(nv_iter, None)
            if nv:
                _overlay(d, dict(nv, backend="CUDA"))

    # Then llama.cpp's view. Match by vendor and name overlap, falling back to the
    # first unmatched same-vendor card; a loader device with no sysfs twin is kept
    # so nothing the loader CAN use is dropped.
    unmatched = []
    for ld in llama_devices:
        target = None
        for sd in base:
            if sd["vendor"] != ld["vendor"] or sd.get("_matched"):
                continue
            if ld["name"].lower() in sd["name"].lower() \
                    or sd["name"].lower() in ld["name"].lower():
                target = sd
                break
        if target is None:
            for sd in base:
                if sd["vendor"] == ld["vendor"] and not sd.get("_matched"):
                    target = sd
                    break
        if target is not None:
            target["_matched"] = True
            _overlay(target, ld)
        else:
            ld["integrated"] = _integrated(ld["name"])
            unmatched.append(ld)

    merged = base + unmatched
    for d in merged:
        d.pop("_matched", None)
    return merged


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


# PCI vendor ids, as the kernel reports them in /sys/class/drm/*/device/vendor.
_PCI_VENDOR = {"0x10de": "nvidia", "0x1002": "amd", "0x1022": "amd", "0x8086": "intel"}


def vendor():
    """Which GPU vendor is present - WITHOUT asking llama.cpp.

    Deliberately separate from gpu(): picking the llama.cpp *build* to download
    must not recurse into the device enumeration, which is itself done by running
    llama.cpp. This only reads cheap, vendor-specific facts.
    """
    info = system()
    if info["os"] == "Darwin":
        return "apple" if info.get("apple_silicon") else "unknown"
    if info["os"] == "Linux":
        found = set()
        for path in glob.glob("/sys/class/drm/card*/device/vendor"):
            try:
                with open(path, encoding="utf-8") as fh:
                    found.add(fh.read().strip().lower())
            except OSError:
                continue
        for pci, name in _PCI_VENDOR.items():
            if pci in found:
                return name
        # No DRM info (containers, odd kernels): fall back to the tools.
        if _which("nvidia-smi"):
            return "nvidia"
        return "unknown"
    if info["os"] == "Windows":
        wmi = _windows_wmi()
        if wmi:
            return _vendor_of(wmi.get("name", ""))
        if _which("nvidia-smi"):
            return "nvidia"
    return "unknown"


def _which(name):
    """shutil.which that never raises and never leaves the process's PATH."""
    from shutil import which
    try:
        return which(name)
    except Exception:
        return None


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


_live_cache = {"at": 0.0, "value": None}
_LIVE_TTL = 1.0


def _nvidia_smi_live():
    """Live NVIDIA memory + utilisation parsed from ``nvidia-smi`` (fallback).

    Used when NVML is unavailable (old pynvml against a new driver, or a sandbox
    without /dev/nvidiactl). ``nvidia-smi --query-gpu`` is the one NVIDIA tool
    already proven to answer, so it is the reliable second source.
    """
    out = _run(["nvidia-smi",
                "--query-gpu=memory.used,memory.total,utilization.gpu",
                "--format=csv,noheader,nounits"], timeout=5).strip()
    if not out:
        return None
    line = out.splitlines()[0]
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 3 or not all(p.isdigit() for p in parts[:3]):
        return None
    used, total, util = int(parts[0]) * _MIB, int(parts[1]) * _MIB, int(parts[2])
    if total <= 0:
        return None
    return {"used": used, "total": total, "free": max(0, total - used), "util": util}


def gpu_live():
    """Live NVIDIA memory + utilisation for the sidebar.

    ``gpu()`` spawns ``llama-server --list-devices``, so it is capped at a 5 s
    TTL. This reads NVML in-process (fast) and falls back to ``nvidia-smi``, so
    the sidebar can ask on every 2 s redraw and the figure actually moves while
    the card works. Returns {"used","total","free","util"} (bytes, percent 0-100)
    or None when no source answers - callers then fall back to ``gpu()``.
    """
    now = time.time()
    if _live_cache["value"] is not None and now - _live_cache["at"] < _LIVE_TTL:
        return _live_cache["value"]
    value = None
    try:
        import warnings
        warnings.filterwarnings("ignore", message=".*pynvml package is deprecated.*")
        import pynvml
        pynvml.nvmlInit()
        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            value = {"used": int(mem.used), "total": int(mem.total),
                     "free": int(mem.free), "util": int(util.gpu)}
        finally:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass
    except Exception:
        value = None
    if value is None:
        value = _nvidia_smi_live()
    _live_cache["at"] = now
    _live_cache["value"] = value
    return value


def _summarise_devices(devices, notes, source):
    """Turn a device list into the ``gpu()`` result shape.

    Shared by the llama.cpp path and the Linux sysfs path. It picks ONE primary
    device to judge fit on - never the sum. On a hybrid laptop the iGPU's "VRAM"
    is system RAM, so adding it to the dGPU's invents memory that does not exist
    (measured here: AMD 610M 7.8 GB + RTX 5060 8.0 GB was reported as 15.8 GB,
    which would have called a 12 GB model "gpu"). Prefer a dedicated card, then
    the largest one.
    """
    for d in devices:
        d.setdefault("integrated", False)
    dedicated = [d for d in devices if not d["integrated"]]
    primary = max(dedicated or devices, key=lambda d: d["total"])
    shared = primary["integrated"] or primary.get("vendor") == "apple"
    # Say which one is actually used, and why the others are not, so the choice
    # can be shown rather than merely made. Two different questions, and
    # conflating them is what made "1 of 2" useless: can llama.cpp offload to
    # this device at all (yes - it was listed), and does doing so add memory (no,
    # for an APU, because its "VRAM" is the system RAM already counted above).
    for d in devices:
        d["primary"] = d is primary
        d["usable"] = True
        d["adds_memory"] = not d["integrated"]
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
        "backend": primary.get("backend", ""),
        "total": primary["total"],
        "free": primary["free"],
        "unified": shared,
        "multi": len(devices) > 1,
        "devices": devices,
        "source": source,
        "approximate": any(d.get("approx") for d in devices),
        "notes": notes,
    }


def _probe_gpu():
    notes = []
    devices, why = _llamacpp_devices()

    info = system()

    # Linux: enumerate every GPU from sysfs (NVIDIA included) and overlay the
    # loader's and NVML's answers. This is where a hybrid NVIDIA + AMD-APU laptop
    # is seen as "2 GPUs": when the NVIDIA driver is version-skewed, llama.cpp's
    # Vulkan sees only the AMD iGPU (or nothing) and the discrete card would
    # otherwise be reported as absent.
    if info["os"] == "Linux":
        linux = _linux_gpus(devices)
        if linux:
            for d in linux:
                d.setdefault("integrated", _integrated(d["name"]))
            return _summarise_devices(linux, notes, "Linux DRM sysfs")

    if devices:
        for d in devices:
            d["integrated"] = _integrated(d["name"])
        return _summarise_devices(devices, notes, "llama.cpp --list-devices")
    notes.append(why)

    alt = None
    if info["os"] == "Darwin" and info.get("apple_silicon"):
        alt = _apple_unified(ram()["total"])
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
    sysinfo, memory, g = system(), ram(), gpu(refresh)
    fw_ram = firmware_ram()
    notes = list(g["notes"])
    # Only worth saying when the shortfall is big enough to notice (an APU
    # reservation, not rounding).
    if fw_ram and fw_ram > memory["total"] * 1.02:
        notes.append(
            "{:.1f} GB of RAM is visible to the firmware and {:.1f} GB is usable; "
            "the difference is reserved for the integrated GPU (frame buffer), ACPI "
            "and firmware. Windows Task Manager shows the installed size, so it "
            "reads higher - neither number is wrong.".format(
                fw_ram / GB, memory["total"] / GB))
    return {
        "os": sysinfo["os"],
        "os_release": sysinfo["os_release"],
        "machine": sysinfo["machine"],
        "cpu": sysinfo["cpu"],
        "cpu_count": sysinfo["cpu_count"],
        "python": sysinfo["python"],
        "apple_silicon": bool(sysinfo.get("apple_silicon")),

        "ram_total_gb": round(memory["total"] / GB, 2),
        # "free" is the raw figure Linux reports and is nearly always tiny; it is
        # NOT the headroom. ram_available_gb is what a task manager calls
        # available and what fit() and the UIs must use.
        "ram_free_gb": round(memory["free"] / GB, 2),
        "ram_available_gb": round(memory["available"] / GB, 2),
        "ram_used_gb": round(memory["used"] / GB, 2),
        "ram_percent": memory["percent"],
        "ram_firmware_gb": round(fw_ram / GB, 2) if fw_ram else 0.0,

        "vram_total_gb": round(g["total"] / GB, 2),
        "vram_free_gb": round(g["free"] / GB, 2),

        "gpu_name": g["name"],
        "gpu_vendor": g["vendor"],
        "gpu_backend": g["backend"],
        "gpu_unified": g["unified"],
        "gpu_multi": g["multi"],
        "gpu_source": g["source"],
        "gpu_approximate": g["approximate"],
        "gpu_primary": g["name"],
        # How many cards could actually be pooled: an integrated one adds no
        # memory, so counting it would promise RAM that does not exist.
        "gpu_usable_count": sum(1 for d in g["devices"] if d.get("adds_memory")),
        "gpu_devices": [
            {"label": d["label"], "name": d["name"], "vendor": d["vendor"],
             "total_gb": round(d["total"] / GB, 2), "free_gb": round(d["free"] / GB, 2),
             "in_use": bool(d.get("primary")), "integrated": bool(d.get("integrated")),
             "usable": bool(d.get("usable")), "adds_memory": bool(d.get("adds_memory"))}
            for d in g["devices"]
        ],
        "notes": notes,
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
    # Headroom, not the raw "free": the page cache is reclaimable and free()'s
    # free column ignores that, which would refuse models that fit comfortably.
    ram = spec.get("ram_available_gb") or spec.get("ram_free_gb") or 0

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
            spec.get("ram_available_gb") or 0, spec.get("ram_total_gb") or 0),
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
