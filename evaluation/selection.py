"""Adaptive candidate generation + selection controller (Phase 4).

Replaces "generate a fixed 30 and enhance all of them" with:

    generate an initial batch
      -> score ONLY the new candidates
      -> HARD-gate: drop invalid ones (no post-processing spent on rejects)
      -> accumulate the accepted
      -> stop when target reached OR the hard cap is hit
      -> otherwise generate a smaller follow-up batch and repeat
    then RANK the accepted pool (soft score), apply diversity, take up to target.

Design rules enforced here:
  * hard rejects (accept/deny) are separate from soft penalties (ranking).
  * identity thresholds are CONFIG/calibration inputs — default None (disabled) so no
    universal cosine constant is ever assumed; calibrate per embedding model, then set.
  * rejected candidates are never post-processed (the controller yields raw accepted
    candidates; the caller post-processes ONLY the selected winners).
  * old candidates are never re-scored — each batch scores only its new images.
  * the manifest records every batch's seeds, running accepted count and rejection reasons.
  * a hard cap bounds total generation so GPU spend can't run away.
  * the final output count reflects ACTUAL accepted images, never the requested target.

The controller is generation-agnostic: `generate_batch(n, seed_start)` and `evaluate(cand)`
are injected, so this is fully unit-testable without a GPU and later wired to real inference.
"""
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class WavePlan:
    target: int = 30
    initial_batch: int = 40
    followup_batch: int = 20
    hard_cap: int = 120           # max TOTAL candidates generated across all batches
    base_seed: int = 1000


@dataclass
class HardGates:
    """Accept/reject thresholds. Composition/geometry gates have sane geometric defaults;
    IDENTITY gates default to None = DISABLED, because a raw cosine value is not portable
    across embedding models — calibrate from your validation data, then set them."""
    min_face_height_pct: float = 28.0
    max_face_height_pct: float = 60.0
    require_single_face: bool = True
    max_yaw_abs: float = 0.35
    min_eye_visibility: float = 0.6
    min_centroid_similarity: Optional[float] = None   # calibrate per model before enabling
    min_member_similarity: Optional[float] = None     # calibrate per model before enabling
    min_sharpness: Optional[float] = None


@dataclass
class SoftWeights:
    """Ranking weights for the accepted pool (a blend is fine for RANKING, never for the
    evaluation report). prompt_adherence is pending (no licensed classifier) so it does not
    contribute yet; diversity is applied at selection time, not per-image."""
    identity: float = 0.45
    composition: float = 0.20
    sharpness: float = 0.10
    # prompt (0.15) and diversity (0.10) reserved; see module + report notes.


def hard_gate(scores, gates: HardGates):
    """Return (accepted: bool, reasons: [str]). Reasons accumulate — an image can fail for
    several independent reasons, all recorded."""
    reasons = []
    comp = scores.get("composition", {}) or {}
    fc = comp.get("face_count", 0)
    if fc == 0:
        reasons.append("no_face")
    elif gates.require_single_face and fc > 1:
        reasons.append("multiple_faces")

    fh = comp.get("face_height_pct")
    if fh is not None:
        if fh < gates.min_face_height_pct:
            reasons.append("face_too_small")
        elif fh > gates.max_face_height_pct:
            reasons.append("face_too_large")

    yaw = comp.get("yaw_offset")
    if yaw is not None and abs(yaw) > gates.max_yaw_abs:
        reasons.append("excessive_yaw")

    eye = comp.get("eye_visibility")
    if eye is not None and eye < gates.min_eye_visibility:
        reasons.append("eyes_occluded")

    ident = scores.get("identity", {}) or {}
    if gates.min_centroid_similarity is not None:
        cs = ident.get("centroid_similarity")
        if cs is None or cs < gates.min_centroid_similarity:
            reasons.append("identity_below_centroid")
    if gates.min_member_similarity is not None:
        ms = ident.get("min_member_similarity")
        if ms is None or ms < gates.min_member_similarity:
            reasons.append("identity_below_member")

    vq = scores.get("visual_quality", {}) or {}
    if gates.min_sharpness is not None:
        sh = vq.get("sharpness")
        if sh is None or sh < gates.min_sharpness:
            reasons.append("too_blurry")

    return (len(reasons) == 0, reasons)


def soft_score(scores, weights: SoftWeights):
    """Rank an ACCEPTED candidate in [0,1]. Uses only the signals that exist and renormalizes
    by their weights, so a pending signal (prompt adherence) neither inflates nor deflates the
    result. Never a pass/fail verdict — that is hard_gate's job."""
    parts, wsum = 0.0, 0.0
    ident = scores.get("identity", {}) or {}
    if "centroid_similarity" in ident:
        cs = max(-1.0, min(1.0, ident["centroid_similarity"]))
        parts += (cs + 1.0) / 2.0 * weights.identity
        wsum += weights.identity
    comp = scores.get("composition", {}) or {}
    if "face_height_pct" in comp:
        fh = comp["face_height_pct"]
        band = 1.0 if 35.0 <= fh <= 50.0 else max(0.0, 1.0 - abs(fh - 42.5) / 42.5)
        center = 1.0 - min(1.0, comp.get("center_offset", 0.0) / 0.5)
        parts += (0.6 * band + 0.4 * center) * weights.composition
        wsum += weights.composition
    vq = scores.get("visual_quality", {}) or {}
    if vq.get("sharpness") is not None:
        parts += min(1.0, vq["sharpness"] / 300.0) * weights.sharpness
        wsum += weights.sharpness
    return parts / wsum if wsum > 0 else 0.0


def greedy_diverse_select(ranked, target, similarity_fn=None, max_similarity=0.9):
    """Take up to `target` from a score-descending list, skipping a candidate that is more
    than `max_similarity` similar to one already picked. `ranked` is a list of dicts each
    with a 'candidate' key. similarity_fn(cand_a, cand_b)->[0,1]; None disables dedup
    (plain top-N). Diversity means varied attire/bg/pose — NOT identity/age/hair drift, so a
    production similarity_fn should compare appearance, and identity is guarded separately."""
    if similarity_fn is None:
        return ranked[:target]
    chosen = []
    for item in ranked:
        if len(chosen) >= target:
            break
        if all(similarity_fn(item["candidate"], c["candidate"]) <= max_similarity
               for c in chosen):
            chosen.append(item)
    return chosen


def run_adaptive_selection(plan: WavePlan, gates: HardGates, weights: SoftWeights,
                           generate_batch: Callable, evaluate: Callable,
                           similarity_fn=None, max_similarity=0.9):
    """Drive the wave loop. Returns {'selected': [...], 'manifest': {...}}.

    selected: score-ranked, diversity-trimmed, up to `plan.target` accepted candidates,
              each {'candidate', 'scores', 'soft'} — RAW, not yet post-processed.
    manifest: per-batch seeds/accepted/rejections + final actual counts (rule 5, 7).
    """
    accepted = []
    total_generated = 0
    batches = []
    seed = plan.base_seed
    batch_size = plan.initial_batch
    batch_index = 0

    while len(accepted) < plan.target and total_generated < plan.hard_cap:
        n = min(batch_size, plan.hard_cap - total_generated)   # never exceed the cap
        seeds = list(range(seed, seed + n))
        candidates = generate_batch(n, seed)                   # raw, un-post-processed
        rejections = {}
        for cand in candidates:
            s = evaluate(cand)                                 # score ONLY new candidates
            ok, reasons = hard_gate(s, gates)
            if ok:
                accepted.append({"candidate": cand, "scores": s,
                                 "soft": soft_score(s, weights)})
            else:
                for r in reasons:
                    rejections[r] = rejections.get(r, 0) + 1
        total_generated += n
        batches.append({
            "batch_index": batch_index,
            "seed_start": seed,
            "seeds": seeds,
            "n_generated": n,
            "n_accepted_this_batch": len(candidates) - sum(rejections.values()),
            "n_accepted_running": len(accepted),
            "rejections": rejections,
        })
        seed += n
        batch_index += 1
        batch_size = plan.followup_batch

    accepted.sort(key=lambda x: x["soft"], reverse=True)
    selected = greedy_diverse_select(accepted, plan.target, similarity_fn, max_similarity)

    manifest = {
        "target": plan.target,
        "hard_cap": plan.hard_cap,
        "total_generated": total_generated,
        "accepted_available": len(accepted),
        "final_output_count": len(selected),          # ACTUAL, never the requested target
        "hit_target": len(accepted) >= plan.target,
        "capped": total_generated >= plan.hard_cap and len(accepted) < plan.target,
        "batches": batches,
    }
    return {"selected": selected, "manifest": manifest}
