"""Phase-6 Quality Gate orchestration (ARCHITECTURE.md §5-§8).

`run_quality_gate` is PURE control flow: all GPU work (generation) is behind the `regenerate`
callback and all model work behind `evaluate`, so the retry/budget logic is unit-testable with
fakes. Contract:
  - deliver exactly one winner per slot that has a candidate (NEVER short a paid slot);
  - retry ONLY slots whose best candidate did not clear acceptance_threshold;
  - stop at retry_limit rounds OR when candidate_budget is exhausted, whichever comes first;
  - candidates over-generated for retries are COGS, never billed (§5).

`run_quality_gate_for_job` is the runtime integration wrapper called from the orchestrator; it
builds the (commercial-safe) embedder + engines, computes the identity centroid from the user's
reference crops, and supplies a GPU regenerate callback. It is only reachable under
QUALITY_GATE=1, and its regenerate path must be validated on an A100 before shipping.
"""
from __future__ import annotations


def run_quality_gate(candidates, profile, plan, evaluate, select, regenerate, log=lambda m: None):
    """See module docstring. evaluate(cands)->scored; select(scored, plan)->winners;
    regenerate(slot_ids)->new candidates for those slots."""
    scored = list(evaluate(candidates))
    budget_used = len(candidates)
    winners = select(scored, plan)

    rounds = 0
    while rounds < max(0, plan.retry_limit):
        failed = [w.slot_id for w in winners if not w.scored.accepted]
        if not failed:
            break
        remaining = plan.candidate_budget - budget_used
        if remaining <= 0:
            log(f"quality-gate: candidate_budget {plan.candidate_budget} exhausted; "
                f"delivering best-available for slots {failed}")
            break
        new_cands = list(regenerate(failed[: min(len(failed), remaining)]))
        if not new_cands:
            log("quality-gate: regenerate produced no candidates; stopping retries")
            break
        budget_used += len(new_cands)
        scored.extend(evaluate(new_cands))
        winners = select(scored, plan)
        rounds += 1

    cleared = sum(1 for w in winners if w.scored.accepted)
    log(f"quality-gate: {cleared}/{len(winners)} slots cleared threshold "
        f"{plan.acceptance_threshold} after {rounds} retry round(s); "
        f"candidates used {budget_used}/{plan.candidate_budget}")
    return winners


def run_quality_gate_for_job(candidates, plan, ctx, ref_faces, prompt_engine, gen_engine,
                             log=lambda m: None):
    """Integration wrapper (QUALITY_GATE=1 only). Builds the embedder + engines, derives the
    identity centroid from the user's already-loaded reference crops, and runs the gate with a
    GPU regenerate callback. Gate config (threshold/retry/budget) is read from env so a
    validation run can tune it without a redeploy.

    GPU NOTE: the regenerate path re-runs generation for failed slots and MUST be validated on
    an A100 before shipping. Deterministic seeds mean a naive re-generation reproduces the same
    image — retries need seed variation (QG_SEED_BASE) to explore, which is exercised only under
    the flag.
    """
    import dataclasses
    import os

    from domain import IdentityProfile, PromptSet

    from .engines.embedder import centroid, load_default_embedder
    from .engines.evaluation import IdentityEvaluationEngine
    from .engines.selection import SlotSelectionEngine

    embedder = load_default_embedder()  # raises until a commercial scorer is integrated

    vecs = [embedder.embed(f) for f in (ref_faces or [])]
    profile = IdentityProfile(
        user_id=plan.user_id,
        identity_centroid=tuple(centroid(vecs) or ()),
    )

    ev = IdentityEvaluationEngine(ctx, embedder, log)
    sel = SlotSelectionEngine(log)

    threshold = float(os.environ.get("QG_THRESHOLD", plan.acceptance_threshold) or 0.0)
    retry = int(os.environ.get("QG_RETRY", plan.retry_limit) or 0)
    budget = int(os.environ.get("QG_BUDGET", plan.candidate_budget) or plan.candidate_budget)
    gplan = dataclasses.replace(
        plan, acceptance_threshold=threshold, retry_limit=retry, candidate_budget=budget)

    seed_base = int(os.environ.get("QG_SEED_BASE", "5000") or "5000")

    def _regenerate(slot_ids):
        # GPU: rebuild the failed slots' prompts with NEW seeds and generate fresh candidates.
        all_prompts = prompt_engine.build(gplan)
        want = set(slot_ids)
        subset = []
        for p in all_prompts.prompts:
            sid = p.slot_id if p.slot_id is not None else 0
            if sid in want:
                subset.append(dataclasses.replace(p, seed=seed_base + len(subset)))
        if not subset:
            return []
        return gen_engine.generate(PromptSet(tuple(subset)), None)

    return run_quality_gate(
        candidates, profile, gplan,
        lambda cs: ev.score(cs, profile), sel.select, _regenerate, log)
