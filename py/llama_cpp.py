#!/usr/bin/env python3
"""llama_cpp.py - run llama.cpp from the terminal, on Linux and everywhere else.

WHY THIS EXISTS

``llama-cpp-python`` is a *compiled* package: pip must build or fetch a binary
matching your Python, your CPU and your GPU backend, and on Linux that very often
fails (no wheel for your Python, no CUDA/ROCm toolchain, a libstdc++ mismatch).
This module gives the same shape of API without any compiled dependency. It
drives the ``llama-server`` binary that this project already provisions, over its
OpenAI-compatible HTTP endpoint, using only the standard library - so it works on
any Linux box that can run llama.cpp at all.

    import llama_cpp                      # with PYTHONPATH=<project>/py
    llm = llama_cpp.Llama("models/gemma/gemma-3-12b-it-Q4_K_M.gguf")
    print(llm("Q: 2+2? A:", max_tokens=32)["choices"][0]["text"])

    for chunk in llm("Tell me a story", max_tokens=200, stream=True):
        print(chunk["choices"][0]["text"], end="", flush=True)

From the shell:

    python llama_cpp.py --specs                # hardware, and what will offload
    python llama_cpp.py --which                # the binary, and its devices
    python llama_cpp.py -m MODEL -p "hello"    # one-shot
    python llama_cpp.py -m MODEL --chat        # interactive

The server is started on demand, waited for, and shut down on exit (including
Ctrl+C). GPU offload, thread count and context size are chosen from the detected
hardware, so NVIDIA/AMD/Intel/Apple pick sensible defaults with no flags.

Nothing here needs root. The binary is found via $LLAMA_SERVER, then $PATH, then
this project's own tools/llama.cpp - and downloaded if it is missing entirely.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

GB = 1024 ** 3

#: Host/port defaults. Chosen away from 8080 so a server another tool started is
#: not silently adopted (and so we never kill somebody else's process).
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8081


def _log(message: str) -> None:
    """Progress goes to stderr, so stdout stays pipeable."""
    print("[llama_cpp] {}".format(message), file=sys.stderr, flush=True)


# --------------------------------------------------------------- the binary

def find_llama_server(allow_install: bool = True) -> str:
    """Locate llama-server, or "" when there is none to be found.

    Order: $LLAMA_SERVER, then $PATH, then the project's own tools/llama.cpp
    (which the installer unpacks), then - unless disabled - an automatic
    download of the build for this machine.
    """
    explicit = os.environ.get("LLAMA_SERVER", "").strip()
    if explicit:
        if os.path.isfile(explicit):
            return explicit
        _log("$LLAMA_SERVER is set to {} but that file does not exist".format(explicit))

    from shutil import which
    found = which("llama-server")
    if found:
        return found

    try:                                    # this project's own copy
        import llama_installer
        installed = llama_installer.find_installed()
        if installed and os.path.isfile(installed):
            return installed
        if allow_install and not os.environ.get("TRIOFORGE_NO_AUTO_INSTALL"):
            _log("no llama-server found - downloading the prebuilt build for this machine")
            result = llama_installer.install_llamacpp()
            if result.get("ok"):
                _log("installed {}".format(result.get("path")))
                return result.get("path") or ""
            _log("install failed: {}".format(result.get("error")))
    except Exception as exc:                # noqa: BLE001 - never fatal
        _log("could not look for llama.cpp: {}".format(exc))
    return ""


def _server_flags(exe: str) -> set:
    """Which flags this build understands, so an older binary still starts."""
    try:
        out = subprocess.run([exe, "--help"], capture_output=True, timeout=20,
                             text=True).stdout
    except Exception:
        return set()
    return set(re.findall(r"--[a-z0-9][a-z0-9-]+", out or ""))


# --------------------------------------------------------------- the server

class Llama:
    """A llama.cpp model, driven over llama-server's HTTP API.

    Deliberately close to ``llama-cpp-python``'s ``Llama``, so code written
    against that package keeps working:

        llm = Llama(model_path, n_ctx=4096)
        llm.create_completion(prompt, max_tokens=64)
        llm.create_chat_completion(messages=[...])

    It is a *context manager*; leaving the block stops the server it started.
    A server that was already running on the same host/port is reused and left
    alone - this class only ever stops what it started itself.
    """

    def __init__(self, model_path: str, *, n_ctx: int = 4096,
                 n_gpu_layers: int | None = None, n_threads: int | None = None,
                 host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 server_args=None, verbose: bool = True, timeout: float = 600.0):
        self.model_path = os.path.abspath(os.path.expanduser(model_path))
        if not os.path.isfile(self.model_path):
            raise FileNotFoundError("no such model: {}".format(self.model_path))
        self.host, self.port = host, int(port)
        self.n_ctx = int(n_ctx)
        self.verbose = verbose
        self.timeout = float(timeout)
        self._proc = None
        self._logfile = None
        self._exe = ""

        self.n_gpu_layers, self.n_threads = self._plan(
            n_gpu_layers, n_threads, verbose)

        if self._already_running():
            self._log("reusing the llama-server already on {}:{}".format(host, port))
            return
        self._exe = find_llama_server()
        if not self._exe:
            raise RuntimeError(
                "llama-server was not found. Install llama.cpp, or set "
                "LLAMA_SERVER=/path/to/llama-server")
        self._spawn(server_args or [])

    # ------------------------------------------------------------ planning

    def _plan(self, n_gpu_layers, n_threads, verbose):
        """Pick offload and thread count from the real hardware.

        The model's file size decides whether the layers can live on the GPU:
        all of them when it fits, none when there is no GPU, and llama.cpp's own
        auto-fit in between (passing no -ngl at all is the request for that).
        """
        size_gb = os.path.getsize(self.model_path) / GB
        layers, threads = n_gpu_layers, n_threads
        try:
            import hardware
            spec = hardware.specs()
            verdict = hardware.fit(size_gb, spec)
            if layers is None:
                if verdict == "gpu":
                    layers = 999              # every layer; llama.cpp clamps it
                elif verdict == "cpu":
                    layers = 0
                else:
                    layers = None             # "split": let llama.cpp auto-fit
            if threads is None and verdict != "gpu":
                # Only pin threads when the CPU is doing the work; otherwise the
                # server's own default beats a guess.
                threads = max(1, min(int(spec.get("cpu_count") or 4), 8))
            if verbose:
                vram = spec.get("vram_total_gb") or 0
                _log("model {:.2f} GB · verdict {} · {} GB GPU memory · {}".format(
                    size_gb, verdict, vram,
                    "all layers on the GPU" if layers == 999
                    else ("auto-fit layers" if layers is None else "CPU only")))
                for note in (spec.get("notes") or [])[:2]:
                    _log(note)
        except Exception as exc:            # noqa: BLE001 - defaults are fine
            if verbose:
                _log("hardware detection unavailable ({}), using defaults".format(exc))
        return layers, threads

    # ------------------------------------------------------------ plumbing

    @property
    def base_url(self) -> str:
        return "http://{}:{}".format(self.host, self.port)

    def _already_running(self) -> bool:
        try:
            with urllib.request.urlopen(self.base_url + "/health", timeout=2):
                return True
        except Exception:
            return False

    def _spawn(self, extra) -> None:
        cmd = [self._exe, "-m", self.model_path,
               "--host", self.host, "--port", str(self.port),
               "-c", str(self.n_ctx)]
        if self.n_gpu_layers is not None:
            cmd += ["-ngl", str(self.n_gpu_layers)]
        if self.n_threads:
            cmd += ["-t", str(self.n_threads)]
        cmd += list(extra)

        flags = _server_flags(self._exe)
        if flags and "--no-webui" in flags:
            cmd.append("--no-webui")        # we never open the browser UI

        if self.verbose:
            _log("starting: {}".format(" ".join(cmd)))
        # start_new_session puts the server in its own process group, so Ctrl+C
        # reaches us first and we can stop it cleanly instead of leaving an
        # orphan holding the GPU.
        self._logfile = open(os.path.join(
            os.path.dirname(self.model_path) or ".", ".llama_cpp_server.log"), "wb")
        self._proc = subprocess.Popen(cmd, stdout=self._logfile,
                                      stderr=subprocess.STDOUT,
                                      stdin=subprocess.DEVNULL,
                                      start_new_session=True)
        self._wait_ready()

    def _wait_ready(self) -> None:
        """Block until /health reports ok, the process dies, or we time out."""
        deadline = time.time() + self.timeout
        last = 0.0
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(
                    "llama-server exited with code {} while loading {}\n{}".format(
                        self._proc.returncode, os.path.basename(self.model_path),
                        self.tail_log()))
            try:
                with urllib.request.urlopen(self.base_url + "/health", timeout=3) as r:
                    body = json.loads(r.read().decode("utf-8", "replace") or "{}")
                if body.get("status") in ("ok", "no slot available"):
                    if self.verbose:
                        _log("ready on {}".format(self.base_url))
                    return
            except Exception:
                pass
            if self.verbose and time.time() - last > 10:
                last = time.time()
                _log("still loading... ({:.0f}s)".format(
                    self.timeout - (deadline - time.time())))
            time.sleep(0.4)
        raise TimeoutError("llama-server did not become ready within {:.0f}s".format(
            self.timeout))

    def tail_log(self, lines: int = 12) -> str:
        """The end of the server's own log - the actual reason it failed."""
        path = os.path.join(os.path.dirname(self.model_path) or ".",
                            ".llama_cpp_server.log")
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return "".join(fh.readlines()[-lines:])
        except OSError:
            return ""

    def _post(self, route: str, payload: dict, stream: bool):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.base_url + route, data=data,
                                     headers={"Content-Type": "application/json"})
        if not stream:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        return self._stream(req)

    def _stream(self, req):
        """Yield OpenAI-shaped chunks from the server's SSE stream."""
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    return
                try:
                    yield json.loads(body)
                except ValueError:
                    continue

    # ------------------------------------------------------------ the API

    def create_completion(self, prompt: str, *, max_tokens: int = 256,
                          temperature: float = 0.7, top_p: float = 0.95,
                          stop=None, stream: bool = False, echo: bool = False):
        """Text in, text out. Same shape as llama-cpp-python's method."""
        payload = {"prompt": prompt, "max_tokens": int(max_tokens),
                   "temperature": float(temperature), "top_p": float(top_p),
                   "stream": bool(stream), "echo": bool(echo)}
        if stop:
            payload["stop"] = stop if isinstance(stop, list) else [stop]
        return self._post("/v1/completions", payload, stream)

    def create_chat_completion(self, messages, *, max_tokens: int = 256,
                               temperature: float = 0.7, top_p: float = 0.95,
                               stop=None, stream: bool = False):
        """Chat in, chat out. ``messages`` is [{"role","content"}, ...]."""
        payload = {"messages": messages, "max_tokens": int(max_tokens),
                   "temperature": float(temperature), "top_p": float(top_p),
                   "stream": bool(stream)}
        if stop:
            payload["stop"] = stop if isinstance(stop, list) else [stop]
        return self._post("/v1/chat/completions", payload, stream)

    def __call__(self, prompt: str, **kwargs):
        """``llm("prompt")`` behaves like create_completion, as in llama-cpp-python."""
        return self.create_completion(prompt, **kwargs)

    # ------------------------------------------------------------ lifecycle

    def close(self) -> None:
        """Stop the server, but only if this instance started it."""
        if self._proc is None:
            return
        proc, self._proc = self._proc, None
        if proc.poll() is None:
            if self.verbose:
                _log("stopping llama-server (pid {})".format(proc.pid))
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    proc.kill()
        if self._logfile:
            self._logfile.close()
            self._logfile = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


# --------------------------------------------------------------- the shell

def _cmd_specs() -> int:
    """What the machine has, and what that means for a model."""
    try:
        import hardware
    except ImportError:
        print("hardware.py is not importable; run from the project's py/ directory")
        return 1
    spec = hardware.specs()
    print("system   : {} {} / {} cores".format(
        spec["os"], spec["machine"], spec["cpu_count"]))
    print("memory   : {:.1f} GB used of {:.1f} GB ({:.0f}%)".format(
        spec["ram_used_gb"], spec["ram_total_gb"], spec["ram_percent"]))
    print("available: {:.1f} GB".format(spec["ram_available_gb"]))
    for d in spec.get("gpu_devices") or []:
        if d.get("in_use"):
            print("gpu      : {} ({:.2f} GB free of {:.2f} GB) [in use]".format(
                d["name"], d["free_gb"], d["total_gb"]))
        else:
            why = "shares system RAM" if not d.get("adds_memory") else "idle"
            print("gpu      : {} ({:.2f} GB free of {:.2f} GB) [{}]".format(
                d["name"], d["free_gb"], d["total_gb"], why))
    print("n_gpu_layers for a model:")
    for size in (2.0, 4.6, 8.0, 16.0):
        v = hardware.fit(size, spec)
        print("    {:>5.1f} GB -> {:<8} {}".format(
            size, v, "all layers on the GPU" if v == "gpu"
            else ("auto-fit" if v == "split" else
                  ("CPU only" if v == "cpu" else "too big"))))
    return 0


def _cmd_which() -> int:
    exe = find_llama_server(allow_install=False)
    if not exe:
        print("llama-server: not found (set LLAMA_SERVER, or let it auto-install)")
        return 1
    print("llama-server: {}".format(exe))
    try:
        out = subprocess.run([exe, "--list-devices"], capture_output=True,
                             text=True, timeout=30).stdout
        print(out.strip() or "(the build reports no devices - it will run on the CPU)")
    except Exception as exc:
        print("could not list devices: {}".format(exc))
    return 0


def _cmd_chat(llm: "Llama", system: str | None, args) -> int:
    history = []
    if system:
        history.append({"role": "system", "content": system})
    print("chatting with {} - Ctrl+C or 'exit' to stop".format(
        os.path.basename(llm.model_path)))
    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in ("exit", "quit", "/q"):
            return 0
        history.append({"role": "user", "content": line})
        print("bot> ", end="", flush=True)
        parts = []
        try:
            for chunk in llm.create_chat_completion(
                    history, max_tokens=args.max_tokens,
                    temperature=args.temperature, stream=True):
                piece = (chunk.get("choices") or [{}])[0].get("delta", {}).get("content")
                if piece:
                    parts.append(piece)
                    print(piece, end="", flush=True)
        except KeyboardInterrupt:
            print(" (interrupted)")
        print()
        if parts:
            history.append({"role": "assistant", "content": "".join(parts)})


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="llama_cpp.py",
        description="Run llama.cpp from the terminal - no compiled Python package.")
    p.add_argument("-m", "--model", help="path to a .gguf model")
    p.add_argument("-p", "--prompt", help="one-shot prompt (omit for --chat)")
    p.add_argument("--chat", action="store_true", help="interactive chat")
    p.add_argument("-n", "--max-tokens", type=int, default=256)
    p.add_argument("-t", "--temperature", type=float, default=0.7)
    p.add_argument("-c", "--ctx", type=int, default=4096, help="context size")
    p.add_argument("--ngl", type=int, default=None,
                   help="layers to offload (default: decided from your GPU)")
    p.add_argument("--threads", type=int, default=None)
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--system", default=None, help="system prompt for --chat")
    p.add_argument("--specs", action="store_true", help="show hardware and exit")
    p.add_argument("--which", action="store_true",
                   help="show the llama-server binary and its devices, then exit")
    p.add_argument("-q", "--quiet", action="store_true")
    args = p.parse_args(argv)

    if args.specs:
        return _cmd_specs()
    if args.which:
        return _cmd_which()
    if not args.model:
        p.error("a model is required (-m), or use --specs / --which")
    if not os.path.isfile(os.path.expanduser(args.model)):
        print("no such model: {}".format(args.model), file=sys.stderr)
        return 2

    llm = Llama(args.model, n_ctx=args.ctx, n_gpu_layers=args.ngl,
                n_threads=args.threads, host=args.host, port=args.port,
                verbose=not args.quiet)
    try:
        if args.chat or not args.prompt:
            return _cmd_chat(llm, args.system, args)
        if args.prompt:
            for chunk in llm.create_completion(
                    args.prompt, max_tokens=args.max_tokens,
                    temperature=args.temperature, stream=True):
                piece = (chunk.get("choices") or [{}])[0].get("text")
                if piece:
                    print(piece, end="", flush=True)
            print()
            return 0
        return 0
    except KeyboardInterrupt:
        print()
        return 130
    except (urllib.error.URLError, RuntimeError, TimeoutError) as exc:
        print("error: {}".format(exc), file=sys.stderr)
        tail = llm.tail_log()
        if tail:
            print("--- llama-server log ---\n{}".format(tail), file=sys.stderr)
        return 1
    finally:
        llm.close()


if __name__ == "__main__":
    sys.exit(main())
