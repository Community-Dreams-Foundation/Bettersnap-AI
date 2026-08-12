"""Phase-6 Quality Gate — CPU unit tests (no torch, no GPU, no model).

Covers the pieces the gate is built from, with a SYNTHETIC embedder so the logic is exercised
without any real face model:
  - embedder cosine / centroid math
  - IdentityEvaluationEngine scoring (centroid similarity; no-face -> 0)
  - SlotSelectionEngine (best-per-slot, acceptance threshold, never shorts a paid slot)
  - run_quality_gate retry orchestration (converges, respects retry_limit + candidate_budget)

Run: python -m pytest tests/test_quality_gate.py -q   (from the repo root)
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from domain import (  # noqa: E402
    CategoryRule, GenerationPlan, IdentityProfile, OutputSlot, Plan, PlanType,
    Prompt, Candidate, Ref, RefKind, ScoredCandidate, Scores,
)
from runtime.engines.embedder import centroid, cosine  # noqa: E402
from runtime.engines.evaluation import IdentityEvaluationEngine  # noqa: E402
from runtime.engines.selection import SlotSelectionEngine  # noqa: E402
from runtime.quality_gate import run_quality_gate  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────────
class FakeCtx:
    def __init__(self, images):
        self.images = images


class FakeEmbedder:
    """Maps the ctx.images value (a marker) to a fixed vector, or None for 'no face'."""
    def __init__(self, table):
        self.table = table

    def embed(self, marker):
        return self.table.get(marker)


def _prompt(seed=1000, slot=0):
    return Prompt(positive="p", negative="n", seed=seed, slot_id=slot)


def _cand(cid, slot):
    return Candidate(cid, _prompt(slot=slot), Ref(RefKind.CANDIDATE, f"gen://{cid}"), slot_id=slot)


def _plan(threshold=0.5, retry=0, budget=4, slots=(0,)):
    base = Plan(key="k", plan_type=PlanType.ONE_TIME, image_count=len(slots),
                credits_per_image=1, max_attires=99, max_backgrounds=99,
                category_rule=CategoryRule.MIXABLE)
    return GenerationPlan(
        user_id="u", job_id="j", plan=base, billable_count=len(slots), credit_cost=0,
        candidate_budget=budget, acceptance_threshold=threshold, retry_limit=retry,
        slots=tuple(OutputSlot(s) for s in slots))


# ── embedder math ────────────────────────────────────────────────────────────
def test_cosine_and_centroid():
    assert cosine([1, 0, 0], [1, 0, 0]) == 1.0
    assert cosine([1, 0, 0], [0, 1, 0]) == 0.0
    assert cosine(None, [1, 0]) == 0.0          # missing face
    assert cosine([0, 0], [1, 1]) == 0.0        # zero vector
    c = centroid([[2, 0, 0], [0, 0, 0]])        # mean [1,0,0] -> normalized [1,0,0]
    assert c == [1.0, 0.0, 0.0]
    assert centroid([None, None]) is None


# ── evaluation ───────────────────────────────────────────────────────────────
def test_evaluation_scores_against_centroid():
    ctx = FakeCtx({"gen://a": "A", "gen://b": "B", "gen://c": "C"})
    emb = FakeEmbedder({"A": [1, 0, 0], "B": [0, 1, 0], "C": None})  # C = no face
    ev = IdentityEvaluationEngine(ctx, emb)
    profile = IdentityProfile(user_id="u", identity_centroid=(1.0, 0.0, 0.0))
    scored = ev.score([_cand("a", 0), _cand("b", 1), _cand("c", 2)], profile)
    by_id = {s.candidate.id: s.scores.identity for s in scored}
    assert by_id["a"] == 1.0    # matches centroid
    assert by_id["b"] == 0.0    # orthogonal
    assert by_id["c"] == 0.0    # no face detected


# ── selection ────────────────────────────────────────────────────────────────
def test_selection_best_per_slot_and_never_shorts():
    sel = SlotSelectionEngine()
    scored = [
        ScoredCandidate(_cand("a", 0), Scores(identity=0.9)),
        ScoredCandidate(_cand("b", 0), Scores(identity=0.4)),   # same slot, worse
        ScoredCandidate(_cand("d", 1), Scores(identity=0.3)),   # slot 1, below threshold
    ]
    winners = sel.select(scored, _plan(threshold=0.7, slots=(0, 1)))
    got = {w.slot_id: (w.scored.candidate.id, w.scored.accepted) for w in winners}
    assert got[0] == ("a", True)     # best in slot 0, clears 0.7
    assert got[1] == ("d", False)    # only option in slot 1, below bar but STILL delivered
    assert len(winners) == 2         # never shorts a paid slot


# ── retry orchestration ──────────────────────────────────────────────────────
def test_no_retry_when_all_accepted():
    scores = {"a": 0.9, "b": 0.9}
    sel = SlotSelectionEngine()
    calls = {"n": 0}

    def regen(slots):
        calls["n"] += 1
        return []

    def evaluate(cands):
        return [ScoredCandidate(c, Scores(identity=scores[c.id])) for c in cands]

    winners = run_quality_gate([_cand("a", 0), _cand("b", 1)], IdentityProfile(user_id="u"),
                               _plan(threshold=0.7, retry=3, budget=8, slots=(0, 1)),
                               evaluate, sel.select, regen)
    assert calls["n"] == 0                    # nothing failed -> never regenerated
    assert all(w.scored.accepted for w in winners)


def test_retry_improves_failed_slot_within_budget():
    scores = {"a": 0.2}                        # slot 0 starts below the 0.7 bar
    sel = SlotSelectionEngine()

    def evaluate(cands):
        return [ScoredCandidate(c, Scores(identity=scores[c.id])) for c in cands]

    def regen(slots):
        # produce a better candidate for the failed slot
        nid = "a_retry"
        scores[nid] = 0.95
        return [_cand(nid, slots[0])]

    winners = run_quality_gate([_cand("a", 0)], IdentityProfile(user_id="u"),
                               _plan(threshold=0.7, retry=3, budget=4, slots=(0,)),
                               evaluate, sel.select, regen)
    assert len(winners) == 1
    assert winners[0].scored.candidate.id == "a_retry"
    assert winners[0].scored.accepted is True


def test_budget_caps_retries_and_still_delivers_best_available():
    scores = {"a": 0.1}
    sel = SlotSelectionEngine()
    calls = {"n": 0}

    def evaluate(cands):
        return [ScoredCandidate(c, Scores(identity=scores.get(c.id, 0.1))) for c in cands]

    def regen(slots):
        calls["n"] += 1
        nid = f"r{calls['n']}"
        scores[nid] = 0.1                      # never good enough
        return [_cand(nid, slots[0])]

    # budget 3 = 1 initial + 2 retry candidates, even though retry_limit is high
    winners = run_quality_gate([_cand("a", 0)], IdentityProfile(user_id="u"),
                               _plan(threshold=0.7, retry=10, budget=3, slots=(0,)),
                               evaluate, sel.select, regen)
    assert calls["n"] == 2                     # stopped when budget (3) hit
    assert len(winners) == 1                   # slot still delivered (best-available)
    assert winners[0].scored.accepted is False


def test_retry_limit_zero_never_regenerates():
    scores = {"a": 0.1}
    sel = SlotSelectionEngine()
    calls = {"n": 0}

    def evaluate(cands):
        return [ScoredCandidate(c, Scores(identity=scores[c.id])) for c in cands]

    def regen(slots):
        calls["n"] += 1
        return [_cand("x", slots[0])]

    winners = run_quality_gate([_cand("a", 0)], IdentityProfile(user_id="u"),
                               _plan(threshold=0.7, retry=0, budget=8, slots=(0,)),
                               evaluate, sel.select, regen)
    assert calls["n"] == 0
    assert len(winners) == 1
