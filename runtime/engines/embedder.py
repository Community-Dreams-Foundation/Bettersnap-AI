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


# ArcFace canonical 5-point template for a 112x112 aligned face (insightface arcface_dst),
# order: [left_eye, right_eye, nose, left_mouth, right_mouth].
_ARCFACE_DST = (
    (38.2946, 51.6963), (73.5318, 51.5014), (56.0252, 71.7366),
    (41.5493, 92.3655), (70.7299, 92.2041),
)


def _default_yunet_path() -> str:
    import os
    env = os.environ.get("YUNET_ONNX_PATH")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))  # repo root
    return os.path.join(root, "Bettersnap-aI_Backend", "shared", "models",
                        "face_detection_yunet_2023mar.onnx")


class ArcFaceOnnxEmbedder:
    """Apache-2.0 ArcFace ResNet100 (ONNX) identity embedder — the commercial-safe replacement
    for buffalo_l. YuNet-detects the largest face, 5-point aligns it to 112x112, runs the ONNX
    session, and returns an L2-normalized 512-d vector. Heavy deps (onnxruntime / cv2 / numpy)
    are imported LAZILY so this module stays importable for the gate's pure-logic paths (cosine,
    centroid) and unit tests without them.

    Preprocessing is the insightface standard for this model family: BGR->RGB (swapRB), subtract
    127.5, scale 1/127.5 — so scores stay comparable to the buffalo_l benchmark (same family).
    """

    def __init__(self, model_path: str, yunet_path: Optional[str] = None, providers=None):
        import cv2  # noqa: F401  (validate availability early)
        import numpy as np
        import onnxruntime as ort

        self._cv2 = cv2
        self._np = np
        self._sess = ort.InferenceSession(
            model_path, providers=providers or ["CPUExecutionProvider"])
        self._input = self._sess.get_inputs()[0].name
        self._yunet_path = yunet_path or _default_yunet_path()
        self._dst = np.array(_ARCFACE_DST, dtype=np.float32)

    def _largest_face(self, bgr):
        cv2 = self._cv2
        h, w = bgr.shape[:2]
        det = cv2.FaceDetectorYN.create(self._yunet_path, "", (w, h), 0.6, 0.3, 5000)
        det.setInputSize((w, h))
        _, faces = det.detect(bgr)
        if faces is None or len(faces) == 0:
            return None
        return max(faces, key=lambda f: float(f[2]) * float(f[3]))

    def embed(self, image):
        np, cv2 = self._np, self._cv2
        rgb = np.array(image.convert("RGB"))
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        face = self._largest_face(bgr)
        if face is None:
            return None
        # YuNet 5 landmarks (cols 4..13): right_eye, left_eye, nose, right_mouth, left_mouth.
        # Order them GEOMETRICALLY (by x) rather than by name, so a mirrored naming convention
        # can never flip the alignment: leftmost eye -> template[0], rightmost -> template[1];
        # same for the two mouth corners.
        lm = face[4:14].reshape(5, 2).astype(np.float32)
        eyes = lm[[0, 1]][np.argsort(lm[[0, 1]][:, 0])]
        mouth = lm[[3, 4]][np.argsort(lm[[3, 4]][:, 0])]
        src = np.array([eyes[0], eyes[1], lm[2], mouth[0], mouth[1]], dtype=np.float32)
        M = self._umeyama(src, self._dst)
        if M is None:
            return None
        aligned = cv2.warpAffine(bgr, M, (112, 112), borderValue=0)
        # RAW [0,255] RGB (scale 1.0, no mean). This ONNX export bakes its own normalization in,
        # so the insightface (x-127.5)/127.5 COLLAPSES it (all embeddings ~parallel, ~0 separation).
        # Validated on held-out subjects: same-person ~0.64-0.79, different-person ~0.21 (sep +0.44).
        blob = cv2.dnn.blobFromImage(aligned, 1.0, (112, 112), (0, 0, 0), swapRB=True)
        out = self._sess.run(None, {self._input: blob})[0][0].astype("float64")
        norm = float(np.linalg.norm(out))
        return (out / norm).tolist() if norm else None

    def _umeyama(self, src, dst):
        """Similarity transform (rotation + uniform scale + translation) via SVD — insightface's
        alignment method. More stable for a 5-point set than cv2's robust estimators (LMEDS/RANSAC
        misbehave with so few points, which mangles the alignment and collapses the embeddings)."""
        np = self._np
        src = src.astype("float64")
        dst = dst.astype("float64")
        m, n = src.mean(0), dst.mean(0)
        sc, dc = src - m, dst - n
        U, S, Vt = np.linalg.svd(dc.T @ sc / src.shape[0])
        R = U @ Vt
        if np.linalg.det(R) < 0:
            Vt[-1] *= -1
            R = U @ Vt
        var = (sc ** 2).sum() / src.shape[0]
        if var == 0:
            return None
        s = S.sum() / var
        M = np.zeros((2, 3))
        M[:2, :2] = s * R
        M[:2, 2] = n - s * R @ m
        return M.astype("float32")


def load_default_embedder() -> FaceEmbedder:
    """Return the configured commercial-safe embedder — Apache-2.0 ArcFace ResNet100 ONNX.

    The model path comes from ARCFACE_ONNX_PATH (baked into the inference image, or set locally
    for validation). Raises if it isn't configured, so the gate stays inert until the licensed
    model is actually present — an unlicensed scorer can never ship by default.
    """
    import os
    model_path = os.environ.get("ARCFACE_ONNX_PATH")
    if not model_path or not os.path.exists(model_path):
        raise NotImplementedError(
            "ARCFACE_ONNX_PATH is unset or the model file is missing. Provide the Apache-2.0 "
            "ArcFace ResNet100 ONNX (see SCORER_COMPARISON.md) — buffalo_l is non-commercial "
            "and must not be used in-product."
        )
    return ArcFaceOnnxEmbedder(model_path)
