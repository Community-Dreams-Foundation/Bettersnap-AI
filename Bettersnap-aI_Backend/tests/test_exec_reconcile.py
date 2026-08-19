"""Pure-classifier tests for shared.exec_reconcile — the six execution-outcome cases the
control-plane watcher must handle. No Azure, no DB, no workload.

Run: python -m unittest tests.test_exec_reconcile   (from the backend dir)
"""
import os
import sys
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from shared import exec_reconcile as er  # noqa: E402
from shared import startup_stall as ss   # noqa: E402


def ev(reason, t):
    return {"reason": reason, "time": t}


class SixCaseTests(unittest.TestCase):
    # 1) image-pull stall — still pulling well past the threshold
    def test_image_pull_stall(self):
        d = er.classify_execution(
            {"exit_code": None, "reason": None, "events": [ev("AssigningReplica", 0)],
             "execution_id": "x"}, now=600)
        self.assertEqual(d["action"], er.ACTION_REFUND)
        self.assertEqual(d["failure_class"], ss.INFRA_IMAGE_PULL_STALL.lower())
        self.assertTrue(d["is_infra"])

    # 2) image pulled but container never started (the real Run #2 shape)
    def test_pulled_but_not_started(self):
        d = er.classify_execution(
            {"exit_code": None, "reason": None,
             "events": [ev("AssigningReplica", 0), ev("PulledImage", 1042)],
             "execution_id": "v4bd2yj"}, now=1500)
        self.assertEqual(d["action"], er.ACTION_REFUND)
        self.assertEqual(d["failure_class"], ss.INFRA_POST_PULL_START_STALL.lower())
        self.assertTrue(d["is_infra"])

    # 3a) normal slow pull followed by a successful start (still running) -> do nothing
    def test_slow_pull_then_started_is_pending(self):
        d = er.classify_execution(
            {"exit_code": None, "reason": None,
             "events": [ev("AssigningReplica", 0), ev("PulledImage", 300), ev("ContainerStarted", 420)],
             "execution_id": "x"}, now=100000)
        self.assertEqual(d["action"], er.ACTION_NONE)
        self.assertEqual(d["failure_class"], er.CLASS_PENDING)
        self.assertFalse(d["is_infra"])

    # 3b) ACA success still requires proof that result blobs landed.
    def test_clean_success(self):
        d = er.classify_execution(
            {"execution_status": "Succeeded", "preflight_exit_code": 0, "events": []},
            now=0,
        )
        self.assertEqual(d["action"], er.ACTION_VERIFY_DELIVERY)
        self.assertEqual(d["failure_class"], er.CLASS_SUCCESS)
        self.assertFalse(d["is_infra"])

    def test_passing_preflight_does_not_hide_failed_execution(self):
        d = er.classify_execution(
            {"execution_status": "Failed", "preflight_exit_code": 0,
             "execution_id": "oom-exec"},
            now=0,
        )
        self.assertEqual(d["action"], er.ACTION_REFUND)
        self.assertEqual(d["failure_class"], er.CLASS_APPLICATION)

    def test_passing_preflight_without_terminal_aca_status_is_pending(self):
        d = er.classify_execution(
            {"execution_status": "Running", "preflight_exit_code": 0,
             "events": [ev("ContainerStarted", 1)]},
            now=10,
        )
        self.assertEqual(d["action"], er.ACTION_NONE)
        self.assertEqual(d["failure_class"], er.CLASS_PENDING)

    # 4) manually stopped execution -> refund, but NOT infra
    def test_manually_stopped(self):
        d = er.classify_execution(
            {"exit_code": None, "reason": "ManuallyStopped", "events": [], "execution_id": "x"}, now=0)
        self.assertEqual(d["action"], er.ACTION_REFUND)
        self.assertEqual(d["failure_class"], er.CLASS_OPERATOR_STOPPED)
        self.assertFalse(d["is_infra"])

    # 5) container preflight exit 42 / 43 / 44 -> infra, refund, distinct class
    def test_preflight_exit_codes(self):
        for code, cls in ((42, "infra_no_gpu"), (43, "infra_gpu_unusable"), (44, "infra_gpu_probe_error")):
            d = er.classify_execution(
                {"execution_status": "Failed", "preflight_exit_code": code,
                 "events": [ev("AssigningReplica", 0), ev("ContainerStarted", 200)],
                 "execution_id": "x", "diagnostic_reason": f"reason-{code}"}, now=0)
            self.assertEqual(d["action"], er.ACTION_REFUND, code)
            self.assertEqual(d["failure_class"], cls, code)
            self.assertTrue(d["is_infra"], code)
            self.assertEqual(d["reason"], f"reason-{code}", "diagnostic reason preserved")

    # 6) application exit AFTER the preflight passed and the container started -> NOT infra
    def test_application_failure_after_start(self):
        d = er.classify_execution(
            {"execution_status": "Failed", "preflight_exit_code": 0,
             "events": [ev("AssigningReplica", 0), ev("PulledImage", 100), ev("ContainerStarted", 220)],
             "execution_id": "x"}, now=0)
        self.assertEqual(d["action"], er.ACTION_REFUND)
        self.assertEqual(d["failure_class"], er.CLASS_APPLICATION)
        self.assertFalse(d["is_infra"], "an application failure must never be labelled infra")


class TaxonomyTests(unittest.TestCase):
    def test_infra_classes_membership(self):
        for c in ("infra_no_gpu", "infra_gpu_unusable", "infra_gpu_probe_error",
                  ss.INFRA_IMAGE_PULL_STALL.lower(), ss.INFRA_POST_PULL_START_STALL.lower()):
            self.assertIn(c, er.INFRA_CLASSES)
        for c in (er.CLASS_APPLICATION, er.CLASS_OPERATOR_STOPPED, er.CLASS_SUCCESS,
                  er.CLASS_PENDING, er.CLASS_DELIVERY_MISSING):
            self.assertNotIn(c, er.INFRA_CLASSES)

    def test_preflight_infra_wins_over_application_branch(self):
        # exit 43 also occurs after ContainerStarted; the infra branch must win.
        d = er.classify_execution(
            {"execution_status": "Failed", "preflight_exit_code": 43,
             "events": [ev("ContainerStarted", 10)]}, now=0)
        self.assertEqual(d["failure_class"], "infra_gpu_unusable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
