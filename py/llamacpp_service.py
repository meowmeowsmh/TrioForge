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
_running_ctx = None
_lock = threading.Lock()
# Anti-storm: the timestamp of the last spawn we did. While a server is still
# loading, a new request must reuse it instead of starting a second copy.
_last_spawn = 0.0
_SPAWN_COOLDOWN = 20.0
# Idle unload: the model is only needed while you are talking to it.
_last_used = 0.0
_idle_thread_started = False
# Set when the GPU runs out of memory during generation. -1 = let llama.cpp decide,
# otherwise the number of layers to put on the GPU (0 = CPU only).
_gpu_layers_override = -1


def gpu_oom_recovery(error_text: str) -> dict:
    """React to a GPU out-of-memory failure: try again with less on the GPU.

    A Vulkan/CUDA allocation can fail *during generation* even though the model
    loaded, because the decode buffers only need allocating then:

        decode() failed: vk::Device::allocateMemory: ErrorOutOfDeviceMemory

    The model server is then unusable until it is restarted with a smaller GPU
    footprint, so we stop it and remember to put fewer layers there next time -
    fewer layers, then KV in RAM, then the CPU. The next message picks it up.
    """
    global _gpu_layers_override, _last_spawn
    if "outofdevicememory" not in (error_text or "").lower().replace(" ", "") \
            and "failed to allocate" not in (error_text or "").lower() \
            and "out of memory" not in (error_text or "").lower():
        return {"handled": False}
    current = _gpu_layers_override
    if current < 0:
        # We let llama.cpp fit the layers; step down to a deliberately small number
        # so the weights are not the problem any more.
        _gpu_layers_override = 16
        detail = "16 layers on the GPU, the rest on the CPU"
    elif current > 0:
        _gpu_layers_override = 0
        detail = "CPU only (the GPU ran out of memory twice)"
    else:
        _gpu_layers_override = 0
        detail = "CPU only - the GPU is too full right now"
    stop()
    _last_spawn = 0.0                    # allow the next request to start immediately
    _log("GPU out of memory; next start will use {}".format(detail))
    return {"handled": True, "detail": detail}

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


def _default_server_args(model_path=None, ctx_size=None):
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
    # 16k is stable in ~8 GB of VRAM (32k crashed it). The context is the main lever
    # on KV-cache memory, so it is overridable: TRIOFORGE_CTX_SIZE=4096 on a small
    # machine, or 32768 if you have the room.
    try:
        ctx = int(ctx_size) if ctx_size else int(os.environ.get("TRIOFORGE_CTX_SIZE", "16384") or 16384)
    except Exception:
        ctx = 16384
    args = [
        "--flash-attn", "on",
        "--ctx-size", str(ctx),
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


def _free_vram_bytes():
    """Free GPU memory in bytes, or None when there is no NVIDIA GPU to ask.

    Used to decide whether a model can live in VRAM instead of RAM - the difference
    between a few hundred MB of system memory and five gigabytes of it.
    """
    try:
        import warnings
        warnings.filterwarnings("ignore", message=".*pynvml package is deprecated.*")
        import pynvml
        pynvml.nvmlInit()
        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            return int(pynvml.nvmlDeviceGetMemoryInfo(handle).free)
        finally:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass
    except Exception:
        return None


def _log(message: str) -> None:
    """Write a line to logs/llamacpp.log (the same file the server writes to)."""
    try:
        path = root_path("logs", "llamacpp.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8", errors="replace") as fh:
            fh.write("[{}] {}\n".format(time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except Exception:
        pass


_flag_cache = {}


def _supports_flag(exe, flag):
    """Whether `exe` understands `flag` (cached per executable).

    The launcher accepts whatever llama-server the user has - a distro package, an
    old release tarball, or the bundled build. Older builds predate flags such as
    --no-mmproj-offload, and an unknown flag makes llama-server exit immediately, so
    ask --help once and remember the answer. --help exits before touching the GPU,
    so this is cheap and safe.
    """
    key = (exe, flag)
    if key in _flag_cache:
        return _flag_cache[key]
    supported = True
    try:
        # llama-server is a console program. On Windows, started from this app (which
        # runs windowless via start.vbs), it would pop a terminal window for the probe -
        # the same reason the real spawn further down passes these flags. DETACHED_PROCESS
        # so it cannot attach to, or allocate, a console at all.
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0))
        out = subprocess.run([exe, "--help"], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, timeout=15, **kwargs)
        supported = flag in (out.stdout or b"").decode("utf-8", "replace")
    except Exception:
        # Could not ask (missing shared libs, odd wrapper, timeout). Assume the flag
        # exists: an unknown-argument exit names itself in the log, whereas dropping
        # the flag silently brings back the projector OOM crash.
        supported = True
    _flag_cache[key] = supported
    return supported


def _watch_for_load_crash(proc, log_offset, delay=20.0):
    """Notice a llama-server that dies while loading, and step the GPU footprint down.

    A load-time allocation failure aborts the process *before* it serves anything, so
    the request that triggered the start fails with a connection error. The OOM
    keywords gpu_oom_recovery() looks for never arrive, so every retry repeats the
    exact same crash - three identical aborts in logs/llamacpp.log, with the user
    left with a model server that never comes up. Reading the bytes the process just
    wrote is the only way to see what happened.
    """
    def _watch():
        try:
            code = proc.wait(timeout=delay)
        except Exception:
            return                      # still alive after `delay`: it is loading/loaded
        if code is None:
            return
        tail = ""
        try:
            with open(root_path("logs", "llamacpp.log"), "r",
                      encoding="utf-8", errors="replace") as fh:
                fh.seek(log_offset)
                tail = fh.read()
        except Exception:
            pass
        low = tail.lower()
        if ("outofdevicememory" in low.replace(" ", "")
                or "failed to allocate" in low
                or "ggml_assert" in low):
            _log("llama-server exited during load with an allocation failure; "
                 "stepping the GPU footprint down for the next start")
            gpu_oom_recovery("failed to allocate: ErrorOutOfDeviceMemory")
    threading.Thread(target=_watch, daemon=True).start()


def start(model=None, ctx_size=None):
    global _process, _running_model, _last_spawn, _last_used, _running_ctx
    # Resolve the requested context length to a concrete int (the UI's token
    # dropdown passes a value; a missing/invalid one falls back to the env var,
    # then the stable 16k default).
    try:
        if ctx_size is not None and str(ctx_size).strip() not in ("", "None"):
            ctx = int(ctx_size)
        else:
            ctx = int(os.environ.get("TRIOFORGE_CTX_SIZE", "16384") or 16384)
    except (TypeError, ValueError):
        ctx = 16384
    if ctx < 1:
        ctx = 16384
    # Every request (a chat, the UI asking to start it) counts as use, so the idle
    # unload can never take the model away from a session in progress.
    _last_used = time.time()
    ensure_idle_watchdog()
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

        # Already running with the requested model AND context → nothing to do.
        if (_process is not None and _process.poll() is None
                and _same_model(_running_model, model_path)
                and _running_ctx == ctx):
            return {"running": True, "model": os.path.basename(model_path),
                    "ctx_size": _running_ctx, "message": "already running"}

        # Stop a previous instance we own (different model, OR same model but a
        # different requested context length — llama-server takes --ctx-size only at
        # startup, so a context change means a restart).
        terminated_ours = False
        if _process is not None and _process.poll() is None:
            try:
                _process.terminate()
                terminated_ours = True
            except Exception:
                pass
            _process = None

        # If the port is taken by an external process, figure out whether it is
        # actually serving the requested model. A stale server (e.g. one started
        # earlier with a different model) must be restarted — otherwise the user
        # selects model X but keeps getting answers from a stale model Y.
        if _port_in_use(host, port):
            if terminated_ours:
                # We just stopped OUR server for a model/context change. The port
                # may still be held by the dying process for a moment — wait for it
                # to free, then fall through to a fresh start (reusing it would
                # silently ignore the requested change).
                for _ in range(15):
                    if not _port_in_use(host, port):
                        break
                    time.sleep(1)
                if _port_in_use(host, port):
                    _kill_stale_llama_server(host, port)
                    time.sleep(1)
            else:
                # Anti-storm: if something is listening and we spawned it moments ago,
                # it is still loading. Starting another one would load a second copy of
                # the model into RAM.
                if (time.time() - _last_spawn) < _SPAWN_COOLDOWN:
                    _running_model = model_path
                    _running_ctx = ctx
                    return {"running": True, "model": os.path.basename(model_path),
                            "message": "llama-server is starting (reusing it)"}

                running_model = _server_model(host, port)
                if running_model is None or _same_model(running_model, model_path):
                    # Either it is the model we want, or we cannot tell which model it
                    # is. Reuse it. Only a POSITIVE mismatch justifies killing a server
                    # that may hold gigabytes of loaded model.
                    _running_model = model_path
                    _running_ctx = ctx
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
        # ── Where will the model actually live: VRAM or RAM? ─────────────────
        # llama-server was being started with no -ngl at all, so a 5-6 GB model was
        # loaded into RAM (the "opening the app takes my memory to 86%" report) while
        # a 7 GB GPU sat idle. Decide from the real numbers and say so in the log.
        size = 0
        try:
            size = os.path.getsize(model_path)
        except Exception:
            pass
        # A vision projector (mmproj) is loaded onto the GPU as well, and llama.cpp's
        # auto-fit does NOT reserve room for it. That is what killed the server with
        #     failed to allocate Vulkan1 buffer of size 607815296
        # inside clip_model_loader::load_tensors (libmtmd.so): auto-fit spent the last
        # of the VRAM on text layers, the projector's buffer then could not be
        # allocated, and GGML_ASSERT(buffer) aborted the whole process. Count it here.
        mmproj_size = 0
        if mmproj:
            try:
                mmproj_size = os.path.getsize(mmproj)
            except Exception:
                mmproj_size = 0
        vram_free = _free_vram_bytes()
        ram_free = _free_ram_bytes()
        # Forcing every layer needs room for the weights AND the KV cache AND the
        # compute buffers AND the projector. Without that headroom llama.cpp aborts
        # with "failed to fit params to free device memory" / "failed to allocate
        # Vulkan1 buffer" and the model quietly runs on the CPU instead - which is
        # exactly how a 6 GB model ends up in RAM. When it does not fit comfortably
        # we pass nothing and let llama.cpp auto-fit the number of layers itself.
        kv_headroom = int(1.5 * 1073741824)
        offload = bool(vram_free and size
                       and vram_free > size * 1.12 + kv_headroom + mmproj_size)

        try:
            _log(("model {:.2f} GB{} · VRAM free {} · RAM free {} -> {}").format(
                size / 1073741824.0,
                "" if not mmproj_size else " + {:.2f} GB projector".format(
                    mmproj_size / 1073741824.0),
                "{:.2f} GB".format(vram_free / 1073741824.0) if vram_free else "unknown",
                "{:.2f} GB".format(ram_free / 1073741824.0) if ram_free else "unknown",
                "all layers on the GPU" if offload
                else "let llama.cpp auto-fit the layers (model + KV cache do not fit entirely)"))
        except Exception:
            pass

        # Refuse a CPU load that would not fit: this is what turned a 15 GB machine
        # into a swapping one. Needs ~1.5 GB of headroom for the KV cache, compute
        # buffers and the rest of Windows. The full model size is deliberately used
        # here even when layers are offloaded (an over-estimate), which also absorbs
        # the projector, so it is NOT added a second time and this stays a last-resort
        # guard rather than something that can refuse a load that would have fitted.
        if (not offload and ram_free and size
                and os.environ.get("TRIOFORGE_SKIP_RAM_CHECK", "").strip() not in ("1", "true", "on")):
            if size + int(1.5 * 1073741824) > ram_free:
                return {"running": False,
                        "error": "not enough free memory for {}: the model needs about {:.1f} GB and only "
                                 "{:.1f} GB of RAM is free{} (it would put this machine into swap).\n"
                                 "Options: pick a smaller GGUF, close some apps, use Ollama (it offloads to "
                                 "the GPU), or override with TRIOFORGE_SKIP_RAM_CHECK=1.".format(
                                     os.path.basename(model_path), size / 1073741824.0,
                                     ram_free / 1073741824.0,
                                     "" if vram_free is None
                                     else " and the GPU has only {:.1f} GB free (the model needs about "
                                          "{:.1f} GB to fit there)".format(vram_free / 1073741824.0,
                                                                          size * 1.12 / 1073741824.0))}

        cmd = [exe, "-m", model_path, "--host", host, "--port", str(port)]
        if mmproj:
            cmd += ["--mmproj", mmproj]
        # Keep the projector on the CPU unless the VRAM budget above clearly had room
        # for it. Its buffer is allocated outside llama.cpp's auto-fit, so leaving it
        # on the GPU is what aborted the server (see the mmproj_size note above).
        # Vision still works; it is just encoded on the CPU.
        if mmproj and not offload:
            if _supports_flag(exe, "--no-mmproj-offload"):
                cmd += ["--no-mmproj-offload"]
                _log("keeping the vision projector on the CPU (no VRAM to spare for it)")
            else:
                # An old llama.cpp keeps the projector on the GPU with nothing reserved
                # for it - the exact abort this fix exists to prevent. Say so, rather
                # than failing again with no explanation in the log.
                _log("warning: {} does not accept --no-mmproj-offload; the projector will "
                     "be placed on the GPU with little VRAM free, so a load-time OOM abort "
                     "is likely - update llama.cpp".format(os.path.basename(exe)))
        # Peak GPU/performance defaults (config llama_args may override).
        cmd += _default_server_args(model_path, ctx)
        forced = _gpu_layers_override
        if forced >= 0:
            # Stepped down after a GPU out-of-memory. This WINS over the VRAM
            # estimate: the estimate said the model fitted and then the decode
            # buffers failed to allocate, so trust the failure, not the arithmetic.
            cmd += ["--n-gpu-layers", str(forced), "--no-kv-offload"]
            _log("using {} GPU layers with the KV cache in RAM (after a GPU OOM)".format(forced))
        elif offload:
            # All layers on the GPU: this is the difference between a 5 GB RAM load
            # and a few hundred MB of RAM + VRAM.
            cmd += ["--n-gpu-layers", "99"]
        else:
            # Let llama.cpp fit the layers itself, and keep the KV cache in system
            # RAM: that is what "decode() failed ... ErrorOutOfDeviceMemory" needs.
            cmd += ["--no-kv-offload"]
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
        log_offset = 0
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            try:
                log_offset = os.path.getsize(log_path)
            except OSError:
                log_offset = 0
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
        _running_ctx = ctx
        _last_spawn = time.time()
        _last_used = time.time()
        ensure_idle_watchdog()
        # A load-time crash is otherwise silent: watch this child so the next start
        # steps the GPU footprint down instead of repeating the identical abort.
        _watch_for_load_crash(_process, log_offset)
        return {"running": True, "model": os.path.basename(model_path),
                "message": "starting llama.cpp with {}".format(os.path.basename(model_path))}


def stop():
    global _process, _running_model, _running_ctx
    with _lock:
        if _process is not None and _process.poll() is None:
            try:
                _process.terminate()
            except Exception:
                pass
        _process = None
        _running_model = None
        _running_ctx = None
    return {"running": False, "message": "stopped"}


def touch() -> None:
    """Mark the model server as used right now (keeps the idle unload away)."""
    global _last_used
    _last_used = time.time()


def _idle_watchdog() -> None:
    """Unload the model after a period without use, so memory comes back.

    A 5 GB GGUF kept resident for days is why "opening the app" looked like a memory
    problem: the model is only needed while you are actually talking to it. Ollama
    does the same thing (its models unload after a few idle minutes).
    """
    global _idle_thread_started
    while True:
        time.sleep(30)
        try:
            # Default 5 minutes, like Ollama: long enough not to reload during a
            # normal conversation, short enough that the RAM comes back by itself.
            limit = int(os.environ.get("TRIOFORGE_IDLE_UNLOAD", "300") or 0)
        except Exception:
            limit = 300
        if limit <= 0:
            continue
        with _lock:
            running = _process is not None and _process.poll() is None
            idle_for = time.time() - _last_used
        if running and idle_for > limit:
            name = os.path.basename(_running_model or "model")
            stop()
            _log("unloaded {} after {:.0f} minutes idle - memory freed".format(
                name, idle_for / 60.0))


def ensure_idle_watchdog() -> None:
    global _idle_thread_started
    if _idle_thread_started:
        return
    _idle_thread_started = True
    threading.Thread(target=_idle_watchdog, daemon=True).start()
