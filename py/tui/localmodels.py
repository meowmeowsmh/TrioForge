"""TrioForge's own on-disk GGUF models.

The point of this module is that the terminal client offers **the same offline
files the web UI does**, instead of asking you to remember a model name.

It deliberately reuses ``llamacpp_service`` rather than re-implementing the scan,
so the two front-ends can never disagree about:

* which directories hold models       (``_model_roots``)
* what counts as a model vs a mmproj  (``_is_mmproj``)
* which projector pairs with a model  (``find_mmproj``)
* what input a model accepts          (``model_capabilities``)

The import is guarded: if the module is missing (someone runs the TUI standalone)
:func:`available` simply returns an empty list and the provider picker falls back
to the endpoint's ``/models`` list.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class LocalModel:
    name: str                       # short name, for the picker
    path: str                       # absolute .gguf path
    size_gb: float = 0.0
    caps: set[str] = field(default_factory=set)
    projector: str | None = None    # paired mmproj, for vision models

    @property
    def caps_label(self) -> str:
        order = ["text", "image", "video", "audio"]
        return "+".join(c for c in order if c in self.caps) or "text"


def _service():
    """The TrioForge service module, or None when it cannot be imported."""
    try:
        import llamacpp_service  # noqa: PLC0415 - optional, imported on demand
        return llamacpp_service
    except Exception:  # noqa: BLE001 - any failure means "not available"
        return None


def available() -> list[LocalModel]:
    """Every offline model on disk, projectors excluded, biggest first."""
    svc = _service()
    if svc is None:
        return []

    out: list[LocalModel] = []
    for path in svc._list_gguf_files():
        base = os.path.basename(path)
        if svc._is_mmproj(base):
            continue            # a vision projector is not a model on its own
        try:
            size = os.path.getsize(path) / (1024 ** 3)
        except OSError:
            size = 0.0
        try:
            caps = set(svc.model_capabilities(path) or ())
        except Exception:  # noqa: BLE001
            caps = {"text"}
        try:
            proj = svc.find_mmproj(path)
        except Exception:  # noqa: BLE001
            proj = None
        out.append(LocalModel(
            name=svc._base_gguf_name(base) or base,
            path=path,
            size_gb=size,
            caps=caps,
            projector=proj,
        ))
    out.sort(key=lambda m: -m.size_gb)
    return out


def find(name: str) -> LocalModel | None:
    """Look a model up by short name, basename or path."""
    if not name:
        return None
    target = os.path.basename(name).lower()
    for m in available():
        if target in (m.name.lower(), os.path.basename(m.path).lower()):
            return m
    for m in available():                    # fall back to a substring match
        if target in m.name.lower():
            return m
    return None


def fit_badge(size_gb: float) -> str:
    """A short "will this fit?" verdict for the picker, '' when unknown.

    Reads the same ``hardware.fit`` the ``/specs`` table uses, so the badge and the
    verdict always agree. Cached inside hardware.py, so a list of a dozen models
    costs one probe.
    """
    if size_gb <= 0:
        return ""
    try:
        import hardware
        verdict = hardware.fit(size_gb)
    except Exception:  # noqa: BLE001 - a missing hardware module is not fatal
        return ""
    return {"gpu": "fits in VRAM", "split": "split GPU+CPU (slower)",
            "cpu": "CPU only", "too_big": "too big"}.get(verdict, "")


def rows() -> list[tuple[str, str]]:
    """(name, description) pairs for the picker."""
    out = []
    for m in available():
        bits = [f"{m.size_gb:.1f} GB", m.caps_label]
        if m.projector:
            bits.append("vision projector paired")
        badge = fit_badge(m.size_gb)
        if badge:
            bits.append(badge)
        out.append((m.name, "  ·  ".join(bits)))
    return out
