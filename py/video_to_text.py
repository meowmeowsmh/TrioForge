"""
video_to_text.py — video → frames for TrioForge vision models.

Turns an uploaded video into a small set of JPEG frames that a vision model can
read (gemma-4 / Qwen-VL via llama.cpp, Ollama, etc.). Uses ffmpeg, which is
auto-detected on PATH (nothing hard-coded); if ffmpeg isn't installed the module
returns empty and the caller falls back to a "no video support" note.

A short video is sampled at a fixed number of evenly-spaced frames; each frame is
returned as {"b64": <base64 jpeg>, "name": <frame-N.jpg>, "mime": "image/jpeg"}
so it can be fed straight into the existing image-vision path.
"""

import base64
import logging
import os
import shutil
import subprocess
import tempfile
import time

logger = logging.getLogger(__name__)

# Max frames to sample (keeps requests small; most vision models cap ~a few images).
MAX_FRAMES = 6

# Audio file extensions the app can feed to audio-capable models (gemma-4 E2B/E4B/12B).
# Detection is mime-first (audio/*), falling back to this list when mime is missing.
_AUDIO_EXTS = {
    ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus",
    ".flac", ".wma", ".aiff", ".aif", ".amr", ".caf", ".webm",
}

# gemma-4 audio input: 16 kHz mono WAV, maximum 30 seconds.
AUDIO_MAX_SECONDS = 30


def is_audio_file(name: str, mime: str = "") -> bool:
    """True if a file is an audio clip, detected by MIME type or extension."""
    if mime and mime.lower().startswith("audio/"):
        return True
    ext = os.path.splitext(name or "")[1].lower()
    return ext in _AUDIO_EXTS


_FFMPEG_CACHE = []          # [path] once resolved, so we probe the version only once

# Why the most recent conversion/extraction produced nothing. Callers report this
# instead of assuming "ffmpeg is missing" - which was wrong every time a current
# ffmpeg was installed but the file, codec or audio track was the real problem.
LAST_ERROR = ""


def last_error():
    """A human-readable reason for the most recent failure ("" when there was none)."""
    return LAST_ERROR


def _fail(message, *args):
    """Record + log why a conversion produced nothing (and return None)."""
    global LAST_ERROR
    try:
        text = message.format(*args) if args else message
    except Exception:
        text = str(message)
    LAST_ERROR = text
    logger.warning("%s", text)
    return None


def find_ffmpeg():
    """Return the ffmpeg executable to use, or None.

    Order matters. A very old ffmpeg found first on PATH used to win, and an old
    build silently fails on modern containers/codecs - which surfaced as a
    misleading "is ffmpeg installed?" error. So:

      1. TRIOFORGE_FFMPEG (explicit override)
      2. the build this app installed itself: tools/ffmpeg (see ffmpeg_installer)
      3. ffmpeg on PATH
      4. common per-user locations

    The chosen path and its version are logged, so the log says which build ran.
    """
    if _FFMPEG_CACHE:
        return _FFMPEG_CACHE[0]

    found = None
    override = os.environ.get("TRIOFORGE_FFMPEG", "").strip()
    if override and os.path.isfile(override):
        found = override

    if not found:
        try:
            import ffmpeg_installer
            found = ffmpeg_installer.find_installed()
        except Exception:
            found = None

    if not found:
        found = shutil.which("ffmpeg")

    if not found:
        # Common Windows locations (so we don't hard-code a single absolute path,
        # but still find it if it isn't on PATH).
        candidates = [
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\ffmpeg\bin\ffmpeg.exe"),
            os.path.expandvars(r"%APPDATA%\Python\Python314\Scripts\ffmpeg.exe"),
            os.path.expandvars(r"%APPDATA%\Python\Python313\Scripts\ffmpeg.exe"),
            os.path.expandvars(r"%APPDATA%\Python\Python312\Scripts\ffmpeg.exe"),
        ]
        for c in candidates:
            if c and os.path.isfile(c):
                found = c
                break

    if not found:
        return None

    _FFMPEG_CACHE.append(found)
    try:
        import subprocess
        r = subprocess.run([found, "-version"], capture_output=True, timeout=20)
        first = (r.stdout or b"").decode("utf-8", "replace").splitlines()
        logger.info("ffmpeg: %s (%s)", found, first[0].strip() if first else "version unknown")
    except Exception:
        logger.info("ffmpeg: %s", found)
    return found


def ffmpeg_version(path=None):
    """First line of ``ffmpeg -version`` for `path` (or the chosen ffmpeg), or ""."""
    import subprocess
    exe = path or find_ffmpeg()
    if not exe:
        return ""
    try:
        r = subprocess.run([exe, "-version"], capture_output=True, timeout=20)
        lines = (r.stdout or b"").decode("utf-8", "replace").splitlines()
        return lines[0].strip() if lines else ""
    except Exception:
        return ""


def _decode_video(b64):
    """Decode a base64 data-URI or raw base64 string into bytes."""
    if not b64:
        return None
    b64 = b64.strip()
    if "," in b64:
        b64 = b64.split(",", 1)[1]
    try:
        return base64.b64decode(b64)
    except Exception as e:
        logger.warning("Video base64 decode failed: %s", e)
        return None


def extract_frames(video_b64, max_frames=MAX_FRAMES):
    """Return a list of frame dicts ({b64,name,mime}) sampled from a video.

    Returns [] if ffmpeg is unavailable, the video can't be decoded, or no frames
    could be produced. Never raises — callers just fall back to "no video support".
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        logger.warning("ffmpeg not found; video-to-text unavailable.")
        return []
    data = _decode_video(video_b64)
    if not data:
        return []

    tmpdir = tempfile.mkdtemp(prefix="trioforge_video_")
    in_path = os.path.join(tmpdir, "input.mp4")
    try:
        with open(in_path, "wb") as f:
            f.write(data)

        # Probe duration so we can sample evenly across the whole clip.
        dur = _probe_duration(ffmpeg, in_path)
        n = max(1, min(max_frames, dur if dur and dur >= 1 else 1))
        n = int(n)

        frames = []
        for i in range(n):
            t = (i + 0.5) * (dur / n) if (dur and dur > 0) else 0
            out_path = os.path.join(tmpdir, f"frame_{i:02d}.jpg")
            cmd = [
                ffmpeg, "-y", "-ss", f"{t:.3f}", "-i", in_path,
                "-frames:v", "1", "-q:v", "3", out_path,
            ]
            r = subprocess.run(cmd, capture_output=True, timeout=60)
            if r.returncode == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
                with open(out_path, "rb") as fh:
                    frames.append({
                        "b64": base64.b64encode(fh.read()).decode("ascii"),
                        "name": f"video-frame-{i+1}.jpg",
                        "mime": "image/jpeg",
                    })
        return frames
    except Exception as e:
        logger.error("Video frame extraction failed: %s", e)
        return []
    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


def audio_to_wav_b64(audio_b64, name="audio", max_seconds=AUDIO_MAX_SECONDS):
    """Convert an audio clip to 16 kHz mono PCM WAV and return its raw base64.

    Gemma-4 (E2B/E4B/12B) audio input requires 16 kHz mono WAV, capped at 30 s, so
    we convert with ffmpeg (auto-detected on PATH, nothing hard-coded). Returns the
    base64 WITHOUT a data-URI prefix (llama.cpp's ``input_audio.data`` wants it raw),
    or None if ffmpeg is missing / conversion fails. Never raises.
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return _fail("No ffmpeg found - install it from the Services panel "
                     "(or set TRIOFORGE_FFMPEG) so audio can be converted.")
    data = _decode_video(audio_b64)
    if not data:
        return _fail("The attached audio could not be decoded (empty or invalid base64).")

    ext = os.path.splitext(name or "")[1].lower() or ".bin"
    tmpdir = tempfile.mkdtemp(prefix="trioforge_audio_")
    in_path = os.path.join(tmpdir, "input" + ext)
    out_path = os.path.join(tmpdir, "out.wav")
    try:
        with open(in_path, "wb") as f:
            f.write(data)
        cmd = [
            ffmpeg, "-y", "-i", in_path,
            "-ar", "16000", "-ac", "1", "-t", str(max_seconds),
            "-c:a", "pcm_s16le", "-f", "wav", out_path,
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        if r.returncode != 0 or not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
            tail = (r.stderr or b"")[-300:].decode("utf-8", "replace").strip()
            return _fail("ffmpeg could not read '{}' ({}). ffmpeg said: {}",
                         name, ffmpeg, tail.replace("\n", " ") or "no output")
        with open(out_path, "rb") as fh:
            return base64.b64encode(fh.read()).decode("ascii")
    except Exception as e:
        return _fail("Audio conversion crashed: {}: {}", type(e).__name__, e)
    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


def _chunk_wav_file(ffmpeg, wav_path, chunk_seconds=AUDIO_MAX_SECONDS):
    """Split a 16 kHz mono WAV on disk into <= chunk_seconds base64 WAV chunks.

    Uses probe-duration + ``-ss``/``-t`` per chunk (the same pattern as
    extract_frames) so it works on any ffmpeg build. Returns a list of raw base64
    strings (possibly empty); never raises.
    """
    try:
        dur = _probe_duration(ffmpeg, wav_path)
        if not dur or dur <= 0:
            dur = chunk_seconds  # unknown → assume one chunk (whole clip)
        tmpdir = os.path.dirname(wav_path)
        chunks = []
        n = max(1, int((dur + chunk_seconds - 1) // chunk_seconds))
        for i in range(n):
            start = i * chunk_seconds
            seg = min(chunk_seconds, dur - start)
            if seg <= 0:
                break
            out_path = os.path.join(tmpdir, f"chunk_{i:03d}.wav")
            cmd = [
                ffmpeg, "-y", "-ss", f"{start:.3f}", "-i", wav_path,
                "-t", f"{seg:.3f}",
                "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
                out_path,
            ]
            r = subprocess.run(cmd, capture_output=True, timeout=120)
            if r.returncode == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
                with open(out_path, "rb") as fh:
                    chunks.append(base64.b64encode(fh.read()).decode("ascii"))
        return chunks
    except Exception as e:
        logger.error("WAV chunking failed: %s", e)
        return []


def audio_to_wav_chunks(audio_b64, name="audio", chunk_seconds=AUDIO_MAX_SECONDS):
    """Convert audio to 16 kHz mono WAV and split it into <= chunk_seconds parts.

    Gemma-4 audio input is capped at 30 s per clip, so long audio (a full song) is
    split into sequential segments, each returned as its own raw base64 WAV string.
    Returns a list of base64 strings (possibly empty on failure); never raises.
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        _fail("No ffmpeg found - install it from the Services panel "
              "(or set TRIOFORGE_FFMPEG) so audio can be converted.")
        return []
    data = _decode_video(audio_b64)
    if not data:
        _fail("The attached audio could not be decoded (empty or invalid base64).")
        return []

    ext = os.path.splitext(name or "")[1].lower() or ".bin"
    tmpdir = tempfile.mkdtemp(prefix="trioforge_audio_")
    in_path = os.path.join(tmpdir, "input" + ext)
    wav_path = os.path.join(tmpdir, "audio.wav")
    try:
        with open(in_path, "wb") as f:
            f.write(data)
        cmd = [
            ffmpeg, "-y", "-i", in_path,
            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav_path,
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=300)
        if r.returncode != 0 or not os.path.isfile(wav_path) or os.path.getsize(wav_path) == 0:
            tail = (r.stderr or b"")[-300:].decode("utf-8", "replace").strip()
            _fail("ffmpeg could not read '{}' ({}). ffmpeg said: {}",
                  name, ffmpeg, tail.replace("\n", " ") or "no output")
            return []
        return _chunk_wav_file(ffmpeg, wav_path, chunk_seconds)
    except Exception as e:
        _fail("Audio conversion crashed: {}: {}", type(e).__name__, e)
        return []
    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


def extract_audio_chunks(video_b64, name="video", chunk_seconds=AUDIO_MAX_SECONDS):
    """Extract the AUDIO track from a video (mp4/mov/webm) and split it into
    <= chunk_seconds 16 kHz mono WAV segments for speech-to-text.

    This is the "video-to-text reads the audio" path: ffmpeg drops the video stream
    (``-vn``) and keeps only the soundtrack, which is then chunked like any clip.
    Returns a list of raw base64 WAV strings ([] if the video has no audio track);
    never raises.
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        _fail("No ffmpeg found - install it from the Services panel "
              "(or set TRIOFORGE_FFMPEG) so a video's audio can be read.")
        return []
    data = _decode_video(video_b64)
    if not data:
        _fail("The attached video could not be decoded (empty or invalid base64).")
        return []

    ext = os.path.splitext(name or "")[1].lower() or ".mp4"
    tmpdir = tempfile.mkdtemp(prefix="trioforge_vaudio_")
    in_path = os.path.join(tmpdir, "input" + ext)
    wav_path = os.path.join(tmpdir, "audio.wav")
    try:
        with open(in_path, "wb") as f:
            f.write(data)
        cmd = [
            ffmpeg, "-y", "-i", in_path, "-vn",
            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav_path,
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=300)
        if r.returncode != 0 or not os.path.isfile(wav_path) or os.path.getsize(wav_path) == 0:
            tail = (r.stderr or b"")[-300:].decode("utf-8", "replace").strip()
            low = tail.lower()
            if "does not contain any stream" in low or "no audio" in low or "matches no streams" in low:
                _fail("'{}' has no audio track, so there is nothing to transcribe.", name)
            else:
                _fail("ffmpeg could not read the audio of '{}' ({}). ffmpeg said: {}",
                      name, ffmpeg, tail.replace("\n", " ") or "no output")
            return []
        return _chunk_wav_file(ffmpeg, wav_path, chunk_seconds)
    except Exception as e:
        _fail("Video audio extraction crashed: {}: {}", type(e).__name__, e)
        return []
    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


def _probe_duration(ffmpeg, path):
    """Return video duration in seconds (float) or None."""
    import re
    cmd = [ffmpeg, "-i", path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        # ffmpeg prints duration to stderr: "Duration: 00:00:02.00, ..."
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
        if m:
            h, mi, s = m.group(1), m.group(2), m.group(3)
            return int(h) * 3600 + int(mi) * 60 + float(s)
    except Exception:
        pass
    return None
