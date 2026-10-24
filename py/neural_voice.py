"""Natural "AI" voices for the read-aloud button (Microsoft neural voices).

Windows ships three SAPI voices — David, Mark and Zira — and the browser can only
speak with voices the operating system has installed, so the voice picker offered
exactly those three no matter what. Windows' "Add voices" page can install a few
more, but the voices people mean by "a proper AI voice" are *neural* models, and
Microsoft serves 300+ of them through the same endpoint Edge's Read-aloud uses.

This module is the server half of that: it lists the voices and turns text into
MP3 for ``/api/tts/voices`` and ``/api/tts/speak``. It is deliberately optional
and lazy, so a frozen offline install keeps working exactly as before:

* ``edge-tts`` not installed  -> :func:`available` is False, the picker shows the
  Windows voices and says why the neural list is missing;
* no internet                 -> same, with the failure reported instead of a
  hang (the list falls back to a previously saved copy when one exists);
* ``TRIOFORGE_TTS=off``       -> switched off entirely, no import, no network.

Privacy, because the UI repeats it to the user: with a neural voice selected the
text being read leaves the machine for Microsoft's service. The Windows voices
stay local, and every failure falls back to them silently.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import threading
import time

from paths import root_path

# Voices that read any language in the list are marked "Multilingual" by the
# service; this one is the default because replies are usually English but often
# quote Malay or Chinese text, which it handles without a second voice.
DEFAULT_VOICE = "en-US-AvaMultilingualNeural"

#: Longest text accepted in one request. edge-tts splits internally, so this only
#: caps an accidental "read the whole log file" request.
MAX_TEXT = 6000

#: How long a fetched voice list is reused (seconds, memory / disk).
LIST_TTL = 3600
DISK_TTL = 7 * 24 * 3600

CACHE_PATH = root_path("json_configuration", "tts_voices.json")

# Voice ids go into SSML, which is built from an f-string inside edge-tts, so only
# the shape the service itself uses is accepted. Three or four dash-separated
# segments of letters/digits: "en-US-AvaNeural", and the longer ids the catalogue
# genuinely contains ("iu-Cans-CA-SiqiqiniNeural", "zh-CN-liaoning-XiaobeiNeural").
# Still refuses path tricks, markup and whitespace.
_VOICE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,10}){1,2}-[A-Za-z0-9]{2,40}$")

_VOICES: list[dict] | None = None
_VOICES_AT = 0.0
_LAST_ERROR = ""


class NeuralVoiceError(RuntimeError):
    """A neural voice could not be used. The message is safe to show the user."""


def _disabled_by_env() -> bool:
    raw = (os.environ.get("TRIOFORGE_TTS") or "").strip().lower()
    return raw in {"0", "off", "false", "no", "none", "disabled"}


def available() -> bool:
    """True when neural voices can be used at all (installed and not switched off)."""
    if _disabled_by_env():
        return False
    try:
        return importlib.util.find_spec("edge_tts") is not None
    except (ImportError, ValueError):
        return False


def _timeout() -> float:
    try:
        value = float(os.environ.get("TRIOFORGE_TTS_TIMEOUT") or 30)
    except (TypeError, ValueError):
        return 30.0
    return value if value > 0 else 30.0


def status() -> dict:
    """One dict the picker can show verbatim: availability and the reason why not."""
    if _disabled_by_env():
        reason = "Neural voices are switched off (TRIOFORGE_TTS=off)."
    elif not available():
        reason = ("Neural voices need the optional edge-tts package: "
                  "pip install edge-tts")
    else:
        reason = _LAST_ERROR
    voice_list = _cached_voices()
    return {
        "available": available(),
        "reason": reason,
        "count": len(voice_list),
        "default": DEFAULT_VOICE,
    }


def _friendly(short_name: str) -> str:
    """"en-US-AvaMultilingualNeural" -> "Ava (multilingual)"."""
    tail = short_name.rsplit("-", 1)[-1] if "-" in short_name else short_name
    for suffix in ("MultilingualNeural", "Neural"):
        if tail.endswith(suffix):
            tail = tail[: -len(suffix)]
            if suffix == "MultilingualNeural":
                return f"{tail} (multilingual)"
            break
    return tail or short_name


def _normalise(raw: dict) -> dict | None:
    short = (raw.get("ShortName") or "").strip()
    if not _VOICE_RE.match(short):
        return None
    gender = (raw.get("Gender") or "").strip().title()
    if gender not in {"Male", "Female"}:
        gender = ""
    locale = (raw.get("Locale") or "").strip()
    return {
        "id": short,
        "name": _friendly(short),
        "gender": gender,
        "locale": locale,
        "multilingual": "Multilingual" in short,
        "label": f"{_friendly(short)} · {locale}" if locale else _friendly(short),
    }


def _read_disk_cache() -> list[dict] | None:
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
        if time.time() - float(blob.get("saved", 0)) > DISK_TTL:
            return None
        voices = [v for v in (blob.get("voices") or []) if isinstance(v, dict) and v.get("id")]
        return voices or None
    except (OSError, ValueError, TypeError):
        return None


def _write_disk_cache(voices: list[dict]) -> None:
    """Best effort: an unwritable config folder must not fail a request."""
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as fh:
            json.dump({"saved": time.time(), "voices": voices}, fh)
    except OSError:
        pass


def _cached_voices() -> list[dict]:
    """Voice list without ever touching the network (memory, then disk)."""
    global _VOICES
    if _VOICES is None:
        _VOICES = _read_disk_cache()
    return _VOICES or []


async def _list_async() -> list[dict]:
    import edge_tts

    raw = await edge_tts.list_voices()
    voices = [v for v in (_normalise(item) for item in raw or []) if v]
    if not voices:
        raise NeuralVoiceError("The voice service returned an empty list.")
    return voices


async def _speak_async(text: str, voice: str, rate: int, pitch: int) -> bytes:
    import edge_tts

    communicate = edge_tts.Communicate(
        text,
        voice,
        rate=f"{rate:+d}%",
        pitch=f"{pitch:+d}Hz",
    )
    audio = bytearray()
    async for chunk in communicate.stream():
        if chunk.get("type") == "audio":
            audio += chunk.get("data") or b""
    if len(audio) < 256:
        raise NeuralVoiceError("The voice service returned no audio.")
    return bytes(audio)


def _run(coro, timeout: float):
    """Run a coroutine to completion with a hard deadline, from any thread.

    Flask serves requests on worker threads, so there is normally no running loop;
    if there ever is one, the work moves to its own thread instead of failing.
    """
    try:
        asyncio.get_running_loop()
        running = True
    except RuntimeError:
        running = False

    if not running:
        return asyncio.run(asyncio.wait_for(coro, timeout))

    box: dict = {}

    def _worker() -> None:
        try:
            box["value"] = asyncio.run(asyncio.wait_for(coro, timeout))
        except BaseException as exc:  # noqa: BLE001 - handed back to the caller
            box["error"] = exc

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(timeout + 5)
    if "error" in box:
        raise box["error"]
    if "value" not in box:
        raise NeuralVoiceError(f"Timed out after {int(timeout)}s.")
    return box["value"]


def voices(force: bool = False) -> list[dict]:
    """Every neural voice the service offers, cached in memory and on disk."""
    global _VOICES, _VOICES_AT, _LAST_ERROR
    if not available():
        return _cached_voices()
    if not force and _VOICES and time.time() - _VOICES_AT < LIST_TTL:
        return _VOICES
    try:
        # The list is needed the moment the page loads, so it gets a tighter budget
        # than synthesis: an offline machine should fill the picker with the cached
        # voices in seconds, not sit on the full TRIOFORGE_TTS_TIMEOUT.
        fetched = _run(_list_async(), min(_timeout(), 10.0))
    except Exception as exc:  # noqa: BLE001 - offline is normal, report it
        _LAST_ERROR = f"Could not reach the voice service ({type(exc).__name__})."
        return _cached_voices()
    _VOICES = fetched
    _VOICES_AT = time.time()
    _LAST_ERROR = ""
    _write_disk_cache(fetched)
    return fetched


def find(voice_id: str) -> dict | None:
    """Look a voice up by id, fetching the list if it has not been loaded yet."""
    for voice in voices():
        if voice["id"] == voice_id:
            return voice
    return None


def synthesize(text: str, voice: str = DEFAULT_VOICE, rate: int = 0, pitch: int = 0) -> bytes:
    """Render ``text`` to MP3 with ``voice``; raises :class:`NeuralVoiceError`."""
    if not available():
        raise NeuralVoiceError(status()["reason"] or "Neural voices are unavailable.")
    text = (text or "").strip()
    if not text:
        raise NeuralVoiceError("There is nothing to read out.")
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT]
    voice = (voice or DEFAULT_VOICE).strip()
    if not _VOICE_RE.match(voice):
        raise NeuralVoiceError(f"Not a valid neural voice id: {voice!r}")

    def _percent(value) -> int:
        """Rate/pitch as a clamped integer; a bad value means "no adjustment"."""
        try:
            number = int(value)
        except (TypeError, ValueError):
            return 0
        return max(-50, min(50, number))

    rate = _percent(rate)
    pitch = _percent(pitch)
    try:
        return _run(_speak_async(text, voice, rate, pitch), _timeout() + 15)
    except NeuralVoiceError:
        raise
    except asyncio.TimeoutError as exc:
        raise NeuralVoiceError(
            f"The voice service did not answer within {int(_timeout())}s."
        ) from exc
    except Exception as exc:  # noqa: BLE001 - network, TLS, auth, format...
        raise NeuralVoiceError(
            f"The voice service failed ({type(exc).__name__}: {exc})."
        ) from exc
