# openai_api.py – serve an OpenAI-compatible API from TrioForge's local models.
#
# Why: TrioForge already manages llama-server, so any device on your network (phone,
# another PC, a script) can use your GPU instead of a paid cloud API.
#
#   GET  /v1/models             – list locally available GGUF models
#   POST /v1/chat/completions   – OpenAI-shaped chat (streaming or not)
#
# Optional auth: set TRIOFORGE_API_KEY and clients must send
#   Authorization: Bearer <key>
# Leave it unset for open access on localhost only.

import os
import logging

import requests
from flask import Blueprint, request, jsonify, Response

import llamacpp_service
from providers.llm_providers import get_provider

logger = logging.getLogger(__name__)

openai_bp = Blueprint('openai_api', __name__)


def _expected_key():
    return (os.environ.get("TRIOFORGE_API_KEY") or "").strip()


def _auth_error():
    """Return a 401 response tuple if the caller is not authorised, else None."""
    want = _expected_key()
    if not want:
        return None
    header = request.headers.get("Authorization", "") or ""
    got = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if got != want:
        return jsonify({"error": {"message": "Invalid API key",
                                  "type": "invalid_request_error",
                                  "code": "invalid_api_key"}}), 401
    return None


def _local_model_ids():
    """Model ids the local llama.cpp provider can serve."""
    try:
        return list(get_provider("llamacpp").list_models() or [])
    except Exception as exc:
        logger.warning("could not list local models: %s", exc)
        return []


@openai_bp.route('/v1/models', methods=['GET'])
def list_models():
    auth = _auth_error()
    if auth:
        return auth
    data = [{"id": m, "object": "model", "created": 0, "owned_by": "trioforge"}
            for m in _local_model_ids()]
    return jsonify({"object": "list", "data": data})


@openai_bp.route('/v1/chat/completions', methods=['POST'])
def chat_completions():
    auth = _auth_error()
    if auth:
        return auth

    payload = request.get_json(silent=True) or {}
    model = (payload.get("model") or "").strip()
    stream = bool(payload.get("stream"))

    # Make sure the local server is running for the requested model. start() only
    # spawns it, so wait for readiness before forwarding - otherwise the first
    # request after a cold start races the model load and fails.
    state = llamacpp_service.start(model=model or None)
    if state.get("error") and not state.get("running"):
        return jsonify({"error": {"message": state["error"],
                                  "type": "server_error"}}), 503
    try:
        get_provider("llamacpp")._check_server()
    except Exception as exc:
        logger.warning("llama.cpp did not report ready: %s", exc)

    if model:
        # llama-server registers models by full path, not the bare filename.
        try:
            payload["model"] = get_provider("llamacpp")._resolve_model_path(model)
        except Exception:
            pass

    url = llamacpp_service.server_url().rstrip("/") + "/chat/completions"

    try:
        if stream:
            upstream = requests.post(url, json=payload, stream=True, timeout=900)
            if upstream.status_code >= 400:
                return Response(upstream.content, status=upstream.status_code,
                                mimetype=upstream.headers.get("Content-Type",
                                                             "application/json"))

            def _relay():
                try:
                    for chunk in upstream.iter_content(chunk_size=None):
                        if chunk:
                            yield chunk
                finally:
                    upstream.close()

            return Response(_relay(), mimetype="text/event-stream",
                            headers={"Cache-Control": "no-cache",
                                     "X-Accel-Buffering": "no"})

        upstream = requests.post(url, json=payload, timeout=900)
        return Response(upstream.content, status=upstream.status_code,
                        mimetype=upstream.headers.get("Content-Type",
                                                     "application/json"))
    except Exception as exc:
        logger.error("OpenAI proxy failed: %s", exc)
        return jsonify({"error": {"message": "upstream request failed: %s" % exc,
                                  "type": "server_error"}}), 502
