"""IdentityProfile — the per-user source of truth for "who this person is".

ARCHITECTURE.md §3.2. Built ONCE per training, BEFORE any model runs, and consumed by
Personalization, Evaluation, and Selection. It outlives every generation model — swapping
DreamBooth for PhotoMaker or SDXL for Flux does not change it.

Pure Python: holds refs + scalar metadata only. The `identity_centroid` is a plain float
vector (the runtime computes it with numpy/torch and stores the plain floats here) — never a
tensor — so this stays importable in the CPU control plane.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .refs import Ref


class IdentityStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True)
class CropQuality:
    """Per-crop quality metadata from the acquisition gate (shared/crops.py::assess).
    Drives Phase 3 KPIs (reject-rate, face-px, blur) and reference selection."""
    crop: Ref
    face_px: int
    confidence: float
    eye_ratio: float | None = None
    blur: float | None = None
    warn_low_res: bool = False


@dataclass
class IdentityProfile:
    user_id: str
    reference_crops: list[Ref] = field(default_factory=list)
    crop_quality: list[CropQuality] = field(default_factory=list)
    # Embedding centroid (outlier-removed), as plain floats — commercial-safe embedder (§8).
    identity_centroid: tuple[float, ...] = ()
    # Pointer to the personalization asset (blob). None until Personalization has run.
    adapter: Ref | None = None
    version: int = 1
    status: IdentityStatus = IdentityStatus.PENDING
