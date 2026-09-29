"""Providers, API keys and model selection.

Everything lives in one JSON file (``~/.config/forge/config.json``) so a key is
entered once and remembered:

    {
      "provider": "openrouter",
      "providers": {
        "local":      {"base_url": "http://127.0.0.1:8080/v1", "api_key": ""},
        "openrouter": {"base_url": "https://openrouter.ai/api/v1",
                       "api_key": "${OPENROUTER_API_KEY}"}
      }
    }

Two deliberate decisions:

* A key may be written as ``${ENV_VAR}``. It is resolved at load time and the
  file keeps the variable name, not the secret - so a config you paste into a
  bug report does not leak the key.
* The file is created with mode 0600. :func:`check_permissions` warns if it has
  been loosened since.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("FORGE_CONFIG_DIR", Path.home() / ".config" / "forge"))
CONFIG_FILE = CONFIG_DIR / "config.json"

# Everything here speaks the OpenAI /chat/completions dialect, so one backend
# class drives them all. `models` is only a hint for the picker; the live list
# is fetched from the endpoint when it answers.
DEFAULT_PROVIDERS: dict[str, dict] = {
    # This list is exactly the set TrioForge's own providers/llm_providers.py
    # implements - no more, no less. Each "note" is the one-line answer to
    # "what is this and do I need it?" shown in the setup wizard.
    "local": {
        "base_url": "http://127.0.0.1:8080/v1",
        "api_key": "",
        "label": "Local (llama.cpp)",
        "note": "Your own .gguf files, run on this machine. No key, works offline.",
        "models": [],
    },
    "ollama": {
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key": "",
        "label": "Ollama",
        "note": "Local models managed by Ollama. No key. Install models with `ollama pull`.",
        "models": [],
    },
    "huggingface": {
        "base_url": "https://router.huggingface.co/v1",
        "api_key": "${HF_TOKEN}",
        "label": "Hugging Face",
        "note": "Hosted models from huggingface.co. Free tier available; needs an HF token.",
        "models": [],
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "api_key": "${GROQ_API_KEY}",
        "label": "Groq",
        "note": "Very fast hosted inference. Free tier. Key from console.groq.com.",
        "models": [],
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "${DEEPSEEK_API_KEY}",
        "label": "DeepSeek",
        "note": "Hosted DeepSeek models. Cheap, strong at code. Key from platform.deepseek.com.",
        "models": ["deepseek-chat", "deepseek-reasoner"],
    },
    "claude": {
        "base_url": "https://api.anthropic.com/v1",
        "api_key": "${ANTHROPIC_API_KEY}",
        "label": "Claude (Anthropic)",
        "note": "Anthropic's hosted models. Paid. Key from console.anthropic.com.",
        "models": ["claude-sonnet-4-5", "claude-opus-4-1", "claude-haiku-4-5"],
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "api_key": "${GEMINI_API_KEY}",
        "label": "Google Gemini",
        "note": "Google's hosted models. Generous free tier. Key from aistudio.google.com.",
        "models": [
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-2.0-flash",
        ],
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "${OPENROUTER_API_KEY}",
        "label": "OpenRouter",
        "note": "One key for hundreds of models from many vendors. Pay-as-you-go.",
        "models": [],
    },
}

# Providers TrioForge cannot chat with. Listed so the wizard can explain why they
# are missing instead of silently omitting them.
NOT_CHAT = {
    "comfyui": "image generation, not chat - use it from the web UI",
}

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def resolve(value: str) -> str:
    """Expand ``${VAR}`` using the environment. Unset vars become ''."""
    if not value:
        return ""
    return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)


def is_indirect(value: str) -> bool:
    """True if the stored value is an env reference rather than the literal key."""
    return bool(value and _ENV_RE.search(value))


@dataclass
class Config:
    # Populated by load() when a config written against an older provider list
    # had to be repaired - the app prints these once.
    notes: list[str] = field(default_factory=list)
    provider: str = "local"
    model: str = ""
    providers: dict[str, dict] = field(default_factory=lambda: json.loads(
        json.dumps(DEFAULT_PROVIDERS)))

    # ---------------------------------------------------------------- lookups
    @property
    def current(self) -> dict:
        return self.providers.setdefault(
            self.provider, {"base_url": "", "api_key": "", "models": []})

    @property
    def base_url(self) -> str:
        return self.current.get("base_url", "")

    @property
    def api_key(self) -> str:
        """The RESOLVED key (env expanded), for sending to the server."""
        return resolve(self.current.get("api_key", ""))

    @property
    def raw_key(self) -> str:
        """What is stored on disk - possibly ``${VAR}``."""
        return self.current.get("api_key", "")

    def has_key(self) -> bool:
        return bool(self.api_key)

    # ---------------------------------------------------------------- updates
    def set_key(self, key: str) -> None:
        self.current["api_key"] = key

    def set_base_url(self, url: str) -> None:
        self.current["base_url"] = url

    def add_provider(self, name: str, base_url: str, api_key: str = "") -> None:
        self.providers[name] = {
            "base_url": base_url, "api_key": api_key,
            "label": name, "models": [],
        }
        self.provider = name

    def set_models(self, names: list[str]) -> None:
        self.current["models"] = names


# Providers an earlier version of this file invented. TrioForge has no such
# backend, so leaving them in the picker would offer something that cannot work.
# Providers this file offered that TrioForge has NO chat backend for. Offering
# them meant "pick a provider, then discover it cannot work" - exactly the
# confusion this list exists to avoid.
#
#   together / mistral / xai  - I invented them; TrioForge never had them.
#   openai                    - TrioForge has no OpenAI chat provider. The
#                               openai_api.py feature is the OPPOSITE: TrioForge
#                               SERVING an OpenAI-shaped API from your local
#                               models. Re-add with /provider-add if ever needed.
INVENTED = {"together", "mistral", "xai", "openai"}


def _refresh_docs(providers: dict) -> bool:
    """Overwrite label/note from the built-in defaults.

    label and note are DOCUMENTATION, not user data: if this file is corrected
    (say a provider's description was wrong), a config written earlier should not
    keep showing the stale wording. Keys, base_url and models are left alone.
    """
    changed = False
    for name, defaults in DEFAULT_PROVIDERS.items():
        entry = providers.get(name)
        if not entry:
            continue
        for k in ("label", "note"):
            new = defaults.get(k)
            if new and entry.get(k) != new:
                entry[k] = new
                changed = True
    return changed


def _migrate(providers: dict) -> list[str]:
    """Fix up a config written against the old, wrong provider list.

    * ``anthropic`` -> ``claude`` (TrioForge's own name for it)
    * drop providers TrioForge has no backend for
    Returns a list of human-readable notes about what changed.
    """
    notes: list[str] = []

    if "anthropic" in providers:
        old = providers.pop("anthropic")
        if "claude" not in providers:
            providers["claude"] = old
            notes.append("renamed provider 'anthropic' -> 'claude'")
        elif old.get("api_key") and not providers["claude"].get("api_key"):
            providers["claude"]["api_key"] = old["api_key"]
            notes.append("moved the anthropic key -> claude")

    for name in INVENTED:
        if name in providers:
            providers.pop(name)
            notes.append(f"removed provider {name!r} (not a TrioForge backend)")

    return notes


def load(path: Path | None = None) -> Config:
    """Read the config, filling in any provider the file is missing."""
    path = path or CONFIG_FILE
    data: dict = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}

    cfg = Config(
        provider=data.get("provider", "local"),
        model=data.get("model", ""),
    )
    merged = json.loads(json.dumps(DEFAULT_PROVIDERS))
    for name, entry in (data.get("providers") or {}).items():
        merged.setdefault(name, {"base_url": "", "api_key": "", "models": []})
        merged[name].update(entry)
    cfg.providers = merged
    docs_changed = _refresh_docs(cfg.providers)
    notes = _migrate(cfg.providers)
    if cfg.provider not in cfg.providers:
        cfg.provider = next(iter(cfg.providers))

    # Self-healing: if the migration changed anything, write it back so the fix
    # only ever happens once and the file matches what the picker shows.
    if notes or docs_changed:
        cfg.notes = notes
        try:
            save(cfg, path)
        except OSError:
            pass
    return cfg


def save(cfg: Config, path: Path | None = None) -> Path:
    """Write the config 0600 - it may hold API keys."""
    path = path or CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "provider": cfg.provider,
        "model": cfg.model,
        "providers": cfg.providers,
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return path


def check_permissions(path: Path | None = None) -> str | None:
    """Return a warning string if the config is readable by others."""
    path = path or CONFIG_FILE
    if not path.is_file():
        return None
    try:
        mode = path.stat().st_mode
    except OSError:
        return None
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        return f"{path} is readable by other users (chmod 600 it)"
    return None


def masked(raw: str) -> str:
    """A safe-to-print view of a stored key.

    Shows enough to recognise WHICH key it is (last 4), never enough to use it.
    Env references are shown as-is - they are not secrets.
    """
    if not raw:
        return "(none)"
    if is_indirect(raw):
        return raw + ("  ·  resolved" if resolve(raw) else "  ·  NOT set in env")
    if len(raw) <= 4:
        return "•" * len(raw)
    return "•" * 8 + raw[-4:]


def key_state(cfg: Config, name: str | None = None) -> str:
    """One-line summary of a provider's key, for tables and prompts."""
    entry = (cfg.providers.get(name) if name else cfg.current) or {}
    raw = entry.get("api_key", "")
    if not raw:
        return "no key"
    if is_indirect(raw):
        return f"{raw} (env {'set' if resolve(raw) else 'unset'})"
    return f"saved · {masked(raw)}"


def describe(cfg: Config) -> list[tuple[str, str]]:
    """Rows for the /provider table."""
    rows: list[tuple[str, str]] = []
    for name, entry in sorted(cfg.providers.items()):
        mark = "▶" if name == cfg.provider else " "
        key = entry.get("api_key", "")
        if not key:
            state = "no key"
        elif is_indirect(key) and not resolve(key):
            state = f"{key} (unset)"
        elif is_indirect(key):
            state = f"{key} (resolved)"
        else:
            state = "key set"
        note = entry.get("note") or entry.get("base_url", "")
        rows.append((f"{mark} {name}", f"{state}  ·  {note}"))
    return rows
