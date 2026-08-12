"""Tests for shared.startup_stall — the image-pull vs post-pull-start stall classifier.

Pure functions, no Azure, no workload. Run:
    python -m unittest tests.test_startup_stall   (from the backend dir)
"""
import os
import sys
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from shared import startup_stall as ss  # noqa: E402


def ev(reason, t):
    return {"reason": reason, "time": t}


class StartupStallTests(unittest.TestCase):
    def test_container_started_is_not_a_stall(self):
        events = [ev("AssigningReplica", 0), ev("PulledImage", 100), ev("ContainerStarted", 220)]
        r = ss.classify_startup(events, now=100000)
        self.assertEqual(r["classification"], ss.RUNNING)
        self.assertFalse(r["should_stop"])

    def test_terminated_is_terminal(self):
        events = [ev("AssigningReplica", 0), ev("ContainerTerminated", 500)]
        r = ss.classify_startup(events, now=100000)
        self.assertEqual(r["classification"], ss.TERMINATED)
        self.assertFalse(r["should_stop"])

    def test_still_pulling_before_threshold_is_pending(self):
        events = [ev("AssigningReplica", 0)]           # no PulledImage yet
        r = ss.classify_startup(events, now=300)        # < 480
        self.assertEqual(r["classification"], ss.PENDING)
        self.assertFalse(r["should_stop"])

    def test_still_pulling_past_threshold_is_image_pull_stall(self):
        events = [ev("AssigningReplica", 0)]           # never pulled
        r = ss.classify_startup(events, now=500)        # > 480
        self.assertEqual(r["classification"], ss.INFRA_IMAGE_PULL_STALL)
        self.assertTrue(r["should_stop"])

    def test_pull_finished_late_gets_post_pull_grace_not_stopped_at_8min(self):
        # pull completes at 470s (just under the 8-min mark); at 490s we must NOT stop —
        # the grace runs from pull completion (470+300=770).
        events = [ev("AssigningReplica", 0), ev("PulledImage", 470)]
        r = ss.classify_startup(events, now=490)
        self.assertEqual(r["classification"], ss.PENDING)
        self.assertFalse(r["should_stop"])
        self.assertEqual(r["deadline"], 470 + ss.POST_PULL_GRACE_S)

    def test_post_pull_grace_expired_is_post_pull_start_stall(self):
        events = [ev("AssigningReplica", 0), ev("PulledImage", 470)]
        r = ss.classify_startup(events, now=800)        # > 770
        self.assertEqual(r["classification"], ss.INFRA_POST_PULL_START_STALL)
        self.assertTrue(r["should_stop"])

    # ── Replay of the real Run #2 (v4bd2yj), seconds from AssigningReplica ──────
    #   assign  t=0        (17:43:13)
    #   pull    t=1042     (18:00:35)
    #   operator stop at t=1160 (18:02:33) — 118s after pull completed
    def test_run2_stop_at_1160s_was_premature(self):
        events = [ev("AssigningReplica", 0), ev("PulledImage", 1042)]
        r = ss.classify_startup(events, now=1160)
        self.assertEqual(r["classification"], ss.PENDING,
                         "Run #2 was still within the post-pull grace -> stopping was premature")
        self.assertFalse(r["should_stop"])
        self.assertEqual(r["deadline"], 1042 + ss.POST_PULL_GRACE_S)  # 1342

    def test_run2_would_be_stall_after_deadline(self):
        events = [ev("AssigningReplica", 0), ev("PulledImage", 1042)]
        r = ss.classify_startup(events, now=1400)       # > 1342
        self.assertEqual(r["classification"], ss.INFRA_POST_PULL_START_STALL)
        self.assertTrue(r["should_stop"])

    def test_infra_classes_are_credit_safe_set(self):
        self.assertIn(ss.INFRA_IMAGE_PULL_STALL, ss.INFRA_STALL_CLASSES)
        self.assertIn(ss.INFRA_POST_PULL_START_STALL, ss.INFRA_STALL_CLASSES)

    def test_no_assignment_is_pending(self):
        r = ss.classify_startup([], now=100000)
        self.assertEqual(r["classification"], ss.PENDING)
        self.assertFalse(r["should_stop"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
