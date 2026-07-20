"""Face-detection + size-gate tests for shared/crops.py.

WHY NO REAL PHOTOS AS FIXTURES: the only photos that exercise these paths are real
users' training uploads (personal data). They are deliberately NOT committed. The
detection behaviour is tested by stubbing `detect_faces`, which is the seam the
production code actually depends on; the ONNX model itself is OpenCV's, already tested
upstream. The measured real-world numbers that motivated each threshold are recorded in
the crops.py module docstring so the reasoning survives without the images.
"""
import io
import os
import sys
import unittest

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared import crops  # noqa: E402
from shared.crops import (  # noqa: E402
    FaceTooSmallError, NoFaceError, MultipleFacesError,
    MIN_FACE_PX, WARN_FACE_PX, DEFAULT_SIZE, DEFAULT_FACE_FRAC,
    crop_head_and_shoulders, detect_largest_face, head_and_shoulders_box, assess,
)


def _img_bytes(w=1200, h=1600, colour=(120, 120, 120)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), colour).save(buf, "JPEG", quality=90)
    return buf.getvalue()


class FaceSizeGate(unittest.TestCase):
    """A correctly-detected but tiny face must be rejected, not upscaled into mush."""

    def setUp(self):
        self._real = crops.detect_faces
        self.addCleanup(lambda: setattr(crops, "detect_faces", self._real))

    def _stub(self, boxes):
        crops.detect_faces = lambda bgr: boxes

    def test_rejects_face_below_minimum(self):
        # 86px was the measured height of a real upload that produced a ~3.8x upscale.
        self._stub([((500, 500, 68, 86), 0.94)])
        with self.assertRaises(FaceTooSmallError) as ctx:
            crop_head_and_shoulders(_img_bytes())
        self.assertEqual(ctx.exception.face_px, 86)
        self.assertEqual(ctx.exception.required_px, MIN_FACE_PX)

    def test_accepts_face_at_minimum(self):
        self._stub([((400, 400, 180, MIN_FACE_PX), 0.94)])
        out = crop_head_and_shoulders(_img_bytes())
        self.assertTrue(out and isinstance(out, bytes))

    def test_no_face_still_raises(self):
        self._stub([])
        with self.assertRaises(NoFaceError):
            crop_head_and_shoulders(_img_bytes())

    def test_group_photo_still_raises(self):
        # Two comparably-sized faces -> solo-photo rule trips before the size gate.
        self._stub([((100, 100, 200, 250), 0.95), ((700, 120, 190, 240), 0.93)])
        with self.assertRaises(MultipleFacesError):
            crop_head_and_shoulders(_img_bytes())


class EyeOcclusionGate(unittest.TestCase):
    """Sunglasses must be rejected: eyes carry more identity than any other feature, and
    a set where the only sharp photos are sunglasses trains an eyeless adapter.

    Thresholds come from measurement on a real upload set (see crops.py):
        sunglasses    0.37, 0.55
        eyes visible  0.91 - 1.04
    """

    def setUp(self):
        self._faces = crops.detect_faces
        self._ratio = crops.eye_visibility_ratio
        self.addCleanup(lambda: setattr(crops, "detect_faces", self._faces))
        self.addCleanup(lambda: setattr(crops, "eye_visibility_ratio", self._ratio))
        crops.detect_faces = lambda bgr: [((400, 400, 200, 260), 0.95)]

    def test_rejects_measured_sunglasses_values(self):
        for ratio in (0.366, 0.553):
            crops.eye_visibility_ratio = lambda bgr, r=ratio: r
            with self.assertRaises(crops.EyesOccludedError):
                crop_head_and_shoulders(_img_bytes())

    def test_accepts_measured_visible_eye_values(self):
        for ratio in (0.913, 0.933, 1.037):
            crops.eye_visibility_ratio = lambda bgr, r=ratio: r
            self.assertTrue(crop_head_and_shoulders(_img_bytes()))

    def test_unmeasurable_is_not_treated_as_occluded(self):
        # None means "could not measure", NOT "covered" — a detector quirk must never
        # silently reject a good photo.
        crops.eye_visibility_ratio = lambda bgr: None
        self.assertTrue(crop_head_and_shoulders(_img_bytes()))

    def test_size_gate_takes_priority(self):
        # A tiny face is unusable regardless of eyes; report the actionable reason.
        crops.detect_faces = lambda bgr: [((400, 400, 60, 86), 0.94)]
        crops.eye_visibility_ratio = lambda bgr: 0.3
        with self.assertRaises(FaceTooSmallError):
            crop_head_and_shoulders(_img_bytes())

    def test_inspection_mode_skips_both_gates(self):
        crops.eye_visibility_ratio = lambda bgr: 0.2
        self.assertTrue(crop_head_and_shoulders(_img_bytes(), enforce_min_face=False))


class HighestConfidenceWins(unittest.TestCase):
    """Regression for the masonry bug: a BIGGER low-confidence box must not beat the
    smaller high-confidence real face. Haar had no score and picked the largest, so a
    university crest at 178px won over a 68px face and a photo of a wall was trained on.
    """

    def setUp(self):
        self._real = crops.detect_faces
        self.addCleanup(lambda: setattr(crops, "detect_faces", self._real))

    def test_picks_confidence_not_area(self):
        crest = ((354, 522, 178, 178), 0.41)
        person = ((820, 685, 68, 86), 0.94)
        crops.detect_faces = lambda bgr: sorted([crest, person], key=lambda t: -t[1])
        bgr = np.zeros((1600, 1200, 3), dtype=np.uint8)
        self.assertEqual(detect_largest_face(bgr), person[0])


class Framing(unittest.TestCase):
    def test_face_occupies_configured_fraction(self):
        face_h = 300
        _, _, side = head_and_shoulders_box((500, 400, 240, face_h), 2000, 2000)
        self.assertAlmostEqual(side, face_h / DEFAULT_FACE_FRAC, delta=2)

    def test_box_stays_inside_image(self):
        left, top, side = head_and_shoulders_box((10, 10, 100, 120), 640, 480)
        self.assertGreaterEqual(left, 0)
        self.assertGreaterEqual(top, 0)
        self.assertLessEqual(left + side, 640)
        self.assertLessEqual(top + side, 480)

    def test_warn_threshold_is_the_no_upscale_point(self):
        self.assertEqual(WARN_FACE_PX, round(DEFAULT_SIZE * DEFAULT_FACE_FRAC))


class Assess(unittest.TestCase):
    def test_blank_image_reports_no_face(self):
        self.assertEqual(assess(_img_bytes())["code"], "FACE_NOT_FOUND")

    def test_garbage_bytes_report_not_an_image(self):
        self.assertEqual(assess(b"not an image")["code"], "NOT_AN_IMAGE")

    def test_assess_never_raises_on_bad_input(self):
        for bad in (b"", b"\x00\x01\x02", _img_bytes(8, 8)):
            self.assertIn("code", assess(bad))


if __name__ == "__main__":
    unittest.main(verbosity=2)
