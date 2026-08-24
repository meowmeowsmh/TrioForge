#!/usr/bin/env python3
"""
TrioForge — combined local voice-to-voice agent.

Starts llama.cpp (the LLM server) and the Hugging Face speech-to-speech
agent together as ONE managed process — so you don't have to run two
commands in two terminal tabs.

It writes:
    voiceguide_llama.cpp_guide/voice_agent.log       # everything (server + agent)
    voiceguide_llama.cpp_guide/conversations.log     # the speech-to-speech output

Usage:
    python voice_agent.py
    python voice_agent.py --config path/to/config.json

Press Ctrl+C to stop both services cleanly.
"""

import argparse
import json
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "voiceguide_llama.cpp_guide" / "config.json"

DEFAULT_CONFIG_DATA = {
    "llama_server": r"C:\Users\user\AppData\Local\Microsoft\WinGet\Packages\ggml.llamacpp_Microsoft.Winget.Source_8wekyb3d8bbwe\llama-server.exe",
    "model": "models/Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf",
    "llama_host": "127.0.0.1",
    "llama_port": 8080,
    "speech_to_speech": "speech-to-speech",
    "speech_args": [
        "--mode", "local", "--no_smart_turn",
        "--llm_backend", "chat-completions",
        "--model_name", "Qwen3.5-9B",
        "--responses_api_base_url", "http://127.0.0.1:8080/v1",
        "--responses_api_api_key", "",
        "--stt_device", "cpu",
        "--parakeet_tdt_device", "cpu",
        "--tts", "facebookMMS",
        "--facebook_mms_device", "cpu",
    ],
    "log_dir": "voiceguide_llama.cpp_guide",
}


def _resolve(value):
    p = Path(str(value))
    return str(p) if p.is_absolute() else str((HERE / p).resolve())


def _now():
    return datetime.now().strftime("%H:%M:%S")


def _write(line, handles):
    print(line, flush=True)
    for h in handles:
        if h:
            h.write(line + "\n")
            h.flush()


def _tee(proc, handles, prefix):
    def run():
        if not proc.stdout:
            return
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            if line:
                _write("[{}] {}".format(prefix, line), handles)
    threading.Thread(target=run, daemon=True).start()


def _wait_ready(base_url, timeout=180):
    import urllib.request
    start = time.time()
    while time.time() - start < timeout:
        try:
            urllib.request.urlopen(base_url + "/health", timeout=1)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def main():
    parser = argparse.ArgumentParser(description="TrioForge combined voice-to-voice agent.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to config.json")
    args = parser.parse_args()

    cfg_path = Path(args.config)
    if cfg_path.exists():
        config = json.loads(cfg_path.read_text(encoding="utf-8"))
    else:
        config = DEFAULT_CONFIG_DATA
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        print("Wrote default config to", cfg_path)

    log_dir = Path(config.get("log_dir", "voiceguide_llama.cpp_guide"))
    if not log_dir.is_absolute():
        log_dir = HERE / log_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    agent_log = (log_dir / "voice_agent.log").open("a", encoding="utf-8")
    conversations = (log_dir / "conversations.log").open("a", encoding="utf-8")

    host = config.get("llama_host", "127.0.0.1")
    port = int(config.get("llama_port", 8080))
    base_url = "http://{}:{}".format(host, port)

    # ---- 1. llama.cpp server ----
    _write("[{}] Starting llama.cpp server on {} ...".format(_now(), base_url), [agent_log])
    llama_cmd = [
        _resolve(config.get("llama_server")),
        "-m", _resolve(config.get("model")),
        "--host", host,
        "--port", str(port),
    ]
    llama_proc = subprocess.Popen(
        llama_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    _tee(llama_proc, [agent_log], "LLM")

    _write("[{}] Waiting for server to be ready ...".format(_now()), [agent_log])
    if not _wait_ready(base_url):
        _write("[{}] Server did not become ready in time.".format(_now()), [agent_log])
        if llama_proc.poll() is None:
            llama_proc.terminate()
        sys.exit(1)
    _write("[{}] Server ready.".format(_now()), [agent_log])

    # ---- 2. speech-to-speech agent ----
    _write("[{}] Starting speech-to-speech agent ...".format(_now()), [agent_log])
    s2s_cmd = [_resolve(config.get("speech_to_speech"))] + config.get("speech_args", [])
    s2s_proc = subprocess.Popen(
        s2s_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    _tee(s2s_proc, [agent_log, conversations], "VOICE")

    _write(
        "[{}] Both services running. Speak / chat with the voice agent, "
        "and press Ctrl+C to stop.".format(_now()),
        [agent_log],
    )

    try:
        while True:
            time.sleep(1)
            if s2s_proc.poll() is not None:
                _write("[{}] Voice agent exited (code {}).".format(_now(), s2s_proc.returncode), [agent_log])
                break
    except KeyboardInterrupt:
        pass
    finally:
        _write("[{}] Shutting down ...".format(_now()), [agent_log])
        for p in (s2s_proc, llama_proc):
            if p and p.poll() is None:
                try:
                    p.terminate()
                except Exception:
                    pass
        agent_log.close()
        conversations.close()
        print("Stopped.")


if __name__ == "__main__":
    sys.exit(main())
