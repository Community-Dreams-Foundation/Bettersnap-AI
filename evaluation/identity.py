"""Identity scoring for one generated image against the reference centroid.

Reports THREE raw numbers, not a pass/fail — thresholds are set per-model from validation
data elsewhere (a raw cosine value is not portable across embedding models):

    centroid_similarity     cosine(gen, cleaned centroid)
    min_member_similarity   cosine to the WORST-matching accepted reference
    mean_member_similarity  cosine to the accepted references, averaged

min_member_similarity is the guard against a generic 'average face': an image can sit near
the centroid while resembling none of the real photos. Comparing to individual members
catches that.
"""
import numpy as np

from .centroid import l2_normalize


def cosine(a, b):
    return float(np.dot(l2_normalize(a), l2_normalize(b)))


def identity_scores(gen_embedding, centroid, member_embeddings=None):
    g = l2_normalize(gen_embedding)
    out = {"centroid_similarity": float(np.dot(g, l2_normalize(centroid)))}
    if member_embeddings is not None and len(member_embeddings):
        M = l2_normalize(np.asarray(member_embeddings, dtype=np.float64))
        sims = M @ g
        out["min_member_similarity"] = float(sims.min())
        out["mean_member_similarity"] = float(sims.mean())
    return out
