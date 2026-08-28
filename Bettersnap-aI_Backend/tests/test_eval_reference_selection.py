"""Reference-selection strategy B — quality ranking + disqualification. YuNet/composition is
stubbed so the ranking math is deterministic without a model or committed fixtures."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from evaluation import reference_selection as rs  # noqa: E402
from evaluation import composition as comp  # noqa: E402


class ReferenceSelection(unittest.TestCase):
    def setUp(self):
        self._c = comp.composition_scores
        self._s = comp.sharpness
        self.addCleanup(lambda: setattr(comp, "composition_scores", self._c))
        self.addCleanup(lambda: setattr(comp, "sharpness", self._s))
        # reference_selection imported the names directly; patch there too
        self._rc = rs.composition_scores
        self._rs = rs.sharpness
        self.addCleanup(lambda: setattr(rs, "composition_scores", self._rc))
        self.addCleanup(lambda: setattr(rs, "sharpness", self._rs))

    def _mk(self, mapping, sharp=200.0):
        # mapping: id(int via array[0,0]) -> composition dict
        rs.composition_scores = lambda img: mapping[int(img[0, 0, 0])]
        rs.sharpness = lambda img: sharp

    def _img(self, tag):
        a = np.zeros((10, 10, 3), np.uint8)
        a[0, 0, 0] = tag
        return a

    def test_no_face_is_unusable(self):
        self._mk({0: {"face_count": 0}})
        s = rs.score_reference(self._img(0))
        self.assertFalse(s["usable"])
        self.assertEqual(s["reason"], "no_face")

    def test_turned_face_disqualified(self):
        self._mk({0: {"face_count": 1, "yaw_offset": 0.6, "eye_visibility": 0.9,
                      "face_height_pct": 40}})
        self.assertFalse(rs.score_reference(self._img(0))["usable"])

    def test_sunglasses_disqualified(self):
        self._mk({0: {"face_count": 1, "yaw_offset": 0.0, "eye_visibility": 0.4,
                      "face_height_pct": 40}})
        self.assertEqual(rs.score_reference(self._img(0))["reason"][:4], "eyes")

    def test_frontal_beats_turned(self):
        mapping = {
            0: {"face_count": 1, "yaw_offset": 0.30, "eye_visibility": 0.9, "face_height_pct": 40},
            1: {"face_count": 1, "yaw_offset": 0.02, "eye_visibility": 0.9, "face_height_pct": 40},
        }
        self._mk(mapping)
        best, scores = rs.select_best_reference([self._img(0), self._img(1)])
        self.assertEqual(best, 1, "the more frontal crop should win")
        self.assertGreater(scores[1]["quality"], scores[0]["quality"])

    def test_all_unusable_returns_none(self):
        self._mk({0: {"face_count": 0}, 1: {"face_count": 2}})
        best, scores = rs.select_best_reference([self._img(0), self._img(1)])
        self.assertIsNone(best)

    def test_tie_breaks_to_lower_index(self):
        mapping = {
            0: {"face_count": 1, "yaw_offset": 0.05, "eye_visibility": 0.9, "face_height_pct": 40},
            1: {"face_count": 1, "yaw_offset": 0.05, "eye_visibility": 0.9, "face_height_pct": 40},
        }
        self._mk(mapping)
        best, _ = rs.select_best_reference([self._img(0), self._img(1)])
        self.assertEqual(best, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
