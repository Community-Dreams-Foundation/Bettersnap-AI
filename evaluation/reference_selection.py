"""IP-Adapter reference selection — strategy B ('best neutral, frontal, high-quality face').

Phase-3 ablation compares:
    A. current behavior: use the FIRST crop (img0) as the IP-Adapter reference
    B. use the BEST crop, chosen here by a quality ranking

This module runs in the harness (CPU, has YuNet via shared.crops), NOT in the GPU image —
so no detector is shipped into inference. It emits the chosen index; the GPU worker just
honors IP_ADAPTER_REF_INDEX. The ranking IS a weighted blend, which is fine: this is
SELECTION (pick one reference), not evaluation scoring (where blending is forbidden).

Quality favors a frontal gaze, visible eyes, adequate face size and sharpness — the traits
that make a clean identity reference. A crop with no face or occluded eyes is disqualified.
"""
from .composition import composition_scores, sharpness

# Selection weights (documented, tunable). Frontality dominates: a turned/averted reference
# is the worst input for a face IP-Adapter.
_W_FRONTAL = 0.40
_W_EYES = 0.25
_W_SIZE = 0.20
_W_SHARP = 0.15

# A reference this un-frontal (|yaw| >= this) or with eyes this dim is disqualified outright.
_MAX_YAW = 0.5
_MIN_EYE = 0.6


def score_reference(image_bgr):
    """Quality assessment of one candidate reference crop. Returns a dict with a `quality`
    in [0,1] and its components, or {'usable': False, 'reason': ...} when unusable."""
    comp = composition_scores(image_bgr)
    if comp.get("face_count", 0) < 1:
        return {"usable": False, "reason": "no_face"}
    if comp.get("face_count", 0) > 1:
        return {"usable": False, "reason": "multiple_faces"}

    yaw = abs(comp.get("yaw_offset", 0.0))
    eyes = comp.get("eye_visibility", 1.0)
    if yaw >= _MAX_YAW:
        return {"usable": False, "reason": f"too_turned(yaw={yaw:.2f})"}
    if eyes < _MIN_EYE:
        return {"usable": False, "reason": f"eyes_occluded(ratio={eyes:.2f})"}

    frontal = 1.0 - min(1.0, yaw / _MAX_YAW)          # 1 = dead-on, 0 = at the yaw limit
    eyes_n = min(1.0, max(0.0, (eyes - _MIN_EYE) / (1.05 - _MIN_EYE)))
    size_n = min(1.0, comp.get("face_height_pct", 0.0) / 45.0)   # 45%+ is plenty
    sharp_n = min(1.0, sharpness(image_bgr) / 300.0)             # rough normalization
    quality = (_W_FRONTAL * frontal + _W_EYES * eyes_n
               + _W_SIZE * size_n + _W_SHARP * sharp_n)
    return {"usable": True, "quality": round(quality, 4),
            "frontal": round(frontal, 3), "eyes": round(eyes_n, 3),
            "size": round(size_n, 3), "sharp": round(sharp_n, 3),
            "yaw_abs": round(yaw, 3), "face_height_pct": comp.get("face_height_pct")}


def select_best_reference(images_bgr):
    """Rank candidate crops; return (best_index, per_candidate_scores). best_index is the
    highest-quality USABLE crop, or None if none are usable. Ties break to the lower index
    (deterministic)."""
    scores = [score_reference(im) for im in images_bgr]
    usable = [(i, s) for i, s in enumerate(scores) if s.get("usable")]
    if not usable:
        return None, scores
    best_index = max(usable, key=lambda t: (t[1]["quality"], -t[0]))[0]
    return best_index, scores
