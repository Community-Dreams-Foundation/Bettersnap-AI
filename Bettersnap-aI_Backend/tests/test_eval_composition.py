"""Composition geometry math. The YuNet detector is stubbed so the arithmetic
(face-height %, centering, headroom, yaw, clipping) is verified deterministically without a
model or committed face fixtures."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from evaluation import composition as comp  # noqa: E402


class Geometry(unittest.TestCase):
    def setUp(self):
        self._orig = comp._crops
        self.addCleanup(lambda: setattr(comp, "_crops", self._orig))

    def _stub(self, faces, eye=0.9, count=1):
        comp._crops = lambda: (lambda img: faces, lambda img: eye, lambda img: count)

    def test_no_face(self):
        self._stub([])
        self.assertEqual(comp.composition_scores(np.zeros((1000, 800, 3), np.uint8)),
                         {"face_count": 0})

    def test_centered_frontal_face(self):
        # 800x1000 frame, face 200x240 at (300,300): centered horizontally, frontal eyes/nose
        pts = [(350, 380), (450, 380), (400, 430), (360, 500), (440, 500)]
        self._stub([((300, 300, 200, 240), 0.94, pts)])
        s = comp.composition_scores(np.zeros((1000, 800, 3), np.uint8))
        self.assertEqual(s["face_count"], 1)
        self.assertAlmostEqual(s["face_height_pct"], 24.0, places=1)
        self.assertAlmostEqual(s["center_offset"], 0.0, places=2)      # face center == frame center
        self.assertAlmostEqual(s["headroom_frac"], 0.30, places=2)
        self.assertAlmostEqual(s["yaw_offset"], 0.0, places=2)         # nose at eye midpoint
        self.assertFalse(s["top_clipped"])
        self.assertFalse(s["chin_clipped"])
        self.assertEqual(s["eye_visibility"], 0.9)

    def test_turned_head_has_yaw(self):
        # nose shifted toward the right eye (x=350) => nonzero yaw
        pts = [(350, 380), (450, 380), (365, 430), (360, 500), (440, 500)]
        self._stub([((300, 300, 200, 240), 0.9, pts)])
        s = comp.composition_scores(np.zeros((1000, 800, 3), np.uint8))
        self.assertLess(s["yaw_offset"], -0.2)   # nose left of eye-midpoint (400)/span(100)

    def test_off_center_face(self):
        pts = [(650, 380), (750, 380), (700, 430), (660, 500), (740, 500)]
        self._stub([((600, 300, 200, 240), 0.9, pts)])
        s = comp.composition_scores(np.zeros((1000, 800, 3), np.uint8))
        self.assertGreater(s["center_offset"], 0.3)   # face pushed to the right edge

    def test_clipping_flags(self):
        pts = [(350, 5), (450, 5), (400, 40), (360, 90), (440, 90)]
        self._stub([((300, 0, 200, 1000), 0.9, pts)])   # spans full height
        s = comp.composition_scores(np.zeros((1000, 800, 3), np.uint8))
        self.assertTrue(s["top_clipped"])
        self.assertTrue(s["chin_clipped"])

    def test_multi_face_reported(self):
        pts = [(350, 380), (450, 380), (400, 430), (360, 500), (440, 500)]
        self._stub([((300, 300, 200, 240), 0.9, pts)], count=3)
        s = comp.composition_scores(np.zeros((1000, 800, 3), np.uint8))
        self.assertEqual(s["face_count"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
