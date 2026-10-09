# Changelog

All notable changes to TrioForge, newest first. This file is also the body of each
GitHub release (a workflow publishes it whenever a `v*` tag is pushed).

## [1.5.0] — plugins, connectors & DeepSeek Harness

### Plugins, skills & MCP — one inventory

A full extension system, gathered in one 🧩 **Plugins** panel (and `/plugins` in the terminal):

- **plugins** — a single `.py` file in `plugins/` exposes agent tools (`TOOLS` + a
  `dispatch(name, args)` callable), plus an optional `register(app)` for HTTP routes.
- **skills** — Markdown instruction packs in `skills/`; only their name/description is
  advertised until the model opts in with `use_skill`.
- **MCP** — external servers are borrowed as tools (`mcp__server__tool`), with enable/disable.
- A browsable **catalog**, install-by-Git-URL/folder, and a failed extension is shown as
  *broken*, not silently missing.

### Connectors — Gmail, Obsidian, Google Calendar

- **Gmail** — `gmail_list_inbox` / `gmail_read_message` / `gmail_status`, through real
  "Sign in with Google" OAuth (with an App Password over IMAP fallback). Credentials stay
  local-only; disconnecting keeps the one-time Google app.
- **Obsidian** — read, search and write notes in a local vault (fully offline — just a folder path).
- **Google Calendar** — `calendar_list_events` / `calendar_status`, its own OAuth app,
  kept deliberately separate from Gmail.

### DeepSeek Harness plugin

`dsh_run` / `dsh_status` hand a whole coding task to DeepSeek Harness (`dsh headless`) and
read back the finished result. It ships **disabled by default** (enable it in the 🧩 Plugins
panel to use) and is detected in the 🚀 Setup panel — which also ignores the unrelated
Debian program that happens to be called `dsh`.

### Docker launcher: backup, restore, download, update

`docker/application.sh` gained `--backup`, `--restore <tar.gz>`, `--download "<query>"` (they
work even without Docker), and `--update` now does `git pull --ff-only` + rebuild + `up -d`.

### Local models can reach plugins — no workspace needed, no tool template needed

Two things had locked offline (llama.cpp) models out of plugins and connectors:

- the agent only ran when a **workspace folder** was set, so Gmail/Obsidian/skills/MCP
  — which don't need a folder — were unreachable until you created one. Workspace
  file tools still require a folder, but plugin/connector/skill/MCP tools are now
  offered on their own.
- local models that ignore the native `tools` parameter (gemma-3 and friends) could
  never call a tool. The web app now offers them the same fenced ```tool {…}```
  text protocol the terminal client already uses, so any GGUF can reach the
  connectors, not just models with a function-calling template.

### Links that are really controls keep their own colour

The theme stylesheet coloured every `<a>` with the theme accent (`!important`), which
also caught links used as UI controls, repainting their own colours bright pink (very
obvious in the Galaxy theme):

- link-buttons — the Setup modal's **"⬇ Download"** and the Design Studio's
  **"⬇ HTML"** (white/light labels on a blue or near-black button)
- the **Chat / Notes / Cork Board** pills — Chat is a `<button>` but Notes and Cork
  Board are `<a>`, so only those two were recoloured and the row looked inconsistent

The `a` rule now skips any link that carries its own background, or that is a
`.tab-btn`; plain text links stay accented.

### Your name above a message is white again

The theme stylesheet painted `.msg-name` with the accent, so the name you set in your
profile showed up bright pink instead of the white the message component intends (the
bot's name stays its own blue). The override is gone, and the message timestamp below
it is now readable white rather than the faint half-dim it used to be.

### Timestamps & the desktop window

- History timestamps that came back as raw ISO (`2026-09-28T00:56:07.893032`) now render
  as `Sep 28, 2026 · 12:56 AM`.
- A one-character typo had disabled the Windows single-instance guard, so a second launch
  fought the first over the port and neither painted — fixed, and covered by a regression test.
- `--window` on Linux now checks for the WebKitGTK engine up front and prints exactly what
  to install instead of dying with an opaque GTK error.

## [1.4.9] — search & download models, and Docker on Apple Silicon

### Search and download GGUF models from the terminal

- `/search <query>` lists matching Hugging Face GGUF repos and their download
  counts — browsing only, nothing is pulled.
- `/download <query>` goes one step further: pick a repo, see its `.gguf` files
  with sizes (model first, then any `mmproj` projector), and pull the one you
  choose into `models/` — the same folder the web UI's download uses.

### Docker builds on Apple Silicon and Linux x86_64

The image moved to `python:3.12-slim` (multi-arch, so one `Dockerfile` serves
arm64 and amd64), gained a healthcheck on `TRIOFORGE_PORT`, and `.dockerignore`
now keeps the host's plugins, backups, tests and screenshots out of the image.
The Docker launcher scripts are executable out of the box, the installer message
names the correct Ubuntu/Mint package (`docker-compose-v2`), and the README walks
through the whole first-time setup (including removing a stale `docker.com` repo).

## [1.4.8] — the model picker tells you what will actually fit

### The picker shows the fit before you load it

`ctrl+l`, `/models` and `/start` now badge every offline model with its fit verdict,
computed from the live hardware spec — so a model that cannot live entirely in VRAM
is flagged *before* it is loaded instead of discovered as "why is this 1 tok/s":

- `fits in VRAM` — the whole model and its cache sit on the GPU
- `split GPU+CPU (slower)` — bigger than the card, so part runs on the CPU
- `CPU only` / `too big` — no offload, or it will not fit at all

`/start` also prints a one-line warning naming the model and the reason when it is
going to split or run on CPU, so a slow reply is explained up front.

## [1.4.7] — the design studio: live preview, files, and a database that remembers

### A brief becomes a working prototype, live

The new **Design Studio** turns a one-line brief into a project and renders it live:

- **live preview + code side by side**, with the page no longer truncated mid-CSS
  (the generation budget lifted from 8k to 65k tokens)
- a **Files tab** beside Preview, showing projects as folders (`index.html` +
  `style.css` / `app.js`) instead of a flat pile of html files
- **Python projects**: a brief can produce `main.py` + modules + `requirements.txt`,
  and a **live terminal** runs them — and speaks the project's language

### Designs survive a reload (database)

Design briefs and their generated files are now written to the database, so closing
the chat no longer loses the whole design — a stored design re-renders its live
preview from history.

### Token counter and window

- the token counter shows **total tokens consumed**, and "thinking" during reasoning
  instead of a misleading `0.0 tok/s`
- the desktop window restores itself when WebView2 collapses it to a sliver

## [1.4.6] — the desktop app actually opens, and a real DeepSeek model picker

### The desktop app opens one window that shows up

Double-clicking the app used to leave nothing on screen while TrioForge processes
piled up in Task Manager. Two faults combined:

- the "a window is already open" marker was written *after* the server was spawned
  and the window created — and with a plain write — so every impatient click started
  another full window process, six servers racing for one port and six WebView2
  engines fighting over one locked profile folder
- "the window is ready" meant *the form object exists*, not *a visible window is on
  screen*; pywebview only shows the form after WebView2 finishes initialising, so
  when that wedged, every watchdog concluded the app was healthy while you stared at
  an empty desktop

The window slot is now claimed atomically at the top of start-up, readiness means a
real visible window, a lingering hidden form is asked to show itself, and a window
that never appears retries once with a fresh profile and then hands you the app in
your browser instead of lingering invisibly. The recorded hang also expires after a
day and clears when a window paints, so one bad night no longer docks the GPU
forever.

### The DeepSeek dropdown shows the actual models

The DeepSeek provider now presents its harness-style catalogue instead of raw ids:

- **DeepSeek-V41-Flash** — text + image, 1M-token context
- **DeepSeek-V4-Pro** — text, 1M-token context, with an Off / Low / High / Max
  reasoning-effort selector that rides to the API as `reasoning_effort`

An info line shows the selected model's name, context window and input modalities,
the 👁 vision badge and image routing come from the catalogue's capabilities, and a
new `/providers/model_catalog` endpoint serves the metadata.

## [1.4.5] — memory, hybrid-GPU fixes, and /set token

### The sidebar shows the GPU working in real time

The right-hand "Machine" panel no longer prints a frozen `1.1/8.0GB free` that
looked broken while a turn ran. The primary GPU line now reads live through NVML
(in-process, with an `nvidia-smi` fallback) and shows `6.9/8.0GB 98%` — memory
used plus utilisation — refreshing on every sidebar redraw. An integrated GPU is
labelled `shares RAM` instead of a misleading shared-memory size.

### /set token caps the reply length and the context window

`/set token <max> [context]` sets how many tokens one answer may produce and the
context window handed to llama.cpp (applied on the next model load). Both values
persist in `~/.config/forge/config.json`; `/set token` with no argument shows the
current limits. Defaults: 2048 output / 16384 context.

### Hybrid laptops report every GPU again, not just the integrated one

On Linux, `py/hardware.py` only enumerated cards that publish an `amdgpu`/`i915`
`mem_info_vram_total` node, so a hybrid NVIDIA + AMD-APU machine showed just the
integrated Radeon (or nothing) and hid the discrete card entirely. Detection now
builds the inventory from `/sys/class/drm/card*` directly:

- **NVIDIA** VRAM is read from the card's 64-bit prefetchable PCIe BAR (there is
  no `mem_info_vram_*` node), and its marketing name from
  `/proc/driver/nvidia/gpus/*/information` — both stay readable even when the
  driver is version-skewed and `nvidia-smi`/NVML fail with `DriverNotLoaded`
- **AMD/Intel** keep reading `mem_info_vram_*`, and an APU iGPU (GTT far larger
  than its visible VRAM window) is correctly marked as sharing system RAM
- the loader's `--list-devices` answer and NVML are overlaid where they can see a
  card, so a Vulkan build that only lists the iGPU no longer makes the RTX card
  vanish — the sidebar and `/specs` now say "2 GPUs" with the discrete card as
  primary

### The agent has a memory — DuckDB behind a Bloom filter

`py/memory.py` gives the agent facts that outlive a session ("my llama port is
8080") and finds them again later:

- a **Bloom filter held in RAM** answers "definitely absent" without touching the
  disk, so a miss ends the lookup there; only a possible hit reads the vault
- the values live in a **DuckDB** table, which compresses the text column itself,
  so nothing zips or unzips by hand
- `recall` rewrites a plain sentence into candidate keys in RAM first, which is
  what makes "what was my port setting again?" find `port_setting` with no
  embedding model and no network
- `/memory` prints how many lookups the gate answered in RAM, so the saving is
  measured rather than claimed
- the filter is **persisted as one small blob** and **rebuilt when it fills** -
  not rescanned at boot (which would cost the very I/O the gate exists to avoid),
  and not left to saturate (a full Bloom filter answers YES to everything)

The agent gains a ninth tool, `memory` (`remember` / `recall` / `lookup` /
`forget` / `list` / `stats`), and `/memory` works in both the full-screen client
and the plain UI through the shared command table. `duckdb` is imported lazily:
without it, everything else still runs and only memory reports how to install it.

### Local model: stop leaking, load what fits, and auto-scan

The local-model path had three failure modes that each looked like "llama.cpp
won't run":

- `stop()` only killed the child *that process* started, so every restart leaked
  a llama-server holding ~6 GB of RAM and VRAM — which is what later made loads
  "fail" for lack of memory. It now kills whatever holds the port (after
  confirming it is llama-server) and verifies the port is free before saying so.
- the RAM guard judged the whole file size against free RAM, refusing a model
  that fits across **both** GPU and CPU. A tested `_plan_load` now decides it:
  fits-in-VRAM, splits GPU+CPU, or genuinely refuses. Split loads quantise the KV
  cache to `q8_0` by default.
- a saved model path that went stale (or was recorded on another machine) left
  the server permanently off. `start()` now auto-scans the model roots and runs
  the best model that fits, so the app works per-machine with no hand-edited
  config.

### Security and correctness (audited, each with a test)

- `bash` timeout no longer orphans the command's child processes; the cap is
  tunable via `TRIOFORGE_BASH_TIMEOUT`.
- dangerous tools are denied when no approval callback is wired.
- memory writes are atomic against a crash (row + filter), and over-long keys are
  handled consistently.
- tool output is secret-redacted before it reaches a model — and in team mode,
  before it is sent to the cloud senior.

### TUI polish

- the mood face no longer clips off-screen, the "Thinking…" title no longer keeps
  its focus highlight, and streamed answers re-render at a throttled rate instead
  of flickering per token.
- typing `/q` pleads on Enter only, holds 2.5 s, and a second `ctrl+q` leaves at
  once.

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
