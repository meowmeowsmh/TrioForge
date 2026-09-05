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
- 🌐 **Optional web search** — DuckDuckGo integration for up-to-date answers.
- 📎 **File & image upload** — attach images, PDFs, code files, and documents.
- 🎤 **Voice input** — speech-to-text directly in your browser.
- 🖱️ **Drag & drop** — drop files or folders onto the chat window.
- 📑 **Persistent chats** — conversations auto-save and survive restarts.
- 🗄️ **SQLite audit log** — every message is logged; recover deleted chats.
- 📝 **Notes & corkboard** — built-in tools for organizing facts and ideas.
- 💾 **Live monitor** — real-time RAM & VRAM usage tracking.
- 🖼️ **Image generation** — via **OpenRouter** (52 cloud image models), **Gemini** (Nano Banana / Imagen), or **ComfyUI** (Z-Image Turbo / FHDR, local).
- 🎬 **Video generation** — via **OpenRouter** (28 cloud video models: Kling, Veo 3, Hailuo, …) or **ComfyUI** (Wan 2.2 / LTX, free & offline).
- 🔒 **HTTPS** — auto-generates SSL certificates on Windows.

---

## 🆕 Recently added

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

```bash
# 1. Clone the repository
git clone https://github.com/meowmeowsmh/TrioForge.git
cd TrioForge

# 2. Install dependencies
#    With uv (recommended) — the launcher uses this automatically:
#        uv sync
#    Or with pip:
#        pip install -r requirements.txt

# 3. Pull a local model (or any model you prefer)
ollama pull vaultbox/qwen3.5-uncensored:9b
# ...e.g. `ollama pull llama3.2` or `ollama pull qwen2.5` also work

# 4. Run it — see "▶️ How to run" below (pick the file for your OS)
```

Then open **https://localhost:5001/** in your browser.

> **Tip:** if the app starts in plain HTTP (no certificates), use **http://localhost:5001/** instead.

### ⬇ Downloading a GGUF model from Hugging Face

You can download any GGUF model straight into the app without leaving the UI:

1. Click the **⬇** button in the top bar.
2. Enter the **repo id** (e.g. `bartowski/Qwen2.5-7B-Instruct-GGUF`).
3. Enter the **GGUF filename** (e.g. `Qwen2.5-7B-Instruct-Q4_K_M.gguf`).
4. Optionally enter a matching **mmproj** filename for vision models (e.g. `mmproj-Qwen2.5-7B-Instruct-BF16.gguf`).

The file(s) land in `models/` and appear in the **llama.cpp** dropdown — ready to run locally (including workspace tools). The download is non-blocking and you'll see the result in the status bar.

---

## ▶️ How to run — which file do I use?

There are three launch files, but you only ever need **one**. Pick by your operating system:

| Your OS | Use this file | How |
|---------|---------------|-----|
| 🪟 **Windows** | `application.bat` | Double-click it |
| 🐧 **Linux / macOS / WSL** | `run.sh` | Run `./run.sh` in a terminal (first time: `chmod +x run.sh`) |
| 🛠️ Any OS (advanced) | `launcher.py` | `python launcher.py` |

> **They all do the exact same thing.** `application.bat` and `run.sh` are just thin wrappers that call `launcher.py`, which auto-detects your OS, installs dependencies if needed, and starts the app.
>
> So the simple rule:
> - **Windows users → double-click `application.bat`**
> - **Everyone else → run `./run.sh`**
>
> You can ignore the other two files.

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

The model can call three workspace tools:

- `list_files` — list what's in the folder
- `read_file` — read a text file inside the folder
- `write_file` — write a text file (only when folder access is `readwrite`)

> 🔒 **Path safety:** every file path is resolved against the configured folder, and `..` traversal outside that folder is blocked.

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

## 🔒 Running with HTTPS

### Windows / macOS — Waitress

`python py/app.py` auto-generates SSL certificates (via **mkcert**) and serves HTTPS on port 5001. You can also run the dedicated Waitress entry point:

```bash
python py/https_guni_n_waitress.py
```

### Linux / Docker — Gunicorn

```bash
gunicorn -c py/gunicorn_conf.py py.app:app
```

> Gunicorn relies on `fork()`, so it runs natively on Linux/WSL2, not Windows.

---

## 🐳 Docker

Docker runs the app with **gunicorn** (a Linux production WSGI server — the native Windows path uses Waitress instead). This is the recommended way to run TrioForge on a **Linux server, WSL2, or a NAS**.

```bash
cd docker
docker compose build     # build the image
docker compose up -d     # start in detached mode
docker compose logs -f   # follow the logs
```

Then open **https://localhost:5001/** (or `http://localhost:5001/` if you skip certs — see below).

### How it works

1. **`Dockerfile`** — a slim `python:3.10-slim` image. It installs the system build tools, the Python deps from `requirements.txt`, copies the app, then starts gunicorn on port **5001**. The CMD runs `docker-entrypoint.sh` as the entrypoint *before* gunicorn.
2. **`docker-entrypoint.sh`** — a tiny script that generates a **self-signed TLS certificate** (`cert_store/localhost+1.pem` + `-key.pem`) if you haven't mounted one, then execs gunicorn. `gunicorn_conf.py` reads those certs and serves **HTTPS**. Your browser will warn the cert isn't trusted — mount your own `cert_store/` volume (e.g. mkcert-issued) if you want a trusted cert, or ignore it.
3. **`docker-compose.yml`** — binds port **`5001:5001`**, mounts your data directories as volumes, and points the app at your host's Ollama via `OLLAMA_BASE_URL=http://host.docker.internal:11434`. `extra_hosts: host.docker.internal:host-gateway` makes that hostname resolve on Linux Docker Engine too (Docker Desktop on Mac/Windows/WSL2 already maps it).

### Connecting Ollama

The app talks to Ollama on the host (not in a container). Two options:

- **Ollama on the host** (default) — leave `OLLAMA_BASE_URL` as `http://host.docker.internal:11434`. Works out of the box on Docker Desktop and on Linux with `host-gateway`.
- **Ollama in its own container** — uncomment the `ollama:` service in `docker-compose.yml`, then point the app at `OLLAMA_BASE_URL=http://ollama:11434`.

### Data persistence & git-ignore

The compose file mounts these as volumes so your data survives restarts:

| Host path | Container path | Purpose |
|-----------|----------------|---------|
| `../json_configuration` | `/app/json_configuration` | Conversations, notes, model config |
| `sqlite_volume` (named volume) | `/app/sqlite_data` | SQLite chat history (named volume avoids Windows FS I/O errors) |
| `../static/uploads` | `/app/static/uploads` | Uploaded files & generated media |
| `../cert_store` | `/app/cert_store` | TLS certificates |

**Important:** the image deliberately excludes your local models. `models/` (~11 GB of GGUF weights) is in `.dockerignore`, and your `.venv/` is too — the container installs its own deps and runs its own model files locally on the host. Cloud providers (OpenRouter / Gemini / Groq / etc.) need no local weights.

> ⚠️ The app uses `fork()`/POSIX signals, so gunicorn only runs on **Linux / WSL2 / macOS**, not native Windows. On Windows use `application.bat` (Waitress) instead.

---

## 🗂️ Where your data lives

| Path | What's stored |
|------|---------------|
| `json_configuration/` | Conversations, notes, model config, attachments |
| `sqlite_data/` | SQLite databases (chat history, notes, corkboard) |
| `cert_store/` | Auto-generated SSL certificates |
| `static/uploads/` | Uploaded images/files |

All of the above are **git-ignored** — every user keeps their own data private.

---

## 🧱 Project structure

```
TrioForge/
├── py/                          # ← all Python code
│   ├── app.py                   # Main Flask app + chat/conversation routes
│   ├── common.py                # Shared JSON / SQLite / embedding helpers
│   ├── paths.py                 # Project-root path helper
│   ├── comfyui_service.py       # ComfyUI image + video generation (live workflow discovery)
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
│   └── gunicorn_conf.py         # Gunicorn server config (Linux)
├── docker/                      # ← Docker files
│   ├── Dockerfile
│   ├── docker-compose.yml
│   └── docker-entrypoint.sh
├── pyproject.toml               # uv project (deps + optional groups)
├── uv.lock                      # uv lockfile (reproducible env)
├── application.bat              # Windows launcher (double-click)
├── run.sh                       # Linux / macOS / WSL launcher
├── voice_agent.bat              # Voice agent launcher (double-click)
├── templates/
│   └── index.html               # Frontend (HTML/CSS/JS)
├── static/                      # Static vendor assets (highlight, mermaid, …) + generated media
├── models/                      # Ollama Modelfile + instruction (GGUF weights git-ignored)
├── voiceguide_llama.cpp_guide/  # Voice agent config + logs (logs git-ignored)
├── json_configuration/          # User data (git-ignored)
├── sqlite_data/                 # SQLite databases (git-ignored)
├── cert_store/                  # Auto-generated SSL certificates (git-ignored)
├── requirements.txt / README.md / LICENSE / SECURITY.md / Disclaimer.md / CODE_REVIEW.md
└── .gitignore / .dockerignore
```

---

## 📄 License

Released under the [MIT License](LICENSE).
