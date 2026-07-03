"""
LLM Provider abstraction – all providers are optional and graceful.
Supports image (vision) input for providers and models that allow it.
"""

import os
import glob
import requests
import json
from typing import List, Dict, Any, Optional

# ── Vision model registry ──────────────────────────────────────────────────────
VISION_MODELS = {
    "groq": {
        "meta-llama/llama-4-scout-17b-16e-instruct",
        "meta-llama/llama-4-maverick-17b-128e-instruct",
        "meta-llama/llama-4-scout",
        "meta-llama/llama-4-maverick",
    },
    "huggingface": {
        "llava-hf/llava-1.5-7b-hf",
        "llava-hf/llava-1.5-13b-hf",
        "llava-hf/llava-v1.6-mistral-7b-hf",
        "llava-hf/llava-v1.6-34b-hf",
        "google/gemma-3-4b-it",
        "google/gemma-3-12b-it",
        "google/gemma-3-27b-it",
        "google/paligemma-3b-mix-448",
        "microsoft/Phi-3-vision-128k-instruct",
        "microsoft/phi-4-multimodal-instruct",
        "Qwen/Qwen2-VL-7B-Instruct",
        "Qwen/Qwen2-VL-72B-Instruct",
    },
    "llamacpp": {
        "llava", "bakllava", "cogvlm", "moondream",
        "minicpm-v", "phi3-vision", "phi-3-vision",
        "gemma-3", "llama-4", "qwen2-vl",
    },
    "ollama": {
        "llava", "bakllava", "cogvlm", "moondream",
        "minicpm-v", "minicpm", "phi3-vision", "llama4",
        "gemma3", "gemma4",
        "qwen2-vl", "qwen2.5vl", "llava-llama3",
        "qwen3.5-uncensored",
        "openscan",
    },
    "claude": {
        "claude-3-5-sonnet-20241022",
        "claude-3-opus-20240229",
        "claude-3-sonnet-20240229",
        "claude-3-haiku-20240307",
        "claude-3-5-haiku-20241022",
    },
    "deepseek": set(),  # no vision support
}


def model_supports_vision(provider_name: str, model_name: str) -> bool:
    if not model_name:
        return False
    known = VISION_MODELS.get(provider_name, set())
    if provider_name in ("groq", "huggingface", "claude"):
        return model_name.lower() in {m.lower() for m in known}
    model_lower = model_name.lower()
    return any(keyword in model_lower for keyword in known)


class LLMProvider:
    def generate(self, messages: List[Dict[str, str]], **kwargs) -> str:
        raise NotImplementedError

    def generate_with_image(self, messages: List[Dict[str, str]],
                            images: List[Dict], **kwargs) -> str:
        note = f"[{len(images)} image(s) attached – this model does not support native vision]"
        messages = list(messages)
        if messages:
            messages[-1] = {**messages[-1], "content": note + "\n" + messages[-1].get("content", "")}
        return self.generate(messages, **kwargs)

    def list_models(self, api_key: Optional[str] = None) -> List[str]:
        return []


class OllamaProvider(LLMProvider):
    """Ollama provider using /api/chat for all requests (preserves conversation history)."""
    def __init__(self, model: str = "vaultbox/qwen3.5-uncensored:9b",
                 base_url: str = "http://127.0.0.1:11434"):
        self.model = model
        self.base_url = base_url
        self.chat_url = f"{base_url}/api/chat"

    def _prepare_messages(self, messages: List[Dict], images: Optional[List[Dict]] = None) -> List[Dict]:
        """Convert provider messages to Ollama /api/chat format, embedding images if present.

        NOTE: Ollama's native /api/chat does NOT use the OpenAI-style
        content=[{"type": "image_url", ...}] format. It expects `content`
        to remain a plain string, with raw base64 images passed in a
        separate top-level `images` list on the message.
        """
        if images:
            msgs = [m.copy() for m in messages]
            last_user_idx = None
            for i in range(len(msgs) - 1, -1, -1):
                if msgs[i].get("role") == "user":
                    last_user_idx = i
                    break
            if last_user_idx is not None:
                user_msg = msgs[last_user_idx]
                b64_list = []
                for img in images:
                    b64 = img["b64"]
                    if "," in b64:
                        b64 = b64.split(",", 1)[1]
                    b64_list.append(b64)
                msgs[last_user_idx] = {
                    "role": "user",
                    "content": user_msg.get("content", ""),
                    "images": b64_list
                }
            return msgs
        else:
            return messages

    def generate(self, messages: List[Dict[str, str]],
                 images: Optional[List[Dict]] = None, **kwargs) -> str:
        model = kwargs.get("model") or self.model
        num_gpu = kwargs.get("num_gpu", 99)
        low_vram = kwargs.get("low_vram", False)

        chat_messages = self._prepare_messages(messages, images)

        payload = {
            "model": model,
            "messages": chat_messages,
            "stream": False,
            "keep_alive": 300,
            "options": {
                "temperature": 0.7,
                "num_predict": 2048,
                "num_ctx": 4096,
                "num_gpu": num_gpu,
                "low_vram": low_vram,
            }
        }

        resp = requests.post(self.chat_url, json=payload, timeout=180)
        resp.raise_for_status()
        data = resp.json()
        return data.get("message", {}).get("content", "")

    def generate_with_image(self, messages: List[Dict[str, str]],
                            images: List[Dict], **kwargs) -> str:
        return self.generate(messages, images=images, **kwargs)

    def list_models(self, api_key: Optional[str] = None) -> List[str]:
        try:
            resp = requests.get(f"{self.base_url}/api/tags", timeout=5)
            resp.raise_for_status()
            return [m["name"] for m in resp.json().get("models", [])]
        except:
            return []


# ================== IMPROVED LLAMA.CPP PROVIDER ==================
class LlamaCppProvider(LLMProvider):
    def __init__(self, models_dir: str = "./models",
                 server_url: str = "http://127.0.0.1:8080/v1"):
        self.models_dir = os.path.abspath(models_dir)
        self.server_url = server_url.rstrip("/")
        self._ensure_models_dir()
        self.available_models = self._discover_models()

    def _ensure_models_dir(self):
        if not os.path.exists(self.models_dir):
            os.makedirs(self.models_dir, exist_ok=True)

    def _discover_models(self) -> List[str]:
        # Local .gguf files
        gguf_files = glob.glob(os.path.join(self.models_dir, "*.gguf"))
        local_models = [os.path.basename(f) for f in gguf_files]

        # Try to fetch models from server (if it supports /models endpoint)
        server_models = []
        try:
            resp = requests.get(f"{self.server_url}/models", timeout=3)
            if resp.status_code == 200:
                data = resp.json()
                if "data" in data:
                    server_models = [m["id"] for m in data["data"]]
        except Exception:
            pass

        all_models = list(set(local_models + server_models))
        return all_models

    def list_models(self, api_key: Optional[str] = None) -> List[str]:
        return self.available_models

    def _resolve_model_path(self, model: Optional[str]) -> str:
        if not model:
            if self.available_models:
                model = self.available_models[0]
            else:
                raise Exception("No models found in ./models folder and no model specified.")
        # If it's just a filename, try to make it an absolute path
        if os.path.sep not in model and not model.startswith("/") and not model.startswith("\\"):
            candidate = os.path.join(self.models_dir, model)
            if os.path.exists(candidate):
                return candidate
            # Otherwise assume server already knows it by name
            return model
        return model

    def _check_server(self):
        """Raise an exception if the llama.cpp server is not reachable."""
        try:
            requests.get(self.server_url, timeout=2)
        except Exception:
            raise ConnectionError(
                "llama.cpp server is not running or not reachable. "
                "Please start it with: ./server -m <model.gguf> --host 127.0.0.1 --port 8080"
            )

    def generate(self, messages: List[Dict[str, str]],
                 model: Optional[str] = None, **kwargs) -> str:
        self._check_server()
        model_path = self._resolve_model_path(model)

        payload = {
            "model": model_path,
            "messages": messages,
            "stream": False,
            "temperature": kwargs.get("temperature", 0.7),
            "max_tokens": kwargs.get("max_tokens", 2048),
        }
        try:
            resp = requests.post(
                f"{self.server_url}/chat/completions",
                json=payload,
                timeout=180
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except requests.exceptions.Timeout:
            raise Exception("llama.cpp server timed out. Try reducing context size or use a smaller model.")
        except requests.exceptions.ConnectionError:
            raise Exception("Cannot connect to llama.cpp server. Is it running?")
        except Exception as e:
            raise Exception(f"llama.cpp error: {e}")

    def generate_with_image(self, messages: List[Dict[str, str]],
                            images: List[Dict], **kwargs) -> str:
        self._check_server()
        model_path = self._resolve_model_path(kwargs.get("model"))

        content_parts = []
        for img in images:
            b64 = img["b64"]
            if "," in b64:
                b64 = b64.split(",", 1)[1]
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"}
            })
        last_text = messages[-1].get("content", "") if messages else ""
        content_parts.append({"type": "text", "text": last_text})

        vision_messages = []
        for m in messages[:-1]:
            vision_messages.append({"role": m["role"], "content": m["content"]})
        vision_messages.append({"role": "user", "content": content_parts})

        payload = {
            "model": model_path,
            "messages": vision_messages,
            "stream": False,
            "temperature": kwargs.get("temperature", 0.7),
            "max_tokens": kwargs.get("max_tokens", 2048),
        }
        try:
            resp = requests.post(
                f"{self.server_url}/chat/completions",
                json=payload,
                timeout=180
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            raise Exception(f"llama.cpp vision error: {e}")


# ================== OTHER PROVIDERS ==================

class HuggingFaceProvider(LLMProvider):
    def __init__(self, model: str = "microsoft/DialoGPT-medium",
                 api_token: Optional[str] = None):
        self.model = model
        self.api_token = api_token or os.environ.get("HF_API_TOKEN")
        self._available = True
        try:
            import huggingface_hub
        except ImportError:
            self._available = False
            print("⚠️ huggingface_hub not installed. Run: pip install huggingface_hub")

    def list_models(self, api_key: Optional[str] = None) -> List[str]:
        text_models = [
            "microsoft/DialoGPT-medium",
            "google/flan-t5-base",
            "google/flan-t5-large",
            "microsoft/Phi-3-mini-4k-instruct",
            "HuggingFaceH4/zephyr-7b-beta",
        ]
        vision_models = [
            "llava-hf/llava-1.5-7b-hf",
            "llava-hf/llava-v1.6-mistral-7b-hf",
            "google/gemma-3-4b-it",
            "google/gemma-3-12b-it",
            "google/gemma-3-27b-it",
            "google/paligemma-3b-mix-448",
            "microsoft/Phi-3-vision-128k-instruct",
            "Qwen/Qwen2-VL-7B-Instruct",
        ]
        return text_models + vision_models

    def _make_headers(self, api_key: Optional[str] = None) -> Dict:
        headers = {"Content-Type": "application/json"}
        token = api_key or self.api_token
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def generate(self, messages: List[Dict[str, str]], **kwargs) -> str:
        if not self._available:
            raise Exception("Hugging Face provider not available – missing huggingface_hub.")
        model = kwargs.get("model") or self.model
        prompt = messages[-1]["content"] if messages else ""
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        url = f"https://api-inference.huggingface.co/models/{model}"
        payload = {
            "inputs": prompt,
            "parameters": {
                "max_new_tokens": 512,
                "temperature": 0.7,
                "do_sample": True,
                "return_full_text": False
            }
        }
        headers = self._make_headers(kwargs.get("api_key"))
        try:
            resp = requests.post(url, headers=headers, json=payload,
                                 timeout=60, verify=False)
            resp.raise_for_status()
            result = resp.json()
            if isinstance(result, list) and result:
                return result[0].get("generated_text", str(result[0]))
            elif isinstance(result, dict):
                return result.get("generated_text", str(result))
            return str(result)
        except requests.exceptions.HTTPError as e:
            if resp.status_code == 401:
                raise Exception("Invalid Hugging Face token. Please check your token.")
            elif resp.status_code == 503:
                raise Exception("Hugging Face API is overloaded. Please wait and retry.")
            raise Exception(f"Hugging Face API error: {e}")
        except Exception as e:
            raise Exception(f"Failed to generate response: {e}")

    def generate_with_image(self, messages: List[Dict[str, str]],
                            images: List[Dict], **kwargs) -> str:
        if not self._available:
            raise Exception("Hugging Face provider not available.")
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        model = kwargs.get("model") or self.model
        prompt = messages[-1]["content"] if messages else ""
        headers = self._make_headers(kwargs.get("api_key"))
        content_parts = []
        for img in images:
            b64 = img["b64"]
            if "," in b64:
                b64 = b64.split(",", 1)[1]
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"}
            })
        content_parts.append({"type": "text", "text": prompt})
        payload = {
            "inputs": {
                "messages": [{"role": "user", "content": content_parts}]
            },
            "parameters": {
                "max_new_tokens": 512,
                "temperature": 0.7,
            }
        }
        url = f"https://api-inference.huggingface.co/models/{model}/v1/chat/completions"
        try:
            resp = requests.post(url, headers=headers, json=payload,
                                 timeout=60, verify=False)
            resp.raise_for_status()
            result = resp.json()
            if "choices" in result:
                return result["choices"][0]["message"]["content"]
            if isinstance(result, list) and result:
                return result[0].get("generated_text", str(result[0]))
            return str(result)
        except requests.exceptions.HTTPError as e:
            if resp.status_code in (401, 403):
                raise Exception("Invalid or missing Hugging Face token for this model.")
            elif resp.status_code == 503:
                raise Exception("Hugging Face model is loading. Wait a moment and retry.")
            raise Exception(f"HuggingFace vision API error: {e}")
        except Exception as e:
            raise Exception(f"HuggingFace vision request failed: {e}")


class GroqProvider(LLMProvider):
    def __init__(self, api_key: Optional[str] = None):
        self._default_key = api_key or os.environ.get("GROQ_API_KEY")
        self._available = bool(self._default_key)
        if not self._available:
            print("⚠️ GROQ_API_KEY not set. Provide it via UI or set env var.")

    def _get_key(self, kwargs) -> str:
        key = kwargs.get("api_key") or self._default_key
        if not key:
            raise Exception("Groq API key is required. Enter it in the API Key field.")
        return key

    def _get_client(self, api_key: Optional[str] = None):
        key = api_key or self._default_key
        if not key:
            raise Exception("Groq API key is required.")
        try:
            from groq import Groq
            return Groq(api_key=key)
        except ImportError:
            raise Exception("groq library not installed. Run: pip install groq")

    def list_models(self, api_key: Optional[str] = None) -> List[str]:
        key = api_key or self._default_key
        FALLBACK_MODELS = [
            "llama-3.1-8b-instant",
            "llama-3.3-70b-versatile",
            "meta-llama/llama-4-scout-17b-16e-instruct",
            "meta-llama/llama-4-maverick-17b-128e-instruct",
            "openai/gpt-oss-120b",
            "openai/gpt-oss-20b",
            "groq/compound",
            "groq/compound-mini",
            "qwen/qwen3-32b",
            "qwen/qwen3.6-27b",
        ]
        if not key:
            return FALLBACK_MODELS
        try:
            headers = {"Authorization": f"Bearer {key}"}
            resp = requests.get("https://api.groq.com/openai/v1/models",
                                headers=headers, timeout=10)
            resp.raise_for_status()
            models = [m["id"] for m in resp.json().get("data", [])]
            return models if models else FALLBACK_MODELS
        except Exception as e:
            print(f"⚠️ Failed to fetch Groq models: {e}")
            return FALLBACK_MODELS

    def generate(self, messages: List[Dict[str, str]],
                 model: str = "llama-3.3-70b-versatile", **kwargs) -> str:
        client = self._get_client(kwargs.get("api_key"))
        chat = client.chat.completions.create(messages=messages, model=model)
        return chat.choices[0].message.content

    def generate_with_image(self, messages: List[Dict[str, str]],
                            images: List[Dict], **kwargs) -> str:
        client = self._get_client(kwargs.get("api_key"))
        model = kwargs.get("model", "meta-llama/llama-4-scout-17b-16e-instruct")
        content_parts = []
        for img in images:
            b64 = img["b64"]
            if "," in b64:
                b64 = b64.split(",", 1)[1]
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"}
            })
        last_text = messages[-1].get("content", "") if messages else ""
        content_parts.append({"type": "text", "text": last_text})
        vision_messages = [
            {"role": m["role"], "content": m["content"]}
            for m in messages[:-1]
        ] + [{"role": "user", "content": content_parts}]
        chat = client.chat.completions.create(
            messages=vision_messages,
            model=model
        )
        return chat.choices[0].message.content


class DeepSeekProvider(LLMProvider):
    def __init__(self, api_key: Optional[str] = None):
        self._default_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        self._available = bool(self._default_key)
        if not self._available:
            print("⚠️ DEEPSEEK_API_KEY not set. Provide it via UI or set env var.")

    def _get_key(self, kwargs) -> str:
        key = kwargs.get("api_key") or self._default_key
        if not key:
            raise Exception("DeepSeek API key is required. Enter it in the API Key field.")
        return key

    def _get_headers(self, api_key: str) -> Dict:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

    def list_models(self, api_key: Optional[str] = None) -> List[str]:
        key = api_key or self._default_key
        if not key:
            return [
                "deepseek-v4-flash",
                "deepseek-v4-pro",
            ]
        try:
            headers = self._get_headers(key)
            resp = requests.get("https://api.deepseek.com/v1/models", headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            models = [m["id"] for m in data.get("data", [])]
            return models if models else [
                "deepseek-v4-flash",
                "deepseek-v4-pro",
            ]
        except Exception as e:
            print(f"⚠️ Failed to fetch DeepSeek models: {e}")
            return [
                "deepseek-v4-flash",
                "deepseek-v4-pro",
            ]

    def get_model_info(self, model_id: str) -> dict:
        """Return description, capabilities, and pricing for a given DeepSeek model."""
        info = {
            "deepseek-chat": {
                "description": "General‑purpose chat (R1 / V3) – best for reasoning, conversation, and complex tasks.",
                "capabilities": ["Chat", "Reasoning", "Multilingual"],
                "pricing": {"input": "$0.14/M", "output": "$0.28/M"}
            },
            "deepseek-coder": {
                "description": "Optimised for coding, debugging, code generation, and explanation.",
                "capabilities": ["Code generation", "Debugging", "Code explanation"],
                "pricing": {"input": "$0.14/M", "output": "$0.28/M"}
            },
            "deepseek-vl": {
                "description": "Vision‑language model – understands images and text, answers questions about visuals.",
                "capabilities": ["Image analysis", "Multimodal", "Visual QA"],
                "pricing": {"input": "$0.14/M", "output": "$0.28/M"}
            },
            "deepseek-v2": {
                "description": "Older general‑purpose chat model (V2) – still useful for basic tasks.",
                "capabilities": ["Chat", "Text generation"],
                "pricing": {"input": "$0.14/M", "output": "$0.28/M"}
            },
            "deepseek-math": {
                "description": "Specialised for mathematical reasoning, equations, and proofs.",
                "capabilities": ["Math", "Logic", "Problem solving"],
                "pricing": {"input": "$0.14/M", "output": "$0.28/M"}
            },
            "deepseek-llm": {
                "description": "Original base model – foundation for all DeepSeek variants.",
                "capabilities": ["Text generation", "Foundation model"],
                "pricing": {"input": "$0.14/M", "output": "$0.28/M"}
            }
        }
        return info.get(model_id, {
            "description": "DeepSeek model – check API docs for details.",
            "capabilities": [],
            "pricing": {}
        })

    def generate(self, messages: List[Dict[str, str]], model: str = "deepseek-chat", **kwargs) -> str:
        key = self._get_key(kwargs)
        headers = self._get_headers(key)
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "max_tokens": 2048,
            "temperature": 0.7
        }
        resp = requests.post("https://api.deepseek.com/v1/chat/completions",
                             headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def generate_with_image(self, messages: List[Dict[str, str]], images: List[Dict], **kwargs) -> str:
        note = f"[{len(images)} image(s) attached – this model does not support native vision]"
        new_messages = list(messages)
        if new_messages:
            new_messages[-1] = {**new_messages[-1], "content": note + "\n" + new_messages[-1].get("content", "")}
        return self.generate(new_messages, **kwargs)


class ClaudeProvider(LLMProvider):
    def __init__(self, api_key: Optional[str] = None):
        self._default_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._available = bool(self._default_key)
        if not self._available:
            print("⚠️ ANTHROPIC_API_KEY not set. Provide it via UI or set env var.")

    def _get_key(self, kwargs) -> str:
        key = kwargs.get("api_key") or self._default_key
        if not key:
            raise Exception("Claude (Anthropic) API key is required. Enter it in the API Key field.")
        return key

    def _get_headers(self, api_key: str) -> Dict:
        return {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json"
        }

    def list_models(self, api_key: Optional[str] = None) -> List[str]:
        key = api_key or self._default_key
        if not key:
            return ["claude-3-5-sonnet-20241022", "claude-3-opus-20240229",
                    "claude-3-sonnet-20240229", "claude-3-haiku-20240307"]
        try:
            headers = self._get_headers(key)
            resp = requests.get("https://api.anthropic.com/v1/models", headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            models = [m["id"] for m in data.get("data", [])]
            return models if models else ["claude-3-5-sonnet-20241022", "claude-3-opus-20240229",
                                          "claude-3-sonnet-20240229", "claude-3-haiku-20240307"]
        except Exception as e:
            print(f"⚠️ Failed to fetch Claude models: {e}")
            return ["claude-3-5-sonnet-20241022", "claude-3-opus-20240229",
                    "claude-3-sonnet-20240229", "claude-3-haiku-20240307"]

    def generate(self, messages: List[Dict[str, str]], model: str = "claude-3-5-sonnet-20241022", **kwargs) -> str:
        key = self._get_key(kwargs)
        headers = self._get_headers(key)
        system = ""
        claude_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system = msg["content"]
            else:
                claude_messages.append(msg)
        payload = {
            "model": model,
            "messages": claude_messages,
            "max_tokens": 2048,
            "temperature": 0.7
        }
        if system:
            payload["system"] = system
        resp = requests.post("https://api.anthropic.com/v1/messages",
                             headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]

    def generate_with_image(self, messages: List[Dict[str, str]], images: List[Dict], **kwargs) -> str:
        key = self._get_key(kwargs)
        model = kwargs.get("model", "claude-3-5-sonnet-20241022")
        headers = self._get_headers(key)
        content_blocks = []
        for img in images:
            b64 = img["b64"]
            if "," in b64:
                b64 = b64.split(",", 1)[1]
            content_blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": b64
                }
            })
        last_text = messages[-1].get("content", "") if messages else ""
        content_blocks.append({"type": "text", "text": last_text})
        claude_messages = []
        for i, msg in enumerate(messages[:-1]):
            claude_messages.append({"role": msg["role"], "content": msg["content"]})
        claude_messages.append({"role": "user", "content": content_blocks})
        payload = {
            "model": model,
            "messages": claude_messages,
            "max_tokens": 2048,
            "temperature": 0.7
        }
        system = ""
        for msg in messages:
            if msg["role"] == "system":
                system = msg["content"]
                break
        if system:
            payload["system"] = system
        resp = requests.post("https://api.anthropic.com/v1/messages",
                             headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]