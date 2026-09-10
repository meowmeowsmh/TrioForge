# ⚙️ TrioForge

> **Your own private, free AI workspace.** Chat with any local model, organize notes, and plan ideas on a corkboard — all on **your machine**, all **offline-first**, all **free with Ollama**.

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/)
[![GitHub last commit](https://img.shields.io/github/last-commit/meowmeowsmh/TrioForge)](https://github.com/meowmeowsmh/TrioForge)

---

## 💡 Why TrioForge? (instead of ChatGPT)

| | TrioForge | Typical cloud AI |
|---|---|---|
| 🔒 **Privacy** | Your chats stay on **your machine**. | Your chats go to their servers. |
| 💸 **Cost** | **$0** with local Ollama models. | Subscription or per-token fees. |
| 📴 **Offline** | Chat works with **no internet** (local models). | Requires a connection. |
| 🧠 **Your models** | Qwen, Llama, Mistral, DeepSeek — you pick. | Fixed to whatever they offer. |
| 🗂️ **All-in-one** | Chat **+** notes **+** corkboard in one app. | Just chat. |
| ⚙️ **Open & yours** | Self-host, inspect, and extend it. | Closed black box. |

**Bottom line:** if you care about privacy, money, or running your own models, TrioForge is the free, self-hosted alternative.

---

## 🎯 Who it's for

- **Privacy-conscious users** who don't want their conversations in the cloud.
- **Self-hosters** who like running their own tools on their own hardware.
- **Budget users** who want a capable AI assistant without a subscription.
- **Students & researchers** who want chat plus a personal knowledge base (notes + corkboard).
- **LLM hobbyists** who want to switch between local models freely.

---

## 📸 See it in action

| Chat | Notes | Corkboard |
|------|-------|-----------|
| ![Chat interface](chat.png) | ![Notes](notes.png) | ![Cork Board](cork_board.png) |

---

## ✨ Features

- 🔓 **100% free** — no API keys, no limits when using local Ollama models.
- 🧠 **Any local model** — Qwen, Llama, Mistral, DeepSeek, and more.
- 🧰 **Multi-provider** — Ollama, llama.cpp, Hugging Face, Groq, DeepSeek, Claude, **Gemini**, and **OpenRouter** (one key → hundreds of models: GPT, Claude, Gemini, Llama, plus vision, workspace tools, image & video).
- 🤖 **Coding agent** — pick a workspace folder; the AI can **list**, **search**, **read**, **write**, **edit** (surgical string replacement), **run shell commands**, and **search the web**, looping up to 20 tool steps per task — with a **live diff panel** showing exactly what it edits (and any code it prints, persisted across restarts).
- 🤝 **Multi-agent** — run up to 6 models in parallel on one task, each with an optional role.
- 🤖 **Full computer access** — optional, gated: open URLs/apps, type, press keys, screenshot (needs explicit opt-in).
- 🗣️ **Voice (Jarvis-style)** — local speech-to-speech, browser STT/TTS, and `/open` voice commands for system control.
- 🌐 **External web search** — DuckDuckGo integration gives local models real-world knowledge (toggle 🔍 in the input bar, or as an agent tool).
- 🧠 **Thinking (reasoning) mode** — when a reasoning model returns its chain-of-thought, it streams **live** into a collapsible "🧠 Thinking" block (Ollama, llama.cpp, DeepSeek, Groq, OpenRouter, HF), with live generation timing.
- 📚 **Document chat (RAG)** — upload PDFs, Word docs, Markdown, code, CSV, etc.; ask questions and get answers grounded in your documents (keyword retrieval out-of-the-box, semantic embeddings when installed).
- ⚔️ **A/B model compare** — run two models side-by-side on one prompt and compare answers + latency.
- 👁 **Multi-modal vision** — upload images and have the AI describe/answer about them (works with vision-capable models: Claude, Gemini, GPT, Qwen-VL, LLaVA, …).
- 📎 **File & image upload** — attach images, PDFs, code files, and documents.
- 🎤 **Voice input** — speech-to-text directly in your browser.
- 🖱️ **Drag & drop** — drop files or folders onto the chat window.
- 📑 **Persistent chats** — conversations auto-save and survive restarts.
- 🗄️ **SQLite audit log** — every message is logged; recover deleted chats.
- 📝 **Notes & corkboard** — built-in tools for organizing facts and ideas. Notes **auto-save as you type** (debounced), and the knowledge graph animates with unique node shapes, isolated-node motion, and boundary bounce so no node drifts off-screen.
- 💾 **Live monitor** — real-time RAM & VRAM usage tracking.
- 🖼️ **Image generation** — via **OpenRouter** (52 cloud image models), **Gemini** (Nano Banana / Imagen), or **ComfyUI** (Z-Image Turbo / FHDR, local).
- 🎬 **Video generation** — via **OpenRouter** (28 cloud video models: Kling, Veo 3, Hailuo, …) or **ComfyUI** (Wan 2.2 / LTX, free & offline).
- 🔌 **Plugins** — drop a `.py` file into `plugins/` to add routes/features; loads automatically at startup.
- 🌐 **Remote access** — LAN (binds `0.0.0.0`) + optional internet tunnel (cloudflared/ngrok).
- 🔓 **Uncensored by default** — ships with an abliterated Qwen model as the default.
- 🚀 **First-run setup checker** — auto-detects missing local services/models and shows download links.
- 🔒 **HTTPS** — auto-generates SSL certificates on Windows.

---

## 🆕 Recently added

- 🍎🐧🪟 **True cross-platform support (Windows · macOS · Linux · WSL)** — the app now auto-locates its tools on every OS:
  - **⚡ Auto-install llama.cpp** from the Setup panel: it detects your GPU backend (**Metal** on Apple Silicon, **CUDA** on NVIDIA, **ROCm** on AMD, **Vulkan**, or **CPU**) and downloads the matching prebuilt build.
  - **Homebrew on Apple Silicon is searched** (`/opt/homebrew/bin`) — the previous reason `brew install llama.cpp` was silently not found on M-series Macs.
  - **No HTTPS certificate scare**: Linux/macOS serve plain **`http://localhost:5003`** (localhost is a secure context — mic/clipboard still work, and no "not secure" warning even in Firefox). Windows keeps HTTPS. Force either with `TRIOFORGE_SSL=0` / `=1`.
  - **Auto port selection**: if 5003 is held by another app, the app moves to the next free port instead of wrongly claiming "already running".
  - **Model folders are created on startup** (`models/`, `video_model/`, `universal_models_to_text/`), so a fresh `git clone` always has somewhere to drop GGUFs.
  - **macOS smoke test in CI** (`.github/workflows/macos-smoke.yml`) runs the app on a real Apple Silicon runner.
- 🎙️ **Voice-to-voice is now required + installable** — the Setup panel installs the `speech-to-speech` package (**⚡ Install**), and the agent runs on its own port **8082** so it never collides with the chat llama-server. Only **ComfyUI** is optional (your choice).
- 🔄 **UI updates now apply on a normal refresh** — the HTML was cached in memory keyed only on the model, so edits/`git pull`s appeared to "do nothing" until a restart. The cache is now keyed on the file's mtime + size.
- 🐳 **Docker can use a host llama-server** — `LLAMA_HOST=host.docker.internal` puts the app in "remote mode": it connects to llama.cpp running on your host (where the GPU is) instead of trying to launch one inside the container.
- 📝 **Live coding panel (fixed & upgraded)** — now captures **any code the model prints** (fenced, indented, or code-heavy replies) from **any provider** — not just agent tool calls. A **frontend path** reports rendered code even if the server misses it, and everything is **persisted to `sqlite_data/edits.db`** so it survives restarts. Auto-opens on a coding burst, shows a green-dot notification, and the 📝 button no longer pops open on page load.
- 🐛 **llama.cpp streaming fix** — streaming mode now auto-starts the llama-server (and resolves the bare GGUF filename to its full path), so local models connect and stream instead of returning 500.
- 📚 **RAG error clarity** — unreadable/scanned PDFs and unsupported files now return a clear message ("scanned/image-only, requires OCR") instead of silently storing 0 chunks.
- ⚔️ **A/B model compare** — a new ⚔️ panel runs two models side-by-side on one prompt (threaded, in parallel) and shows each answer with its provider, model, and generation time.
- 🧰 **MCP-style agent tools** — the coding agent gained a `web_search` tool (DuckDuckGo) alongside `list_files`, `search_files`, `read_file`, `write_file`, `edit_file`, and `run_command`.
- 🤖 **Coding agent (upgraded)** — the workspace tools now include `list_files` (recursive), `search_files` (grep), `read_file`, `write_file`, `edit_file` (surgical string replacement), and `run_command` (bounded shell execution). The tool loop runs up to **20** model→tool round-trips per task.
- 🧠 **Thinking (reasoning) mode** — DeepSeek, llama.cpp, Groq, and OpenRouter reasoning models now surface their `reasoning_content`, saved with the message and rendered as a collapsible "🧠 Thinking" block.
- 📚 **Document chat (RAG)** — a new `📚` button + panel lets you upload documents (PDF/Word/Markdown/code/CSV), which are chunked and indexed into SQLite (`rag.db`). Toggle `📚` in the input bar and the most relevant chunks are injected into your prompt. Works with plain keyword retrieval out-of-the-box; upgrades to semantic search when `sentence-transformers` is installed.
- 🧠 **Assistant personas** — pick from presets (🎓 Friendly Tutor, 💻 Code Mentor, 📊 Professional Analyst) or write your own **✏️ Custom** persona. Applies across **Chat**, **Notes**, and **Cork Board**, and remembers your choice.
- 🎭 **Persona vs default voice** — the persona voice is used for **API-key providers** (Groq / Hugging Face / DeepSeek / Claude), while **local providers** (Ollama / llama.cpp) speak with the default assistant voice — so each bot sounds the way you'd expect.
- 🖥️ / ☁️ **Local vs API badge** — every bot reply shows a small pill so you can instantly tell whether it came from a **local model** or an **API-key model**, even after a reload.
- 🔑 **Cross-page key/provider persistence** — your selected provider, model, and API key now carry over between Chat, Notes, and Cork Board (shared localStorage) and survive reloads.
- 🔏 **Independent per-provider API keys** — each provider (OpenRouter, Gemini, Groq, DeepSeek, Claude, …) stores its **own** key in its own slot, so switching never overwrites another provider's key. One shared input box just shows/saves whichever provider is currently selected.
- 🧰 **OpenRouter as the all-in-one provider** — one key gets you text chat, **workspace tools** (read/write files in a folder), **image generation** (52 models), and **video generation** (28 models), with a searchable live model dropdown. Image/video routes use the dedicated `/api/v1/images/generations` (image) and `/api/v1/videos` (video) endpoints.
- 🧾 **Clear error messages** — service errors are classified instead of raw codes: invalid/missing key (401), payment/credit (402), rate limit (429), server error (5xx), timeout, or unreachable — shown in the message area with a **Paid (API)** / **Free (local)** tag.
- 🧹 **Ollama memory cleanup** — switching models now unloads the previous model first, so RAM/VRAM stops stacking up.
- 💬 **Circular loading spinner** — a clean rotating spinner while the bot is generating.
- 👤 **User & bot profiles** — click an avatar to open a profile popup with a profile picture, optional name, bio, gender, born-at, and stay-at fields. Reset the image back to default any time.

---

## 🚀 Quick Start

> 🐍 **No Python? No problem.** You don't need Python (or Docker) installed — the
> launcher finds/installs the **latest Python** and shows a **Launch** prompt.
> See **[INSTALL_PYTHON.md](INSTALL_PYTHON.md)** for the full step-by-step.

```bash
# 1. Clone the repository
git clone https://github.com/meowmeowsmh/TrioForge.git
cd TrioForge

# 2. Run it — ONE command does the whole first-run setup:
#    - finds / installs Python (latest)
#    - creates the project venv and installs ALL core deps into it
#      (flask, flask-compress, psutil, frontmatter, providers…) — no manual pip
#    - creates the model folders
#    Windows: double-click application.bat
#    Linux / macOS / WSL: ./run.sh

# 3. In the app → 🚀 Setup panel → click "⚡ Auto-install"  (llama.cpp for your GPU)
# 4. Get a model → click "⬇" to download from Hugging Face, or drop a .gguf
#    into models/ (or video_model/ or universal_models_to_text/)
# 5. Pick it in the dropdown, type, Enter — done.
```

The app opens by itself at **http://localhost:5003** (Linux / macOS) or
**https://localhost:5003** (Windows).

> **Ports:** if 5003 is already held by another program, TrioForge **picks the next
> free port** and prints it — it only reports "already running" when it really *is*
> TrioForge on that port. Override with `TRIOFORGE_PORT=xxxx ./run.sh`.
>
> **HTTPS:** Linux/macOS serve **plain HTTP** on purpose — `localhost` is a "secure
> context", so there's **no scary certificate warning** (even in Firefox) and the
> mic/clipboard still work. Force HTTPS with `TRIOFORGE_SSL=1 ./run.sh` (and install
> the mkcert CA so your browser trusts it); `TRIOFORGE_SSL=0` forces HTTP on Windows.
>
> **Models are files, not packages** — nothing installs them for you except the
> built-in ⬇ downloader. Just put a `.gguf` in the right folder and it appears.

### 🚀 First-run setup checker

On first launch, TrioForge shows a **Setup panel** that detects which local services/files are present vs. missing, each with a download link — so you're never left guessing why a provider says "connection refused".

| Item | Required? | Auto? |
|------|-----------|-------|
| **Ollama** | ✅ | Manual install — [ollama.com/download](https://ollama.com/download) |
| **llama.cpp** (`llama-server`) | ✅ | **⚡ Auto-install** (auto-detects your GPU backend) — the app auto-starts it |
| **Voice-to-voice** (speech-to-speech) | ✅ | **⚡ Install** in the Setup panel (runs with llama.cpp on port 8082) |
| **GGUF models** | ✅ | Via the ⬇ button or Hugging Face; app auto-loads from `models/` |
| **ComfyUI** (image/video) | ❌ optional | **User chooses** — [comfy.org/download](https://www.comfy.org/download); cloud image/video works without it |

You can reopen the panel anytime with the **🚀** button in the top bar.

### 🌍 Platform support

| Platform | Launcher | Local llama.cpp | Default URL |
|---|---|---|---|
| 🪟 **Windows** | `application.bat` | winget build, or **⚡ Auto-install** | `https://localhost:5003` |
| 🐧 **Linux** | `./run.sh` | `apt`/build, or **⚡ Auto-install** | `http://localhost:5003` |
| 🍎 **macOS (Apple Silicon)** | `./run.sh` | `brew install llama.cpp`, or **⚡ Auto-install** | `http://localhost:5003` |
| 🐧🪟 **WSL2** | `./run.sh` | same as Linux | `http://localhost:5003` |
| 🐳 **Docker** | `./docker/application.sh` | host llama-server via `LLAMA_HOST` | `http://localhost:5002` |

Everything is auto-located, so nobody hand-edits a path:

- **GPU backend detected** — Metal (Apple Silicon) / CUDA (NVIDIA) / ROCm (AMD) / Vulkan / CPU.
- **llama-server found** on `PATH`, in `winget`, `/opt/homebrew/bin`, `/usr/local/bin`,
  `/usr/bin`, `/opt/llama.cpp`, `~/.local/bin`, `~/llama.cpp{,/build/bin}`, release-tarball
  dirs, or `tools/llama.cpp` (where **⚡ Auto-install** extracts it). `LLAMA_SERVER`
  overrides it outright.
- **Model folders created on startup**, so a fresh `git clone` always has somewhere to drop GGUFs.
- **ffmpeg** found via `PATH` (and installed in the Docker image) for video/audio-to-text.
- **Port auto-selection** — if 5003 is busy, the next free port is used automatically.

macOS support is verified in CI on a real Apple Silicon runner —
see [`.github/workflows/macos-smoke.yml`](.github/workflows/macos-smoke.yml).

### ⬇ Downloading a GGUF model from Hugging Face

You can download any GGUF model straight into the app without leaving the UI:

1. Click the **⬇** button in the top bar.
2. Enter the **repo id** (e.g. `bartowski/Qwen2.5-7B-Instruct-GGUF`).
3. Enter the **GGUF filename** (e.g. `Qwen2.5-7B-Instruct-Q4_K_M.gguf`).
4. Optionally enter a matching **mmproj** filename for vision models (e.g. `mmproj-Qwen2.5-7B-Instruct-BF16.gguf`).

The file(s) land in `models/` and appear in the **llama.cpp** dropdown — ready to run locally (including workspace tools). The download is non-blocking and you'll see the result in the status bar.

### 🗂️ Organizing models + vision projectors (llama.cpp) — automatic pairing & capability folders

llama.cpp loads a text `.gguf` model, and vision models also need a **projector** (`mmproj` `.gguf`) so they can read images/video. TrioForge pairs the two **automatically** and reads the model's **folder name** to know what input it accepts. You never hand-edit any config — you just create the folder, drop the files in, and it works.

#### 📁 Step 1 — the three folders (exact names, copy these)

Create these folders at the **top level of the project** (next to `py/` and `models/`):

| Exact folder name | What it's for | What input the model accepts |
|---|---|---|
| `models` | normal chat + image models | text, image |
| `video_model` | video models | video only |
| `universal_models_to_text` | all-in-one models | text, image, video, audio |

> ⚠️ The names are **exact** — it's `video_model` (singular) and `universal_models_to_text` (underscores). If you name it `video_models` or `universal-model-to-text`, the app won't find it and the model won't show in the dropdown.

#### 📁 Step 2 — what files go in each folder

Each model needs **two files** if it's a vision/video model:

1. **The model file** — the big `.gguf` (the "brain").
2. **The projector file** — a smaller `.gguf` with `mmproj` in its name (the "eyes").

A **text-only** model needs only the `.gguf` (no projector).

Here is the **complete example** of all three folders, exactly as they should look on disk:

```text
TrioForge/
├── models/                                  ← text + image models
│   ├── gemma-4/
│   │   ├── gemma-4-12B-it-qat-UD-Q4_K_XL.gguf        ← the model
│   │   └── mmproj-BF16.gguf                          ← the projector
│   ├── qwen3.5/
│   │   ├── Qwen3.5-9B-...-Q4_K_M.gguf                ← the model
│   │   └── mmproj-Qwen3.5-9B-...-BF16.gguf           ← the projector
│   └── ornith/
│       └── Ornith-1.5-9B-Q4_K_M.gguf                 ← text-only: no projector
│
├── video_model/                             ← video-only models
│   └── VideoGuard/
│       ├── VideoGuard-Qwen3.5-9B-...-Q4_K_M.gguf     ← the model
│       └── VideoGuard-Qwen3.5-9B-....mmproj-bf16.gguf← the projector
│
└── universal_models_to_text/                ← all-to-all models (text/image/video/audio)
    ├── gemma-4-E4B-it-ultra-uncensored-heretic-Q5_K_M.gguf   ← the model
    └── gemma-4-E4B-it-mmproj-BF16.gguf                       ← the projector
```

#### 📁 Step 3 — rules that matter (don't skip)

- **The projector sits NEXT TO the model** (same folder) — that's how the app pairs them automatically. No name-matching needed when they're in the same folder.
- **Text-only models** just have no projector file in their folder.
- **The folder = the restriction.** Post a video to a `models/` model → you get a clear *"this model can't read video"* message. Move that model into `video_model/` (video) or `universal_models_to_text/` (everything) and it works.
- **Projector naming** is detected automatically whether it's `mmproj-...`, `....mmproj-...`, or `...-mmproj-...` — any `.gguf` filename containing `mmproj` is treated as a projector.

#### Automatic pairing rules (all built in, nothing to configure)

1. **Same folder** as the model (recommended — zero conflicts).
2. **Shared base name** across the model roots.
3. Optional override via `mmproj_pairs` in `voiceguide_llama.cpp_guide/config.json` — only if you ever need to force a pairing.
4. A single generic quant-only projector (`mmproj-BF16.gguf`) for a known vision model, as a last resort. If there's more than one generic projector, it refuses to guess (never loads a wrong projector, which would crash the server).

> 🔍 The **llama.cpp** dropdown lists every `.gguf` under `models/`, `video_model/`, and `universal_models_to_text/` (recursively, subfolders included), each tagged with its folder capability. Vision models automatically get a matching projector; the server is launched with `--jinja` (for reasoning) and `--image-min-tokens 1024` only for Qwen-VL-style models (which need it) — so gemma-4, Qwen3.5, VideoGuard, etc. load cleanly.

---

## ▶️ How to run — which file do I use?

There are three launch files, but you only ever need **one**. Pick by your operating system:

| Your OS | Use this file | How |
|---------|---------------|-----|
| 🪟 **Windows** | `application.bat` | Double-click it |
| 🐧 **Linux / WSL** | `run.sh` | `./run.sh` in a terminal (first time: `chmod +x run.sh`) |
| 🍎 **macOS (Intel or Apple Silicon)** | `run.sh` | Same: `./run.sh` — it uses Homebrew's Python and finds `/opt/homebrew/bin` tools |
| 🛠️ Any OS (advanced) | `launcher.py` | `python launcher.py` |

> **They all do the exact same thing.** `application.bat` and `run.sh` are just thin wrappers that call `launcher.py`, which auto-detects your OS, installs dependencies if needed, and starts the app.
>
> So the simple rule:
> - **Windows users → double-click `application.bat`**
> - **Everyone else (Linux, WSL, macOS) → run `./run.sh`**
>
> You can ignore the other two files.

`run.sh` is a proper first-run installer: it finds/installs Python, creates the venv
(`.venv-linux`), installs **all** core dependencies (not just flask — it also checks
`flask_compress`, `psutil` and `frontmatter`), and creates the model folders. Add `--ml`
(or `TRIOFORGE_ML=1`) to also install the optional torch/semantic-search stack.

The launcher also shows a small menu (Run on Windows / Run on Linux-macOS-WSL / Auto-detect / Quit) so you can pick how to start it.

---

## 🎬 Image & video generation

TrioForge generates images **and videos**. Pick the backend in each panel, then type a prompt.

### 🖼️ Images
Three backends, chosen in the 🖼️ panel:

| Backend | Models | Needs | Notes |
|---------|--------|-------|-------|
| **OpenRouter** | 52 cloud image models (Mai-Image, Recraft, Seedream, Grok Imagine, …) | OpenRouter API key | Aspect ratio + size (1K/2K/4K) controls |
| **Gemini** | Nano Banana (`gemini-2.5-flash-image`) / Imagen | Gemini API key | Cloud |
| **ComfyUI** | Z-Image Turbo / FHDR | Local ComfyUI | Free & offline |

### 🎬 Videos
Two backends, chosen in the 🎬 panel:

| Backend | Models | Needs | Notes |
|---------|--------|-------|-------|
| **OpenRouter** | 28 cloud video models (Kling, Veo 3, Hailuo, …) | OpenRouter API key | Set duration in seconds (5–15s typical) |
| **ComfyUI** | Wan 2.2 / LTX | Local ComfyUI | Free & offline, VRAM-hungry |

ComfyUI is auto-detected and its workflows are discovered from `blueprints/` and `user/*/workflows/`.

- **🖼️ Images** — click the 🖼️ button in the top bar, pick a backend + model, type a prompt, and the result is saved to your chat history.
- **🎬 Videos** — click the 🎬 button, pick a backend + model, set resolution/duration, and generate a playable clip.

> ⚠️ ComfyUI video models (Wan 2.2 / LTX) are large and VRAM-hungry — start with a short length and small resolution. ComfyUI must be running on `COMFYUI_URL` (default `http://127.0.0.1:8188`).

Generated images/videos are saved under `static/uploads/generated/` and `static/uploads/generated_video/`.

---

## ⚙️ Configuration

Configuration is done through environment variables — all optional, the app works out of the box with Ollama.

| Variable | Purpose | Default |
|----------|---------|---------|
| `TRIOFORGE_PORT` | Port the app listens on (auto-picks the next free port if busy) | `5003` (`5001` under gunicorn/Docker) |
| `TRIOFORGE_SSL` | `1` = force HTTPS, `0` = force plain HTTP, unset = auto (HTTP on Linux/macOS, HTTPS on Windows) | *(auto)* |
| `TRIOFORGE_WORKERS` | Gunicorn worker count (Docker) | `2` |
| `TRIOFORGE_ML` | `1` = also install the optional torch/semantic-search stack | *(unset)* |
| `LLAMA_SERVER` | Explicit path to the `llama-server` executable (skips auto-detection) | *(auto-detected)* |
| `LLAMA_HOST` | llama-server host. Set it (e.g. `host.docker.internal`) to enable **remote mode** — connect instead of launching a local server | `127.0.0.1` |
| `LLAMA_PORT` | llama-server port | `8080` |
| `OLLAMA_BASE_URL` | Ollama server URL | `http://127.0.0.1:11434` |
| `GROQ_API_KEY` | Groq provider key | *(unset)* |
| `DEEPSEEK_API_KEY` | DeepSeek provider key | *(unset)* |
| `ANTHROPIC_API_KEY` | Claude provider key | *(unset)* |
| `OPENROUTER_API_KEY` | OpenRouter key — gateway to hundreds of models (GPT/Claude/Gemini/Llama/…) + workspace tools | *(unset)* |
| `GEMINI_API_KEY` | Gemini provider key (or `GOOGLE_API_KEY`) | *(unset)* |
| `OLLAMA_REGISTRY_TOKEN` | Token for `ollama push` | *(unset)* |
| `COMFYUI_URL` | ComfyUI server URL (image + video generation) | `http://127.0.0.1:8188` |
| `COMFYUI_INSTALL` | Optional explicit ComfyUI install path | *(auto-detected)* |

API keys can also be entered directly in the web UI. Keys are never written to disk.

---

## 🗂️ Workspaces & folder access

Workspaces let a model **read and write files on your machine**, scoped to one folder you choose. A workspace has just four fields:

| Field | Purpose |
|-------|---------|
| **Folder** | The folder the model can access (e.g. `D:\TrioForge`). |
| **Folder access** | `read` (list + read only) or `readwrite` (also write). |
| **Thinking** | `low` / `mid` / `high` effort hint. |
| **Dependencies** | Optional dependency notes for the model. |

The model can call six workspace tools:

| Tool | What it does | Needs `readwrite`? |
|------|--------------|-------------------|
| `list_files` | List a folder (optionally the whole tree, recursively) | no |
| `search_files` | Grep file names + contents for a term or regex | no |
| `read_file` | Read a text file (up to 20 KB) | no |
| `write_file` | Create/overwrite a text file | ✅ yes |
| `edit_file` | Surgical replacement of one exact string (safe, unique-match) | ✅ yes |
| `run_command` | Run a shell command in the folder (30 s cap, output truncated to 4 KB) | no |

> 🔒 **Path safety:** every file path is resolved against the configured folder, and `..` traversal outside that folder is blocked. `run_command` runs with the workspace folder as its working directory, so it inherits the same scoping.

> ⚠️ `run_command` executes real shell commands on your machine. Only point the workspace at folders you trust, and keep `Folder access` at `read` unless you actually want the model to modify files.

The tool loop runs up to **20** model↔tool round-trips per message, so a request like *"create a `hello.py` that prints hi, run it, then fix any errors"* can be completed end-to-end without you babysitting it.

### Which providers support it

| Provider | Workspace tools |
|----------|-----------------|
| Ollama | ✅ (chat + streaming) |
| llama.cpp (local GGUF) | ✅ |
| DeepSeek | ✅ |
| Groq | ✅ |
| Claude | ✅ |
| **OpenRouter** | ✅ (any model that supports tools — GPT/Claude/Gemini/Llama) |

> ⚠️ Tool reliability depends on the model. For llama.cpp, **Qwen3.5** handles tools well; small models (e.g. 1B) often emit malformed tool calls.

Just ask naturally — e.g. *"read py/app.py"* or *"create a file notes.txt with this content"* — and the model will call the tools for you.

---

## 📚 Document chat (RAG)

Upload documents and chat with them — the AI retrieves the relevant parts and answers from your files instead of guessing.

1. Click the **📚** button in the top bar to open the document panel.
2. **⬆ Upload docs** — PDF, `.docx`, Markdown, `.txt`, code (`.py`/`.js`/…), CSV, HTML, and more.
3. Documents are split into overlapping chunks and stored in `sqlite_data/rag.db` (survives restarts).
4. Toggle the **📚** button in the message input bar to **ON**, then ask your question.

### How retrieval works

- **Out of the box** — fast keyword-overlap scoring (no extra dependencies needed).
- **With the optional ML stack installed** (`pip install -r requirements-ml.txt`) — semantic search using `all-MiniLM-L6-v2` embeddings, so it matches meaning, not just exact words.

> ⚠️ **`requirements-ml.txt` pulls in `torch` + the CUDA toolkit (~2 GB).** It is **not** installed by default so first-run stays light. Only install it if you want semantic RAG search.

The top ~6 matching chunks are injected into the prompt with a `[Reference documents]` header, so the model answers grounded in your docs. Document indexing is stored per-file (re-uploading a file replaces its old chunks).

> ℹ️ For best results with large PDFs, install the semantic-search stack. For quick Q&A over code/notes, the keyword fallback is already good.

---

## 🧠 Thinking (reasoning) mode

When you use a reasoning model (DeepSeek R1/V3, Qwen3.5 via Ollama or llama.cpp, Groq, or OpenRouter reasoning models), the model's chain-of-thought is captured **live as it streams** and shown as a collapsible **"🧠 Thinking"** block above the answer — updating in real time while the model thinks, then finalized when it answers.

- Works automatically — no toggle needed. Reasoning is captured from every streaming provider:
  - **Ollama** → its `thinking` field (streamed live)
  - **llama.cpp / DeepSeek / Groq / OpenRouter / Hugging Face** → `reasoning_content` / `reasoning` deltas (streamed live)
- The ⏱️ status readout (bottom bar) shows **live token speed + elapsed time** while generating, and the final duration when it finishes.
- Reasoning is saved with the message, so it's still there after a reload.
- Set the thinking **effort** hint (`low`/`mid`/`high`) in the workspace settings (⚙️).

---

## 🔌 Plugins

TrioForge has a lightweight plugin system. Drop a single `.py` file into the top-level `plugins/` folder and it loads automatically at startup.

```python
# plugins/my_plugin.py
MANIFEST = {
    "name": "my-plugin",
    "title": "My Plugin",
    "version": "1.0.0",
    "description": "What it does",
}

def register(app):                 # optional — receives the Flask app
    from flask import jsonify

    @app.route("/api/plugin/my-plugin/ping")
    def _ping():
        return jsonify({"ok": True})
```

- Plugins can add routes, register blueprints, or hook into the app.
- A broken plugin is **reported and skipped** — it never crashes the app.
- The UI lists loaded plugins via `GET /api/plugins`. An example plugin (`plugins/example_hello.py`) ships with the repo.
- See `plugins/README.md` for the full guide.

> 🔒 Plugins are trusted Python that runs inside the app process — only install plugins you trust (same rule as browser/VSCode extensions).

---

## 🆚 Feature comparison vs. other tools

| Feature | TrioForge | Notes |
|---------|-----------|-------|
| Coding agent (read/write/edit/run + loop) | ✅ | Scoped to one folder you choose; 20-step loop + live diff panel |
| MCP-style tools (file ops, shell, web search) | ✅ | `list_files`/`search_files`/`read_file`/`write_file`/`edit_file`/`run_command`/`web_search` |
| Computer control (browser/apps/typing) | ✅ | Opt-in via full-access; `open_url`/`open_app`/`type_text`/`press_keys`/`screenshot` |
| Multi-agent (parallel models) | ✅ | 🤝 panel — up to 6 models on one task |
| Voice (STT / TTS / speech-to-speech) | ✅ | Local speech-to-speech + `/open` voice command |
| External web search | ✅ | DuckDuckGo, free, no API key |
| Thinking / reasoning display | ✅ | Collapsible chain-of-thought + live timing |
| Document chat (RAG) | ✅ | Keyword now, semantic with optional install |
| Multi-modal vision | ✅ | Model-dependent |
| A/B model compare (side-by-side) | ✅ | ⚔️ panel — two models, one prompt |
| Remote access (LAN / tunnel) | ✅ | LAN via `0.0.0.0`; tunnel via cloudflared/ngrok |
| Plugins / extensions | ✅ | Drop-in `.py` files in `plugins/` |
| Uncensored by default | ✅ | Default is `vaultbox/qwen3.5-uncensored`; curated abliterated list |
| Image generation | ✅ | OpenRouter / Gemini / ComfyUI |
| Video generation | ✅ | OpenRouter / ComfyUI |
| Notes + corkboard | ✅ | Unique advantage |
| SQLite audit log | ✅ | Recover deleted chats |
| HTTPS auto-SSL | ✅ | On Windows |
| No-code drag-drop agent builder | 🟡 planned | Drag-drop exists; node-based agent graph not yet |

---

## ⚔️ A/B model compare

Run two models side-by-side on the same prompt and compare answers, latency, and quality.

1. Click the **⚔️** button in the top bar to open the compare panel.
2. Pick a provider + model for side **A** and side **B** (they can differ by provider, model, or both).
3. Type a prompt and click **⚔️ Compare**.

Both models run in parallel (threaded) and their replies render side-by-side, each labeled with its provider, model, and generation time. Works across all providers (Ollama, llama.cpp, Groq, DeepSeek, Claude, OpenRouter, Gemini, HF).

---

## 🤝 Multi-agent (several models, one task)

Run up to 6 models **in parallel** on the same task, each with an optional role.

1. Click the **🤝** button in the top bar.
2. Add agents (+ Add agent), each with a provider + model + optional role (e.g. *"code reviewer"*, *"security auditor"*).
3. Type the task and click **🤝 Run**.

Each agent answers independently and its reply renders in its own card. This is a parallel "council" mode — useful for cross-checking an answer, getting multiple code solutions, or comparing how different models reason about the same problem. (For a single-prompt head-to-head, use ⚔️ A/B compare instead.)

---

## 📝 Live coding panel

The live-coding panel shows a real-time, right-side view of everything the AI produces as **code** — whether it's editing files with the agent tools or simply **printing code in the chat**.

- Click **📝** in the top bar to open it.
- Each entry shows the file, the tool used, and a colored `-` / `+` **unified diff**.

### What gets captured (auto-detected, any provider)
The panel records code from **any** source, big or small:
- **Agent file edits** — `write_file` / `edit_file` tool calls (with a workspace folder set to `readwrite`).
- **Printed code blocks** — fenced ```` ```lang … ``` ```` blocks, indented (pasted) code, and code-heavy replies. Captured by **both** the backend and the browser (frontend posts the rendered reply), so no provider or route is missed.
- **Direct workspace writes** — any file written through the app's write API.

### Persistence
Edits are stored in **`sqlite_data/edits.db`** (via `edits_store`), so they **survive app restarts**. The panel rebuilds from disk on launch. **Clear** wipes the history (memory + DB).

### Notifications
When code/edits arrive, the 📝 button shows a **green dot**. If the model produces a genuine burst of edits (or code), the panel **auto-opens**; an idle timeout auto-closes it. You can also open/close it manually.

This turns the agent into a *visible* editor: you see exactly what it's changing or generating, line by line, and it never disappears on restart.

---

## 🤖 Full computer access (browser / apps / typing / screenshots)

The agent can control your whole machine — open URLs in the browser, launch apps, type text, press keys, and take screenshots — **only after you explicitly enable it**.

1. Open **⚙️ Workspace settings**.
2. Tick **"🤖 Full computer access"** and Save.

This unlocks these agent tools (on top of the folder tools):

| Tool | What it does | Needs |
|------|--------------|-------|
| `open_url` | Open a URL in the default browser | full access |
| `open_app` | Launch an app by name/path (notepad, calc, chrome…) | full access |
| `type_text` | Type into the focused window | full access + `pyautogui` |
| `press_keys` | Press a key combo (`ctrl+c`, `alt+tab`…) | full access + `pyautogui` |
| `screenshot` | Capture the screen to `static/uploads/screenshots/` | full access + `pyautogui` |

> ⚠️ **This gives the AI control of your entire machine** (browser, apps, keyboard). Only enable it for a workspace/task you trust, and keep an eye on the live coding panel. `pyautogui` is optional — install it with `pip install pyautogui` for typing/keys/screenshots.

---

## 🗣️ Voice (Jarvis-style)

TrioForge has local **speech-to-speech** (via `py/tools/voice_agent.py` — STT + llama.cpp + TTS, all on-device) plus browser **speech input** (🎤 button) and **text-to-speech output** (🔊 button).

For *system control by voice*, use the voice chat's `/open` command (requires full access):

```
/open https://github.com        # open a website
/open notepad                    # open an app
/bye /clear /help                # voice agent control
```

> Honest limits: this is local STT→LLM→TTS, not a full cloud "assistant" with a wake word or OS-level microphone daemon. It won't reliably fill web forms or click buttons on its own — that requires a computer-use model (e.g. Claude Computer Use) plus a screenshot loop. The building blocks (voice + browser/app control + screenshot tools) are all here; a full autonomous Jarvis loop is the next step if you want it.

---

## 🎬 Video-to-text (describe/answer about a video)

Upload a video and ask about it. TrioForge samples the clip into a few frames with **ffmpeg** (auto-detected on PATH — nothing hard-coded) and feeds them to your vision model (gemma-4 / Qwen-VL via llama.cpp, Ollama, …).

- `POST /api/video/frames` with `{"b64": "…"}` returns the sampled JPEG frames.
- The chat route does this automatically: when you attach a video, its frames are extracted and sent through the normal image-vision path, so a vision model can describe or answer about the clip.
- If ffmpeg isn't installed, it degrades gracefully (a "no video support" note) instead of crashing.

---

## 🌐 Remote access (phone / LAN / tunnel)

TrioForge binds to **`0.0.0.0`** (all interfaces), so it's reachable from any device on your network out of the box.

- **LAN (same Wi-Fi):** open `http://<your-computer-IP>:5003` on your phone (the app logs
  the exact URL at startup — **http** on Linux/macOS, **https** on Windows).
  - You may need to allow the port through your firewall (`5003`).
  - On **https**, other devices will warn about the self-signed cert — that's expected; use
    `Advanced → Continue`. Using **http** on the LAN avoids the warning entirely (but note
    the mic/clipboard "secure context" benefits only apply to `localhost`, not a LAN IP).
- **Internet (anywhere):** run a tunnel from another terminal, then open the tunnel URL.
  Match the scheme your instance uses (`http` unless you set `TRIOFORGE_SSL=1`):
  - **cloudflared** (free): `cloudflared tunnel --url http://localhost:5003`
  - **ngrok**: `ngrok http 5003`

> ⚠️ Exposing the app to the internet lets *anyone* with the URL use it. TrioForge has no built-in auth — put a reverse proxy with a password (or a tunnel with auth) in front of it before exposing it publicly.

---

## 🔓 Uncensored by default

The default model is **`vaultbox/qwen3.5-uncensored:9b`** — an abliterated (refusal-removed) model, so TrioForge answers freely out of the box with local Ollama. You can swap to any model you like, including the curated abliterated/uncensored vision list built into the app (`UNCENSORED_VISION_MODELS`).

---

## 🔒 HTTP vs HTTPS

**By default the app serves plain HTTP on Linux/macOS and HTTPS on Windows.**

Why the difference? A self-signed certificate makes browsers show a scary
*"Your connection is not private"* page — and **Firefox uses its own trust store**, so it
warns even when the OS trusts the mkcert CA. Plain `http://localhost` avoids that
**without giving anything up**: `localhost` is a **secure context**, so the microphone,
clipboard and crypto APIs all keep working.

| OS | Default URL | Cert warning? |
|----|-------------|---------------|
| Linux / macOS | `http://localhost:5003` | ✅ none |
| Windows | `https://localhost:5003` | none (mkcert CA trust-installed) |
| Docker | `http://localhost:5002` | ✅ none |

Force either mode at any time:

```bash
TRIOFORGE_SSL=1 ./run.sh    # force HTTPS (only warning-free if your browser trusts the cert)
TRIOFORGE_SSL=0 ./run.sh    # force plain HTTP
```

Certificates are generated automatically with **mkcert** — the correct binary is
downloaded for your OS/arch (`darwin-arm64`, `linux-x64`, `windows-amd64`, …), or an
existing `mkcert` on your `PATH` is used. The CA lives in `~/.local/share/mkcert`
(Linux/macOS) or `%LOCALAPPDATA%\mkcert` (Windows).

### Waitress entry point (Windows / macOS)

```bash
python py/https_guni_n_waitress.py
```

### Gunicorn entry point (Linux / WSL2 / Docker)

```bash
gunicorn -c py/gunicorn_conf.py py.app:app
```

> Gunicorn relies on `fork()`, so it runs natively on Linux/WSL2, not Windows.

---

## 🐳 Docker

Docker runs the app with **gunicorn** (a Linux production WSGI server — the native
Windows path uses Waitress instead). This is the recommended way to run TrioForge on a
**Linux server, WSL2, or a NAS**.

### 🚀 One command (recommended)

```bash
chmod +x docker/application.sh     # first time only
./docker/application.sh
```

`docker/application.sh` is the Docker equivalent of `run.sh` — it does the whole first-run
setup for you:

1. Checks **Docker + Compose** are installed **and the daemon is running** — with
   copy-paste per-OS install instructions if either is missing (macOS Homebrew / Linux
   `apt` + `usermod -aG docker`).
2. Creates the **host folders that get bind-mounted** (`json_configuration/`,
   `sqlite_data/`, `static/uploads/`, `cert_store/`, `logs/`, and the three model folders)
   as *your* user — so Docker never creates root-owned directories in your project.
3. Checks for a **host `llama-server`** (on `PATH`, `/opt/homebrew/bin`, `/usr/local/bin`,
   `/usr/bin`, `~/llama.cpp/build/bin`, …) and tells you whether it's actually reachable on
   `:8080` — so you know before you chat whether local models will work.
4. **Builds** the image (only when needed) and **starts** the stack.
5. Prints the exact **URL**, plus how to view logs, check status, and stop it.

```bash
./docker/application.sh --build        # force a rebuild
./docker/application.sh --no-build     # start without building
./docker/application.sh --foreground   # run attached (Ctrl+C stops it)
./docker/application.sh --logs         # follow logs
./docker/application.sh --status       # container status
./docker/application.sh --stop         # stop + remove
./docker/application.sh --help
```

### Or do it by hand

```bash
cd docker
docker compose build     # build the image
docker compose up -d     # start in detached mode
docker compose logs -f   # follow the logs
```

Then open **http://localhost:5002/** — the compose maps host `5002` → container `5001`
to dodge a stale WSL port-relay. Set `TRIOFORGE_PORT` **and** the `ports:` mapping
together if you want a different one (e.g. `"5003:5003"` + `TRIOFORGE_PORT=5003`).

### How it works

0. **`docker/application.sh`** — the one-command helper described above. It only *drives*
   Docker (checks, folders, build, up) and adds nothing to the image itself.
1. **`Dockerfile`** — slim `python:3.10-slim` plus build tools, **openssl** and **ffmpeg**
   (video/audio-to-text). Installs `requirements.txt`, copies the app, pre-creates the
   data + model folders, and exposes **5001**. The optional ML stack
   (`requirements-ml.txt`, torch ~2 GB) is commented out so the image stays small —
   uncomment to bake it in.
2. **`docker-entrypoint.sh`** — serves **plain HTTP by default** (no cert scare). Only when
   `TRIOFORGE_SSL=1` does it generate a self-signed cert and let gunicorn serve HTTPS.
3. **`gunicorn_conf.py`** — binds `TRIOFORGE_PORT` (default `5001`), honours
   `TRIOFORGE_SSL`, and keeps the worker count **low (2)**: TrioForge keeps per-process
   state and SQLite writes are only guarded per-process, so many workers cause
   *"database is locked"*. Override with `TRIOFORGE_WORKERS`.

### llama.cpp in Docker (the important bit)

**The image does not ship `llama-server`** — it's a GPU service, and you want it on the
host anyway. The compose sets `LLAMA_HOST=host.docker.internal`, which puts the app into
**remote mode**: it simply *connects* to a llama-server running on your host instead of
trying to launch one inside the container (and it needs no local executable for that).

```bash
# on the HOST (where your GPU is):
llama-server -m models/your-model.gguf --port 8080
# then:
cd docker && docker compose up -d
```

The model folders are bind-mounted, so GGUFs you drop on the host appear in the
container's dropdown. If you'd rather run llama.cpp *inside* the container, uncomment the
`deploy:` GPU block in the compose file (needs the image to include llama-server plus the
NVIDIA Container Toolkit).

### Connecting Ollama

The app talks to Ollama on the host (not in a container). Two options:

- **Ollama on the host** (default) — leave `OLLAMA_BASE_URL` as `http://host.docker.internal:11434`. Works out of the box on Docker Desktop and on Linux with `host-gateway`.
- **Ollama in its own container** — uncomment the `ollama:` service in `docker-compose.yml`, then point the app at `OLLAMA_BASE_URL=http://ollama:11434`.

### Data persistence & git-ignore

The compose file mounts these as volumes so your data survives restarts:

| Host path | Container path | Purpose |
|-----------|----------------|---------|
| `../json_configuration` | `/app/json_configuration` | Conversations, notes, model config |
| `../sqlite_data` (bind mount) | `/app/sqlite_data` | SQLite chat history — shares the host's actual DBs |
| `../static/uploads` | `/app/static/uploads` | Uploaded files & generated media |
| `../cert_store` | `/app/cert_store` | TLS certificates (only used when `TRIOFORGE_SSL=1`) |
| `../models`, `../video_model`, `../universal_models_to_text` | `/app/...` | Your GGUF models — drop them on the host, they appear in the container |
| `../logs` | `/app/logs` | `server.log` + `llamacpp.log` — read these to diagnose a model failure |

**Important:** the image deliberately excludes your local models **and** the auto-installed
llama.cpp binaries. `models/`, `video_model/`, `universal_models_to_text/`, `tools/llama.cpp/`
(hundreds of MB) and `.venv*` are all in `.dockerignore`. The container installs its own deps,
and local inference runs on the **host** (llama.cpp remote mode / Ollama), not inside the image.

> ⚠️ gunicorn relies on `fork()`/POSIX signals, so it only runs on **Linux / WSL2 / macOS**,
> not native Windows. On Windows use `application.bat` (Waitress) instead.

---

## 🗂️ Where your data lives

| Path | What's stored |
|------|---------------|
| `json_configuration/` | Conversations, notes, model config, attachments |
| `sqlite_data/` | SQLite databases (chat history, notes, corkboard) |
| `cert_store/` | Auto-generated SSL certificates (only used with `TRIOFORGE_SSL=1`) |
| `static/uploads/` | Uploaded images/files |
| `logs/` | `server.log` + `llamacpp.log` (llama-server output — read this if a model fails) |
| `tools/llama.cpp/` | Auto-installed llama.cpp builds from **⚡ Auto-install** |

All of the above are **git-ignored** — every user keeps their own data private. (Same for
auto-installed llama.cpp binaries: they're excluded, so a clone stays small.)

### 🚫 Model files are never committed

Local model weights are large and user-specific, so `.gitignore` excludes them **entirely** — no GGUF, safetensors, projector, Modelfile, or download cache is tracked:

```
/models/
/video_model/
/universal_models_to_text/
*.gguf  *.safetensors  *.bin  *.pt  *.pth  *.onnx  *.ckpt
```

Only the tiny human-written **docs/config** inside `models/` (e.g. `models/instruction.md` and the Ollama `Modelfile`/`Modelfile2` pointers) are kept — the actual `.gguf` weights are never committed.

---

## 🧱 Project structure

```
TrioForge/
├── py/                          # ← all Python code
│   ├── app.py                   # Main Flask app + chat/conversation routes
│   ├── common.py                # Shared JSON / SQLite / embedding helpers
│   ├── paths.py                 # Project-root path helper
│   ├── llamacpp_service.py      # llama-server lifecycle + cross-platform exe/model resolution
│   ├── llama_installer.py       # ⚡ Auto-install llama.cpp for the detected GPU backend
│   ├── setup_check.py           # First-run checker + GPU-backend detection (Metal/CUDA/ROCm/Vulkan/CPU)
│   ├── comfyui_service.py       # ComfyUI image + video + audio generation (live workflow discovery)
│   ├── video_to_text.py         # Video → frames / audio → WAV chunks (ffmpeg) for vision & audio models
│   ├── providers/
│   │   └── llm_providers.py     # LLM provider abstraction (Ollama, llama.cpp, Groq, DeepSeek, Claude, Gemini, OpenRouter)
│   ├── features/
│   │   ├── notes.py             # Notes blueprint (Obsidian-style knowledge base)
│   │   ├── cork_board.py        # Corkboard blueprint (pins, links, AI assist)
│   │   └── viewer.py            # Image viewer blueprint
│   ├── tools/
│   │   ├── launcher.py          # Cross-platform launcher
│   │   └── voice_agent.py       # Local voice-to-voice agent launcher
│   ├── https_guni_n_waitress.py # Waitress + HTTPS server (Windows)
│   └── gunicorn_conf.py         # Gunicorn server config (Linux/Docker)
├── docker/                      # ← Docker files
│   ├── application.sh           # One-command Docker setup/launch (Linux/macOS)
│   ├── Dockerfile
│   ├── docker-compose.yml
│   └── docker-entrypoint.sh
├── .github/workflows/
│   └── macos-smoke.yml          # CI: runs + tests the app on real Apple Silicon
├── pyproject.toml               # uv project (deps + optional groups)
├── uv.lock                      # uv lockfile (reproducible env)
├── application.bat              # Windows launcher (double-click)
├── run.sh                       # Linux / macOS / WSL launcher (auto-setup)
├── voice_agent.bat              # Voice agent launcher (double-click)
├── templates/
│   └── index.html               # Frontend (HTML/CSS/JS)
├── static/                      # Static vendor assets (highlight, mermaid, …) + generated media
├── tools/llama.cpp/             # ⚡ Auto-installed llama.cpp builds (git-ignored)
├── logs/                        # server.log + llamacpp.log (git-ignored)
├── models/                      # Image/text GGUF models + Ollama Modelfile (weights git-ignored)
├── video_model/                 # Video-only GGUF models (git-ignored)
├── universal_models_to_text/    # All-to-all models: text/image/video/audio → text (git-ignored)
├── voiceguide_llama.cpp_guide/  # Voice agent config + logs (logs git-ignored)
├── json_configuration/          # User data (git-ignored)
├── sqlite_data/                 # SQLite databases (git-ignored)
├── cert_store/                  # Auto-generated SSL certificates (git-ignored)
├── requirements.txt / requirements-ml.txt / README.md / LICENSE / SECURITY.md / Disclaimer.md / CODE_REVIEW.md
└── .gitignore / .dockerignore
```

---

## 📄 License

Released under the [MIT License](LICENSE).
