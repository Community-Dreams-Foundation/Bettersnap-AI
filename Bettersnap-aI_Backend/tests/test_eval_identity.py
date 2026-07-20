"""Identity-centroid + scoring logic (pure numpy — synthetic embeddings, no model)."""
import os
import sys
import unittest

import numpy as np

# repo root (for the `evaluation` package) is two levels up from Bettersnap-aI_Backend/tests
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from evaluation.centroid import l2_normalize, remove_outliers, build_centroid  # noqa: E402
from evaluation.identity import cosine, identity_scores  # noqa: E402


def _cluster(base, n, jitter, seed):
    rng = np.random.RandomState(seed)
    return l2_normalize(base + jitter * rng.randn(n, len(base)))


class Centroid(unittest.TestCase):
    def test_l2_normalize_unit_length(self):
        v = l2_normalize([3.0, 4.0])
        self.assertAlmostEqual(float(np.linalg.norm(v)), 1.0, places=6)

    def test_centroid_of_tight_cluster_points_at_base(self):
        base = l2_normalize([1.0, 0.2, -0.3, 0.5])
        E = _cluster(base, 10, 0.02, seed=1)
        c = build_centroid(E)
        self.assertGreater(cosine(c["centroid"], base), 0.99)
        self.assertEqual(c["n_kept"], 10)
        self.assertGreater(c["intra_cohesion"], 0.9)

    def test_outlier_is_removed(self):
        base = l2_normalize([1.0, 0.0, 0.0, 0.0])
        E = _cluster(base, 8, 0.02, seed=2)
        # inject a clear outlier pointing the other way (a bad crop / wrong face)
        outlier = l2_normalize([-1.0, 0.1, 0.0, 0.0])
        E = np.vstack([E, outlier])
        kept, idx = remove_outliers(E)
        self.assertNotIn(len(E) - 1, idx, "the injected outlier must be dropped")
        self.assertEqual(len(kept), len(E) - 1)

    def test_outlier_does_not_distort_centroid(self):
        base = l2_normalize([0.0, 1.0, 0.0])
        E = np.vstack([_cluster(base, 8, 0.02, seed=3), l2_normalize([0.0, -1.0, 0.2])])
        c = build_centroid(E)
        # centroid should still align with the clean cluster, not be dragged toward outlier
        self.assertGreater(cosine(c["centroid"], base), 0.98)

    def test_never_prunes_below_min_keep(self):
        # 3 mutually-different vectors: min_keep protects them all
        E = np.eye(3)
        kept, idx = remove_outliers(E, min_keep=3)
        self.assertEqual(len(kept), 3)

    def test_low_cohesion_flags_weak_set(self):
        # near-orthogonal embeddings = an inconsistent input set
        E = l2_normalize(np.eye(5) + 0.01)
        c = build_centroid(E)
        self.assertLess(c["intra_cohesion"], 0.5)

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            build_centroid(np.zeros((0, 4)))


class Scoring(unittest.TestCase):
    def test_generic_face_near_centroid_but_far_from_members(self):
        # Two distinct accepted looks; their centroid is a blend matching NEITHER well.
        a = l2_normalize([1.0, 1.0, 0.0])
        b = l2_normalize([1.0, -1.0, 0.0])
        members = np.vstack([a, b])
        centroid = build_centroid(members, min_keep=2)["centroid"]
        generic = centroid  # a generated "average face" sitting exactly on the centroid
        s = identity_scores(generic, centroid, members)
        # high centroid similarity...
        self.assertGreater(s["centroid_similarity"], 0.99)
        # ...but min-member catches that it matches neither real photo as well
        self.assertLess(s["min_member_similarity"], s["centroid_similarity"])

    def test_true_match_scores_high_on_all(self):
        base = l2_normalize([0.3, 0.7, -0.2, 0.6])
        members = _cluster(base, 6, 0.03, seed=7)
        centroid = build_centroid(members)["centroid"]
        s = identity_scores(base, centroid, members)
        self.assertGreater(s["centroid_similarity"], 0.95)
        self.assertGreater(s["min_member_similarity"], 0.9)

    def test_scores_without_members(self):
        c = l2_normalize([1.0, 0.0])
        s = identity_scores([1.0, 0.0], c)
        self.assertIn("centroid_similarity", s)
        self.assertNotIn("min_member_similarity", s)


if __name__ == "__main__":
    unittest.main(verbosity=2)
