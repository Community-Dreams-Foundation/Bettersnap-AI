"""Identity reference centroid — the cleaned average of a user's accepted face embeddings.

Pipeline (per review):
    embeddings (one per accepted image)
      -> L2-normalize each
      -> remove outliers (a blurred profile / filtered selfie / bad crop distorts the mean)
      -> average the survivors
      -> normalize the centroid

Also reports intra-set cohesion (mean pairwise cosine among survivors). LOW cohesion means
the input set itself is weak/inconsistent — per review, that should FLAG THE TRAINING SET
rather than justify lowering the output identity threshold until everything passes.

Pure numpy, no model dependency, so it is fully unit-testable with synthetic vectors.
"""
import numpy as np


def l2_normalize(v, axis=-1, eps=1e-10):
    v = np.asarray(v, dtype=np.float64)
    n = np.linalg.norm(v, axis=axis, keepdims=True)
    return v / np.maximum(n, eps)


def remove_outliers(embeddings, z_thresh=2.0, min_keep=3):
    """Drop embeddings whose cosine distance to the provisional mean direction is an upper
    outlier (z-score > z_thresh). Never drops below min_keep; for tiny sets keeps all.
    Returns (kept_unit_embeddings, kept_indices)."""
    E = l2_normalize(np.asarray(embeddings, dtype=np.float64))
    n = len(E)
    if n <= min_keep:
        return E, list(range(n))
    mean = l2_normalize(E.mean(axis=0))
    dist = 1.0 - (E @ mean)                      # cosine distance to mean (unit vectors)
    sd = dist.std()
    if sd < 1e-9:
        return E, list(range(n))                 # all identical direction — keep all
    z = (dist - dist.mean()) / sd
    keep = [i for i in range(n) if z[i] <= z_thresh]
    if len(keep) < min_keep:                     # never over-prune: keep the closest
        keep = sorted(int(i) for i in np.argsort(dist)[:min_keep])
    return E[keep], keep


def build_centroid(embeddings, z_thresh=2.0, min_keep=3):
    """accepted embeddings -> {centroid, kept_indices, n_kept, n_total, intra_cohesion}.

    `intra_cohesion` is the mean pairwise cosine among kept embeddings in [-1, 1]; higher =
    a more self-consistent identity. Callers should surface a low value as a weak-input
    warning, not silently proceed.
    """
    E = np.asarray(embeddings, dtype=np.float64)
    if E.ndim != 2 or len(E) == 0:
        raise ValueError("build_centroid needs a non-empty 2D array of embeddings")
    kept, idx = remove_outliers(E, z_thresh=z_thresh, min_keep=min_keep)
    centroid = l2_normalize(kept.mean(axis=0))
    n = len(kept)
    if n > 1:
        sims = kept @ kept.T
        cohesion = float((sims.sum() - np.trace(sims)) / (n * (n - 1)))
    else:
        cohesion = 1.0
    return {
        "centroid": centroid,
        "kept_indices": idx,
        "n_kept": n,
        "n_total": int(len(E)),
        "intra_cohesion": cohesion,
    }
