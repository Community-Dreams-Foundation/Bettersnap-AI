"""Commercial-safe face identity embedder — interface + math helpers (ARCHITECTURE.md §8).

The Quality Gate (Phase 6) scores identity as cosine similarity between a candidate's face
embedding and the user's identity centroid. The EMBEDDER is pluggable so the (legally
shippable) model choice is decoupled from the gate logic: the gate is model-free and unit-
testable with a synthetic embedder, and the real Apache-2.0 ArcFace model plugs in behind
`FaceEmbedder` without touching any engine.

buffalo_l (InsightFace) is NON-COMMERCIAL and must NOT be used in-product — see
SCORER_COMPARISON.md for the permissive options. `load_default_embedder()` deliberately
raises until a commercial embedder is chosen and integrated, so nothing can ship an
unlicensed scorer by accident.
"""
from __future__ import annotations

import math
from typing import Optional, Protocol, Sequence, runtime_checkable


@runtime_checkable
class FaceEmbedder(Protocol):
    """A commercial-safe face embedder. `embed` returns an identity vector for the largest
    face in `image` (a PIL image), or None if no usable face is found. Implementations own
    all torch/onnx; the gate never imports a model."""

    def embed(self, image) -> Optional[Sequence[float]]: ...


def cosine(a: Optional[Sequence[float]], b: Optional[Sequence[float]]) -> float:
    """Cosine similarity of two vectors. Robust to non-normalized input; returns 0.0 if either
    is missing, empty, length-mismatched, or a zero vector."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def centroid(vectors: Sequence[Optional[Sequence[float]]]) -> Optional[list[float]]:
    """Mean of the (non-None) vectors, L2-normalized. None if there are none. Outlier removal
    is the training-time profile's job; this is the plain fallback mean used when a profile
    doesn't carry a precomputed identity_centroid."""
    vecs = [v for v in vectors if v]
    if not vecs:
        return None
    dim = len(vecs[0])
    acc = [0.0] * dim
    for v in vecs:
        if len(v) != dim:
            continue
        for i in range(dim):
            acc[i] += v[i]
    n = len(vecs)
    mean = [x / n for x in acc]
    norm = math.sqrt(sum(x * x for x in mean))
    return [x / norm for x in mean] if norm else mean


def load_default_embedder() -> FaceEmbedder:
    """Return the configured commercial-safe embedder. Intentionally NOT implemented until a
    scorer is chosen (see SCORER_COMPARISON.md) — this keeps the Quality Gate wired but inert,
    and guarantees an unlicensed model can never be shipped by default."""
    raise NotImplementedError(
        "No commercial-safe face embedder is integrated yet. Pick one from "
        "SCORER_COMPARISON.md (recommended: Apache-2.0 ArcFace ResNet100 ONNX) and implement "
        "FaceEmbedder here. buffalo_l is non-commercial and must not be used in-product."
    )
