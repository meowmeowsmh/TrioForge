# llamacpp_service.py – manage the llama.cpp server (llama-server) lifecycle.
#
# When the user selects "llama.cpp" in the provider dropdown, the app auto-starts
# the local llama-server; when they switch to another provider, it is stopped.
# Reuses voiceguide_llama.cpp_guide/config.json for the executable + model paths.

import json
import os
import subprocess
import threading

from paths import root_path

CONFIG_PATH = root_path("voiceguide_llama.cpp_guide", "config.json")

_process = None
_lock = threading.Lock()


def _config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _resolve(value):
    value = str(value)
    if os.path.isabs(value) or os.path.dirname(value):
        return value
    return root_path(value)


def _port_in_use(host, port):
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1)
    try:
        return s.connect_ex((host, port)) == 0
    except Exception:
        return False
    finally:
        s.close()


def status():
    cfg = _config()
    host = (cfg or {}).get("llama_host", "127.0.0.1")
    port = int((cfg or {}).get("llama_port", 8080))
    running = _port_in_use(host, port)
    return {"running": running, "host": host, "port": port}


def start():
    global _process
    with _lock:
        cfg = _config()
        if not cfg:
            return {"running": False, "error": "voiceguide_llama.cpp_guide/config.json not found"}
        host = cfg.get("llama_host", "127.0.0.1")
        port = int(cfg.get("llama_port", 8080))
        if _process is not None and _process.poll() is None:
            return {"running": True, "message": "already running"}
        if _port_in_use(host, port):
            # Already serving (maybe started by the voice agent or an earlier run).
            return {"running": True, "message": "already listening on {}:{}".format(host, port)}

        exe = _resolve(cfg.get("llama_server", ""))
        model = _resolve(cfg.get("model", ""))
        if not exe or not os.path.exists(exe):
            return {"running": False, "error": "llama-server executable not found: {}".format(exe)}
        if not model or not os.path.exists(model):
            return {"running": False, "error": "model file not found: {}".format(model)}

        cmd = [exe, "-m", model, "--host", host, "--port", str(port)]
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
        return {"running": True, "message": "starting llama.cpp server on {}:{}".format(host, port)}


def stop():
    global _process
    with _lock:
        if _process is not None and _process.poll() is None:
            try:
                _process.terminate()
            except Exception:
                pass
        _process = None
    return {"running": False, "message": "stopped"}
