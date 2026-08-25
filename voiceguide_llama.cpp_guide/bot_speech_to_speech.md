# Local AI Voice Agent Setup Guide

This guide describes how to deploy a fully local, low-latency voice assistant pipeline utilizing Hugging Face's [speech-to-speech](https://github.com/huggingface/speech-to-speech) framework, `llama.cpp` for large language modeling, and `qwentts.cpp` for text-to-speech functionality [0.1.1, 0.1.2].

---

## 🛠️ Step 1: Install Dependencies

You can install the voice agent ecosystem either directly using `pip` or from the official source repositories [0.1.1].

### Option A: Standard PyPI Installation
```powershell
pip install speech-to-speech
```

### Option B: Installation from Source
```powershell
# Clone the speech-to-speech framework
git clone https://github.com/huggingface/speech-to-speech.git

# Clone the Qwen TTS GGML framework with submodules
git clone --recurse-submodules https://github.com/ServeurpersoCom/qwentts.cpp.git
```

---

## 🏗️ Step 2: Compile the TTS Engine

Navigate to your local `qwentts.cpp` directory to build the binary using your CPU backend inside PowerShell [0.1.2]:

```powershell
cd C:\Users\user\qwentts.cpp
./buildcpu.sh
```

---

## 🚀 Step 3: Run the Local Services

You need two active PowerShell tabs/windows to operate the local voice system. Always launch the LLM server **before** initiating the speech-to-speech client handler [0.1.1].

### Tab 1: Start the `llama.cpp` Server
Execute the following to instantiate the local LLM server hosting your uncensored model profile:
```powershell
& "C:\Users\user\AppData\Local\Microsoft\WinGet\Packages\ggml.llamacpp_Microsoft.Winget.Source_8wekyb3d8bbwe\llama-server.exe" `
  -m "D:\TrioForge\models\Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf" `
  --port 8080
```

### Tab 2: Start the Speech-to-Speech Local Agent
With the server running on port `8080`, spin up the client engine utilizing your local hardware resources:
```powershell
speech-to-speech --mode local --no_smart_turn `
  --llm_backend chat-completions `
  --model_name "Qwen3.5-9B" `
  --responses_api_base_url "http://127.0.0.1:8080/v1" `
  --responses_api_api_key="" `
  --stt_device cpu `
  --parakeet_tdt_device cpu `
  --tts facebookMMS `
  --facebook_mms_device cpu
```

---

## ⚙️ Configuration Matrix

| Component | Backend / Selection | Target Device |
| :--- | :--- | :--- |
| **STT** | Parakeet TDT | CPU |
| **LLM Engine** | `llama.cpp` (via Chat Completions) | Local Host (`:8080`) |
| **TTS Engine** | Facebook MMS | CPU |
| **Turn Detection** | Smart Turn Disabled | N/A |