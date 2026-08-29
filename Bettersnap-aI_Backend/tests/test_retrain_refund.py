"""Retrain-refund integrity in _finish_training.

THE GAP THIS CLOSES
The failed-paid-retrain path restored balances and then wrote a REASON_RETRAIN_REFUND ledger
row unconditionally. Neither UPDATE was rowcount-checked, so a training whose owner no longer
existed committed a ledger entry asserting a refund that moved nothing. It was the last path
in the codebase that could do that.

NO FOREIGN KEY BACKS THIS. lora_trainings.user_id is NOT NULL but unconstrained (migration
004; 022 records the convention: "carry NO FK, matching lora_trainings (004). Validate in
application code"). Application code is therefore the only guard, which is why a rowcount of
zero has to roll the whole transaction back rather than be tolerated.

These drive the REAL _finish_training against the transactional fake from
test_fused_exhaustion_composed, so commit/rollback visibility is modelled rather than assumed.

No Azure, no database, no queue, no GPU.

Run: python -m unittest tests.test_retrain_refund   (from the backend dir)
"""
import os
import sys
import unittest
from unittest import mock

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from tests.test_fused_exhaustion_composed import (          # noqa: E402
    TxDB, TxConn, TxCursor, FakeLedgerModule, function_app,
)

RETRAIN_REFUND = "retrain_refund"


class _FailingCursor(TxCursor):
    """Forces one specific UPDATE to match zero rows, modelling a vanished users row."""

    target = None

    def execute(self, sql, *params):
        s = " ".join(sql.split()).lower()
        if self.target and s.startswith(self.target):
            self.rowcount = 0
            self._fetch = None
            self._fetchall = []
            return
        return super().execute(sql, *params)


def _conn_factory(db, cursor_cls=TxCursor, target=None):
    def make():
        conn = TxConn(db)
        original = conn.cursor

        def cursor():
            cur = cursor_cls(conn)
            cur.target = target
            return cur
        conn.cursor = cursor if cursor_cls is not TxCursor else original
        return conn
    return make


class _Base(unittest.TestCase):
    """One retrain that was PAID for and then failed."""

    monthly_charge = 0
    one_time_charge = 0

    def setUp(self):
        self.db = TxDB()
        self.db.add_user("11111111-1111-4111-8111-111111111111", credits=0, monthly=0, one_time=0,
                         subscription_type="monthly")
        self.db.add_training("T1", user_id="11111111-1111-4111-8111-111111111111", status="training",
                             monthly_credit_cost=self.monthly_charge,
                             one_time_credit_cost=self.one_time_charge)
        self._patches = [
            mock.patch.object(function_app, "credit_ledger", FakeLedgerModule),
            mock.patch.object(function_app, "_identity_adapter_exists", lambda _u: False),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    def finish(self, ok=False, cursor_cls=TxCursor, target=None, **kw):
        with mock.patch.object(function_app, "new_connection",
                               _conn_factory(self.db, cursor_cls, target)):
            return function_app._finish_training(
                "T1", "11111111-1111-4111-8111-111111111111", ok=ok, error="training crashed", **kw)

    def balances(self):
        row = self.db.users["11111111-1111-4111-8111-111111111111"]
        return (row["credits_remaining"], row["monthly_credits_remaining"],
                row["one_time_credits_remaining"])

    def retrain_rows(self):
        return [r for r in self.db.ledger if r[2] == RETRAIN_REFUND]


class SuccessfulRefunds(_Base):
    monthly_charge = 30
    one_time_charge = 0

    def test_monthly_only_retrain_refund(self):
        self.finish()
        self.assertEqual(self.balances(), (30, 30, 0))
        self.assertEqual(len(self.retrain_rows()), 1)
        self.assertEqual(self.retrain_rows()[0][1], 30)
        self.assertEqual(self.db.trainings["T1"]["status"], "failed")


class OneTimeOnlyRefund(_Base):
    monthly_charge = 0
    one_time_charge = 25

    def test_one_time_only_retrain_refund(self):
        self.finish()
        self.assertEqual(self.balances(), (25, 0, 25))
        self.assertEqual(len(self.retrain_rows()), 1)
        self.assertEqual(self.retrain_rows()[0][1], 25)


class MixedRefund(_Base):
    monthly_charge = 20
    one_time_charge = 15

    def test_mixed_retrain_refund_restores_both_buckets(self):
        self.finish()
        self.assertEqual(self.balances(), (35, 20, 15),
                         "aggregate AND both spendable buckets must be restored")
        self.assertEqual(len(self.retrain_rows()), 1)
        self.assertEqual(self.retrain_rows()[0][1], 35)

    def test_duplicate_finish_training_moves_nothing_and_ledgers_nothing(self):
        """The exactly-once guard is the nonterminal->terminal transition itself."""
        self.finish()
        first = self.balances()
        for _ in range(4):
            self.finish()
        self.assertEqual(self.balances(), first)
        self.assertEqual(len(self.retrain_rows()), 1, "no second retrain ledger row")

    def test_success_path_does_not_refund(self):
        self.finish(ok=True)
        self.assertEqual(self.balances(), (0, 0, 0))
        self.assertEqual(self.retrain_rows(), [])
        self.assertEqual(self.db.trainings["T1"]["status"], "completed")


class FreeRetrain(_Base):
    monthly_charge = 0
    one_time_charge = 0

    def test_zero_charge_ledgers_nothing_and_moves_nothing(self):
        self.finish()
        self.assertEqual(self.balances(), (0, 0, 0))
        self.assertEqual(self.retrain_rows(), [])
        self.assertEqual(self.db.trainings["T1"]["status"], "failed")


class IntegrityFailuresRollBackEverything(_Base):
    monthly_charge = 20
    one_time_charge = 15

    def snapshot(self):
        return (dict(self.db.trainings["T1"]), self.balances(), len(self.db.ledger))

    def assert_unchanged(self, before):
        training, balances, ledger_len = before
        self.assertEqual(self.db.trainings["T1"]["status"], training["status"],
                         "the terminal transition must be rolled back too")
        self.assertEqual(self.balances(), balances)
        self.assertEqual(len(self.db.ledger), ledger_len)
        self.assertEqual(self.retrain_rows(), [])

    def test_refund_update_matching_zero_rows_rolls_back(self):
        before = self.snapshot()
        with self.assertRaises(function_app.TrainingRefundIntegrityError):
            self.finish(cursor_cls=_FailingCursor,
                        target="update users set monthly_credits_remaining")
        self.assert_unchanged(before)

    def test_lora_status_update_matching_zero_rows_rolls_back(self):
        before = self.snapshot()
        with self.assertRaises(function_app.TrainingRefundIntegrityError):
            self.finish(cursor_cls=_FailingCursor, target="update users set lora_status")
        self.assert_unchanged(before)

    def test_ledger_insertion_failure_rolls_back(self):
        class BoomLedger(FakeLedgerModule):
            @staticmethod
            def record(cur, user_id, amount, transaction_type, job_id=None):
                if transaction_type == RETRAIN_REFUND:
                    raise RuntimeError("ledger insert exploded")
                return FakeLedgerModule.record(cur, user_id, amount, transaction_type, job_id)

        before = self.snapshot()
        with mock.patch.object(function_app, "credit_ledger", BoomLedger):
            with self.assertRaises(RuntimeError):
                self.finish()
        self.assert_unchanged(before)

    def test_a_rolled_back_training_can_be_retried_cleanly(self):
        """The whole point of rolling back: the run stays non-terminal, so the next watcher
        tick refunds it exactly once once the user row is back."""
        with self.assertRaises(function_app.TrainingRefundIntegrityError):
            self.finish(cursor_cls=_FailingCursor,
                        target="update users set monthly_credits_remaining")
        self.assertEqual(self.db.trainings["T1"]["status"], "training")
        self.finish()          # the retry, against a healthy cursor
        self.assertEqual(self.db.trainings["T1"]["status"], "failed")
        self.assertEqual(self.balances(), (35, 20, 15))
        self.assertEqual(len(self.retrain_rows()), 1, "exactly one refund across both passes")


class NoForeignKeyBacksThis(unittest.TestCase):
    """Item 4: document, from the schema itself, why a rowcount of zero must roll back."""

    def test_lora_trainings_has_no_fk_to_users(self):
        with open(os.path.join(BACKEND_DIR, "migrations", "004_lora_trainings.sql"),
                  encoding="utf-8") as fh:
            ddl = fh.read()
        self.assertIn("user_id               UNIQUEIDENTIFIER NOT NULL", ddl)
        self.assertNotIn("FOREIGN KEY", ddl.upper().replace("FOREIGN KEY (FUSED", "X"))

    def test_the_convention_is_documented_in_022(self):
        with open(os.path.join(BACKEND_DIR, "migrations", "022_teams_organizations.sql"),
                  encoding="utf-8") as fh:
            self.assertIn("carry NO FK, matching lora_trainings", fh.read())

    def test_the_code_says_why_it_must_roll_back(self):
        with open(os.path.join(BACKEND_DIR, "function_app.py"), encoding="utf-8") as fh:
            src = fh.read()
        body = src[src.index("def _finish_training("):]
        body = body[:body.index("\ndef ")]
        self.assertIn("THERE IS NO FOREIGN KEY", body)
        self.assertIn("TrainingRefundIntegrityError", body)
        # The ledger call must come AFTER the rowcount guard on the refund UPDATE, so a
        # 0-row balance write can never be described by a REASON_RETRAIN_REFUND row.
        refund_update = body.index("monthly_credits_remaining = monthly_credits_remaining + ?")
        guard = body.index("if cur.rowcount != 1:", refund_update)
        ledger = body.index("credit_ledger.REASON_RETRAIN_REFUND")
        self.assertLess(refund_update, guard)
        self.assertLess(guard, ledger, "the ledger must not be written before the guard")


if __name__ == "__main__":
    unittest.main(verbosity=2)
