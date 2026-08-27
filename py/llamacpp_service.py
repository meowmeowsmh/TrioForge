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


def find_mmproj(model_path: str):
    """Find the vision-projector (mmproj-*.gguf) that pairs with a text model.

    Works regardless of folder: it scans <project>/models recursively and matches
    the shared base name (mmproj-BASE-<quant> pairs with BASE-<quant>.gguf).
    """
    model_base = _base_gguf_name(os.path.basename(model_path))
    if not model_base:
        return None
    model_base_l = model_base.lower()
    for f in _list_gguf_files():
        bn = os.path.basename(f)
        if bn.lower().startswith("mmproj-"):
            mbase = _base_gguf_name(bn)
            mbase_l = mbase.lower() if mbase else ""
            if mbase_l and (mbase_l in model_base_l or model_base_l in mbase_l):
                return f
    return None


def _config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def resolve_model(value):
    """Resolve a model reference (absolute path, relative path, or bare .gguf name)
    to an absolute path on disk. Bare names are looked up in <project>/models/."""
    if not value:
        return None
    value = str(value).strip().strip('"')
    p = os.path.abspath(value)
    if os.path.isfile(p):
        return p
    # Bare filename → look in the models/ folder.
    cand = os.path.abspath(root_path("models", os.path.basename(value)))
    if os.path.isfile(cand):
        return cand
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


def _default_server_args():
    """GPU/performance defaults so llama-server runs at peak on a local GPU.

    -ngl 999        offload as many layers to the GPU as fit (model + mmproj fit in
                    an 8 GB RTX 5060 here).
    --flash-attn    Flash Attention → faster and lower VRAM (room for the KV cache).
    --ctx-size N    context window sized to fit alongside the model in VRAM.
    --threads       CPU threads for the parts that stay on CPU.
    These are applied first, so anything in config.json `llama_args` overrides them.
    """
    threads = (os.cpu_count() or 4)
    return [
        "-ngl", "999",
        "--flash-attn",
        "--ctx-size", "8192",
        "--threads", str(threads),
    ]


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

        # If the port is taken by an external process (e.g. the voice agent), do not
        # spawn a duplicate that will just fail to bind.
        if _port_in_use(host, port):
            return {"running": True, "model": os.path.basename(model_path),
                    "message": "port {} already in use (another llama-server is running)".format(port)}

        cmd = [exe, "-m", model_path, "--host", host, "--port", str(port)]
        # Pair a vision-projector (mmproj) so the model can read images too.
        mmproj = find_mmproj(model_path)
        if mmproj:
            cmd += ["--mmproj", mmproj]
        # Peak GPU/performance defaults (config llama_args may override).
        cmd += _default_server_args()
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
