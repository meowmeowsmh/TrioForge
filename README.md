🧠 Ollama Custom Chat
https://img.shields.io/badge/License-MIT-green.svg
https://img.shields.io/badge/python-3.8+-blue.svg
https://img.shields.io/github/last-commit/meowmeowsmh/ollama-chat-interface

A full‑featured, multi‑conversation chat interface for Ollama – 100% free, no API keys, no limits (when using Ollama).

✨ Features
Feature	Description
🔓 100% FREE	No API keys, no rate limits, no bills – if you stick to Ollama.
🧠 Any Model	Works with Qwen, Llama, Mistral, DeepSeek, and more.
🌐 Web Search	Optional DuckDuckGo search for up‑to‑date answers.
🎤 Voice Input	Speech‑to‑text directly in your browser.
📎 File/Image Upload	Attach images, PDFs, text files, code files.
💾 Live Monitor	Shows RAM & VRAM usage in real time.
🔒 HTTPS	Auto‑generates certificates on Windows – just run and go.
📂 Project Structure
text
.
├── app.py                    # Main Flask application
├── llm_providers.py          # All provider classes (Ollama, HF, Groq, etc.)
├── requirements.txt          # Python dependencies
├── cert_store/               # SSL certificates (auto‑created)
├── json_configuration/       # Chat history & model config (auto‑created)
└── README.md                 # You are here
Everything auto‑creates itself – just clone, run, and go.

🚀 Quick Start
1️⃣ Install Ollama
Download and install Ollama from ollama.com.
Then pull a model of your choice:

bash
ollama pull vaultbox/qwen3.5-uncensored:9b
2️⃣ Clone this repository
bash
git clone https://github.com/meowmeowsmh/ollama-chat-interface.git
cd ollama-chat-interface
3️⃣ Install Python dependencies
Create a virtual environment (recommended) and install the required packages:

bash
python -m venv venv
source venv/bin/activate      # On Windows: venv\Scripts\activate
pip install -r requirements.txt
4️⃣ Run the app
bash
python app.py
Your browser will open automatically at https://localhost:5000 (or http://localhost:5000 if SSL fails).

⚙️ Configuration
Providers
Ollama (default) – runs entirely locally – no internet needed, no API keys.

llama.cpp – point to your local llama-server instance.

Groq, DeepSeek, Claude (Anthropic), Hugging Face – require API keys (enter them in the UI).
These services are external – availability may vary depending on your network or region.

Memory Optimisation
The app automatically adjusts Ollama’s num_gpu and low_vram based on your available RAM and VRAM – you don’t need to touch any sliders.

Web Search
Enable the 🌐 Search checkbox to let the app fetch up‑to‑date snippets via DuckDuckGo (requires internet).

SSL / HTTPS
On Windows, the app will automatically download mkcert and generate a trusted certificate.
On other platforms, you can run with HTTP or install mkcert manually.

🧪 Usage
Start a new chat – click the + New button in the sidebar.

Attach files – click the paperclip 📎 icon or drag‑and‑drop files into the window.

Voice input – click the microphone 🎤 icon and speak.

AI voice output – click the speaker 🔊 button to have the bot read its responses aloud.

Edit/delete messages – hover over any user message to reveal ✏️ and 🗑️ buttons.

Search chats – use the search bar to filter by title or message content.

Reorder chats – drag conversations in the sidebar to rearrange them (order is saved).

🧩 Troubleshooting
❌ “Cannot connect to Ollama”
Make sure Ollama is running (check with ollama serve or look for the tray icon).

The app expects Ollama at http://127.0.0.1:11434 – if you changed the port, adjust the URL in app.py.

❌ Hugging Face or other providers don’t work / time out / get blocked
This can happen if:

Your network/firewall blocks the API endpoints (e.g., api-inference.huggingface.co).

Government restrictions – some regions block these services entirely, even when using a VPN.

Model is gated – you need to accept terms and provide a valid token.

💡 Solutions:
Stick with Ollama – it runs 100% locally and doesn’t require any internet access or API keys.

If you absolutely need Hugging Face:

Use a different VPN provider (some are blocked, others may work).

Try using the inference endpoint with your own token (entered in the UI) – but if the API itself is blocked, no token will help.

Consider hosting a local Hugging Face model using text-generation-webui or llama.cpp instead.

The app will never crash – if a provider fails, it shows a clear error in the status bar. You can safely switch back to Ollama.

❌ “SSL certificate not trusted”
On Windows, the app auto‑generates certificates – accept the security warning or install the generated certificate.

On Linux/macOS, run mkcert -install manually or use HTTP (the app falls back if certs are missing).

❌ “Voice input not working”
Make sure your browser has microphone permissions (look for the mic icon in the address bar).

The feature uses the Web Speech API – it works in Chrome, Edge, and Safari (not in Firefox).

📦 Requirements
All required packages are listed in requirements.txt.
Install them with:

bash
pip install -r requirements.txt
Minimal (if you only use Ollama and skip search/GPU monitoring):

text
flask>=2.3.0
requests>=2.31.0
psutil>=5.9.0
Full (all providers + search + VRAM):

text
duckduckgo-search>=6.0.0
flask>=2.3.0
groq>=0.3.0
huggingface_hub>=0.16.0
psutil>=5.9.0
pynvml>=11.525.0
requests>=2.31.0
urllib3>=1.26.0
🤝 Contributing
Pull requests are welcome! Feel free to open an issue if you find a bug or have a suggestion.

📄 License
This project is licensed under the MIT License – see the LICENSE file for details.

🙏 Acknowledgements
Ollama for making local LLMs accessible.

Flask for the lightweight web framework.

DuckDuckGo Search for the search feature.

All the open‑source libraries used in this project.

Enjoy chatting with your local AI – no strings attached! 🚀