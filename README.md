# 🧠 Ollama Custom Chat

A full-featured, multi-conversation chat interface for Ollama with:
- 🔓 **100% FREE** – No API keys, no limits, bills you have to go ollama to check here is the link https://ollama.com/settings
- 🧠 **Any model** – Qwen, Llama, Mistral, DeepSeek, etc that exsisted from huggingface and ollama web page thus good luck.
- 🌐 **Web search toggle** – Optional DuckDuckGo search
- 🎤 **Voice input** – Speech-to-text in the browser
- 📎 **File/Image upload** – Attach images, PDFs, text files
- 💾 **Live resource monitor** – Shows RAM & VRAM usage ensure your crashing rate to be low.
- 🔒 **HTTPS** – bat self-setup

## 🚀 Quick Start

### 1. Install Ollama
Download from [ollama.com](https://ollama.com)

Pull a model:

```bash
ollama pull vaultbox/qwen3.5-uncensored:9b

.
├── app.py                  # Main application
├── requirements.txt        # Python dependencies
├── setup_cert.bat          # One‑click HTTPS setup for Windows
├── cert_store/             # SSL certificates (auto‑created)
└── json_configuration/     # Conversations and model config (auto‑created)

# 🔄 How to Update
git pull # to update to check any changes

#The next thing you need to do is :
git clone https://github.com/meowmeowsmh/ollama-chat-interface.git # you first intiating the pulling please support through star 
pip install -r requirements.txt  # if dependencies changed
python app.py # run the code 
