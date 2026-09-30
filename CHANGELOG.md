# Changelog

All notable changes to TrioForge, newest first. This file is also the body of each
GitHub release (a workflow publishes it whenever a `v*` tag is pushed).

## [1.4.0] — cross-platform, and the terminal client (`forge`)

**The headline: local AI now works on whichever machine you sit down at** — Apple,
Windows or Linux — with nothing to download and nothing to compile.

### The terminal client — `forge` / `trioforge`

A full-screen terminal chat (Crush-style) that shares TrioForge's models and saved
API keys, plus a coding agent with real file tools — `ls`, `view`, `write`, `edit`,
`bash`, `grep`, `glob`, `todos` — each tool call drawn as its own card in the
transcript.

```bash
./install.sh                    # Apple/Linux: puts forge + trioforge on your PATH
powershell -ExecutionPolicy Bypass -File .\install.ps1   # Windows

trioforge --version             # -> trioforge 1.4.0
trioforge --echo                # offline demo
trioforge                       # a real session
trioforge "explain this repo"   # one-shot
trioforge --specs               # hardware + what fits
```

- **You never install llama.cpp or pick a build.** The prebuilt binary for *your*
  machine — Metal on Apple Silicon, CUDA/ROCm/Vulkan on Linux and Windows, CPU with
  no GPU — is fetched once and reused. `trioforge --install-llama` / `/llama` control it.
- **The first message on a local model starts the server for you** — no more
  "Connection refused" for forgetting `/start`. If it can't load, the reason appears
  in the chat, not a raw socket error.
- **Pick anything with the mouse.** Every picker (model, provider, the `ctrl+p`
  command palette) is searchable, and you can click a row to move to it and click it
  again to pick it — or just use `↑`/`↓`/`PgUp`/`PgDn` + `enter`.
- **Copy an answer out with `ctrl+y`** (or `/copy`, `/copy 2` for an earlier one) —
  via the terminal clipboard, so it works over SSH too.
- **Multi-line input** with `ctrl+j` (Shift+Enter cannot be told apart from Enter).
- **`/specs` reports the machine** the way a task manager does — `5.0 GB used of
  14.7 GB (34%)`, `9.7 GB available` — and lists **every GPU with its free VRAM and a
  verdict**:

```
    gpus found    2
       Vulkan0    6.70 GB free of 7.83 GB   usable, but shares system RAM
                  AMD Radeon 610M (RADV RAPHAEL_MENDOCINO)
    -> Vulkan1    7.51 GB free of 7.96 GB   IN USE
                  NVIDIA GeForce RTX 5060 Laptop GPU
   can combine    no - only 1 card adds memory; the rest share system RAM
```

Key bindings: `enter` send · `ctrl+j` newline · `ctrl+p` commands · `ctrl+l` model ·
`ctrl+o` provider · `ctrl+y` copy · `tab` chat · `ctrl+n` new · `ctrl+q` quit.

### `py/llama_cpp.py` — a stdlib-only `llama_cpp`

For people who would otherwise `pip install llama-cpp-python` and watch it try to
compile. Drives the same prebuilt `llama-server` over HTTP, so there is nothing to
build, and nothing to type either:

```bash
python py/llama_cpp.py -p "2+2?" -n 40     # model, GPU layers and a free port auto-detected
python py/llama_cpp.py --chat
```
```python
import llama_cpp            # PYTHONPATH=<project>/py
llm = llama_cpp.Llama()     # auto-finds the model in models/
print(llm("Q: 2+2? A:", max_tokens=32)["choices"][0]["text"])
```

### Hardware detection, fixed

- Detects Apple Silicon (Metal/unified), NVIDIA (CUDA/Vulkan), AMD (ROCm/Vulkan) and
  Intel (SYCL/Vulkan) — via `llama-server --list-devices` first, then vendor APIs.
- No longer **sums** an iGPU's shared RAM with the dGPU's VRAM (which once reported
  15.8 GB and would have called a 12 GB model "fits in your GPU").
- RAM is reported as **used/total/% + available** (previously `available` was printed
  under the name "free", disagreeing with `free -h` and the desktop monitor).
- Explains why a "16 GB" machine shows 14.7 GB usable (firmware + iGPU frame buffer).

### Verified on real Windows and Apple Silicon

CI now runs the platform logic *and* `llama_cpp.py`'s lifecycle on real
`windows-latest` and `macos-14` (Apple Silicon) runners — so "works on Windows/macOS"
is executed on every push, not just claimed.

## [1.0.3] — the stability release

Large attachments now stream to disk (a 563 MB recording uploads in seconds), the app
bundles its own current ffmpeg, audio transcription runs four chunks at a time, the
idle timer no longer unloads a model mid-job, and the interface is a single dark
7-theme system (plus custom accents). The optional desktop window samples its own
pixels and falls back to the browser when a GPU can't draw it.
