"""Per-image and per-run reports — FOUR SEPARATE GROUPS, never a single blended score.

There is intentionally no 'overall' key. Per review: do not combine identity, composition,
prompt-adherence and visual-quality into one number until BLINDED human-review data exists
to justify the weights. A model can have strong likeness and poor attire compliance; one
score would hide that.
"""
import numpy as np


def per_image_report(*, identity=None, composition=None, sharpness=None):
    """Assemble one image's four-group report. Missing groups are marked, not faked."""
    return {
        "identity": identity if identity is not None else {"status": "no_embedder"},
        "composition": composition if composition is not None else {"face_count": 0},
        "prompt_adherence": {
            "status": "not_implemented",
            "note": ("needs a commercially-licensed attire/background classifier; "
                     "deliberately NOT faked — left pending in Phase 1"),
        },
        "visual_quality": {
            "sharpness": sharpness,
            "skin_realism": {"status": "human_review_only"},
            "human_review_required": True,
        },
    }
    # NO 'overall' / 'score' key by design.


def _summ(values):
    a = np.asarray([v for v in values if v is not None], dtype=np.float64)
    if not len(a):
        return None
    return {"n": int(len(a)), "mean": round(float(a.mean()), 4),
            "min": round(float(a.min()), 4), "max": round(float(a.max()), 4),
            "p10": round(float(np.percentile(a, 10)), 4)}


def run_report(pipeline_id, dataset_id, per_image, *, centroid_meta=None):
    """Descriptive per-group aggregation across a run's images. Still NO cross-group blend.

    Surfaces a weak-input warning from the centroid's intra_cohesion so an unreliable
    reference set is flagged rather than silently lowering the bar."""
    ident = [im.get("identity", {}) for im in per_image]
    comp = [im.get("composition", {}) for im in per_image]
    report = {
        "pipeline_id": pipeline_id,
        "dataset_id": dataset_id,
        "n_images": len(per_image),
        "identity": {
            "centroid_similarity": _summ([i.get("centroid_similarity") for i in ident]),
            "min_member_similarity": _summ([i.get("min_member_similarity") for i in ident]),
        },
        "composition": {
            "face_height_pct": _summ([c.get("face_height_pct") for c in comp]),
            "yaw_offset_abs": _summ([abs(c["yaw_offset"]) for c in comp if "yaw_offset" in c]),
            "multi_face_count": sum(1 for c in comp if c.get("face_count", 0) > 1),
            "no_face_count": sum(1 for c in comp if c.get("face_count", 0) == 0),
        },
        "prompt_adherence": {"status": "not_implemented"},
        "visual_quality": {"sharpness": _summ([im.get("visual_quality", {}).get("sharpness")
                                               for im in per_image])},
    }
    if centroid_meta is not None:
        coh = centroid_meta.get("intra_cohesion")
        report["reference_set"] = {
            "n_total": centroid_meta.get("n_total"),
            "n_kept": centroid_meta.get("n_kept"),
            "intra_cohesion": coh,
            # threshold is illustrative; calibrate per embedding model from validation data
            "weak_input_warning": (coh is not None and coh < 0.5),
        }
    return report
