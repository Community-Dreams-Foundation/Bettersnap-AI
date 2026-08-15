"""IdentityEvaluationEngine — implements domain.EvaluationEngine (Phase 6, ARCHITECTURE.md §8).

Scores each candidate's identity as cosine similarity between its face embedding and the
user's identity centroid, using a COMMERCIAL-SAFE embedder injected at construction. The
engine is model-free itself — the embedder encapsulates the (Apache-2.0) model — so the same
engine works with any FaceEmbedder and is unit-testable with a synthetic one.

`score()` fills Scores.identity (and mirrors it into Scores.overall for now — aesthetic /
prompt-match / technical are future gate dimensions). It does NOT decide acceptance; that's
the SelectionEngine's job, which owns the plan's acceptance_threshold.
"""
from __future__ import annotations

from domain import ScoredCandidate, Scores

from .embedder import FaceEmbedder, centroid, cosine


class IdentityEvaluationEngine:
    def __init__(self, ctx, embedder: FaceEmbedder, log=lambda m: None):
        self.ctx = ctx
        self.embedder = embedder
        self.log = log

    def score(self, candidates, profile):
        ref = self._centroid_for(profile)
        if ref is None:
            self.log("evaluation: no identity centroid available — all candidates score 0.0")
        scored = []
        for c in candidates:
            identity = 0.0
            img = self._image(c.image_ref.location)
            if img is not None and ref is not None:
                vec = self.embedder.embed(img)
                identity = max(0.0, cosine(vec, ref))  # cosine() returns 0.0 for a missing face
            scored.append(ScoredCandidate(c, Scores(identity=identity, overall=identity)))
        return scored

    def _image(self, key):
        store = getattr(self.ctx, "images", None)
        if store is None:
            return None
        return store.get(key)

    def _centroid_for(self, profile):
        """Prefer the profile's precomputed identity_centroid (built at training time with the
        same commercial embedder). Fall back to embedding the reference crops on the fly."""
        if profile is not None and getattr(profile, "identity_centroid", ()):
            return list(profile.identity_centroid)
        refs = getattr(profile, "reference_crops", []) if profile is not None else []
        vecs = []
        for r in refs:
            img = self._image(r.location)
            if img is not None:
                vecs.append(self.embedder.embed(img))
        return centroid(vecs)
