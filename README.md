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
- 🤖 **Coding agent** — pick a workspace folder; the AI can **list**, **search**, **read**, **write**, **edit** (surgical string replacement), **run shell commands**, and **search the web**, looping up to 20 tool steps per task — with a **live diff panel** showing exactly what it edits.
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
- 📝 **Notes & corkboard** — built-in tools for organizing facts and ideas.
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

### 🚀 First-run setup checker

On first launch, TrioForge shows a **Setup panel** that detects which local services/files are present vs. missing, each with a download link — so you're never left guessing why a provider says "connection refused".

| Item | Required? | Auto? |
|------|-----------|-------|
| **Ollama** | ✅ | Manual install — [ollama.com/download](https://ollama.com/download) |
| **llama.cpp** (`llama-server`) | ✅ | Installed via release or `winget install ggml.llamacpp`; the app **auto-starts** it |
| **GGUF models** | ✅ | Via the ⬇ button or Hugging Face; app auto-loads from `models/` |
| **ComfyUI** (image/video) | ❌ optional | Manual — [comfy.org/download](https://www.comfy.org/download); cloud image/video works without it |
| **Voice-to-voice** | ❌ optional | Manual — speech-to-speech + `voiceguide_llama.cpp_guide/config.json` |

You can reopen the panel anytime with the **🚀** button in the top bar.

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
- **With `sentence-transformers` installed** (`pip install sentence-transformers scikit-learn numpy`) — semantic search using `all-MiniLM-L6-v2` embeddings, so it matches meaning, not just exact words.

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

When the coding agent modifies files (with a workspace folder set to `readwrite`), every `write_file` / `edit_file` is captured as a **unified diff** and shown in a real-time right-side panel.

- Click **📝** in the top bar to open it.
- Watch the model's edits stream in as it works — each entry shows the file, the tool used, and a colored `-` / `+` diff.
- **Clear** resets the panel.

This turns the coding agent into a *visible* editor: instead of a silent final answer, you see exactly what the model is changing, line by line.

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

## 🌐 Remote access (phone / LAN / tunnel)

TrioForge binds to **`0.0.0.0`** (all interfaces), so it's reachable from any device on your network out of the box.

- **LAN (same Wi-Fi):** open `https://<your-computer-IP>:5001` on your phone (the app logs the exact URL at startup, e.g. `https://192.168.1.113:5001`).
  - You may need to allow the port through Windows Firewall (`5001`).
  - Browsers warn about the self-signed cert on other devices — that's expected; proceed to `Advanced → Continue`.
- **Internet (anywhere):** run a tunnel from another terminal, then open the tunnel URL:
  - **cloudflared** (free): `cloudflared tunnel --url https://localhost:5001`
  - **ngrok**: `ngrok http 5001`

> ⚠️ Exposing the app to the internet lets *anyone* with the URL use it. TrioForge has no built-in auth — put a reverse proxy with a password (or a tunnel with auth) in front of it before exposing it publicly.

---

## 🔓 Uncensored by default

The default model is **`vaultbox/qwen3.5-uncensored:9b`** — an abliterated (refusal-removed) model, so TrioForge answers freely out of the box with local Ollama. You can swap to any model you like, including the curated abliterated/uncensored vision list built into the app (`UNCENSORED_VISION_MODELS`).

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
