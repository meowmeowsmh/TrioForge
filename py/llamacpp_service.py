# llamacpp_service.py – manage the llama.cpp server (llama-server) lifecycle.
#
# When the user selects "llama.cpp" in the provider dropdown, the app auto-starts
# the local llama-server with the model chosen in the UI (not a fixed config path);
# when they switch to another provider, it is stopped.
#
# It reuses voiceguide_llama.cpp_guide/config.json for the executable + host/port,
# but the *model* is taken from the UI selection so the server loads exactly what
# the user picked from the models/ folder.

import json
import os
import socket
import subprocess
import threading
import time

from paths import root_path

CONFIG_PATH = root_path("voiceguide_llama.cpp_guide", "config.json")

_process = None
_running_model = None
_lock = threading.Lock()

# Common GGUF quantization suffixes, used to split a model name into its base name
# so the mmproj projector can be paired with the right text model.
_QUANT_TOKENS = {
    "q4_k_m", "q5_k_m", "q6_k", "q8_0", "q4_0", "q5_0", "bf16", "f16", "f32",
    "q2_k", "q3_k", "q8_k", "q4_k_s", "q5_k_s", "q3_k_s", "q2_k_s", "q4_k", "q5_k",
    # XL / L variants (e.g. gemma-4 Q4_K_XL, Q4_K_L) — without these the quant is
    # not stripped, so a gemma-4 model's base name stays full and its mmproj pair
    # ("mmproj-BF16.gguf") fails to match.
    "q4_k_xl", "q5_k_xl", "q6_k_xl", "q4_k_l", "q5_k_l", "q4_k_m_xl",
    "q8_0_xl", "f16_xl",
}
_QUANT_SET = {q.replace("_", "") for q in _QUANT_TOKENS}


def _base_gguf_name(filename: str):
    """Return a model's base name: no .gguf, no mmproj- prefix, no trailing quant."""
    name = os.path.splitext(filename)[0]
    if name.lower().startswith("mmproj-"):
        name = name[len("mmproj-"):]
    parts = name.split("-")
    while parts and parts[-1].lower().replace("_", "") in _QUANT_SET:
        parts.pop()
    return "-".join(parts)


def _list_gguf_files(subdir="models"):
    """All .gguf files under <project>/<subdir>, recursively, as absolute paths."""
    import glob
    pattern = os.path.abspath(root_path(subdir, "**", "*.gguf"))
    return glob.glob(pattern, recursive=True)


def _folder_mmproj(model_path: str):
    """Find an mmproj-*.gguf in the SAME folder as the model.

    This is the recommended, conflict-free layout: each model lives in its own
    subfolder under models/ with its projector beside it, e.g.
        models/gemma-4/gemma-4-...-Q4_K_XL.gguf
        models/gemma-4/mmproj-BF16.gguf
    Pairing by folder is automatic and unambiguous — no name heuristics, no
    hard-coding. Returns the projector path or None.
    """
    model_dir = os.path.dirname(os.path.abspath(model_path))
    # Only trust folder pairing when the model is inside a subfolder of models/,
    # not in the root (where multiple models would otherwise collide).
    models_root = os.path.abspath(root_path("models"))
    if model_dir == models_root or not model_dir.startswith(models_root + os.sep):
        return None
    for f in _list_gguf_files():
        if os.path.dirname(os.path.abspath(f)) != model_dir:
            continue
        bn = os.path.basename(f)
        if bn.lower().startswith("mmproj-"):
            return f
    return None


def find_mmproj(model_path: str):
    """Find the vision-projector (mmproj-*.gguf) that pairs with a text model.

    Priority (all automatic, none hard-coded):
      1) Same folder as the model (recommended organized layout).
      2) Shared base name across models/ (mmproj-BASE-<quant> pairs with BASE-<quant>.gguf).
      3) Explicit `mmproj_pairs` map in voiceguide_llama.cpp_guide/config.json (optional override).
      4) A generic quant-only projector (mmproj-BF16.gguf) for a known vision model.
    """
    # 1) Folder pairing — matches the "one subfolder per model" layout.
    folder_mp = _folder_mmproj(model_path)
    if folder_mp:
        return folder_mp

    model_base = _base_gguf_name(os.path.basename(model_path))
    if not model_base:
        return None
    model_base_l = model_base.lower()

    # 2) Name-based match across all gguf files.
    for f in _list_gguf_files():
        bn = os.path.basename(f)
        if bn.lower().startswith("mmproj-"):
            mbase = _base_gguf_name(bn)
            mbase_l = mbase.lower() if mbase else ""
            if mbase_l and (mbase_l in model_base_l or model_base_l in mbase_l):
                return f

    # 3) Explicit pairing map (config): { "model_base": "mmproj-file.gguf" }
    cfg = _config() or {}
    pairs = cfg.get("mmproj_pairs") or {}
    for mb, mp in pairs.items():
        if mb.lower() in model_base_l or model_base_l in mb.lower():
            p = os.path.abspath(root_path("models", mp))
            if os.path.isfile(p):
                return p

    # 4) Generic quant-only projector (e.g. mmproj-BF16.gguf) for a known vision
    #    model. Only used when it is UNAMBIGUOUS — i.e. there is exactly one such
    #    generic projector. If several exist we can't tell which belongs to which
    #    model, so we refuse to guess rather than load a wrong projector (which
    #    crashes the server on startup).
    vision_hints = ("vl", "-vl", "vision", "gemma", "llava", "minicpm",
                    "pixtral", "phi-3.5", "phi3", "qwen2.5-vl", "qwen2vl", "smolvlm", "internvl")
    if any(h in model_base_l for h in vision_hints):
        generic = [f for f in _list_gguf_files()
                   if os.path.basename(f).lower().startswith("mmproj-")
                   and not _base_gguf_name(os.path.basename(f))]
        if len(generic) == 1:
            return generic[0]
    return None


def _config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def resolve_model(value):
    """Resolve a model reference (absolute path, relative path, or bare .gguf name)
    to an absolute path on disk. Bare names are looked up anywhere under models/
    (so models can be organized into one subfolder per model)."""
    if not value:
        return None
    value = str(value).strip().strip('"')
    p = os.path.abspath(value)
    if os.path.isfile(p):
        return p
    # Bare filename → search models/ (and its subfolders) recursively.
    base = os.path.basename(value)
    for f in _list_gguf_files():
        if os.path.basename(f).lower() == base.lower():
            return f
    return p


def _port_in_use(host, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1)
    try:
        return s.connect_ex((host, port)) == 0
    except Exception:
        return False
    finally:
        s.close()


def server_ready(host, port, timeout=180):
    """Poll /health until the llama-server reports ready (model fully loaded)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection((host, port), timeout=1)
            s.sendall(b"GET /health HTTP/1.0\r\nHost: localhost\r\n\r\n")
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = s.recv(512)
                if not chunk:
                    break
                data += chunk
            s.close()
            if b"200" in data.split(b"\r\n")[0] or b"ok" in data.lower():
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def status():
    cfg = _config() or {}
    host = cfg.get("llama_host", "127.0.0.1")
    port = int(cfg.get("llama_port", 8080))
    running = _port_in_use(host, port)
    model = os.path.basename(_running_model) if _running_model else (cfg.get("model", ""))
    return {"running": running, "host": host, "port": port, "model": model}


def _default_server_args(model_path=None):
    """Stable llama-server defaults for an 8 GB GPU.

    -ngl is intentionally NOT forced to 999: leaving it unset lets llama-server
    auto-fit the number of GPU layers so the model + KV cache + compute buffers all
    fit in VRAM (forcing 999 crashed it). Flash attention + KV-cache quantization
    keep memory low so a 16k context runs comfortably.

    `--image-min-tokens 1024` is applied ONLY for Qwen-VL style vision models,
    which need a high minimum for accurate image reading. Forcing it on every
    model breaks other projectors: e.g. gemma-4's mmproj-BF16 was built with a
    small image_max_pixels, so a 1024-token minimum makes it fail to load
    ("image_max_pixels ... is less than image_min_pixels") and the whole server
    exits. We drop it for non-Qwen-VL models.
    """
    threads = (os.cpu_count() or 4)
    args = [
        "--flash-attn", "on",
        "--ctx-size", "16384",     # 16k: stable in 8 GB VRAM (32k crashed it)
        "--cache-type-k", "q8_0",
        "--cache-type-v", "q8_0",
        "--threads", str(threads),
        # Use the GGUF's embedded chat template (Jinja). This is what makes
        # Qwen3.5's separate reasoning/thinking role work: without --jinja the
        # server uses its default formatting and the model's chain-of-thought
        # is NOT surfaced as reasoning_content, so the 🧠 Thinking block is empty.
        "--jinja",
    ]
    mn = os.path.basename(model_path or "").lower()
    if any(h in mn for h in ("qwen2-vl", "qwen2vl", "qwen2.5-vl", "qwen3-vl", "llava", "minicpm-v", "minicpmv", "pixtral")):
        args += ["--image-min-tokens", "1024"]
    return args


def _send_voice_bye():
    """Send /bye to the voice agent so it stops its llama-server (frees port 8080).

    This keeps things to ONE server at a time: when the user picks a llama.cpp model
    that needs vision, the text-only voice-agent server is shut down first.
    """
    cfg = _config() or {}
    log_dir = cfg.get("log_dir", "voiceguide_llama.cpp_guide")
    control = root_path(log_dir, "control.txt")
    try:
        with open(control, "w", encoding="utf-8") as f:
            f.write("/bye")
    except Exception:
        pass


def _voice_agent_running():
    """Return True if a voice_agent.py process is currently running."""
    try:
        import psutil
        for p in psutil.process_iter(['cmdline']):
            try:
                cmdline = ' '.join(p.info.get('cmdline') or [])
                if 'voice_agent.py' in cmdline:
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def start(model=None):
    global _process, _running_model
    with _lock:
        cfg = _config()
        if not cfg:
            return {"running": False, "error": "voiceguide_llama.cpp_guide/config.json not found"}
        host = cfg.get("llama_host", "127.0.0.1")
        port = int(cfg.get("llama_port", 8080))

        # Use the UI-selected model if provided, else fall back to config.
        model_ref = model or cfg.get("model")
        model_path = resolve_model(model_ref)
        if not model_path or not os.path.isfile(model_path):
            return {"running": False, "error": "model not found: {}".format(model_ref)}

        exe = resolve_model(cfg.get("llama_server", ""))
        if not exe or not os.path.isfile(exe):
            return {"running": False, "error": "llama-server executable not found: {}".format(cfg.get("llama_server"))}

        # Pair a vision-projector (mmproj) so the model can read images too.
        mmproj = find_mmproj(model_path)

        # Already running with the requested model → nothing to do.
        if _process is not None and _process.poll() is None and _running_model == model_path:
            return {"running": True, "model": os.path.basename(model_path), "message": "already running"}

        # Stop a previous (different-model) instance we own.
        if _process is not None and _process.poll() is None:
            try:
                _process.terminate()
            except Exception:
                pass
            _process = None

        # If the port is taken by an external process and the selected model needs
        # vision, stop the TEXT-ONLY voice agent so only ONE (vision-capable) server
        # runs. Otherwise reuse whatever is already listening.
        if _port_in_use(host, port):
            if mmproj and _voice_agent_running():
                _send_voice_bye()
                for _ in range(30):
                    if not _port_in_use(host, port):
                        break
                    time.sleep(1)
                if _port_in_use(host, port):
                    return {"running": False,
                            "error": "port {} is still busy; stop the other llama-server manually".format(port)}
            else:
                return {"running": True, "model": os.path.basename(model_path),
                        "message": "llama-server already running on port {}".format(port)}

        cmd = [exe, "-m", model_path, "--host", host, "--port", str(port)]
        if mmproj:
            cmd += ["--mmproj", mmproj]
        # Peak GPU/performance defaults (config llama_args may override).
        cmd += _default_server_args(model_path)
        cmd += [str(a) for a in cfg.get("llama_args", [])]
        try:
            _process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
        except Exception as e:
            return {"running": False, "error": str(e)}
        _running_model = model_path
        return {"running": True, "model": os.path.basename(model_path),
                "message": "starting llama.cpp with {}".format(os.path.basename(model_path))}


def stop():
    global _process, _running_model
    with _lock:
        if _process is not None and _process.poll() is None:
            try:
                _process.terminate()
            except Exception:
                pass
        _process = None
        _running_model = None
    return {"running": False, "message": "stopped"}
