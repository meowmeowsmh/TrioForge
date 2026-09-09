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

import requests

from paths import root_path

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
    for d in ("/usr/local/bin", "/usr/bin", "/opt/llama.cpp", "/opt/llama.cpp/bin",
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


def _gpu_backend():
    """Detect the local GPU acceleration backend without heavy imports.

    Returns {"os", "arch", "backend", "label"}. backend is one of:
    metal (Apple Silicon MPS) / cuda (NVIDIA) / rocm (AMD) / vulkan / cpu.
    Cached (process-wide) so the Nvidia/AMD probe doesn't run on every request.
    """
    global _gpu_cache
    if _gpu_cache is not None:
        return _gpu_cache

    os_name = platform.system()
    arch = (platform.machine() or "").lower()
    # Apple Silicon → Metal (MPS). Intel Macs are CPU.
    if os_name == "Darwin":
        if arch in ("arm64", "aarch64"):
            _gpu_cache = {"os": "macOS", "arch": "apple-silicon", "backend": "metal",
                          "label": "Metal (Apple Silicon)"}
            return _gpu_cache
        _gpu_cache = {"os": "macOS", "arch": arch, "backend": "cpu",
                      "label": "Apple (CPU)"}
        return _gpu_cache
    # NVIDIA CUDA
    try:
        if shutil.which("nvidia-smi") and subprocess.run(
                ["nvidia-smi", "-L"], capture_output=True, timeout=5).returncode == 0:
            _gpu_cache = {"os": "Linux" if os_name == "Linux" else "Windows", "arch": arch,
                          "backend": "cuda", "label": "NVIDIA CUDA"}
            return _gpu_cache
    except Exception:
        pass
    # AMD ROCm
    try:
        if shutil.which("rocm-smi") and subprocess.run(
                ["rocm-smi"], capture_output=True, timeout=5).returncode == 0:
            _gpu_cache = {"os": "Linux", "arch": arch, "backend": "rocm", "label": "AMD ROCm"}
            return _gpu_cache
    except Exception:
        pass
    # Vulkan
    try:
        if shutil.which("vulkaninfo"):
            _gpu_cache = {"os": "Linux" if os_name == "Linux" else "Windows", "arch": arch,
                          "backend": "vulkan", "label": "Vulkan"}
            return _gpu_cache
    except Exception:
        pass
    _gpu_cache = {"os": os_name, "arch": arch, "backend": "cpu", "label": "CPU only"}
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
    items.append({
        "id": "comfyui",
        "name": "ComfyUI (image & video)",
        "status": "ok" if _comfyui_running() else "offline",
        "detail": "Running" if _comfyui_running() else "Optional — backend: {}".format(gpu["label"]),
        "url": "https://www.comfy.org/download",
        "required": False,
        "hint": _comfyui_install_hint(gpu),
    })

    # 5. Voice-to-voice (optional)
    cfg = _voice_config()
    has_cfg = bool(cfg.get("llama_server"))
    items.append({
        "id": "voice",
        "name": "Voice-to-voice agent",
        "status": "ok" if has_cfg else "missing",
        "detail": "Configured" if has_cfg else "Optional — needs speech-to-speech setup",
        "url": "https://huggingface.co/spaces/huggingface/speech-to-speech",
        "required": False,
        "hint": "Optional. Set up voiceguide_llama.cpp_guide/config.json + the speech-to-speech package.",
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
