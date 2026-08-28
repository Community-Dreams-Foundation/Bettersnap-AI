"""Tests for stage_runtime.run_stage — the REAL production helper, not a copy.

stage_runtime.py lives at the repo root and is deliberately free of torch/diffusers/cv2,
so main.py's stage-init policy (disabled / ok / fatal / degrade) is verifiable here without
the GPU stack. main.py owns the init_fn bodies; those are validated by the GPU build.
"""
import os
import sys
import unittest

# repo root is two levels up from Bettersnap-aI_Backend/tests/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from stage_runtime import run_stage  # noqa: E402  — the real helper under test


class RunStage(unittest.TestCase):
    def setUp(self):
        self.status = {}
        self.logs = []
        self.log = self.logs.append

    def _init_ok(self):
        self.called = True

    def _init_boom(self):
        raise ValueError("weights missing")

    # ── disabled ──────────────────────────────────────────────────────────────
    def test_disabled_records_and_skips(self):
        self.called = False
        ret = run_stage("realism", False, self._init_ok, self.status, log=self.log)
        self.assertFalse(ret)
        self.assertFalse(self.called, "init_fn must NOT run when disabled")
        self.assertEqual(self.status["realism"],
                         {"enabled": False, "initialized": False, "reason": "disabled"})

    # ── enabled + ok ──────────────────────────────────────────────────────────
    def test_enabled_ok(self):
        self.called = False
        ret = run_stage("face_refine", True, self._init_ok, self.status, log=self.log)
        self.assertTrue(ret)
        self.assertTrue(self.called)
        self.assertEqual(self.status["face_refine"],
                         {"enabled": True, "initialized": True, "reason": "ok"})

    # ── enabled + fail + fatal (default) ──────────────────────────────────────
    def test_enabled_fatal_failure_raises_and_records(self):
        with self.assertRaises(RuntimeError) as ctx:
            run_stage("face_refine", True, self._init_boom, self.status, log=self.log)
        # original cause chained
        self.assertIsInstance(ctx.exception.__cause__, ValueError)
        s = self.status["face_refine"]
        self.assertEqual(s["enabled"], True)
        self.assertEqual(s["initialized"], False)
        self.assertEqual(s["reason"], "init_failed")
        # exception detail preserved, machine-readable
        self.assertEqual(s["error"], "ValueError: weights missing")

    # ── enabled + fail + non-fatal (upscale) ──────────────────────────────────
    def test_enabled_degrade_records_and_continues(self):
        ret = run_stage("upscale", True, self._init_boom, self.status,
                        fatal=False, degraded_reason="degraded_to_1024", log=self.log)
        self.assertFalse(ret, "degraded stage returns False (unusable) but does not raise")
        s = self.status["upscale"]
        self.assertEqual(s["reason"], "degraded_to_1024")
        self.assertEqual(s["error"], "ValueError: weights missing")
        self.assertEqual(s["initialized"], False)

    def test_error_preserves_exception_type(self):
        def _boom():
            raise KeyError("params_ema")
        run_stage("upscale", True, _boom, self.status, fatal=False, log=self.log)
        # KeyError str() adds quotes — assert the type prefix and continue
        self.assertTrue(self.status["upscale"]["error"].startswith("KeyError:"))

    def test_status_dict_is_caller_owned(self):
        # Two independent dicts don't bleed into each other (concurrency safety property).
        a, b = {}, {}
        run_stage("x", False, self._init_ok, a, log=self.log)
        run_stage("y", True, self._init_ok, b, log=self.log)
        self.assertIn("x", a)
        self.assertNotIn("x", b)
        self.assertIn("y", b)
        self.assertNotIn("y", a)


if __name__ == "__main__":
    unittest.main(verbosity=2)
