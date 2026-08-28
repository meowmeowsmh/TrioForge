# voice_service.py – start / stop / status the voice-to-voice agent.
#
# The voice agent (py/tools/voice_agent.py) runs its own llama-server on the same
# port as the llama.cpp chat server, so only ONE of them should be active at a time.
# This module lets the app start/stop it from the Services panel.

import os
import subprocess
import sys
import threading

from paths import root_path

VOICE_SCRIPT = root_path("py", "tools", "voice_agent.py")
CONTROL_FILE = root_path("voiceguide_llama.cpp_guide", "control.txt")

_process = None
_lock = threading.Lock()


def status():
    with _lock:
        running = _process is not None and _process.poll() is None
    return {"running": running}


def start():
    global _process
    with _lock:
        if _process is not None and _process.poll() is None:
            return {"running": True, "message": "voice agent already running"}
        if not os.path.exists(VOICE_SCRIPT):
            return {"running": False, "error": "voice_agent.py not found"}
        try:
            _process = subprocess.Popen(
                [sys.executable, str(VOICE_SCRIPT)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
        except Exception as e:
            return {"running": False, "error": str(e)}
        return {"running": True, "message": "starting voice agent… (see 🗒️ Logs > Voice)"}


def stop():
    global _process
    with _lock:
        # Ask the agent to exit gracefully via its control file.
        try:
            with open(CONTROL_FILE, "w", encoding="utf-8") as f:
                f.write("/bye")
        except Exception:
            pass
        if _process is not None and _process.poll() is None:
            try:
                _process.terminate()
            except Exception:
                pass
        _process = None
    return {"running": False, "message": "stopped"}
