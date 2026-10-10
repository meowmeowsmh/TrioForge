"""
First-run setup checker for TrioForge.

Detects which local services / files are present or missing so the UI can show a
friendly "what to install" panel with download links, instead of the user hitting
a cryptic "connection refused" when they pick a provider that isn't set up yet.

Each entry returns:
    id, name, status ("ok" | "missing" | "offline"), detail, url (download/docs)

The check is intentionally lightweight and non-blocking (short timeouts, no heavy
imports) so it can run on every page load.
"""

import glob
import json
import os
import shutil
import subprocess
import platform

# PEP 810: lazy on 3.15+, an ignored module name on 3.12-3.14 (see app.py).
__lazy_modules__ = ["requests"]
import requests

from paths import root_path


def _no_window():
    """CREATE_NO_WINDOW for console programs (see video_to_text._hidden_flags).

    The app runs under pythonw, so a console program started without this makes
    Windows allocate a NEW console window for it - the "cmd keeps popping up" bug.
    """
    import subprocess as _sp
    import os as _os
    if _os.name != "nt":
        return 0
    return getattr(_sp, "CREATE_NO_WINDOW", 0)



OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188")
VOICE_CONFIG = root_path("voiceguide_llama.cpp_guide", "config.json")

_gpu_cache = None


def _voice_config():
    try:
        with open(VOICE_CONFIG, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _remote_reachable(host, port, timeout=2):
    """True if a TCP connection to host:port succeeds (Docker → host llama.cpp)."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    except Exception:
        return False
    finally:
        s.close()


def _llama_server_candidates():
    """Return candidate llama-server executable paths, most likely first.

    Cross-platform: the configured path may be Windows-only (or a bare command
    name), so we also search PATH and common install locations on both Windows
    and Linux/macOS/Docker. Windows-only globs are harmless no-ops elsewhere.
    """
    cfg = _voice_config()
    cands = []
    env_exe = os.environ.get("LLAMA_SERVER", "").strip()
    if env_exe:
        cands.append(env_exe)
    exe = cfg.get("llama_server", "")
    if exe:
        cands.append(exe)
    # Common install locations on Windows.
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        cands.extend(glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages", "*", "llama-server.exe")))
    prog = os.environ.get("ProgramFiles", "")
    if prog:
        cands.extend(glob.glob(os.path.join(prog, "*", "llama-server.exe")))
    # Common install locations on Linux / macOS / Docker.
    home = os.path.expanduser("~")
    for d in ("/usr/local/bin", "/usr/bin",
              # macOS Apple Silicon: Homebrew prefix (NOT /usr/local)
              "/opt/homebrew/bin", "/opt/homebrew/opt/llama.cpp/bin",
              "/opt/llama.cpp", "/opt/llama.cpp/bin",
              "/opt/llama.cpp/build/bin", "/usr/local/lib/llama.cpp/bin",
              os.path.join(home, ".local", "bin"),
              os.path.join(home, "llama.cpp"), os.path.join(home, "llama.cpp", "bin"),
              os.path.join(home, "llama.cpp", "build", "bin"),
              os.path.join(home, "llama-bin"), os.path.join(home, "llama-bin", "bin")):
        cands.extend(glob.glob(os.path.join(d, "llama-server")))
        cands.extend(glob.glob(os.path.join(d, "llama-server.exe")))
    # Release-tarball layouts like ~/llama-b4790-bin-ubuntu-x64/bin/llama-server.
    cands.extend(glob.glob(os.path.join(home, "llama-b*-bin-*", "bin", "llama-server")))
    cands.extend(glob.glob(os.path.join(home, "llama-b*-bin-*", "bin", "llama-server.exe")))
    # Where the in-app auto-installer extracts builds.
    cands.extend(glob.glob(os.path.join(root_path("tools", "llama.cpp"), "**", "llama-server"), recursive=True))
    cands.extend(glob.glob(os.path.join(root_path("tools", "llama.cpp"), "**", "llama-server.exe"), recursive=True))
    # On PATH (both "llama-server" and "llama-server.exe").
    for name in ("llama-server", "llama-server.exe"):
        which = shutil.which(name)
        if which:
            cands.append(which)
    return cands


def _gguf_count():
    return len(glob.glob(root_path("models", "**", "*.gguf"), recursive=True))


def _ollama_status():
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def _comfyui_running():
    try:
        r = requests.get(f"{COMFYUI_URL}/system_stats", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def _comfyui_installed():
    """Path of the ComfyUI install, if it is present even when it is not running.

    Uses the same discovery the image/video panels use, so the Setup panel and the
    generators can never disagree about whether ComfyUI exists.
    """
    try:
        import comfyui_service
        found = comfyui_service.find_comfyui_install()
        return found or None
    except Exception:
        return None


def _vulkan_runtime():
    """True when the Vulkan loader is actually installed.

    The Vulkan build of llama.cpp needs the loader at runtime, so this is the
    thing worth testing. Requiring `vulkaninfo` instead was wrong: that ships in
    vulkan-tools, which most desktops do NOT install - so an AMD or Intel machine
    with a perfectly good Vulkan driver was detected as "no GPU" and silently got
    the CPU build.
    """
    if shutil.which("vulkaninfo"):
        return True
    try:
        import ctypes.util
        for name in ("vulkan", "vulkan-1"):
            if ctypes.util.find_library(name):
                return True
    except Exception:
        pass
    if platform.system() == "Darwin":
        return True          # MoltenVK ships inside the macOS build
    return False


def _probe_ok(cmd, timeout=5):
    """Run a vendor tool; True only when it exists and exits 0."""
    try:
        if not shutil.which(cmd[0]):
            return False
        return subprocess.run(cmd, capture_output=True, timeout=timeout,
                              creationflags=_no_window()).returncode == 0
    except Exception:
        return False


def _gpu_backend():
    """Detect the local GPU acceleration backend without heavy imports.

    Returns {"os", "arch", "backend", "label"}. backend is one of:
    metal (Apple Silicon MPS) / cuda (NVIDIA) / rocm (AMD) / vulkan / cpu.

    The vendor comes from hardware.vendor(), which reads the PCI id on Linux and
    WMI on Windows rather than needing a vendor tool on PATH. Cached
    (process-wide) so the probes do not run on every request.
    """
    global _gpu_cache
    if _gpu_cache is not None:
        return _gpu_cache

    os_name = platform.system()
    arch = (platform.machine() or "").lower()
    where = "Linux" if os_name == "Linux" else "Windows"

    # Apple Silicon -> Metal (MPS). Intel Macs run the macos-x64 build, which is
    # also Metal-capable, so they are not "CPU only" in the same sense.
    if os_name == "Darwin":
        if arch in ("arm64", "aarch64"):
            _gpu_cache = {"os": "macOS", "arch": "apple-silicon", "backend": "metal",
                          "label": "Metal (Apple Silicon)"}
        else:
            _gpu_cache = {"os": "macOS", "arch": arch, "backend": "metal",
                          "label": "Metal (Intel Mac)"}
        return _gpu_cache

    vendor = "unknown"
    try:
        import hardware                      # cheap probe, never touches llama.cpp
        vendor = hardware.vendor()
    except Exception:
        vendor = "unknown"

    # NVIDIA: CUDA when the driver answers, otherwise Vulkan.
    if vendor == "nvidia" or shutil.which("nvidia-smi"):
        if _probe_ok(["nvidia-smi", "-L"]):
            _gpu_cache = {"os": where, "arch": arch, "backend": "cuda",
                          "label": "NVIDIA CUDA"}
            return _gpu_cache

    # AMD: ROCm only when it is really installed; Vulkan otherwise.
    if vendor == "amd" or shutil.which("rocm-smi"):
        if _probe_ok(["rocm-smi"]):
            _gpu_cache = {"os": where, "arch": arch, "backend": "rocm",
                          "label": "AMD ROCm"}
            return _gpu_cache

    # Any real GPU plus a Vulkan loader -> the Vulkan build (AMD, Intel, and
    # NVIDIA cards whose driver has no working nvidia-smi).
    if vendor in ("nvidia", "amd", "intel") and _vulkan_runtime():
        _gpu_cache = {"os": where, "arch": arch, "backend": "vulkan",
                      "label": "Vulkan ({})".format(vendor)}
        return _gpu_cache

    # Last resort: a Vulkan loader with no vendor we recognised. Better the
    # accelerated build than the CPU one when the loader is demonstrably there.
    if _vulkan_runtime():
        _gpu_cache = {"os": where, "arch": arch, "backend": "vulkan", "label": "Vulkan"}
        return _gpu_cache

    _gpu_cache = {"os": os_name, "arch": arch, "backend": "cpu",
                  "label": "CPU only" + ("" if vendor == "unknown" else " ({})".format(vendor))}
    return _gpu_cache


def _llamacpp_install_hint(gpu):
    """Per-OS / per-hardware llama.cpp install steps."""
    b, os_ = gpu["backend"], gpu["os"]
    if os_ == "macOS":
        if b == "metal":
            return ("Apple Silicon uses Metal (no extra driver needed): `brew install llama.cpp`, "
                    "or extract the `llama-bXXXX-bin-macos-arm64.zip` release. Metal acceleration "
                    "is used automatically by llama-server.")
        return ("Intel Mac: use the `llama-bXXXX-bin-macos-x64.zip` release (CPU). "
                "`brew install llama.cpp` also works for the CPU build.")
    if os_ == "Linux":
        if b == "cuda":
            return ("NVIDIA CUDA detected: use the `llama-bXXXX-bin-ubuntu-x64.zip` CUDA build, "
                    "`apt install llama.cpp`, or build with `-DGGML_CUDA=ON`. CUDA is used automatically.")
        if b == "rocm":
            return ("AMD ROCm detected: build llama.cpp with `-DGGML_HIP=ON` (ROCm), or use the CPU "
                    "build if you prefer simpler setup.")
        if b == "vulkan":
            return ("Vulkan detected: build llama.cpp with `-DGGML_VULKAN=ON`, or use the CPU build.")
        return ("CPU-only Linux: `apt install llama.cpp` or download the CPU release. "
                "No GPU driver needed — slower but zero extra setup.")
    if os_ == "Windows":
        if b == "cuda":
            return ("Windows + NVIDIA: `winget install ggml.llamacpp` (CUDA build) or the "
                    "`llama-bXXXX-bin-win-cuda-x64.zip` release. The app auto-starts it when you pick llama.cpp.")
        return ("Windows: `winget install ggml.llamacpp` or download the llama.cpp release. "
                "The app auto-starts it when you pick llama.cpp.")
    return ("Get `llama-server` on PATH from the llama.cpp release for your OS, or set "
            "`LLAMA_SERVER=/full/path/to/llama-server`.")


def _comfyui_install_hint(gpu):
    """Per-OS / per-hardware ComfyUI install steps (Desktop = one-click)."""
    b, os_ = gpu["backend"], gpu["os"]
    if os_ == "macOS":
        return ("macOS: install ComfyUI Desktop (one-click, auto-configured for Apple Silicon/Metal) "
                "from comfy.org/download, or clone https://github.com/comfyanonymous/ComfyUI and "
                "`pip install torch` (MPS backend).")
    if os_ == "Linux":
        if b == "cuda":
            return ("Linux + NVIDIA: ComfyUI Desktop (one-click) or clone ComfyUI + "
                    "`pip install torch --index-url https://download.pytorch.org/whl/cu124` (CUDA).")
        if b == "rocm":
            return ("Linux + AMD: ComfyUI Desktop (one-click) or clone ComfyUI + the PyTorch ROCm build. "
                    "A CPU build always works too.")
        return ("Linux: ComfyUI Desktop (one-click) or clone ComfyUI + CPU PyTorch. "
                "You can also just use the built-in cloud image/video models.")
    if os_ == "Windows":
        return ("Windows: install ComfyUI Desktop (one-click) or use the built-in cloud image/video "
                "models. ComfyUI Desktop auto-configures your GPU backend.")
    return ("Install ComfyUI Desktop (https://www.comfy.org/download) — it auto-configures your GPU "
            "backend. Or use the built-in cloud image/video models instead.")


def _voice_agent_ready():
    """True if the speech-to-speech package is installed (voice-to-voice works)."""
    try:
        return bool(shutil.which("speech-to-speech"))
    except Exception:
        return False


def _deepseek_harness():
    """(path, version) for DeepSeek Harness, or (None, "") when it is not installed.

    Reuses the plugin's own detector so the 🔌 panel and the 🚀 panel can never
    disagree about what is installed (it already knows that Debian/Ubuntu ship a
    different program called `dsh`). A broken plugin just reads as "not found".
    """
    import importlib.util
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "plugins")
    # The plugin ships DISABLED as _deepseek_harness.py and becomes
    # deepseek_harness.py once the user enables it; detect either way.
    for name in ("deepseek_harness.py", "_deepseek_harness.py"):
        path = os.path.join(base, name)
        if not os.path.isfile(path):
            continue
        try:
            spec = importlib.util.spec_from_file_location("trioforge_plugin_deepseek_harness", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            exe = mod._find_dsh()
            if not exe:
                return None, ""
            ver = mod._dsh_version(exe)
            return exe, (".".join(str(x) for x in ver) if ver else "installed")
        except Exception:
            continue
    return None, ""


def check_all():
    """Return the full setup status list."""
    items = []
    gpu = _gpu_backend()

    # 1. Ollama
    ollama_ok = _ollama_status()
    items.append({
        "id": "ollama",
        "name": "Ollama (local chat)",
        "status": "ok" if ollama_ok else "offline",
        "detail": "Running" if ollama_ok else "Not running — install then start it",
        "url": "https://ollama.com/download",
        "required": True,
        "hint": "Install Ollama, then run `ollama pull <model>` for your first model.",
    })

    # 2. llama.cpp (llama-server)
    cands = _llama_server_candidates()
    found = next((c for c in cands if os.path.isfile(c)), None)
    # Remote mode (Docker → host llama-server): no local exe required — report
    # reachable/not-reachable instead so the Docker setup panel is accurate.
    remote_host = os.environ.get("LLAMA_HOST", "").strip()
    if not found and remote_host:
        remote_port = os.environ.get("LLAMA_PORT", "8080")
        remote_ok = _remote_reachable(remote_host, int(remote_port) if str(remote_port).isdigit() else 8080)
        found = ("{}:{}".format(remote_host, remote_port)) if remote_ok else None
    items.append({
        "id": "llamacpp",
        "name": "llama.cpp (llama-server)",
        "status": "ok" if found else "missing",
        "detail": found or "llama-server not found — backend: {}".format(gpu["label"]),
        "url": "https://github.com/ggml-org/llama.cpp/releases",
        "required": True,
        "hint": _llamacpp_install_hint(gpu),
    })

    # 3. GGUF model files
    n = _gguf_count()
    items.append({
        "id": "models",
        "name": "GGUF models",
        "status": "ok" if n > 0 else "missing",
        "detail": f"{n} model file(s) in models/",
        "url": "https://huggingface.co/models?library=gguf",
        "required": True,
        "hint": "Download GGUF models into models/ (or use the ⬇ button in the app).",
    })

    # 4. ComfyUI (optional — image/video)
    # "Not running" is NOT "not installed": the panel used to tell anyone who had
    # ComfyUI Desktop installed but closed to go and download it. Three states now -
    # running, installed-but-closed, and genuinely absent.
    comfy_running = _comfyui_running()
    comfy_dir = None if comfy_running else _comfyui_installed()
    if comfy_running:
        comfy_status, comfy_detail, comfy_hint = "ok", "Running", ""
    elif comfy_dir:
        comfy_status = "offline"
        comfy_detail = "Installed, not running — found at {}".format(comfy_dir)
        comfy_hint = ("ComfyUI is already installed. Open ComfyUI Desktop (or start it) and "
                      "press 🔄 Re-check — nothing to download.")
    else:
        comfy_status = "offline"
        comfy_detail = "Optional — backend: {}".format(gpu["label"])
        comfy_hint = _comfyui_install_hint(gpu)
    items.append({
        "id": "comfyui",
        "name": "ComfyUI (image & video)",
        "status": comfy_status,
        "detail": comfy_detail,
        "url": "" if comfy_dir else "https://www.comfy.org/download",
        "required": False,
        "hint": comfy_hint,
    })

    # 5. Voice-to-voice (REQUIRED — always installed alongside llama.cpp)
    voice_ok = _voice_agent_ready()
    items.append({
        "id": "voice",
        "name": "Voice-to-voice agent",
        "status": "ok" if voice_ok else "missing",
        "detail": "Ready" if voice_ok else "Needs the speech-to-speech package",
        "url": "https://huggingface.co/spaces/huggingface/speech-to-speech",
        "required": True,
        "hint": ("Voice-to-voice ships with llama.cpp. Install the speech-to-speech package "
                 "(⚡ Install) to talk to the app by voice. It runs on its own port 8082."),
    })

    # 6. DeepSeek Harness (optional — hand a whole task to the dsh agent)
    dsh_path, dsh_version = _deepseek_harness()
    items.append({
        "id": "deepseek-harness",
        "name": "DeepSeek Harness (dsh)",
        "status": "ok" if dsh_path else "missing",
        "detail": ("dsh {} — {}".format(dsh_version, dsh_path)) if dsh_path
                  else "Not found on PATH or in the npx cache",
        "url": "https://www.npmjs.com/package/@deepseek-ai/dsh",
        "required": False,
        "hint": ("Optional. Install DeepSeek Harness (npm/npx) to let the agent hand a "
                 "whole task to dsh with the dsh_run tool."),
    })

    return items


def summary():
    """Return {all_required_ok, items} so the UI can decide whether to nag."""
    items = check_all()
    missing_required = [i for i in items if i["required"] and i["status"] != "ok"]
    return {
        "all_required_ok": not missing_required,
        "missing_required": [i["id"] for i in missing_required],
        "items": items,
    }
