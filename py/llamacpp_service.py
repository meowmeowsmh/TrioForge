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
# Anti-storm: the timestamp of the last spawn we did. While a server is still
# loading, a new request must reuse it instead of starting a second copy.
_last_spawn = 0.0
_SPAWN_COOLDOWN = 20.0

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
    """Return a model's base name: no .gguf, no mmproj marker, no trailing quant.

    Handles both projector naming styles so the shared base is recoverable:
      - mmproj-MODEL-<quant>.gguf   (prefix)
      - MODEL.mmproj-bf16.gguf      (suffix, e.g. VideoGuard)
    """
    name = os.path.splitext(filename)[0]
    if name.lower().startswith("mmproj-"):
        name = name[len("mmproj-"):]
    # A ".mmproj-…" suffix marks a projector (VideoGuard style); strip it too.
    lower = name.lower()
    idx = lower.find(".mmproj")
    if idx > 0:
        name = name[:idx]
    parts = name.split("-")
    while parts and parts[-1].lower().replace("_", "") in _QUANT_SET:
        parts.pop()
    return "-".join(parts)


def _is_mmproj(basename: str):
    """True if a .gguf basename is a vision projector.

    Supports many naming conventions so nothing is hard-coded:
      - prefix:   mmproj-<model>-<quant>.gguf        (Qwen-VL style)
      - suffix:   <model>.mmproj-bf16.gguf           (VideoGuard style)
      - hyphen:   <model>-mmproj-BF16.gguf           (gemma-4 universal style)
    Any basename containing "mmproj" (as a token) is a projector.
    """
    return "mmproj" in basename.lower()


def _model_roots():
    """The directories scanned for GGUF models (each scanned recursively).

    Each root has a DIFFERENT capability restriction — the folder a model lives in
    declares what input it accepts (see ``model_capabilities``):
      - models/                     → text + image(vision) input
      - video_model/                → video input only
      - universal_models_to_text/   → all inputs (text, image, video, audio) to text
    Keeping them as SEPARATE roots is what stops a video/universal model's projector
    from overlapping with image-text models.
    """
    return [
        os.path.abspath(root_path("models")),
        os.path.abspath(root_path("video_model")),
        os.path.abspath(root_path("universal_models_to_text")),
    ]


def model_capabilities(model_path):
    """Return the set of input capabilities for a model based on its folder.

    The folder is the restriction: it tells the app what the model is allowed to
    read, so a video posted to a models/ model is rejected instead of silently fed
    frames. Returns a set like {"text", "image"} / {"video"} / {"text","image","video","audio"}.
    """
    mp = os.path.abspath(model_path or "")
    roots = {
        os.path.abspath(root_path("models")): {"text", "image"},
        os.path.abspath(root_path("video_model")): {"video"},
        os.path.abspath(root_path("universal_models_to_text")): {"text", "image", "video", "audio"},
    }
    # Match the deepest root that is an ancestor of the model path.
    best = None
    for root, caps in roots.items():
        if mp == root or mp.startswith(root + os.sep):
            if best is None or len(root) > len(best[0]):
                best = (root, caps)
    return best[1] if best else {"text", "image"}  # default: treat unknown as text+image


def _list_gguf_files(subdir=None):
    """All .gguf files under the model roots, recursively, as absolute paths.

    `subdir` (legacy) restricts to a single root; by default every root is scanned.
    """
    import glob
    roots = [os.path.abspath(root_path(subdir))] if subdir else _model_roots()
    files = []
    for r in roots:
        if os.path.isdir(r):
            files.extend(glob.glob(os.path.join(r, "**", "*.gguf"), recursive=True))
    return files


def _folder_mmproj(model_path: str):
    """Find a projector (.gguf) in the SAME folder as the model.

    Two layouts are supported (both automatic, no hard-coding):
      - subfolder: models/<name>/<model>.gguf + <name>/<proj>.gguf
      - dedicated root: universal_models_to_text/<model>.gguf + <proj>.gguf
        (the root IS the model folder, so the projector next to it pairs directly)

    Returns None if no projector sits beside the model, or if the folder is
    ambiguous (multiple projectors and it isn't clearly the model's own folder).
    """
    model_dir = os.path.dirname(os.path.abspath(model_path))
    roots = _model_roots()
    # Collect projectors in the same directory as the model.
    same_dir_mmproj = [
        f for f in _list_gguf_files()
        if os.path.dirname(os.path.abspath(f)) == model_dir
        and _is_mmproj(os.path.basename(f))
    ]
    if not same_dir_mmproj:
        return None
    # Subfolder of a root → unambiguous, return the projector there.
    if any(model_dir != r and model_dir.startswith(r + os.sep) for r in roots):
        return same_dir_mmproj[0]
    # Model sits directly in a capability ROOT (e.g. universal_models_to_text/).
    # Pair only when there's exactly one projector beside it (unambiguous).
    if model_dir in roots and len(same_dir_mmproj) == 1:
        return same_dir_mmproj[0]
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
        if _is_mmproj(bn):
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
                   if _is_mmproj(os.path.basename(f))
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
    """Resolve a reference to an absolute path on disk — portable across OSes.

    Handles, in order:
      1. an absolute path (normalising both / and \\ separators)
      2. a command name on PATH (e.g. "llama-server" — for executables)
      3. a bare/relative .gguf name, searched under models/, video_model/ and
         universal_models_to_text/
    """
    if not value:
        return None
    value = str(value).strip().strip('"')
    # Normalise separators so a Windows-style "models\\foo.gguf" written in
    # config.json still resolves on Linux, and vice-versa.
    norm = value.replace("\\", os.sep).replace("/", os.sep)
    p = os.path.abspath(norm)
    if os.path.isfile(p):
        return p
    # Command on PATH (for the llama-server executable).
    import shutil
    which = shutil.which(norm) or shutil.which(os.path.basename(norm))
    if which and os.path.isfile(which):
        return which
    # Relative/bare .gguf filename → search the model roots recursively.
    base = os.path.basename(norm)
    for f in _list_gguf_files():
        if os.path.basename(f).lower() == base.lower():
            return f
    return p


def _resolve_llama_server_exe(value):
    """Resolve the llama-server executable portably (Windows, Linux, WSL, Docker).

    The config may hold a Windows-only path or a bare command name. Instead of
    forcing users to hand-edit that path, we fall back through, in order:

      0. the ``LLAMA_SERVER`` env var (e.g. `LLAMA_SERVER=/usr/bin/llama-server`)
      1. the configured path (normalised — ``\\``/``/`` both work)
      2. a command name on PATH ("llama-server" / "llama-server.exe")
      3. common install locations (winget/Program Files on Windows; /usr/local/bin,
         /usr/bin, /opt, ~/.local/bin, ~/llama.cpp, release-tarball dirs on Linux)
    """
    import glob
    import shutil

    # 0) Explicit env override (useful for Docker / non-standard installs).
    env_exe = os.environ.get("LLAMA_SERVER", "").strip()
    if env_exe:
        p = resolve_model(env_exe)
        if p and os.path.isfile(p):
            return p
        for name in ("llama-server", "llama-server.exe"):
            w = shutil.which(name)
            if w and os.path.isfile(w):
                return w

    # 1) Explicit configured path (Windows or POSIX separators).
    p = resolve_model(value)
    if p and os.path.isfile(p):
        return p

    # 2) Command name on PATH (covers a bare "llama-server" in config).
    for name in ("llama-server", "llama-server.exe"):
        w = shutil.which(name)
        if w and os.path.isfile(w):
            return w

    # 3) Common install locations — cross-platform, so Windows globs are harmless
    #    no-ops on Linux and vice-versa.
    home = os.path.expanduser("~")
    cands = []
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        cands.extend(glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages", "*", "llama-server.exe")))
    prog = os.environ.get("ProgramFiles", "")
    if prog:
        cands.extend(glob.glob(os.path.join(prog, "*", "llama-server.exe")))
    dirs = (
        # Linux / macOS-Intel Homebrew / Docker
        "/usr/local/bin", "/usr/bin",
        # macOS Apple Silicon: Homebrew installs here, NOT in /usr/local
        "/opt/homebrew/bin", "/opt/homebrew/opt/llama.cpp/bin",
        "/opt/llama.cpp", "/opt/llama.cpp/bin",
        "/opt/llama.cpp/build/bin", "/usr/local/lib/llama.cpp/bin",
        os.path.join(home, ".local", "bin"),
        os.path.join(home, "llama.cpp"), os.path.join(home, "llama.cpp", "bin"),
        os.path.join(home, "llama.cpp", "build", "bin"),
        os.path.join(home, "llama-bin"), os.path.join(home, "llama-bin", "bin"),
    )
    for d in dirs:
        cands.extend(glob.glob(os.path.join(d, "llama-server")))
        cands.extend(glob.glob(os.path.join(d, "llama-server.exe")))
    # Release-tarball layouts like ~/llama-b4790-bin-ubuntu-x64/bin/llama-server.
    cands.extend(glob.glob(os.path.join(home, "llama-b*-bin-*", "bin", "llama-server")))
    cands.extend(glob.glob(os.path.join(home, "llama-b*-bin-*", "bin", "llama-server.exe")))
    # Where the in-app auto-installer (llama_installer.py) extracts builds.
    tools_root = root_path("tools", "llama.cpp")
    cands.extend(glob.glob(os.path.join(tools_root, "**", "llama-server"), recursive=True))
    cands.extend(glob.glob(os.path.join(tools_root, "**", "llama-server.exe"), recursive=True))
    for c in cands:
        if os.path.isfile(c):
            return c

    # Last resort: the original path, so the caller reports a clear, specific error.
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


def _server_model(host, port):
    """Return the absolute model path the running llama-server is serving, or None.

    Queries ``/v1/models`` (OpenAI-compatible) and returns the model id/path, so we
    can tell whether a stale server on the port is serving the WRONG model.
    """
    try:
        import urllib.request
        with urllib.request.urlopen(
            f"http://{host}:{port}/v1/models", timeout=3
        ) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        for m in (data.get("data") or []) + ([data] if isinstance(data, dict) else []):
            mid = m.get("id") or m.get("model") or m.get("name")
            if mid:
                # Normalise separators for a stable comparison on Windows.
                return os.path.abspath(str(mid).replace("/", os.sep))
    except Exception:
        pass
    return None


def _kill_stale_llama_server(host, port):
    """Terminate a stale llama-server process listening on (host, port).

    The stale process is the same llama-server.exe we normally manage (just left
    over from an earlier app instance), so we can safely kill it by matching its
    executable name and the --port it was launched with. Returns True if the port
    is freed, False otherwise.
    """
    try:
        import psutil
        for conn in psutil.net_connections(kind="inet"):
            try:
                if conn.laddr and conn.laddr.port == port and conn.status == "LISTEN":
                    proc = psutil.Process(conn.pid)
                    cmdline = " ".join(proc.cmdline() or [])
                    if "llama-server" in cmdline and "--port" in cmdline and str(port) in cmdline:
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except Exception:
                            proc.kill()
                        return not _port_in_use(host, port)
            except Exception:
                continue
    except Exception:
        pass
    return False


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


def server_url():
    """Base URL (…/v1) of the llama.cpp server the app should TALK to.

    Same resolution `start()` uses: LLAMA_HOST / LLAMA_PORT win, then
    voiceguide_llama.cpp_guide/config.json, then 127.0.0.1:8080. This matters in
    remote mode (Docker → the host's llama-server): the provider has to send its
    chat requests to the server the service manager actually connected to,
    instead of posting to 127.0.0.1 inside the container.
    """
    cfg = _config() or {}
    host = os.environ.get("LLAMA_HOST") or cfg.get("llama_host", "127.0.0.1")
    try:
        port = int(os.environ.get("LLAMA_PORT") or cfg.get("llama_port", 8080))
    except (TypeError, ValueError):
        port = 8080
    return "http://{}:{}/v1".format(host, port)


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


def _same_model(a, b):
    """True when two model references mean the same file.

    Compares full paths first, then bare file names. A llama-server frequently
    reports only the file name on /v1/models, and abspath()-ing that against the
    app's working directory gives a path that never equals the real one - which
    made every chat request conclude "wrong model", kill the running server and
    start a new one. Each of those reloaded the whole model into RAM (and opened a
    console window): the machine-ending spawn storm.
    """
    if not a or not b:
        return False
    try:
        if os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b))):
            return True
    except Exception:
        pass
    return os.path.normcase(os.path.basename(str(a))) == os.path.normcase(os.path.basename(str(b)))


def _free_ram_bytes():
    """Free physical RAM in bytes, or None if it cannot be determined."""
    try:
        import psutil
        return int(psutil.virtual_memory().available)
    except Exception:
        return None


def start(model=None):
    global _process, _running_model, _last_spawn
    with _lock:
        cfg = _config()
        if not cfg:
            return {"running": False, "error": "voiceguide_llama.cpp_guide/config.json not found"}
        host = os.environ.get("LLAMA_HOST") or cfg.get("llama_host", "127.0.0.1")
        try:
            port = int(os.environ.get("LLAMA_PORT") or cfg.get("llama_port", 8080))
        except (TypeError, ValueError):
            port = 8080

        # Use the UI-selected model if provided, else fall back to config.
        model_ref = model or cfg.get("model")
        model_path = resolve_model(model_ref)
        if not model_path or not os.path.isfile(model_path):
            return {"running": False, "error": "model not found: {}".format(model_ref)}

        # Remote mode (Docker → host llama-server): when LLAMA_HOST is set, the
        # container just connects to the server running ON THE HOST. It never needs
        # a local llama-server executable, and it doesn't try to start one.
        remote_mode = bool(os.environ.get("LLAMA_HOST"))
        if remote_mode:
            if _port_in_use(host, port):
                return {"running": True, "model": os.path.basename(model_path),
                        "message": "llama.cpp (remote {}:{}) is running".format(host, port)}
            return {"running": False,
                    "error": "llama.cpp server not reachable at {}:{} — start llama-server on the host "
                             "(e.g. LLAMA_HOST=host.docker.internal LLAMA_PORT=8080)".format(host, port)}

        # Pair a vision-projector (mmproj) so the model can read images too.
        mmproj = find_mmproj(model_path)

        # Already running with the requested model → nothing to do.
        if _process is not None and _process.poll() is None and _same_model(_running_model, model_path):
            return {"running": True, "model": os.path.basename(model_path), "message": "already running"}

        # Stop a previous (different-model) instance we own.
        if _process is not None and _process.poll() is None:
            try:
                _process.terminate()
            except Exception:
                pass
            _process = None

        # If the port is taken by an external process, figure out whether it is
        # actually serving the requested model. A stale server (e.g. one started
        # earlier with a different model) must be restarted — otherwise the user
        # selects model X but keeps getting answers from a stale model Y.
        if _port_in_use(host, port):
            # Anti-storm: if something is listening and we spawned it moments ago,
            # it is still loading. Starting another one would load a second copy of
            # the model into RAM.
            if (time.time() - _last_spawn) < _SPAWN_COOLDOWN:
                _running_model = model_path
                return {"running": True, "model": os.path.basename(model_path),
                        "message": "llama-server is starting (reusing it)"}

            running_model = _server_model(host, port)
            if running_model is None or _same_model(running_model, model_path):
                # Either it is the model we want, or we cannot tell which model it
                # is. Reuse it. Only a POSITIVE mismatch justifies killing a server
                # that may hold gigabytes of loaded model.
                _running_model = model_path
                return {"running": True, "model": os.path.basename(model_path),
                        "message": "llama-server already running with the requested model"}
            # Wrong model on the port → stop the stale llama-server so we can start
            # the requested one. The stale process is a llama-server.exe (same
            # executable we manage), so we can terminate it directly by name.
            _kill_stale_llama_server(host, port)
            # Give the port a moment to free up.
            for _ in range(15):
                if not _port_in_use(host, port):
                    break
                time.sleep(1)
            if _port_in_use(host, port):
                return {"running": False,
                        "error": "port {} is busy with a different model ({}); stop the other llama-server manually".format(
                            port, os.path.basename(running_model) if running_model else "unknown")}

        # Only need the local executable if we actually have to START a server
        # (an already-running one, e.g. on the host for Docker via host.docker.internal,
        # is reused above without requiring the exe on THIS machine).
        exe = _resolve_llama_server_exe(cfg.get("llama_server", ""))
        if not exe or not os.path.isfile(exe):
            return {"running": False, "error": "llama-server executable not found: {}".format(cfg.get("llama_server"))}

        # RAM sanity check. Loading a model bigger than the free memory is what
        # turns a working machine into a thrashing one (a 12B model on a 15 GB box
        # with no offload will do it). Only refuse when it clearly cannot fit;
        # GPU offload means the file size overstates the RAM needed, hence the
        # generous factor, and TRIOFORGE_SKIP_RAM_CHECK=1 overrides it.
        if os.environ.get("TRIOFORGE_SKIP_RAM_CHECK", "").strip() not in ("1", "true", "on"):
            try:
                need = os.path.getsize(model_path)
                free = _free_ram_bytes()
                if free and need > free * 1.5:
                    return {"running": False,
                            "error": "not enough free RAM: {} needs about {:.1f} GB but only {:.1f} GB "
                                     "is free. Close something, pick a smaller model, or run this model "
                                     "through Ollama (which can offload to the GPU). Override with "
                                     "TRIOFORGE_SKIP_RAM_CHECK=1.".format(
                                         os.path.basename(model_path), need / 1073741824.0, free / 1073741824.0)}
            except Exception:
                pass

        cmd = [exe, "-m", model_path, "--host", host, "--port", str(port)]
        if mmproj:
            cmd += ["--mmproj", mmproj]
        # Peak GPU/performance defaults (config llama_args may override).
        cmd += _default_server_args(model_path)
        cmd += [str(a) for a in cfg.get("llama_args", [])]
        # Run the prebuilt llama-server from ITS OWN directory and point
        # LD_LIBRARY_PATH there: the llama.cpp release tarballs ship libggml.so /
        # libllama.so next to the binaries, and if they aren't on the library path
        # the server dies instantly with "cannot open shared object file". This is
        # a very common Linux failure; setting cwd + LD_LIBRARY_PATH fixes it.
        exe_dir = os.path.dirname(os.path.abspath(exe))
        env = dict(os.environ)
        if os.name != "nt":
            _libp = env.get("LD_LIBRARY_PATH", "")
            env["LD_LIBRARY_PATH"] = exe_dir + (os.pathsep + _libp if _libp else "")
        # Log the server's stdout/stderr so a crash is diagnosable (instead of
        # being swallowed by DEVNULL). Read logs/llamacpp.log to see WHY it failed.
        log_path = root_path("logs", "llamacpp.log")
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            _out = open(log_path, "ab")
        except Exception:
            _out = subprocess.DEVNULL
        try:
            spawn_kwargs = {}
            if os.name == "nt":
                # No console window for the model server. CREATE_NO_WINDOW alone was
                # not enough here: DETACHED_PROCESS is also needed so the child cannot
                # attach to (or allocate) a console at all - otherwise a model server
                # started by a windowless app still opened a terminal window.
                spawn_kwargs["creationflags"] = (
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    | getattr(subprocess, "DETACHED_PROCESS", 0))
                spawn_kwargs["close_fds"] = True
            _process = subprocess.Popen(
                cmd,
                stdout=_out,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=exe_dir,
                env=env,
                **spawn_kwargs
            )
        except Exception as e:
            return {"running": False, "error": str(e)}
        _running_model = model_path
        _last_spawn = time.time()
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
