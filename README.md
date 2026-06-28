# 🧠 Ollama Custom Chat

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/)
[![GitHub last commit](https://img.shields.io/github/last-commit/meowmeowsmh/ollama-chat-interface)](https://github.com/meowmeowsmh/ollama-chat-interface)

> A full-featured, multi-conversation chat interface for [Ollama](https://ollama.com) – 100% free, no API keys, no limits.

## ✨ Features

| Feature | Description |
|---------|-------------|
| 🔓 **100% FREE** | No API keys, no rate limits, no bills. |
| 🧠 **Any Model** | Works with Qwen, Llama, Mistral, DeepSeek, and more. |
| 🌐 **Web Search** | Optional DuckDuckGo search for up‑to‑date answers. |
| 🎤 **Voice Input** | Speech‑to‑text directly in your browser. |
| 📎 **File/Image Upload** | Attach images, PDFs, text files, code files. |
| 💾 **Live Monitor** | Shows RAM & VRAM usage in real time. |
| 🔒 **HTTPS** | Auto‑generates certificates on Windows – just run and go. |

---

## 📂 Project Structure
.
├── app.py # Main application
├── requirements.txt # Python dependencies
├── cert_store/ # SSL certs (auto‑created if needed)
└── json_configuration/ # Chat history & settings (auto‑created)


> **Everything auto‑creates itself** – just clone, run, and go.

---

## 🚀 Quick Start

### 1️⃣ Install Ollama
```bash
# Download from: https://ollama.com
# Then pull a model:
ollama pull vaultbox/qwen3.5-uncensored:9b

### something ain't working 
if user tried to run the model from huggingfaces it might not worked because vpn does not matter due to goverment restricting the hugging interface despite you log in due to the hacker news penetration into huggingfaces