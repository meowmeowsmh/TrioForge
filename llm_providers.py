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
    def __init__(self, model: str = "vaultbox/qwen3.5-uncensored:9b",
                 base_url: str = "http://127.0.0.1:11434"):
        self.model = model
        self.base_url = base_url
        self.api_url = f"{base_url}/api/generate"

    def _build_payload(self, messages, images=None, model=None):
        prompt = messages[-1]["content"] if messages else ""
        payload = {
            "model": model or self.model,
            "prompt": prompt,
            "stream": False,
            "keep_alive": 300,
            "options": {
                "temperature": 0.7,
                "num_predict": 2048,
                "num_ctx": 4096,
                "num_gpu": 99,
                "low_vram": False,
            }
        }
        if images:
            payload["images"] = [
                img["b64"].split(",")[-1] if "," in img["b64"] else img["b64"]
                for img in images if "b64" in img
            ]
        return payload

    def generate(self, messages: List[Dict[str, str]],
                 images: Optional[List[Dict]] = None, **kwargs) -> str:
        model = kwargs.get("model") or self.model
        payload = self._build_payload(messages, images=images, model=model)
        resp = requests.post(self.api_url, json=payload, timeout=180)
        resp.raise_for_status()
        return resp.json().get("response", "")

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


class LlamaCppProvider(LLMProvider):
    def __init__(self, models_dir: str = "./models",
                 server_url: str = "http://127.0.0.1:8080/v1"):
        self.models_dir = models_dir
        self.server_url = server_url.rstrip("/")
        self.available_models = self._discover_models()

    def _discover_models(self) -> List[str]:
        if not os.path.exists(self.models_dir):
            os.makedirs(self.models_dir, exist_ok=True)
        gguf_files = glob.glob(os.path.join(self.models_dir, "*.gguf"))
        return [os.path.basename(f) for f in gguf_files]

    def list_models(self, api_key: Optional[str] = None) -> List[str]:
        return self.available_models

    def _resolve_model_path(self, model: Optional[str]) -> str:
        if model:
            if os.path.sep not in model and not model.startswith("/") and not model.startswith("\\"):
                return os.path.join(self.models_dir, model)
            return model
        if self.available_models:
            return os.path.join(self.models_dir, self.available_models[0])
        raise Exception("No .gguf models found in ./models folder.")

    def generate(self, messages: List[Dict[str, str]],
                 model: Optional[str] = None, **kwargs) -> str:
        model_path = self._resolve_model_path(model)
        payload = {
            "model": model_path,
            "messages": messages,
            "stream": False
        }
        resp = requests.post(f"{self.server_url}/chat/completions",
                             json=payload, timeout=180)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def generate_with_image(self, messages: List[Dict[str, str]],
                            images: List[Dict], **kwargs) -> str:
        model_path = self._resolve_model_path(kwargs.get("model"))
        content_parts = []
        for img in images:
            b64 = img["b64"]
            if not b64.startswith("data:"):
                b64 = f"data:image/jpeg;base64,{b64}"
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": b64}
            })
        last_text = messages[-1].get("content", "") if messages else ""
        content_parts.append({"type": "text", "text": last_text})
        vision_messages = [
            {"role": m["role"], "content": m["content"]}
            for m in messages
        ]
        vision_messages[-1] = {"role": "user", "content": content_parts}
        payload = {
            "model": model_path,
            "messages": vision_messages,
            "stream": False
        }
        resp = requests.post(f"{self.server_url}/chat/completions",
                             json=payload, timeout=180)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


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


# ===== NEW PROVIDERS =====

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
            return ["deepseek-chat", "deepseek-coder"]
        try:
            headers = self._get_headers(key)
            resp = requests.get("https://api.deepseek.com/v1/models", headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            models = [m["id"] for m in data.get("data", [])]
            return models if models else ["deepseek-chat", "deepseek-coder"]
        except Exception as e:
            print(f"⚠️ Failed to fetch DeepSeek models: {e}")
            return ["deepseek-chat", "deepseek-coder"]

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
        # DeepSeek doesn't support vision yet; fallback to text with note
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