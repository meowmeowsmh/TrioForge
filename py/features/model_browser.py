# model_browser.py – find GGUF models on Hugging Face from inside TrioForge.
#
# The actual download already exists (/api/models/download, which pulls the file
# plus an optional mmproj). What was missing was discovery: you had to already know
# the exact repo id and filename. This adds:
#
#   GET /api/models/search?q=...        – search GGUF repos, easiest to hardest to run
#   GET /api/models/files?repo_id=...   – list the .gguf files inside one repo
#   GET /api/models/recommended         – a few known-good small models for 8 GB VRAM

import re
import logging

import requests
from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)

models_bp = Blueprint('model_browser', __name__, url_prefix='/api/models')

HF_API = "https://huggingface.co/api"
TIMEOUT = 20

# Curated starting points: good quality-per-GB for an 8 GB laptop GPU.
RECOMMENDED = [
    {"repo_id": "bartowski/Qwen2.5-7B-Instruct-GGUF",
     "note": "Qwen2.5 7B Instruct - solid all-round chat, fits 8 GB at Q4_K_M"},
    {"repo_id": "bartowski/Meta-Llama-3.1-8B-Instruct-GGUF",
     "note": "Llama 3.1 8B Instruct - strong general assistant"},
    {"repo_id": "bartowski/Qwen2.5-Coder-7B-Instruct-GGUF",
     "note": "Qwen2.5 Coder 7B - good for the C++ / coding work"},
    {"repo_id": "bartowski/gemma-2-9b-it-GGUF",
     "note": "Gemma 2 9B - similar to the Gemma you already run"},
    {"repo_id": "bartowski/Qwen2.5-VL-7B-Instruct-GGUF",
     "note": "Qwen2.5 VL 7B - vision, pairs with its mmproj for screenshots"},
]


def _quant_rank(name):
    """Lower is better: prefer Q4_K_M (best size/quality), then Q5/Q6, then others."""
    order = {"Q4_K_M": 0, "Q4_K_S": 1, "Q5_K_M": 2, "Q5_K_S": 3,
             "Q6_K": 4, "Q8_0": 5, "Q3_K_M": 6, "Q3_K_S": 7, "Q2_K": 8, "F16": 9, "F32": 10}
    for key, rank in order.items():
        if key.lower() in name.lower():
            return rank
    return 50


def _sizes_from(repo_id):
    """Map filename -> size in bytes for a repo ({} if unavailable)."""
    try:
        r = requests.get("%s/models/%s" % (HF_API, repo_id),
                         params={"blobs": "true"}, timeout=TIMEOUT)
        if r.status_code != 200:
            return {}
        out = {}
        for sib in (r.json().get("siblings") or []):
            name = sib.get("rfilename") or ""
            if "size" in sib:
                out[name] = sib["size"]
        return out
    except Exception as exc:
        logger.warning("could not read file list for %s: %s", repo_id, exc)
        return {}


def _gguf_files(repo_id):
    """All .gguf files in a repo, split into models and projectors."""
    sizes = _sizes_from(repo_id)
    if not sizes:
        # Fall back to the plain tree listing (no sizes).
        try:
            r = requests.get("%s/models/%s/tree/main" % (HF_API, repo_id),
                             timeout=TIMEOUT)
            if r.status_code == 200:
                sizes = {e.get("path", ""): e.get("size", 0)
                         for e in r.json() if isinstance(e, dict)}
        except Exception:
            pass
    models, mmprojs = [], []
    for name, size in sizes.items():
        if not name.lower().endswith(".gguf"):
            continue
        entry = {"filename": name, "size": size,
                 "size_gb": round((size or 0) / 1073741824, 2)}
        (mmprojs if "mmproj" in name.lower() else models).append(entry)
    models.sort(key=lambda e: (_quant_rank(e["filename"]), e["size"] or 0))
    mmprojs.sort(key=lambda e: e["filename"])
    return models, mmprojs


# ---------------------------------------------------------------- hardware fit
# A GGUF needs its file size plus room for the KV cache and compute buffers.
# We reserve ~1.5 GB, and 12% for the runtime's own overhead.
KV_OVERHEAD_GB = 1.5
FILE_OVERHEAD = 1.12


try:
    import hardware as _hw          # top-level module under py/
except ImportError:                 # pragma: no cover - only when run out of tree
    _hw = None


def hardware():
    """Detect GPU/RAM so recommendations can be tailored to THIS machine.

    The detection itself lives in hardware.py, because it has to understand
    Apple unified memory and AMD/Intel parts, not just NVML. This is the web
    API's view of the same numbers the terminal client shows.
    """
    if _hw is None:
        return {"vram_free_gb": 0.0, "vram_total_gb": 0.0,
                "ram_free_gb": 0.0, "ram_total_gb": 0.0}
    return _hw.specs()


def fit_for(size_gb, hw=None):
    """How well a model of this size will run here.

    gpu     - fits entirely in GPU memory (fast)
    split   - some layers on the GPU, the rest in RAM (usable, slower)
    cpu     - fits in RAM only (slow)
    too_big - will not fit at all without more memory
    """
    if _hw is None:
        return "cpu"
    return _hw.fit(size_gb, hw)


def fit_label(verdict, hw=None):
    """A fit label that names the actual GPU when there is one."""
    if _hw is None:
        return FIT_LABEL.get(verdict, verdict)
    return _hw.fit_label(verdict, hw)


FIT_LABEL = {
    "gpu": "Fast - fits entirely in your GPU memory",
    "split": "OK - runs partly on GPU, partly in RAM (slower)",
    "cpu": "Slow - RAM only, no GPU offload",
    "too_big": "Too big for your memory right now",
}

@models_bp.route('/search', methods=['GET'])
def search():
    query = (request.args.get('q') or '').strip()
    if not query:
        return jsonify({"error": "q is required"}), 400
    try:
        limit = max(1, min(int(request.args.get('limit', 20)), 50))
    except ValueError:
        limit = 20
    try:
        r = requests.get(HF_API + "/models",
                         params={"search": query, "filter": "gguf",
                                 "limit": limit, "sort": "downloads",
                                 "direction": -1},
                         timeout=TIMEOUT)
        r.raise_for_status()
        raw = r.json()
    except Exception as exc:
        return jsonify({"error": "Hugging Face search failed: %s" % exc}), 502

    results = []
    for item in raw if isinstance(raw, list) else []:
        results.append({
            "repo_id": item.get("id") or item.get("modelId") or "",
            "downloads": item.get("downloads") or 0,
            "likes": item.get("likes") or 0,
            "updated": (item.get("lastModified") or "")[:10],
        })
    return jsonify({"query": query, "results": results})


@models_bp.route('/files', methods=['GET'])
def files():
    repo_id = (request.args.get('repo_id') or '').strip()
    if not repo_id or not re.match(r'^[\w.\-]+/[\w.\-]+$', repo_id):
        return jsonify({"error": "a valid repo_id like 'bartowski/Qwen2.5-7B-Instruct-GGUF' is required"}), 400
    models, mmprojs = _gguf_files(repo_id)
    if not models and not mmprojs:
        return jsonify({"error": "no .gguf files found in %s" % repo_id}), 404
    hw = hardware()
    for entry in models + mmprojs:
        entry["fit"] = fit_for(entry.get("size_gb") or 0, hw)
        entry["fit_label"] = fit_label(entry["fit"], hw)
    return jsonify({"repo_id": repo_id, "models": models, "mmproj": mmprojs,
                    "hardware": hw})


@models_bp.route('/recommended', methods=['GET'])
def recommended():
    """Curated picks annotated with how they will run on THIS machine."""
    hw = hardware()
    out = []
    for item in RECOMMENDED:
        entry = dict(item)
        # 7-8B at Q4_K_M is ~4.6 GB; use that as the yardstick for the pick.
        entry["fit"] = fit_for(4.6, hw)
        entry["fit_label"] = fit_label(entry["fit"], hw)
        out.append(entry)
    return jsonify({"recommended": out, "hardware": hw})


@models_bp.route('/hardware', methods=['GET'])
def hardware_route():
    """What TrioForge thinks this machine has free."""
    hw = hardware()
    hw["advice"] = ("Pick a model marked 'gpu' for the fastest replies; "
                    "'split' still works but is slower.")
    return jsonify(hw)
