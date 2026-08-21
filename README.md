# ⚙️ TrioForge

> A private, offline-first AI workspace — chat with multiple LLMs, manage notes, and organize ideas on a corkboard, all from one self-hosted app.
> **100% free when using [Ollama](https://ollama.com)** — no API keys, no rate limits, no cloud.

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/)
[![GitHub last commit](https://img.shields.io/github/last-commit/meowmeowsmh/TrioForge)](https://github.com/meowmeowsmh/TrioForge)

---

## Table of Contents

1. [Features](#-features)
2. [Screenshots](#-screenshots)
3. [Prerequisites](#-prerequisites)
4. [Quick Start](#-quick-start)
5. [Configuration](#-configuration)
6. [Running with HTTPS](#-running-with-https)
7. [Docker](#-docker)
8. [Where your data lives](#-where-your-data-lives)
9. [Project structure](#-project-structure)
10. [License](#-license)

---

## ✨ Features

- 🔓 **100% free** — no API keys or limits when using local Ollama models.
- 🧠 **Any model** — Qwen, Llama, Mistral, DeepSeek, and more.
- 🧰 **Multi-provider** — Ollama, Groq, Hugging Face, DeepSeek, Claude, and llama.cpp.
- 🌐 **Web search** — optional DuckDuckGo integration for up-to-date answers.
- 📎 **File & image upload** — attach images, PDFs, code files, and text documents.
- 🎤 **Voice input** — speech-to-text directly in your browser.
- 🖱️ **Drag & drop** — drop files or folders straight onto the chat window.
- 📑 **Persistent chats** — conversations auto-save and survive restarts.
- 🗄️ **SQLite audit log** — every message is logged; recover deleted chats.
- 📝 **Notes & corkboard** — built-in tools for organizing facts, notes, and ideas.
- 💾 **Live monitor** — real-time RAM & VRAM usage tracking.
- 🔒 **HTTPS** — auto-generates SSL certificates on Windows.

---

## 📸 Screenshots

| Chat | Notes | Corkboard |
|------|-------|-----------|
| ![Chat interface](image-3.png) | ![Notes](image.png) | ![Cork Board](image-2.png) |

---

## 📦 Prerequisites

| Tool | Why | Link |
|------|-----|------|
| **Python 3.8+** | Runs the app | [Download](https://www.python.org/downloads/) |
| **Ollama** | Local models | [Download](https://ollama.com) |
| **Git** | Cloning *(optional)* | [Download](https://git-scm.com/) |

---

## 🚀 Quick Start

```bash
# 1. Clone the repository
git clone https://github.com/meowmeowsmh/TrioForge.git
cd TrioForge

# 2. Install dependencies
pip install -r requirements.txt

# 3. Pull a local model (or use any model you prefer)
ollama pull vaultbox/qwen3.5-uncensored:9b

# 4. Run the app
python app.py
```

Then open **https://localhost:5001/** in your browser.

> **Tip:** if the app starts in plain HTTP (no certificates), use **http://localhost:5001/** instead.

For a production-style server, see [Running with HTTPS](#-running-with-https) below.

---

## ⚙️ Configuration

Configuration is done through environment variables. All are optional — the app works out of the box with Ollama.

| Variable | Purpose | Default |
|----------|---------|---------|
| `OLLAMA_BASE_URL` | Ollama server URL | `http://127.0.0.1:11434` |
| `GROQ_API_KEY` | Groq provider key | *(unset)* |
| `DEEPSEEK_API_KEY` | DeepSeek provider key | *(unset)* |
| `ANTHROPIC_API_KEY` | Claude provider key | *(unset)* |
| `OLLAMA_REGISTRY_TOKEN` | Token for `ollama push` | *(unset)* |

API keys can also be entered directly in the web UI. Keys are never written to disk.

---

## 🔒 Running with HTTPS

### Windows / macOS — Waitress

`python app.py` auto-generates SSL certificates (via **mkcert**) and serves HTTPS on port 5001. You can also run the dedicated Waitress entry point:

```bash
python https_guni_n_waitress.py
```

### Linux / Docker — Gunicorn

```bash
gunicorn -c gunicorn_conf.py app:app
```

> Gunicorn relies on `fork()`, so it runs natively on Linux/WSL2, not Windows.

---

## 🐳 Docker

```bash
# Build the image
docker compose build

# Start the container in detached mode
docker compose up -d

# Follow the logs to confirm it's running
docker compose logs -f
```

Then open **https://localhost:5001/**.

---

## 🗂️ Where your data lives

| Path | What's stored |
|------|---------------|
| `json_configuration/` | Conversations, notes, model config, attachments |
| `sqlite_data/` | SQLite databases (chat history, notes, corkboard) |
| `cert_store/` | Auto-generated SSL certificates |
| `static/uploads/` | Uploaded images/files |

All of the above are **git-ignored** — every user keeps their own copy.

---

## 🧱 Project structure

```
TrioForge/
├── app.py                    # Main Flask app + chat/conversation routes
├── llm_providers.py          # LLM provider abstraction (Ollama, Groq, DeepSeek, …)
├── notes.py                  # Notes blueprint (Obsidian-style knowledge base)
├── cork_board.py             # Corkboard blueprint (pins, links, AI assist)
├── common.py                 # Shared JSON / SQLite / embedding helpers
├── zoompicleftandright.py    # Image viewer blueprint
├── templates/
│   └── index.html            # Frontend (HTML/CSS/JS)
├── static/                   # Static vendor assets (highlight, mermaid, …)
├── json_configuration/       # User data (git-ignored)
├── sqlite_data/              # SQLite databases (git-ignored)
├── https_guni_n_waitress.py  # Waitress + HTTPS entry point
├── gunicorn_conf.py          # Gunicorn configuration (Linux)
├── Dockerfile / docker-compose.yml
└── requirements.txt
```

---

## 📄 License

Released under the [MIT License](LICENSE).
