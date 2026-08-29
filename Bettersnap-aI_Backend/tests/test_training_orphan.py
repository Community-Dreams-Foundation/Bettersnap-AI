"""Deterministic handling of a training whose owning users row is gone.

THE LOOP THIS ENDS
The previous design rolled back and let the watcher retry. But a users UPDATE that matched 0
rows has PROVEN the row absent — re-running it on the next tick cannot recreate the user, so
the training never terminalized and every tick logged another CRITICAL. Nothing bounded it.

Now the condition is detected ONCE, up front, under UPDLOCK, before any write that targets the
users row; the training is closed exactly once with a durable operator-visible marker; and no
balance, ledger row or parked job is touched.

Two halves:
  * marker construction/parsing — pure, and where the truncation guarantees live;
  * the real _finish_training driven against the transactional fake.

No Azure, no database, no queue, no GPU.

Run: python -m unittest tests.test_training_orphan   (from the backend dir)
"""
import os
import sys
import unittest
from unittest import mock

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from shared import training_orphan as to                 # noqa: E402
from tests.test_fused_exhaustion_composed import (       # noqa: E402
    TxDB, TxConn, FakeLedgerModule, function_app,
)

TID = "11111111-2222-3333-4444-555555555555"
UID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


# ── the marker itself ────────────────────────────────────────────────────────
class MarkerConstruction(unittest.TestCase):
    def test_marker_is_anchored_at_offset_zero(self):
        """So `error LIKE 'ORPHAN_USER:%'` is a prefix predicate, not a substring scan."""
        marker, _ = to.build_orphan_marker(TID, UID)
        self.assertEqual(marker.index(to.MARKER_PREFIX), 0)
        self.assertTrue(to.is_orphan_marker(marker))

    def test_payload_carries_every_required_field(self):
        marker, _ = to.build_orphan_marker(TID, UID, monthly_owed=20, one_time_owed=15)
        payload = to.parse_orphan_marker(marker)
        self.assertEqual(payload["training_id"], TID)
        self.assertEqual(payload["user_id"], UID)
        self.assertEqual(payload["monthly_owed"], 20)
        self.assertEqual(payload["one_time_owed"], 15)
        self.assertEqual(payload["aggregate_owed"], 35)
        self.assertEqual(payload["reason"], to.REASON_PAID)

    def test_free_case_owes_nothing(self):
        payload = to.parse_orphan_marker(to.build_orphan_marker(TID, UID)[0])
        self.assertEqual(payload["aggregate_owed"], 0)
        self.assertEqual(payload["reason"], to.REASON_FREE)

    def test_original_error_is_preserved_after_the_marker(self):
        marker, _ = to.build_orphan_marker(TID, UID, original_error="CUDA OOM at step 42")
        self.assertEqual(to.original_error_from(marker), "CUDA OOM at step 42")
        self.assertIsNotNone(to.parse_orphan_marker(marker))

    def test_a_maximum_length_original_error_cannot_truncate_the_marker(self):
        """The whole point of building the marker FIRST: only the tail is trimmed."""
        huge = "X" * 5000
        marker, _ = to.build_orphan_marker(TID, UID, monthly_owed=999999,
                                           one_time_owed=999999, original_error=huge)
        self.assertLessEqual(len(marker), to.ERROR_COLUMN_MAX)
        payload = to.parse_orphan_marker(marker)
        self.assertIsNotNone(payload, "the marker must survive intact")
        self.assertEqual(payload["aggregate_owed"], 1999998)
        self.assertTrue(to.original_error_from(marker).startswith("X"))

    def test_an_original_error_containing_braces_and_separators_still_parses(self):
        nasty = 'weird {"training_id":"SPOOF"} | ORPHAN_USER:{"a":1} }}} " \\'
        marker, _ = to.build_orphan_marker(TID, UID, original_error=nasty)
        payload = to.parse_orphan_marker(marker)
        self.assertEqual(payload["training_id"], TID, "raw_decode must stop at the real brace")
        self.assertEqual(to.original_error_from(marker), nasty)

    def test_exactly_full_column_is_still_valid(self):
        marker, _ = to.build_orphan_marker(TID, UID, original_error="Y" * 5000)
        self.assertEqual(len(marker), to.ERROR_COLUMN_MAX)
        self.assertIsNotNone(to.parse_orphan_marker(marker))

    def test_an_oversized_marker_raises_rather_than_being_cut(self):
        with self.assertRaises(to.OrphanMarkerTooLong):
            to.build_orphan_marker(TID, UID, column_max=40)

    def test_non_markers_parse_as_none(self):
        for text in (None, "", "CUDA OOM", "prefix ORPHAN_USER:{}", "ORPHAN_USER:not-json"):
            self.assertIsNone(to.parse_orphan_marker(text))
            self.assertEqual(to.original_error_from(text), "")

    def test_negative_amounts_are_REJECTED_not_clamped(self):
        """The defect: max(0, ...) turned a corrupt -5 into 0 and then called the training
        FREE — an accounting corruption reported as a confident "nothing was owed"."""
        marker, payload = to.build_orphan_marker(TID, UID, monthly_owed=-5, one_time_owed=-9)
        self.assertEqual(payload["reason"], to.REASON_INVALID)
        self.assertFalse(payload["amounts_valid"])
        self.assertIsNone(payload["aggregate_owed"])
        self.assertIsNone(payload["monthly_owed"])
        self.assertIsNone(payload["one_time_owed"])
        self.assertIsNone(to.parse_orphan_marker(marker)["aggregate_owed"])

    def test_the_operator_query_predicate_matches(self):
        marker, _ = to.build_orphan_marker(TID, UID, original_error="anything")
        self.assertTrue(marker.startswith("ORPHAN_USER:"),
                        "LIKE 'ORPHAN_USER:%' must match")


# ── the real _finish_training against a missing user ─────────────────────────
class _Base(unittest.TestCase):
    monthly_charge = 0
    one_time_charge = 0
    seed_user = False

    def setUp(self):
        self.db = TxDB()
        if self.seed_user:
            self.db.add_user(UID, credits=0, monthly=0, one_time=0,
                             subscription_type="monthly")
        self.db.add_training(TID, user_id=UID, status="training",
                             monthly_credit_cost=self.monthly_charge,
                             one_time_credit_cost=self.one_time_charge)
        # two parked jobs owned by the missing user — they must NOT be swept
        self.db.add_job("PARK1", user_id=UID, status="waiting_lora")
        self.db.add_reserve("PARK1", user_id=UID)
        self.db.add_job("PARK2", user_id=UID, status="waiting_lora")
        self.db.add_reserve("PARK2", user_id=UID)
        self._patches = [
            mock.patch.object(function_app, "credit_ledger", FakeLedgerModule),
            mock.patch.object(function_app, "_identity_adapter_exists", lambda _u: False),
            mock.patch.object(function_app, "new_connection", lambda: TxConn(self.db)),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    def finish(self, ok=False, error="training crashed"):
        return function_app._finish_training(TID, UID, ok=ok, error=error)

    def marker(self):
        return to.parse_orphan_marker(self.db.trainings[TID]["error"])

    def assert_nothing_else_moved(self):
        self.assertEqual(self.db.users, {}, "no users row may be created or written")
        self.assertEqual(self.db.org_members, {})
        self.assertEqual([r for r in self.db.ledger if r[2] != "job_reserve"], [],
                         "no credit ledger row may describe a refund that did not happen")
        for name in ("PARK1", "PARK2"):
            self.assertEqual(self.db.jobs[name]["status"], "waiting_lora",
                             "%s must not be swept for a user that does not exist" % name)


class FreeTrainingMissingUser(_Base):
    monthly_charge = 0
    one_time_charge = 0

    def test_failed_free_training_terminalizes_once(self):
        self.finish(ok=False)
        self.assertEqual(self.db.trainings[TID]["status"], "failed")
        payload = self.marker()
        self.assertEqual(payload["reason"], to.REASON_FREE)
        self.assertEqual(payload["aggregate_owed"], 0)
        self.assert_nothing_else_moved()

    def test_successful_free_training_has_an_explicit_deterministic_outcome(self):
        """`ok` stays the TRUTH about the run: a training that really succeeded is recorded
        completed even though its owner is gone. It is still marked, and still not swept —
        releasing parked jobs for a nonexistent user would compound the inconsistency."""
        self.finish(ok=True, error=None)
        self.assertEqual(self.db.trainings[TID]["status"], "completed")
        self.assertEqual(self.marker()["reason"], to.REASON_FREE)
        self.assert_nothing_else_moved()

    def test_the_training_leaves_the_watcher_set_so_there_is_no_loop(self):
        self.finish()
        self.assertIn(self.db.trainings[TID]["status"], ("completed", "failed"),
                      "training_watcher only selects dispatching/training")

    def test_duplicate_calls_create_no_duplicate_effects(self):
        self.finish()
        first = dict(self.db.trainings[TID])
        for _ in range(4):
            self.finish()
        self.assertEqual(self.db.trainings[TID]["status"], first["status"])
        self.assertEqual(self.db.trainings[TID]["error"], first["error"])
        self.assert_nothing_else_moved()


class PaidTrainingMissingUser(_Base):
    monthly_charge = 20
    one_time_charge = 15

    def test_terminalizes_unresolved_with_the_exact_amounts_owed(self):
        self.finish()
        self.assertEqual(self.db.trainings[TID]["status"], "failed")
        payload = self.marker()
        self.assertEqual(payload["reason"], to.REASON_PAID)
        self.assertEqual(payload["monthly_owed"], 20)
        self.assertEqual(payload["one_time_owed"], 15)
        self.assertEqual(payload["aggregate_owed"], 35)
        self.assertEqual(payload["training_id"], TID)
        self.assertEqual(payload["user_id"], UID)
        self.assert_nothing_else_moved()

    def test_no_retrain_refund_ledger_row(self):
        self.finish()
        self.assertEqual([r for r in self.db.ledger if r[2] == "retrain_refund"], [])

    def test_marker_survives_when_an_original_error_already_exists(self):
        """The ordinary path uses COALESCE and would have kept the FIRST error, so the marker
        would frequently never have been stored at all."""
        self.db.trainings[TID]["error"] = "trainer exited 137 (OOM)"
        self.finish(error="watcher timeout")
        stored = self.db.trainings[TID]["error"]
        self.assertTrue(stored.startswith(to.MARKER_PREFIX))
        self.assertEqual(self.marker()["aggregate_owed"], 35)
        self.assertEqual(to.original_error_from(stored), "trainer exited 137 (OOM)",
                         "the pre-existing root cause is preserved after the marker")

    def test_a_maximum_length_existing_error_cannot_truncate_the_marker(self):
        self.db.trainings[TID]["error"] = "Z" * to.ERROR_COLUMN_MAX
        self.finish()
        stored = self.db.trainings[TID]["error"]
        self.assertLessEqual(len(stored), to.ERROR_COLUMN_MAX)
        self.assertIsNotNone(self.marker(), "the structured marker must survive intact")
        self.assertEqual(self.marker()["aggregate_owed"], 35)

    def test_duplicate_watcher_calls_do_not_double_mark_or_double_owe(self):
        self.finish()
        first = self.db.trainings[TID]["error"]
        for _ in range(4):
            self.finish()
        self.assertEqual(self.db.trainings[TID]["error"], first)
        self.assertEqual(self.db.trainings[TID]["status"], "failed")
        self.assert_nothing_else_moved()


class AlreadyTerminalFailsClosed(_Base):
    monthly_charge = 20
    one_time_charge = 15

    def test_terminal_update_rowcount_zero_writes_nothing(self):
        """A concurrent terminalization wins; this call must claim nothing."""
        self.db.trainings[TID]["status"] = "failed"
        self.db.trainings[TID]["error"] = "someone else got here first"
        self.finish()
        self.assertEqual(self.db.trainings[TID]["error"], "someone else got here first")
        self.assertIsNone(self.marker())
        self.assert_nothing_else_moved()


class PresentUserIsUnaffected(_Base):
    """The orphan path must not change anything for a normal, existing user."""

    monthly_charge = 20
    one_time_charge = 15
    seed_user = True

    def test_a_normal_paid_retrain_still_refunds_and_sweeps(self):
        self.finish()
        u = self.db.users[UID]
        # 35 from the retrain refund (20 monthly + 15 one-time) PLUS 40 each for the two
        # parked jobs the sweep fails and refunds — both of which the orphan path suppresses.
        self.assertEqual((u["credits_remaining"], u["monthly_credits_remaining"],
                          u["one_time_credits_remaining"]), (115, 20, 95))
        retrain = [r for r in self.db.ledger if r[2] == "retrain_refund"]
        self.assertEqual(len(retrain), 1)
        self.assertEqual(retrain[0][1], 35)
        self.assertEqual(len([r for r in self.db.ledger if r[2] == "job_refund"]), 2)
        self.assertIsNone(self.marker(), "a present user gets no orphan marker")
        for name in ("PARK1", "PARK2"):
            self.assertEqual(self.db.jobs[name]["status"], "failed",
                             "the ordinary sweep still runs for a real user")


class SchemaInvariant(unittest.TestCase):
    """Item 5: why a PAID missing-user case is integrity drift, not a transient condition."""

    def test_credit_transactions_has_an_fk_to_users(self):
        with open(os.path.join(BACKEND_DIR, "migrations", "000_baseline.sql"),
                  encoding="utf-8") as fh:
            ddl = fh.read()
        self.assertIn(
            "CONSTRAINT FK_credit_tx_user FOREIGN KEY (user_id) REFERENCES dbo.users",
            ddl,
            "a retrain_charge row cannot exist without its user, so a PAID training whose "
            "user is gone implies the charge row is gone too — schema drift or manual "
            "deletion, not an ordinary failure")

    def test_lora_trainings_has_no_such_fk(self):
        with open(os.path.join(BACKEND_DIR, "migrations", "004_lora_trainings.sql"),
                  encoding="utf-8") as fh:
            self.assertNotIn("FOREIGN KEY", fh.read().upper())

    def test_the_reasoning_is_recorded_in_the_code(self):
        with open(os.path.join(BACKEND_DIR, "function_app.py"), encoding="utf-8") as fh:
            src = fh.read()
        body = src[src.index("def _terminalize_orphaned_training("):]
        body = body[:body.index("\ndef ")]
        self.assertIn("FK to users", body)
        self.assertIn("schema drift", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
