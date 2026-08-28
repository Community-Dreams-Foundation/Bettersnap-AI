"""Phase-5 composition control: face-size band classifier, new composition metrics
(eye-line, shoulder room), and the prompt-steering module."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from evaluation import composition as comp  # noqa: E402
from evaluation.composition import classify_face_size, HEADSHOT_BANDS  # noqa: E402
from prompt_control import (  # noqa: E402
    apply_composition_control, FRAMING_POSITIVE, FRAMING_NEGATIVE,
)


class FaceSizeBands(unittest.TestCase):
    def test_bands(self):
        self.assertEqual(classify_face_size(18.0), "too_small")
        self.assertEqual(classify_face_size(30.0), "below_preferred")
        self.assertEqual(classify_face_size(42.0), "preferred")
        self.assertEqual(classify_face_size(55.0), "above_preferred")
        self.assertEqual(classify_face_size(70.0), "too_large")
        self.assertEqual(classify_face_size(None), "unknown")

    def test_band_edges(self):
        self.assertEqual(classify_face_size(HEADSHOT_BANDS["hard_min"]), "below_preferred")
        self.assertEqual(classify_face_size(HEADSHOT_BANDS["pref_lo"]), "preferred")
        self.assertEqual(classify_face_size(HEADSHOT_BANDS["pref_hi"]), "preferred")
        self.assertEqual(classify_face_size(HEADSHOT_BANDS["hard_max"]), "above_preferred")

    def test_generated_18pct_is_too_small(self):
        # the real generated headshot measured 18% -> objectively below the headshot floor
        self.assertEqual(classify_face_size(18.16), "too_small")


class NewCompositionMetrics(unittest.TestCase):
    def setUp(self):
        self._orig = comp._crops
        self.addCleanup(lambda: setattr(comp, "_crops", self._orig))

    def _stub(self, faces, eye=0.9, count=1):
        comp._crops = lambda: (lambda img: faces, lambda img: eye, lambda img: count)

    def test_eye_line_and_shoulder_room(self):
        # 800x1000 frame; face 200x240 at (300,300); eyes at y=380
        pts = [(350, 380), (450, 380), (400, 430), (360, 500), (440, 500)]
        self._stub([((300, 300, 200, 240), 0.94, pts)])
        s = comp.composition_scores(np.zeros((1000, 800, 3), np.uint8))
        self.assertAlmostEqual(s["eye_line_frac"], 0.38, places=2)      # 380/1000
        self.assertAlmostEqual(s["below_face_frac"], 0.46, places=2)    # (1000-540)/1000


class PromptControl(unittest.TestCase):
    def test_disabled_is_noop(self):
        t, n = apply_composition_control("a portrait.", "blurry", enabled=False)
        self.assertEqual(t, "a portrait.")
        self.assertEqual(n, "blurry")

    def test_enabled_appends_both(self):
        t, n = apply_composition_control("a portrait.", "blurry", enabled=True)
        self.assertIn(FRAMING_POSITIVE, t)
        self.assertIn(FRAMING_NEGATIVE, n)
        self.assertTrue(t.startswith("a portrait."))
        self.assertTrue(n.startswith("blurry,"))

    def test_idempotent(self):
        t1, n1 = apply_composition_control("a portrait.", "blurry", enabled=True)
        t2, n2 = apply_composition_control(t1, n1, enabled=True)
        self.assertEqual(t1, t2, "framing must not be appended twice")
        self.assertEqual(n1, n2)

    def test_handles_empty_negative(self):
        t, n = apply_composition_control("a portrait.", "", enabled=True)
        self.assertEqual(n, FRAMING_NEGATIVE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
