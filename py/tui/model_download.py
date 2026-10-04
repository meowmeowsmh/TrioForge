"""Download GGUF models from Hugging Face into ``models/``, for the terminal client.

The web UI already has a ``POST /api/models/download`` that pulls a repo/file via
``huggingface_hub``; this is the terminal's equivalent, plus the search half the
web UI never had. A model pulled here and one pulled in the browser land in the
same folder, so llama.cpp sees them identically.
"""

from __future__ import annotations

import os

from paths import root_path

_GB = 1024 ** 3


def _api():
    from huggingface_hub import HfApi
    return HfApi()


def search(query: str, limit: int = 20) -> list[tuple[str, int]]:
    """[(repo_id, downloads)] of GGUF repos matching ``query``, most-downloaded first.

    Returns [] (never raises) so a network or library failure reads as "no results"
    and the caller can show a plain message.
    """
    query = (query or "").strip()
    if not query:
        return []
    out: list[tuple[str, int]] = []
    try:
        # "gguf" in the search text is the reliable filter here: HF has no
        # ``library`` keyword on this version of list_models, and GGUF repos
        # (bartowski/*-GGUF and friends) carry it in the name or tags.
        for info in _api().list_models(search=f"{query} gguf",
                                       sort="downloads", limit=limit):
            out.append((info.modelId, int(getattr(info, "downloads", 0) or 0)))
    except Exception:
        return out
    return out


def files(repo_id: str) -> list[tuple[str, float]]:
    """[(filename, size_gb)] of the GGUF/projector files in a repo.

    The model GGUF first, then any mmproj/clip projector; within each group the
    biggest file first, because a higher quant is usually the one a person wants.
    """
    out: list[tuple[str, float]] = []
    try:
        info = _api().model_info(repo_id, files_metadata=True)
        for sib in (info.siblings or []):
            name = getattr(sib, "rfilename", "") or ""
            if not name.lower().endswith((".gguf", ".safetensors")):
                continue
            size = int(getattr(sib, "size", 0) or 0)
            out.append((name, size / _GB))
    except Exception:
        return out
    out.sort(key=lambda item: (0 if ("mmproj" in item[0].lower() or "clip" in item[0].lower()) else 1,
                               -item[1]))
    return out


def download(repo_id: str, filename: str) -> str:
    """Download one file into ``models/`` and return its absolute path. Raises on error."""
    from huggingface_hub import hf_hub_download

    models_dir = root_path("models")
    os.makedirs(models_dir, exist_ok=True)
    return hf_hub_download(repo_id=repo_id, filename=filename,
                           local_dir=models_dir)
