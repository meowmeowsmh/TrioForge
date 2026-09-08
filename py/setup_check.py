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

import requests

from paths import root_path

OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188")
VOICE_CONFIG = root_path("voiceguide_llama.cpp_guide", "config.json")


def _voice_config():
    try:
        with open(VOICE_CONFIG, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _llama_server_candidates():
    """Return candidate llama-server executable paths, most likely first.

    Cross-platform: the configured path may be Windows-only (or a bare command
    name), so we also search PATH and common install locations on both Windows
    and Linux/macOS/Docker. Windows-only globs are harmless no-ops elsewhere.
    """
    cfg = _voice_config()
    cands = []
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
    for d in ("/usr/local/bin", "/usr/bin", "/opt/llama.cpp", "/opt/llama.cpp/build/bin"):
        cands.extend(glob.glob(os.path.join(d, "llama-server")))
        cands.extend(glob.glob(os.path.join(d, "llama-server.exe")))
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


def check_all():
    """Return the full setup status list."""
    items = []

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
    items.append({
        "id": "llamacpp",
        "name": "llama.cpp (llama-server)",
        "status": "ok" if found else "missing",
        "detail": found or "llama-server not found",
        "url": "https://github.com/ggml-org/llama.cpp/releases",
        "required": True,
        "hint": "Install llama.cpp — `brew install llama.cpp` (macOS), `apt install llama.cpp` (Linux), or `winget install ggml.llamacpp` (Windows). The app auto-starts it when you pick llama.cpp.",
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
        "detail": "Running" if _comfyui_running() else "Optional — for local image/video generation",
        "url": "https://www.comfy.org/download",
        "required": False,
        "hint": "Optional. Cloud image/video works via OpenRouter/Gemini without it.",
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
