"""Face-embedding interface for the evaluation harness.

LICENSING — READ BEFORE SHIPPING ANYTHING:
InsightFace's *code* is MIT, but its downloadable pretrained models (ArcFace / buffalo_*)
are licensed for NON-COMMERCIAL research only. So `InsightFaceEmbedder` is for LOCAL
PROTOTYPING of this harness — NOT for the paid product. Before production: select a
COMMERCIALLY-licensed face-embedding model, validate its demographic performance on your
own data, and implement `FaceEmbedder` for it. The harness is model-agnostic on purpose so
that swap is a one-class change. Nothing in this module is imported by the GPU image or the
Functions app.
"""
from typing import Optional

import numpy as np


class FaceEmbedder:
    """A face -> unit-ish embedding vector. Implementations return None when no usable
    face is found (caller decides what an un-embeddable image means)."""

    name = "abstract"

    def embed(self, image_bgr) -> Optional[np.ndarray]:
        raise NotImplementedError


class InsightFaceEmbedder(FaceEmbedder):
    """RESEARCH-ONLY prototype embedder (see module licensing note — do NOT ship).

    Lazily imports insightface so the harness and its tests import without the package or
    its non-commercial model weights present."""

    name = "insightface(research-only)"

    def __init__(self, model_name: str = "buffalo_l", det_size=(640, 640)):
        self._app = None
        self._model_name = model_name
        self._det_size = det_size

    def _ensure(self):
        if self._app is None:
            from insightface.app import FaceAnalysis
            app = FaceAnalysis(name=self._model_name)
            app.prepare(ctx_id=-1, det_size=self._det_size)   # ctx_id=-1 => CPU
            self._app = app

    def embed(self, image_bgr):
        self._ensure()
        faces = self._app.get(image_bgr)
        if not faces:
            return None
        # largest face = the subject
        f = max(faces, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1]))
        return np.asarray(f.normed_embedding, dtype=np.float64)


def available() -> bool:
    """True if a real (research) embedder can actually run here. Lets the harness degrade
    to logic-only mode (composition + sharpness) when no embedder is installed."""
    import importlib.util
    return importlib.util.find_spec("insightface") is not None
