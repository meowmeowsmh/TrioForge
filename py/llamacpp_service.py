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
