"""Model transport.

The client does not care whether the reply comes from llama-server, Ollama or a
hosted API - everything behind :class:`Backend` streams plain text chunks.

Two implementations:

* :class:`OpenAICompatBackend` - any OpenAI-compatible ``/v1/chat/completions``
  (llama-server, Ollama, OpenRouter, Groq, ...). This is what TrioForge's
  ``LlamaCppProvider`` speaks, so the terminal client sees exactly the same
  models as the web UI.
* :class:`EchoBackend` - an offline stub. It exists so the UI can be built and
  demoed with no model loaded at all.
"""

from __future__ import annotations

import json
import os
import time
from typing import Iterable, Iterator

try:
    import httpx
except ImportError:  # pragma: no cover - httpx is a TrioForge dependency
    httpx = None


class BackendError(RuntimeError):
    pass


def friendly_error(text: str) -> str:
    """Map a raw transport/OS error to something a human understands.

    httpx surfaces OS errors verbatim ("[Errno 10061] Connect call failed"),
    which reads like garbage in the transcript. Translate the two cases users
    actually hit - the local model not running, and a slow/absent reply - and
    leave anything else untouched.
    """
    t = (text or "").lower()
    if "10061" in t or "connection refused" in t or "actively refused" in t \
            or "cannot connect" in t or "errno 111" in t:
        return ("the local model is not running — start it with /start, or just "
                "send a message and it will auto-start")
    if "timed out" in t or "timeout" in t or "readtimeout" in t:
        return "the model took too long to answer (timed out)"
    if "name or service not known" in t or "getaddrinfo" in t \
            or "errno 11001" in t:
        return "could not reach the endpoint — check the provider's base URL"
    if t.startswith("http 401") or "invalid_api_key" in t or "invalid api key" in t \
            or "incorrect api key" in t or "unauthorized" in t:
        return "the API key was rejected (401) — check the key for this provider"
    if t.startswith("http 429"):
        return "rate limited (429) — wait a moment and try again"
    if t.startswith("http 404"):
        return "the model name was not found (404) — check it exists on this provider"
    return text


class Backend:
    """Interface. ``stream`` yields text chunks as they arrive."""

    name = "?"
    where = "?"

    def stream(self, messages: list[dict]) -> Iterator[tuple[str, str]]:  # pragma: no cover
        raise NotImplementedError

    def health(self) -> tuple[bool, str]:  # pragma: no cover
        return True, "ok"


# ---------------------------------------------------------------------------


def _iter_sse(response) -> Iterator[tuple[str, str]]:
    """Yield ``(kind, text)`` from an OpenAI-style SSE stream.

    ``kind`` is ``"content"`` for the answer and ``"reasoning"`` for the model's
    chain of thought. Two field names are in the wild and TrioForge's own
    providers read both, so this does too:

    * ``reasoning_content`` - DeepSeek, and most OpenAI-compatible reasoners
    * ``reasoning``         - OpenRouter and others
    * ``thinking``          - Ollama

    Keeping them separate (rather than concatenating) is what lets the UI show a
    "thinking" block that is clearly not part of the answer.
    """
    for line in response.iter_lines():
        if not line:
            continue
        if line.startswith("data: "):
            line = line[6:]
        if line.strip() == "[DONE]":
            break
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        # A hosted API that fails AFTER the 200 (rate limit, overload, a
        # rejected request) sends an error object instead of choices. Dropping
        # it silently left an empty turn with no hint of what went wrong.
        if isinstance(obj, dict) and obj.get("error") and not obj.get("choices"):
            err = obj["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise BackendError(friendly_error(f"stream error: {msg}"))
        for choice in obj.get("choices", []):
            if choice.get("finish_reason"):
                yield "finish", choice["finish_reason"]
            delta = choice.get("delta") or {}
            for field in ("reasoning_content", "reasoning", "thinking"):
                piece = delta.get(field)
                if piece:
                    yield "reasoning", piece
            # Native tool calls arrive fragmented: the name once, then the JSON
            # arguments a few characters at a time. They are passed through raw
            # and assembled by the agent, which is the only place that knows the
            # call is finished.
            for tc in delta.get("tool_calls") or []:
                fn = tc.get("function") or {}
                yield "tool_call", {
                    "index": tc.get("index", 0),
                    "id": tc.get("id"),
                    "name": fn.get("name"),
                    "arguments": fn.get("arguments"),
                }
            piece = delta.get("content")
            if piece:
                yield "content", piece


# A local model with no cap generates until it fills the whole context. A weak
# 9B that never emits its end-of-stream token rambles at ~10 tok/s for 20+
# minutes - a turn that looks like "16m 0s, ~0 tok". Cap every generation so a
# turn can never run away; 2048 tokens is a full page and plenty for one answer.
MAX_TOKENS = int(os.environ.get("TRIOFORGE_MAX_TOKENS", "2048") or 2048)

# Native tool calling is different: a `write` carries the WHOLE file inside the
# tool-call arguments, so one HTML page is ~3-8k tokens of JSON. Under the 2048
# cap the arguments got truncated mid-string, json.loads failed, and the tool
# ran with NO arguments - the "error: ... is a folder, not a file" loop where the
# agent kept writing to its own cwd. Give tool-calling turns their own, larger
# cap; the 2048 cap still guards the local text-protocol model, which never sets
# `tools` and would otherwise ramble.
MAX_TOKENS_WITH_TOOLS = int(os.environ.get("TRIOFORGE_MAX_TOKENS_TOOLS", "16384") or 16384)

# What a request falls back to when a provider rejects max_tokens as too big
# (older DeepSeek chat models cap output at 8192).
SAFE_MAX_TOKENS = 8192

# Extra attempts for a 429/5xx, a dropped connection, or a 400 that _adapt fixed.
RETRIES = 3


class OpenAICompatBackend(Backend):
    """Talks to an OpenAI-compatible endpoint, streaming by default."""

    def __init__(self, base_url: str, model: str, api_key: str = "",
                 timeout: float = 120.0, temperature: float = 0.7,
                 max_tokens: int = 0):
        self.base_url = base_url.rstrip("/")
        self.model = model or "default"
        self.api_key = api_key
        self.timeout = timeout
        self.temperature = temperature
        # 0 = "use the built-in default" so an unset config never overrides it.
        self.max_tokens = max_tokens or 0
        self.name = model or "default"
        self.where = self.base_url
        # Set by the agent when it wants native tool calling. Left empty for
        # providers (or models) that cannot do it - gemma-3-12b ignores `tools`
        # entirely and needs the text protocol instead.
        self.tools: list[dict] = []
        self.is_deepseek = "deepseek.com" in self.base_url
        # Set after DeepSeek rejected a request for missing reasoning_content.
        self.thinking_off = False

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def health(self) -> tuple[bool, str]:
        if httpx is None:
            return False, "httpx is not installed"
        try:
            r = httpx.get(f"{self.base_url}/models", headers=self._headers(),
                          timeout=5.0)
            if r.status_code == 200:
                return True, "reachable"
            return False, f"HTTP {r.status_code}"
        except Exception as exc:  # noqa: BLE001 - report anything as unreachable
            return False, str(exc)

    def stream(self, messages: list[dict]) -> Iterator[tuple[str, str]]:
        """Yield (kind, text) - kind is 'content' or 'reasoning'."""
        if httpx is None:
            raise BackendError("httpx is not installed")
        tools_on = bool(getattr(self, "tools", None))
        body = {
            "model": self.model,
            "messages": self._wire(messages),
            "stream": True,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens
            or (MAX_TOKENS_WITH_TOOLS if tools_on else MAX_TOKENS),
        }
        if tools_on:
            body["tools"] = self.tools
            body["tool_choice"] = "auto"
        if self.thinking_off:
            body["thinking"] = {"type": "disabled"}
        url = f"{self.base_url}/chat/completions"
        waited_for_load = False
        for attempt in range(RETRIES + 1):
            emitted = False
            try:
                with httpx.stream("POST", url, json=body, headers=self._headers(),
                                  timeout=self.timeout) as r:
                    if r.status_code < 400:
                        for item in _iter_sse(r):
                            emitted = True
                            yield item
                        return
                    status = r.status_code
                    detail = r.read().decode("utf-8", "replace")[:400]
            except BackendError:
                raise
            except Exception as exc:  # noqa: BLE001
                # A dropped connection can be retried only before anything was
                # shown - after that a retry would duplicate the answer.
                if emitted or attempt == RETRIES:
                    raise BackendError(friendly_error(str(exc))) from exc
                time.sleep(2 ** attempt)
                continue

            if status == 503 and "load" in detail.lower() and not waited_for_load:
                # The model is still loading. Wait for it rather than failing
                # the user's very first message.
                waited_for_load = True
                wait_ready(self.base_url, timeout=min(self.timeout, 600.0))
                continue
            if attempt < RETRIES and self._adapt(body, status, detail):
                continue
            if status in (429, 500, 502, 503, 504) and attempt < RETRIES:
                time.sleep(2 ** attempt)
                continue
            raise BackendError(friendly_error(f"HTTP {status}: {detail}"))

    def _wire(self, messages: list[dict]) -> list[dict]:
        """The messages as this endpoint accepts them.

        DeepSeek's thinking mode REQUIRES every earlier assistant message's
        ``reasoning_content`` back when tools are on, and answers HTTP 400
        without it. Other providers may reject the unknown field, so it is only
        sent to DeepSeek.
        """
        if self.is_deepseek and not self.thinking_off:
            return messages
        return [{k: v for k, v in m.items() if k != "reasoning_content"}
                if "reasoning_content" in m else m for m in messages]

    def _adapt(self, body: dict, status: int, detail: str) -> bool:
        """Fix the request for a 400 we know how to fix. True = retry."""
        if status != 400:
            return False
        low = detail.lower()
        if self.is_deepseek and "reasoning" in low and "thinking" not in body:
            # History restored from disk (or trimmed by the window) has no
            # reasoning to pass back. Thinking off makes the API stop demanding
            # it; sticky for this backend so later requests do not fail first.
            self.thinking_off = True
            body["thinking"] = {"type": "disabled"}
            body["messages"] = self._wire(body["messages"])
            return True
        if "max_tokens" in low and body.get("max_tokens", 0) > SAFE_MAX_TOKENS:
            body["max_tokens"] = SAFE_MAX_TOKENS
            return True
        return False


class EchoBackend(Backend):
    """Offline stub: no model contacted.

    It emits a fake chain of thought as well as an answer, so `--echo` exercises
    the same code path a reasoning model does - the reasoning card, the live
    token/timing counter, the lot. Without this the stub would only ever show
    the plainest possible turn.
    """

    name = "echo"
    where = "offline stub"

    def stream(self, messages: list[dict]) -> Iterator[tuple[str, str]]:
        last = next((m["content"] for m in reversed(messages)
                     if m["role"] == "user"), "")

        think = (
            f"The user asked: {last!r}.\n"
            "This is the offline stub, so I am not contacting a model.\n"
            "I will show some reasoning, then an answer, to prove the two are "
            "rendered separately."
        )
        for word in think.split(" "):
            yield "reasoning", word + " "

        answer = (
            f"You said: **{last}**\n\n"
            "This is the offline stub, so no model was contacted.\n\n"
            "Start llama-server (or pass `--base-url`) and the same UI will talk "
            "to the real model.\n\n"
            "```python\nprint('code blocks render too')\n```"
        )
        for word in answer.split(" "):
            yield "content", word + " "


def make_backend(args) -> Backend:
    """Build a backend from parsed CLI arguments."""
    if getattr(args, "echo", False):
        return EchoBackend()
    return OpenAICompatBackend(
        base_url=args.base_url,
        model=args.model,
        api_key=getattr(args, "api_key", "") or "",
        timeout=getattr(args, "timeout", 120.0),
        temperature=getattr(args, "temperature", 0.7),
    )


def wait_ready(base_url: str, timeout: float = 600.0,
               on_tick=None) -> bool:
    """Block until the model is LOADED, not merely listening.

    llama-server binds the port immediately but answers /health with 503
    "Loading model" until the weights are in memory - which for a 6.8 GB model
    takes a while. `start()` returns as soon as the port is open, so without this
    the first message hits a 503.
    """
    if httpx is None:
        return True
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    deadline = time.time() + timeout
    started = time.time()
    while time.time() < deadline:
        try:
            r = httpx.get(f"{root}/health", timeout=4.0)
            if r.status_code == 200:
                return True
            if r.status_code == 503:
                if on_tick:
                    on_tick(time.time() - started)
            else:
                # A server that answers anything else is up; do not hang here.
                return True
        except Exception:  # noqa: BLE001 - not up yet
            if on_tick:
                on_tick(time.time() - started)
        time.sleep(1.5)
    return False


def fetch_models(base_url: str, api_key: str = "") -> list[str]:
    """List the model ids an OpenAI-compatible endpoint offers.

    Returns [] on any failure (unreachable, 401, no key) - callers treat an empty
    list as "could not ask", and /status reports the real reason.
    """
    if httpx is None or not base_url:
        return []
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        r = httpx.get(f"{base_url.rstrip('/')}/models", timeout=8.0, headers=headers)
        if r.status_code != 200:
            return []
        data = r.json().get("data") or []
        ids = [d.get("id") for d in data if d.get("id")]
        return sorted(ids)
    except Exception:  # noqa: BLE001 - any failure means "could not ask"
        return []


def auto_model(base_url: str, api_key: str = "") -> str:
    """The first model the endpoint offers, for the header."""
    names = fetch_models(base_url, api_key)
    return names[0] if names else ""
