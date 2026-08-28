"""Adaptive candidate controller — verifies all seven Phase-4 rules with a fake generator
(no GPU). Candidates are dicts; the fake evaluator counts calls per candidate so we can
prove old candidates are never re-scored."""
import collections
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from evaluation.selection import (  # noqa: E402
    WavePlan, HardGates, SoftWeights, hard_gate, soft_score,
    run_adaptive_selection, greedy_diverse_select,
)

_GOOD = {"composition": {"face_count": 1, "face_height_pct": 42.0, "center_offset": 0.05},
         "identity": {"centroid_similarity": 0.82, "min_member_similarity": 0.75},
         "visual_quality": {"sharpness": 150.0}}
_BAD = {"composition": {"face_count": 0}}   # no_face -> hard reject


class Fake:
    """generate_batch/evaluate pair. `accept(global_id)` decides each candidate's fate."""
    def __init__(self, accept):
        self.accept = accept
        self.eval_calls = collections.Counter()
        self.all_seeds = []

    def generate_batch(self, n, seed_start):
        self.all_seeds.extend(range(seed_start, seed_start + n))
        return [{"id": seed_start + i, "ok": self.accept(seed_start + i)} for i in range(n)]

    def evaluate(self, cand):
        self.eval_calls[cand["id"]] += 1
        return dict(_GOOD) if cand["ok"] else dict(_BAD)


class HardVsSoft(unittest.TestCase):
    def test_hard_gate_returns_bool_and_reasons(self):
        ok, reasons = hard_gate(_GOOD, HardGates())
        self.assertTrue(ok)
        self.assertEqual(reasons, [])
        ok, reasons = hard_gate(_BAD, HardGates())
        self.assertFalse(ok)
        self.assertIn("no_face", reasons)

    def test_multiple_reasons_accumulate(self):
        s = {"composition": {"face_count": 2, "face_height_pct": 12.0,
                             "yaw_offset": 0.9, "eye_visibility": 0.3}}
        ok, reasons = hard_gate(s, HardGates())
        self.assertFalse(ok)
        for r in ("multiple_faces", "face_too_small", "excessive_yaw", "eyes_occluded"):
            self.assertIn(r, reasons)

    def test_soft_score_is_float_in_unit_range(self):
        v = soft_score(_GOOD, SoftWeights())
        self.assertIsInstance(v, float)
        self.assertGreaterEqual(v, 0.0)
        self.assertLessEqual(v, 1.0)


class IdentityThresholdsAreConfig(unittest.TestCase):
    def test_identity_gate_disabled_by_default(self):
        # No identity thresholds set -> an image with weak/no identity still passes the gate
        # on composition alone (no universal cosine assumed).
        s = {"composition": {"face_count": 1, "face_height_pct": 42.0}}
        ok, reasons = hard_gate(s, HardGates())
        self.assertTrue(ok)

    def test_identity_gate_enforced_when_calibrated(self):
        gates = HardGates(min_centroid_similarity=0.7)
        weak = {"composition": {"face_count": 1, "face_height_pct": 42.0},
                "identity": {"centroid_similarity": 0.5}}
        ok, reasons = hard_gate(weak, gates)
        self.assertFalse(ok)
        self.assertIn("identity_below_centroid", reasons)


class WaveLoop(unittest.TestCase):
    def test_stops_after_first_batch_when_target_met(self):
        f = Fake(lambda gid: True)                     # everything accepted
        out = run_adaptive_selection(WavePlan(target=30, initial_batch=40),
                                     HardGates(), SoftWeights(),
                                     f.generate_batch, f.evaluate)
        self.assertEqual(len(out["manifest"]["batches"]), 1, "no follow-up batch needed")
        self.assertEqual(out["manifest"]["total_generated"], 40)
        self.assertEqual(out["manifest"]["final_output_count"], 30)   # trimmed to target
        self.assertEqual(out["manifest"]["accepted_available"], 40)
        self.assertTrue(out["manifest"]["hit_target"])

    def test_generates_followups_until_target(self):
        # accept 1 in 4: first batch of 40 yields 10; target 15 forces a follow-up batch.
        f = Fake(lambda gid: gid % 4 == 0)
        out = run_adaptive_selection(WavePlan(target=15, initial_batch=40, followup_batch=20,
                                              hard_cap=200),
                                     HardGates(), SoftWeights(),
                                     f.generate_batch, f.evaluate)
        self.assertGreaterEqual(out["manifest"]["accepted_available"], 15)
        self.assertGreater(len(out["manifest"]["batches"]), 1)

    def test_hard_cap_bounds_generation(self):
        f = Fake(lambda gid: False)                    # nothing ever passes
        out = run_adaptive_selection(WavePlan(target=30, initial_batch=40, followup_batch=20,
                                              hard_cap=100),
                                     HardGates(), SoftWeights(),
                                     f.generate_batch, f.evaluate)
        self.assertEqual(out["manifest"]["total_generated"], 100)     # 40+20+20+20, capped
        self.assertEqual(out["manifest"]["final_output_count"], 0)
        self.assertTrue(out["manifest"]["capped"])
        self.assertFalse(out["manifest"]["hit_target"])

    def test_final_count_reflects_actual_not_requested(self):
        # only 5 will ever pass, target 30, cap 100
        f = Fake(lambda gid: gid < 1005)               # first 5 ids only
        out = run_adaptive_selection(WavePlan(target=30, initial_batch=40, hard_cap=100,
                                              base_seed=1000),
                                     HardGates(), SoftWeights(),
                                     f.generate_batch, f.evaluate)
        self.assertEqual(out["manifest"]["accepted_available"], 5)
        self.assertEqual(out["manifest"]["final_output_count"], 5)    # NOT 30
        self.assertEqual(len(out["selected"]), 5)

    def test_no_candidate_is_rescored(self):
        f = Fake(lambda gid: gid % 3 == 0)
        run_adaptive_selection(WavePlan(target=15, initial_batch=40, followup_batch=20,
                                        hard_cap=200),
                               HardGates(), SoftWeights(), f.generate_batch, f.evaluate)
        self.assertTrue(all(c == 1 for c in f.eval_calls.values()),
                        "each candidate must be evaluated exactly once (no re-scoring)")

    def test_seeds_never_overlap_across_batches(self):
        f = Fake(lambda gid: False)
        run_adaptive_selection(WavePlan(target=30, initial_batch=40, followup_batch=20,
                                        hard_cap=100),
                               HardGates(), SoftWeights(), f.generate_batch, f.evaluate)
        self.assertEqual(len(f.all_seeds), len(set(f.all_seeds)), "seeds must be unique")

    def test_manifest_records_batches_and_rejections(self):
        f = Fake(lambda gid: False)
        out = run_adaptive_selection(WavePlan(target=5, initial_batch=10, followup_batch=10,
                                              hard_cap=20),
                                     HardGates(), SoftWeights(), f.generate_batch, f.evaluate)
        b0 = out["manifest"]["batches"][0]
        self.assertEqual(b0["n_generated"], 10)
        self.assertEqual(b0["rejections"].get("no_face"), 10)
        self.assertEqual(len(b0["seeds"]), 10)

    def test_selected_are_raw_candidates(self):
        # Controller must not mutate/post-process; selected carries the original candidate.
        f = Fake(lambda gid: True)
        out = run_adaptive_selection(WavePlan(target=3, initial_batch=5),
                                     HardGates(), SoftWeights(), f.generate_batch, f.evaluate)
        for item in out["selected"]:
            self.assertIn("id", item["candidate"])       # untouched fake candidate
            self.assertIn("scores", item)
            self.assertIn("soft", item)


class Diversity(unittest.TestCase):
    def test_top_n_when_no_similarity_fn(self):
        ranked = [{"candidate": i, "soft": 1.0 - i * 0.1} for i in range(5)]
        self.assertEqual(len(greedy_diverse_select(ranked, 3)), 3)

    def test_skips_near_duplicates(self):
        # candidates 0 and 1 are identical (sim 1.0); dedup should drop one
        ranked = [{"candidate": "a", "soft": 0.9}, {"candidate": "a", "soft": 0.8},
                  {"candidate": "b", "soft": 0.7}]
        sim = lambda x, y: 1.0 if x == y else 0.0
        out = greedy_diverse_select(ranked, 3, similarity_fn=sim, max_similarity=0.9)
        self.assertEqual([o["candidate"] for o in out], ["a", "b"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
