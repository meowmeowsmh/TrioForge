# 🧠 Ollama Custom Chat

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/)
[![GitHub last commit](https://img.shields.io/github/last-commit/meowmeowsmh/ollama-chat-interface)](https://github.com/meowmeowsmh/ollama-chat-interface)


---


> **Everything auto‑creates itself** – just clone, run, and go.

---

## 🚀 Quick Start

### 1️⃣ Install Ollama

Download and install Ollama from [ollama.com](https://ollama.com).  
Then pull a model of your choice:

```bash
ollama pull vaultbox/qwen3.5-uncensored:9b

## ✨ Features

| Feature | Description |
|---------|-------------|
| 🔓 **100% FREE** | No API keys, no rate limits, no bills – if you stick to **Ollama**. |
| 🧠 **Any Model** | Works with Qwen, Llama, Mistral, DeepSeek, and more. |
| 🌐 **Web Search** | Optional DuckDuckGo search for up‑to‑date answers. |
| 🎤 **Voice Input** | Speech‑to‑text directly in your browser. |
| 📎 **File/Image Upload** | Attach images, PDFs, text files, code files. |
| 💾 **Live Monitor** | Shows RAM & VRAM usage in real time. |
| 🔒 **HTTPS** | Auto‑generates certificates on Windows – just run and go. |

---

## 📂 Project Structure
.
├── app.py # Main Flask application
├── llm_providers.py # All provider classes (Ollama, HF, Groq, etc.)
├── requirements.txt # Python dependencies
├── cert_store/ # SSL certificates (auto‑created)
├── json_configuration/ # Chat history & model config (auto‑created)
└── README.md # You are here
