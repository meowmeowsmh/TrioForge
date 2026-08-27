"""
LLM Provider abstraction – all providers are optional and graceful.
Supports image (vision) input for providers and models that allow it.
"""

import os
import glob
import re
import hashlib
import base64
import unicodedata
import requests
import json
import logging
from typing import List, Dict, Any, Optional

from paths import root_path

logger = logging.getLogger(__name__)


class ProviderError(Exception):
    """Raised for provider-level failures (auth, connectivity, or API errors)."""


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
        "gemma-3", "llama-4", "qwen2-vl","qwen3.5","qwen3.6",
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
    "deepseek": {
        "deepseek-v4-flash-vision-exp",
    },
}

# ── Helpers ────────────────────────────────────────────────────────────────────
def _strip_b64_prefix(b64: str) -> str:
    """Remove data-URL prefix (e.g. 'data:image/jpeg;base64,') if present."""
    if "," in b64:
        return b64.split(",", 1)[1]
    return b64

def _clean_api_key(key: Optional[str], provider_label: str) -> Optional[str]:
    """Strip whitespace and catch stray non-ASCII characters (en-dashes, smart
    quotes, etc.) that sneak in when a key is copy-pasted from a document,
    webpage, or chat app with 'smart' text substitution. Without this, the
    key gets silently embedded in an HTTP Authorization header later on and
    fails deep inside the HTTP library with a cryptic UnicodeEncodeError
    instead of a message that points at the actual problem."""
    if not key:
        return key
    key = key.strip()
    try:
        key.encode("ascii")
    except UnicodeEncodeError as e:
        bad_char = key[e.start:e.end]
        raise ProviderError(
            f"Your {provider_label} API key contains a character that isn't plain text "
            f"({bad_char!r}, often an en-dash or curly quote introduced by copy-pasting "
            f"from a document, webpage, or chat app). Paste the key into a plain text "
            f"editor first, remove the stray character, then re-enter it here."
        )
    return key


def sanitize_api_key(key: Optional[str]) -> Optional[str]:
    """Public counterpart to _clean_api_key for routes (app.py, notes.py,
    cork_board.py) that accept an api_key straight from request JSON.
    Auto-strips stray non-ASCII characters (en-dash, curly quotes, etc.)
    instead of raising, so a bad paste is silently fixed for that request
    rather than surfacing _clean_api_key's error. Call this on any api_key
    read from request data before it reaches a provider."""
    if not key:
        return key
    key = unicodedata.normalize("NFKD", key)
    key = key.encode("ascii", "ignore").decode("ascii")
    return key.strip()


def model_supports_vision(provider_name: str, model_name: str) -> bool:
    if not model_name:
        return False
    known = VISION_MODELS.get(provider_name, set())
    if provider_name in ("groq", "huggingface", "claude"):
        # exact match (case-insensitive) for known vision models
        return model_name.lower() in {m.lower() for m in known}
    # substring match for others (ollama, llamacpp)
    model_lower = model_name.lower()
    return any(keyword in model_lower for keyword in known)


# ── Universal file reader ───────────────────────────────────────────────────────
# Goal: NEVER just say "binary, can't read it". Every uploaded file — text, code,
# PDF, image, .exe, .dll, .zip, whatever comes out of the file explorer or a
# download — gets turned into *something* the model can reason about.

TEXT_EXTENSIONS = {
    ".txt", ".md", ".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".csv", ".tsv",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".java", ".go", ".rs", ".rb", ".php",
    ".html", ".htm", ".css", ".scss", ".xml", ".yaml", ".yml", ".ini", ".cfg",
    ".conf", ".toml", ".sh", ".bat", ".ps1", ".sql", ".log", ".ipynb", ".env",
    ".rst", ".tex", ".ahk", ".lua", ".kt", ".swift", ".r",
}

# (signature bytes, human label) — checked in order, first match wins
_BINARY_SIGNATURES = [
    (b"MZ", "Windows executable / DLL (PE)"),
    (b"\x7fELF", "Linux executable / library (ELF)"),
    (b"\xca\xfe\xba\xbe", "Java class / Mach-O fat binary"),
    (b"PK\x03\x04", "ZIP-based file (zip / docx / xlsx / pptx / jar / apk)"),
    (b"%PDF", "PDF document"),
    (b"\x89PNG", "PNG image"),
    (b"\xff\xd8\xff", "JPEG image"),
    (b"GIF8", "GIF image"),
    (b"\x1f\x8b", "GZIP archive"),
    (b"7z\xbc\xaf\x27\x1c", "7-Zip archive"),
    (b"Rar!\x1a\x07", "RAR archive"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "Legacy MS Office document (doc/xls/ppt)"),
    (b"ID3", "MP3 audio"),
    (b"RIFF", "RIFF container (wav/avi)"),
]


def _strip_c_comments_generic(text: str) -> str:
    text = re.sub(r'//.*', '', text)
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    return '\n'.join(line for line in text.splitlines() if line.strip())


def _extract_printable_strings(data: bytes, min_len: int = 4, limit: int = 60) -> List[str]:
    """Cheap 'strings'-style pass: pull runs of printable ASCII out of raw bytes
    so binaries (exe, dll, unknown formats) still yield something readable."""
    found = re.findall(rb"[ -~]{%d,}" % min_len, data)
    out = []
    for b in found:
        s = b.decode("ascii", errors="ignore").strip()
        if s and s not in out:
            out.append(s)
        if len(out) >= limit:
            break
    return out


def describe_or_extract_file(name: str, b64: str, mime: str = "") -> str:
    """
    Convert ANY attached file into a text block for the LLM — text/code files
    decode directly, PDFs get real text extraction, and everything else
    (.exe, .dll, .zip, images, unknown binaries) gets identified by signature
    plus any human-readable strings found inside, instead of being dropped.
    """
    try:
        raw = base64.b64decode(_strip_b64_prefix(b64))
    except Exception as e:
        return f"[Could not decode attached file {name}: {e}]"

    size = len(raw)
    ext = os.path.splitext(name)[1].lower()
    header = f"--- File: {name} ({size:,} bytes) ---\n"

    # 1) Known text / code files → decode straight to text
    if ext in TEXT_EXTENSIONS or (mime and mime.startswith("text/")):
        try:
            text = raw.decode("utf-8", errors="replace")
            if ext in (".c", ".cpp", ".h", ".hpp"):
                text = _strip_c_comments_generic(text)
            return header + text[:8000]
        except Exception:
            pass  # fall through to binary handling below

    # 2) PDFs → try real text extraction
    if ext == ".pdf" or raw[:4] == b"%PDF":
        try:
            from pypdf import PdfReader
            import io
            reader = PdfReader(io.BytesIO(raw))
            pages_text = [(p.extract_text() or "") for p in reader.pages[:20]]
            text = "\n".join(pages_text).strip()
            if text:
                return header + text[:8000]
        except Exception:
            pass  # pypdf missing or extraction failed — fall through to metadata

    # 3) Everything else — .exe, .dll, .zip, images, archives, unknown formats —
    #    identify what it is and surface any readable strings inside it.
    kind = "Unknown binary data"
    for sig, label in _BINARY_SIGNATURES:
        if raw.startswith(sig):
            kind = label
            break

    sha256 = hashlib.sha256(raw).hexdigest()
    info = (
        f"{header}"
        f"Type: {kind}\n"
        f"MIME (client-reported): {mime or 'unknown'}\n"
        f"SHA-256: {sha256}\n"
    )

    strings = _extract_printable_strings(raw)
    if strings:
        info += f"Readable strings found inside the file (showing {len(strings)}):\n"
        info += "\n".join(f"  - {s}" for s in strings)
    else:
        info += "No readable text strings found inside the file."

    return info


class LLMProvider:
    # Default generation parameters – can be overridden per provider
    DEFAULT_TEMPERATURE = 0.7
    DEFAULT_MAX_TOKENS = 65536
    # Hard cap for providers whose API rejects very large max_tokens (cloud ones).
    MAX_OUTPUT_TOKENS = 65536

    def generate(self, messages: List[Dict[str, str]], **kwargs) -> str:
        raise NotImplementedError

    def generate_raw(self, messages, **kwargs):
        """Return a raw assistant message dict (content + optional tool_calls).
        Base default: plain generate() with no tool support."""
        return {"role": "assistant", "content": self.generate(messages, **kwargs), "tool_calls": None}

    def generate_with_image(self, messages: List[Dict[str, str]],
                            images: List[Dict], **kwargs) -> str:
        note = f"[{len(images)} image(s) attached – this model does not support native vision]"
        messages = list(messages)
        if messages:
            messages[-1] = {**messages[-1], "content": note + "\n" + messages[-1].get("content", "")}
        return self.generate(messages, **kwargs)

    def list_models(self, api_key: Optional[str] = None) -> List[str]:
        return []

    def get_system_prompt(self) -> str:
        """
        Return the system prompt that should be prepended to every conversation.
        Override in subclasses to add provider‑specific instructions.
        """
        return (
            "Always format tabular data as Markdown tables with headers. "
            "Use **bold** for important terms or emphasis. "
            "Use bullet lists (- or *) for enumerations. "
            "When the user asks for a flowchart, architecture, diagram, sequence diagram, "
            "or any visual structure, output valid Mermaid.js syntax inside a ```mermaid code block. "
            "Keep your responses clear, structured, and easy to read."
        )


class OllamaProvider(LLMProvider):
    """Ollama provider using /api/chat for all requests (preserves conversation history)."""
    def __init__(self, model: str = "vaultbox/qwen3.5-uncensored:9b",
                 base_url: Optional[str] = None):
        self.base_url = base_url or os.environ.get('OLLAMA_BASE_URL', 'http://127.0.0.1:11434')
        self.model = model
        self.chat_url = f"{self.base_url}/api/chat"

    def _prepare_messages(self, messages: List[Dict], images: Optional[List[Dict]] = None) -> List[Dict]:
        """Convert provider messages to Ollama /api/chat format, embedding images if present."""
        if images:
            msgs = [m.copy() for m in messages]
            last_user_idx = None
            for i in range(len(msgs) - 1, -1, -1):
                if msgs[i].get("role") == "user":
                    last_user_idx = i
                    break
            if last_user_idx is not None:
                user_msg = msgs[last_user_idx]
                b64_list = [_strip_b64_prefix(img["b64"]) for img in images]
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
        temperature = kwargs.get("temperature", self.DEFAULT_TEMPERATURE)
        max_tokens = kwargs.get("max_tokens", self.DEFAULT_MAX_TOKENS)
        num_gpu = kwargs.get("num_gpu", 99)
        low_vram = kwargs.get("low_vram", False)

        chat_messages = self._prepare_messages(messages, images)

        payload = {
            "model": model,
            "messages": chat_messages,
            "stream": False,
            "keep_alive": 300,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
                "num_ctx": 4096,
                "num_gpu": num_gpu,
                "low_vram": low_vram,
            }
        }

        try:
            resp = requests.post(self.chat_url, json=payload, timeout=180)
            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            raise ProviderError(f"Ollama request failed: {e}")
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
        except requests.exceptions.RequestException as e:
            # log if you have a logger, otherwise suppress
            return []


class LlamaCppProvider(LLMProvider):
    def __init__(self, models_dir: Optional[str] = None,
                 server_url: str = "http://127.0.0.1:8080/v1",
                 context_length: int = 65536):  # 64k
        self.models_dir = os.path.abspath(models_dir) if models_dir else root_path("models")
        self.server_url = server_url.rstrip("/")
        self.context_length = context_length
        self._ensure_models_dir()
        self.available_models = self._discover_models()

    def _ensure_models_dir(self):
        if not os.path.exists(self.models_dir):
            os.makedirs(self.models_dir, exist_ok=True)

    def _discover_models(self) -> List[str]:
        # Text GGUF models only. mmproj-*.gguf files are vision projectors — they
        # must be paired with their text model via --mmproj (see llamacpp_service),
        # so they are NOT listed as standalone selectable models here.
        gguf_files = glob.glob(os.path.join(self.models_dir, "*.gguf"))
        local_models = [os.path.basename(f) for f in gguf_files
                        if not os.path.basename(f).lower().startswith("mmproj-")]

        server_models = []
        try:
            resp = requests.get(f"{self.server_url}/models", timeout=3)
            if resp.status_code == 200:
                data = resp.json()
                if "data" in data:
                    server_models = [m["id"] for m in data["data"]
                                     if not str(m["id"]).lower().startswith("mmproj-")]
        except Exception:
            pass

        # Dedupe by basename (a running server may return absolute paths that
        # duplicate the local basenames).
        all_models = []
        seen = set()
        for m in local_models + server_models:
            key = os.path.basename(str(m)).lower()
            if key not in seen:
                seen.add(key)
                all_models.append(m)
        return all_models

    def list_models(self, api_key: Optional[str] = None) -> List[str]:
        return self.available_models

    def _resolve_model_path(self, model: Optional[str]) -> str:
        if not model:
            if self.available_models:
                model = self.available_models[0]
            else:
                raise ProviderError("No models found in ./models folder and no model specified.")
        if os.path.sep not in model and not model.startswith("/") and not model.startswith("\\"):
            candidate = os.path.join(self.models_dir, model)
            if os.path.exists(candidate):
                return candidate
        return model

    def _check_server(self, wait_ready: bool = True):
        """Verify the llama-server is reachable. When `wait_ready` is true, poll
        /health until the model finishes loading (large GGUF files take a while),
        so the first message doesn't fail just because the server is still starting."""
        import time
        base = self.server_url.rstrip("/")
        base = base.rsplit("/v1", 1)[0]
        health = base + "/health"
        if wait_ready:
            deadline = time.time() + 120
            while time.time() < deadline:
                try:
                    r = requests.get(health, timeout=2)
                    if r.status_code == 200:
                        return
                except Exception:
                    pass
                time.sleep(1)
            raise ConnectionError(
                "llama.cpp server did not become ready (model may still be loading). "
                "Check the Logs > Server tab or start it manually."
            )
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
        n_ctx = kwargs.get("n_ctx", self.context_length)
        temperature = kwargs.get("temperature", self.DEFAULT_TEMPERATURE)
        max_tokens = min(kwargs.get("max_tokens", self.DEFAULT_MAX_TOKENS),
                         max(256, n_ctx - 256))

        payload = {
            "model": model_path,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "n_ctx": n_ctx,
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
            raise ProviderError("llama.cpp server timed out. Try reducing context size or use a smaller model.")
        except requests.exceptions.ConnectionError:
            raise ProviderError("Cannot connect to llama.cpp server. Is it running?")
        except Exception as e:
            raise ProviderError(f"llama.cpp error: {e}")

    def generate_with_image(self, messages: List[Dict[str, str]],
                            images: List[Dict], **kwargs) -> str:
        self._check_server()
        model_path = self._resolve_model_path(kwargs.get("model"))
        n_ctx = kwargs.get("n_ctx", self.context_length)
        temperature = kwargs.get("temperature", self.DEFAULT_TEMPERATURE)
        max_tokens = min(kwargs.get("max_tokens", self.DEFAULT_MAX_TOKENS),
                         max(256, n_ctx - 256))

        content_parts = []
        for img in images:
            b64 = _strip_b64_prefix(img["b64"])
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
            "temperature": temperature,
            "max_tokens": max_tokens,
            "n_ctx": n_ctx,
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
            raise ProviderError(f"llama.cpp vision error: {e}")


class HuggingFaceProvider(LLMProvider):
    MAX_OUTPUT_TOKENS = 4096  # HF inference is variable; keep a safe cap
    def __init__(self, model: str = "microsoft/DialoGPT-medium",
                 api_token: Optional[str] = None):
        self.model = model
        self.api_token = api_token or os.environ.get("HF_API_TOKEN")
        self._available = True
        try:
            import huggingface_hub
        except ImportError:
            self._available = False
            logger.warning("huggingface_hub not installed. Run: pip install huggingface_hub")

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
        token = _clean_api_key(api_key or self.api_token, "Hugging Face")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def generate(self, messages: List[Dict[str, str]], **kwargs) -> str:
        if not self._available:
            raise ProviderError("Hugging Face provider not available – missing huggingface_hub.")
        model = kwargs.get("model") or self.model
        prompt = messages[-1]["content"] if messages else ""
        temperature = kwargs.get("temperature", self.DEFAULT_TEMPERATURE)
        max_tokens = min(kwargs.get("max_tokens", self.DEFAULT_MAX_TOKENS), self.MAX_OUTPUT_TOKENS)

        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        url = f"https://api-inference.huggingface.co/models/{model}"
        payload = {
            "inputs": prompt,
            "parameters": {
                "max_new_tokens": max_tokens,
                "temperature": temperature,
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
                raise ProviderError("Invalid Hugging Face token. Please check your token.")
            elif resp.status_code == 503:
                raise ProviderError("Hugging Face API is overloaded. Please wait and retry.")
            raise ProviderError(f"Hugging Face API error: {e}")
        except Exception as e:
            raise ProviderError(f"Failed to generate response: {e}")

    def generate_with_image(self, messages: List[Dict[str, str]],
                            images: List[Dict], **kwargs) -> str:
        if not self._available:
            raise ProviderError("Hugging Face provider not available.")
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        model = kwargs.get("model") or self.model
        prompt = messages[-1]["content"] if messages else ""
        temperature = kwargs.get("temperature", self.DEFAULT_TEMPERATURE)
        max_tokens = kwargs.get("max_tokens", self.DEFAULT_MAX_TOKENS)
        headers = self._make_headers(kwargs.get("api_key"))
        content_parts = []
        for img in images:
            b64 = _strip_b64_prefix(img["b64"])
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
                "max_new_tokens": max_tokens,
                "temperature": temperature,
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
                raise ProviderError("Invalid or missing Hugging Face token for this model.")
            elif resp.status_code == 503:
                raise ProviderError("Hugging Face model is loading. Wait a moment and retry.")
            raise ProviderError(f"HuggingFace vision API error: {e}")
        except Exception as e:
            raise ProviderError(f"HuggingFace vision request failed: {e}")


class GroqProvider(LLMProvider):
    MAX_OUTPUT_TOKENS = 32768  # Groq supports large outputs on many models
    def __init__(self, api_key: Optional[str] = None):
        self._default_key = api_key or os.environ.get("GROQ_API_KEY")
        self._available = bool(self._default_key)
        if not self._available:
            logger.warning("GROQ_API_KEY not set. Provide it via UI or set env var.")

    def _get_key(self, kwargs) -> str:
        key = _clean_api_key(kwargs.get("api_key") or self._default_key, "Groq")
        if not key:
            raise ProviderError("Groq API key is required. Enter it in the API Key field.")
        return key

    def _get_client(self, api_key: Optional[str] = None):
        key = _clean_api_key(api_key or self._default_key, "Groq")
        if not key:
            raise ProviderError("Groq API key is required.")
        try:
            from groq import Groq
            return Groq(api_key=key)
        except ImportError:
            raise ProviderError("groq library not installed. Run: pip install groq")

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
            logger.warning("Failed to fetch Groq models: %s", e)
            return FALLBACK_MODELS

    def generate_raw(self, messages: List[Dict[str, str]],
                     model: str = "llama-3.3-70b-versatile", **kwargs) -> dict:
        client = self._get_client(kwargs.get("api_key"))
        temperature = kwargs.get("temperature", self.DEFAULT_TEMPERATURE)
        max_tokens = min(kwargs.get("max_tokens", self.DEFAULT_MAX_TOKENS), self.MAX_OUTPUT_TOKENS)
        params = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        tools = kwargs.get("tools")
        if tools:
            params["tools"] = tools
            params["tool_choice"] = "auto"
        try:
            chat = client.chat.completions.create(**params)
            msg = chat.choices[0].message
            tool_calls = None
            if getattr(msg, "tool_calls", None):
                tool_calls = [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls
                ]
            return {"role": "assistant", "content": msg.content, "tool_calls": tool_calls}
        except Exception as e:
            raise ProviderError(f"Groq API error: {e}")

    def generate(self, messages: List[Dict[str, str]],
                 model: str = "llama-3.3-70b-versatile", **kwargs) -> str:
        msg = self.generate_raw(messages, model=model, **kwargs)
        return msg.get("content") or ""

    def generate_with_image(self, messages: List[Dict[str, str]],
                            images: List[Dict], **kwargs) -> str:
        client = self._get_client(kwargs.get("api_key"))
        model = kwargs.get("model", "meta-llama/llama-4-scout-17b-16e-instruct")
        temperature = kwargs.get("temperature", self.DEFAULT_TEMPERATURE)
        max_tokens = kwargs.get("max_tokens", self.DEFAULT_MAX_TOKENS)
        content_parts = []
        for img in images:
            b64 = _strip_b64_prefix(img["b64"])
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
        try:
            chat = client.chat.completions.create(
                messages=vision_messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens
            )
            return chat.choices[0].message.content
        except Exception as e:
            raise ProviderError(f"Groq vision API error: {e}")


class DeepSeekProvider(LLMProvider):
    # DeepSeek text models + the vision model (deepseek-v4-flash-vision-exp).
    FALLBACK_MODELS = ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-v4-flash-vision-exp"]
    MAX_OUTPUT_TOKENS = 8192  # DeepSeek chat API caps output here

    def __init__(self, api_key: Optional[str] = None):
        self._default_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        self._available = bool(self._default_key)
        if not self._available:
            logger.warning("DEEPSEEK_API_KEY not set. Provide it via UI or set env var.")

    def _get_key(self, kwargs) -> str:
        key = _clean_api_key(kwargs.get("api_key") or self._default_key, "DeepSeek")
        if not key:
            raise ProviderError("DeepSeek API key is required. Enter it in the API Key field.")
        return key

    def _get_headers(self, api_key: str) -> Dict:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

    def list_models(self, api_key: Optional[str] = None) -> List[str]:
        key = api_key or self._default_key
        if not key:
            return self.FALLBACK_MODELS
        try:
            headers = self._get_headers(key)
            resp = requests.get("https://api.deepseek.com/v1/models", headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            models = [m["id"] for m in data.get("data", [])]
            return models if models else self.FALLBACK_MODELS
        except Exception as e:
            logger.warning("Failed to fetch DeepSeek models: %s", e)
            return self.FALLBACK_MODELS

    def get_status(self) -> dict:
        """Check DeepSeek API reachability without exposing internals."""
        if not self._default_key:
            return {"ok": False, "message": "No API key provided"}
        try:
            headers = self._get_headers(self._default_key)
            resp = requests.get("https://api.deepseek.com/v1/models", headers=headers, timeout=5)
            if resp.status_code == 200:
                return {"ok": True, "message": "API online"}
            return {"ok": False, "message": "API returned error"}
        except Exception:
            return {"ok": False, "message": "API unreachable or invalid key"}

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
            "deepseek-v4-flash-vision-exp": {
                "description": "DeepSeek V4 Flash vision model (experimental) – fast image understanding and multimodal chat.",
                "capabilities": ["Image analysis", "Multimodal", "Visual QA", "Fast"],
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

    def generate_raw(self, messages: List[Dict[str, str]], model: str = "deepseek-chat", **kwargs) -> dict:
        key = self._get_key(kwargs)
        headers = self._get_headers(key)
        temperature = kwargs.get("temperature", self.DEFAULT_TEMPERATURE)
        max_tokens = min(kwargs.get("max_tokens", self.DEFAULT_MAX_TOKENS), self.MAX_OUTPUT_TOKENS)
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "max_tokens": max_tokens,
            "temperature": temperature
        }
        thinking = kwargs.get("thinking")
        if thinking:
            payload["reasoning_effort"] = {"low": "low", "mid": "medium", "high": "high"}.get(thinking, "high")
        tools = kwargs.get("tools")
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        try:
            resp = requests.post("https://api.deepseek.com/v1/chat/completions",
                                 headers=headers, json=payload, timeout=180)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]
        except requests.exceptions.RequestException as e:
            raise ProviderError(f"DeepSeek API error: {e}")

    def generate(self, messages: List[Dict[str, str]], model: str = "deepseek-chat", **kwargs) -> str:
        msg = self.generate_raw(messages, model=model, **kwargs)
        return msg.get("content") or ""

    def generate_with_image(self, messages: List[Dict[str, str]],
                            images: List[Dict], **kwargs) -> str:
        """OpenAI-compatible vision request for the deepseek-v4-flash-vision-exp model."""
        key = self._get_key(kwargs)
        headers = self._get_headers(key)
        model = kwargs.get("model", "deepseek-v4-flash-vision-exp")
        temperature = kwargs.get("temperature", self.DEFAULT_TEMPERATURE)
        max_tokens = kwargs.get("max_tokens", self.DEFAULT_MAX_TOKENS)

        content_parts = []
        for img in images:
            b64 = _strip_b64_prefix(img["b64"])
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

        payload = {
            "model": model,
            "messages": vision_messages,
            "stream": False,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        try:
            resp = requests.post("https://api.deepseek.com/v1/chat/completions",
                                 headers=headers, json=payload, timeout=180)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except requests.exceptions.RequestException as e:
            raise ProviderError(f"DeepSeek vision API error: {e}")


class ClaudeProvider(LLMProvider):
    MAX_OUTPUT_TOKENS = 8192  # Anthropic caps max_tokens at 8192 for most models
    def __init__(self, api_key: Optional[str] = None):
        self._default_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._available = bool(self._default_key)
        if not self._available:
            logger.warning("ANTHROPIC_API_KEY not set. Provide it via UI or set env var.")

    def _get_key(self, kwargs) -> str:
        key = _clean_api_key(kwargs.get("api_key") or self._default_key, "Claude (Anthropic)")
        if not key:
            raise ProviderError("Claude (Anthropic) API key is required. Enter it in the API Key field.")
        return key

    def _get_headers(self, api_key: str) -> Dict:
        return {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json"
        }

    def list_models(self, api_key: Optional[str] = None) -> List[str]:
        key = api_key or self._default_key
        FALLBACK_MODELS = [
            "claude-3-5-sonnet-20241022",
            "claude-3-opus-20240229",
            "claude-3-sonnet-20240229",
            "claude-3-haiku-20240307"
        ]
        if not key:
            return FALLBACK_MODELS
        try:
            headers = self._get_headers(key)
            resp = requests.get("https://api.anthropic.com/v1/models", headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            models = [m["id"] for m in data.get("data", [])]
            return models if models else FALLBACK_MODELS
        except Exception as e:
            logger.warning("Failed to fetch Claude models: %s", e)
            return FALLBACK_MODELS

    def generate(self, messages: List[Dict[str, str]], model: str = "claude-3-5-sonnet-20241022", **kwargs) -> str:
        key = self._get_key(kwargs)
        headers = self._get_headers(key)
        temperature = kwargs.get("temperature", self.DEFAULT_TEMPERATURE)
        max_tokens = min(kwargs.get("max_tokens", self.DEFAULT_MAX_TOKENS), self.MAX_OUTPUT_TOKENS)

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
            "max_tokens": max_tokens,
            "temperature": temperature
        }
        if system:
            payload["system"] = system

        try:
            resp = requests.post("https://api.anthropic.com/v1/messages",
                                 headers=headers, json=payload, timeout=120)
            resp.raise_for_status()
            return resp.json()["content"][0]["text"]
        except requests.exceptions.RequestException as e:
            raise ProviderError(f"Claude API error: {e}")

    def generate_with_image(self, messages: List[Dict[str, str]], images: List[Dict], **kwargs) -> str:
        key = self._get_key(kwargs)
        model = kwargs.get("model", "claude-3-5-sonnet-20241022")
        temperature = kwargs.get("temperature", self.DEFAULT_TEMPERATURE)
        max_tokens = kwargs.get("max_tokens", self.DEFAULT_MAX_TOKENS)
        headers = self._get_headers(key)

        content_blocks = []
        for img in images:
            b64 = _strip_b64_prefix(img["b64"])
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
            "max_tokens": max_tokens,
            "temperature": temperature
        }
        system = ""
        for msg in messages:
            if msg["role"] == "system":
                system = msg["content"]
                break
        if system:
            payload["system"] = system

        try:
            resp = requests.post("https://api.anthropic.com/v1/messages",
                                 headers=headers, json=payload, timeout=120)
            resp.raise_for_status()
            return resp.json()["content"][0]["text"]
        except requests.exceptions.RequestException as e:
            raise ProviderError(f"Claude vision API error: {e}")