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
| 🔒 **HTTPS** | One‑click setup with `setup_cert.bat` for Windows. |

---

## 📂 Project Structure

```
.
├── app.py                  # Main application
├── requirements.txt        # Python dependencies
├── setup_cert.bat          # One‑click HTTPS for Windows
├── cert_store/             # SSL certs (auto‑created)
└── json_configuration/     # Chat history & settings (auto‑created)
```

> **Everything auto‑creates itself** – just clone, run, and go.

---

## 🚀 Quick Start

### 1️⃣ Install Ollama
```bash
# Download from: https://ollama.com
# Then pull a model:
ollama pull vaultbox/qwen3.5-uncensored:9b
```

### 2️⃣ Clone & Install
```bash
git clone https://github.com/meowmeowsmh/ollama-chat-interface.git
cd ollama-chat-interface
pip install -r requirements.txt
```

### 3️⃣ Set up HTTPS (Windows)
Right‑click **`setup_cert.bat`** → **Run as administrator**.

*(For Mac/Linux, see [HTTPS Setup](#-https-setup) below.)*

### 4️⃣ Run it
```bash
python app.py
```

Open **`https://localhost:5000`** in your browser.

---

## 🔒 HTTPS Setup

### Windows (One‑Click)
1. Right‑click `setup_cert.bat` → **Run as administrator**.
2. Done. Certificates are auto‑generated.

### Manual (Mac/Linux)
```bash
# Install mkcert
brew install mkcert                      # macOS
# See https://github.com/FiloSottile/mkcert for Linux

# Generate certs
mkdir cert_store
mkcert -install
mkcert localhost 127.0.0.1
mv localhost+1.pem cert_store/
mv localhost+1-key.pem cert_store/
```

---

## 🔄 Updating

```bash
git pull
pip install -r requirements.txt   # if dependencies changed
python app.py
```

---

## 🛠️ Requirements

- Python 3.8+
- Ollama (running locally)
- NVIDIA GPU (optional – for VRAM monitoring)

---

## 📄 License

MIT License – see [LICENSE](LICENSE) for details.

---

## ⭐ Support

If you find this useful, please **⭐ Star** the repo!

---

Built with ❤️ using [Ollama](https://ollama.com) & [Flask](https://flask.palletsprojects.com)
