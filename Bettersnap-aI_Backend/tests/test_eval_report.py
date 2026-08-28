"""Report structure (four separate groups, no blend) + blinded review builder."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from evaluation.report import per_image_report, run_report  # noqa: E402
from evaluation.human_review import build_blinded_review  # noqa: E402


class ReportShape(unittest.TestCase):
    def test_four_groups_present_and_no_blend(self):
        r = per_image_report(
            identity={"centroid_similarity": 0.8, "min_member_similarity": 0.7},
            composition={"face_count": 1, "face_height_pct": 40.0},
            sharpness=120.0)
        for g in ("identity", "composition", "prompt_adherence", "visual_quality"):
            self.assertIn(g, r)
        self.assertNotIn("overall", r, "there must be NO combined score")
        self.assertNotIn("score", r)

    def test_prompt_adherence_is_pending_not_faked(self):
        r = per_image_report()
        self.assertEqual(r["prompt_adherence"]["status"], "not_implemented")

    def test_visual_quality_flags_human_review(self):
        r = per_image_report(sharpness=50.0)
        self.assertTrue(r["visual_quality"]["human_review_required"])
        self.assertEqual(r["visual_quality"]["skin_realism"]["status"], "human_review_only")

    def test_missing_groups_marked_not_faked(self):
        r = per_image_report()
        self.assertEqual(r["identity"]["status"], "no_embedder")

    def test_run_report_aggregates_without_blending(self):
        imgs = [
            per_image_report(identity={"centroid_similarity": 0.9, "min_member_similarity": 0.8},
                             composition={"face_count": 1, "face_height_pct": 42.0, "yaw_offset": 0.1},
                             sharpness=100.0),
            per_image_report(identity={"centroid_similarity": 0.7, "min_member_similarity": 0.5},
                             composition={"face_count": 2, "face_height_pct": 20.0, "yaw_offset": -0.4},
                             sharpness=80.0),
        ]
        rep = run_report("baseline", "user_x", imgs,
                         centroid_meta={"n_total": 10, "n_kept": 8, "intra_cohesion": 0.3})
        self.assertEqual(rep["n_images"], 2)
        self.assertEqual(rep["composition"]["multi_face_count"], 1)
        self.assertEqual(rep["identity"]["centroid_similarity"]["n"], 2)
        self.assertTrue(rep["reference_set"]["weak_input_warning"], "cohesion 0.3 => weak")
        self.assertNotIn("overall", rep)


class BlindedReview(unittest.TestCase):
    def _items(self):
        return [{"pipeline_id": p, "dataset_id": "u1", "image_path": f"/x/{p}_{i}.png"}
                for p in ("A", "B") for i in range(3)]

    def test_sheet_hides_pipeline_key_unblinds(self):
        out = build_blinded_review(self._items(), seed=42)
        self.assertEqual(len(out["sheet"]), 6)
        for row in out["sheet"]:
            self.assertEqual(set(row.keys()), {"review_id", "image_path"},
                             "sheet must NOT leak pipeline_id/dataset_id")
        # every review_id resolves back to full provenance
        for row in out["sheet"]:
            self.assertIn("pipeline_id", out["key"][row["review_id"]])

    def test_deterministic_for_seed(self):
        a = build_blinded_review(self._items(), seed=7)
        b = build_blinded_review(self._items(), seed=7)
        self.assertEqual([r["image_path"] for r in a["sheet"]],
                         [r["image_path"] for r in b["sheet"]])

    def test_is_actually_shuffled(self):
        items = self._items()
        out = build_blinded_review(items, seed=1)
        self.assertNotEqual([r["image_path"] for r in out["sheet"]],
                            [it["image_path"] for it in items])

    def test_missing_image_path_raises(self):
        with self.assertRaises(ValueError):
            build_blinded_review([{"pipeline_id": "A"}], seed=1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
