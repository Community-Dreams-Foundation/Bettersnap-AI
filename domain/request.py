"""GenerationRequest — raw customer intent.

First of the three-stage flow (ARCHITECTURE.md §3.1):
    GenerationRequest  -> [control plane resolves] -> GenerationPlan -> [GPU worker] -> GenerationJob

Produced by the API layer from the request body. It is intent ONLY: no plan rules, no
validation, no credits — the control plane resolves all of that into a GenerationPlan.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GenerationRequest:
    user_id: str
    gender: str
    age_range: str = ""
    hair_color: str = ""
    # Category-qualified refs, e.g. "business_suit.navy_suit_tie" (see shared/catalog.py).
    attire_refs: tuple[str, ...] = ()
    background_refs: tuple[str, ...] = ()
    # custom_scene mode: a user-typed scene, wrapped with identity + quality at generation.
    custom_prompt: str = ""
    # Monthly plans may request a session size within their cap; None = use plan default.
    requested_image_count: int | None = None
