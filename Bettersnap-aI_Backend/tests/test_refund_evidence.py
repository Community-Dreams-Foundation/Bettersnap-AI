"""Evidence rules for a refund amount, existing-refund verification, and marker clearing.

Three accounting-integrity rules live here, all of which were violated before:

  1. NO AMOUNT IS EVER GUESSED. build_refund_plan used to turn missing or unparseable
     job_params into a ONE-credit refund, silently under-refunding a 40-credit job and
     ledgering that as the whole debt.
  2. A refund is SETTLED only when exactly one job_refund row exists for the right user and
     the right amount. The previous `any(...)` check read "one correct row plus one wrong
     row" as settled and cleared the marker, permanently hiding the bad row.
  3. A paid refund whose marker does not clear must roll the whole compensation back, or the
     next tick pays it again.

No Azure, no database, no queue, no GPU.

Run: python -m unittest tests.test_refund_evidence   (from the backend dir)
"""
import json
import os
import sys
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from shared import provisioning_retry as pr                     # noqa: E402
from tests.test_provisioning_retry import DB, FakeCursor, FakeLedger    # noqa: E402
from tests.test_refund_plan import SHAPES, params, seed_reserve  # noqa: E402


class _NoClearCursor(FakeCursor):
    """A cursor whose marker UPDATE never matches a row."""

    def execute(self, sql, *args):
        if " ".join(sql.split()).lower().startswith("update jobs set job_params = ?"):
            self.rowcount = 0
            self._fetch = None
            self._rows = []
            return
        return super().execute(sql, *args)


class EvidenceForTheAmount(unittest.TestCase):
    """Item 2: the reserve ledger is the cross-check, and nothing is ever guessed."""

    def make(self, job_params, reserve=None, source_type=None):
        db = DB()
        ledger = FakeLedger()
        db.add_job("J1", job_params=job_params, source_type=source_type)
        for amount in (reserve or ()):
            ledger.add(db, "11111111-1111-4111-8111-111111111111", -amount, ledger.REASON_JOB_RESERVE, "J1")
        return db, FakeCursor(db), ledger

    def build(self, *a, **kw):
        db, cur, ledger = self.make(*a, **kw)
        return pr.build_refund_plan(cur, "J1", credit_ledger=ledger)

    # -- accepted -------------------------------------------------------
    def test_reserve_agreeing_with_job_params_is_accepted(self):
        plan = self.build(params(40), reserve=[40], source_type="one_time")
        self.assertEqual(plan["total"], 40)

    def test_legacy_aggregate_only_job_with_no_reserve_row_is_accepted(self):
        """THE NARROW COMPATIBILITY RULE. credit_transactions is created in 000_baseline, not
        024 — 024's own header records that the table "already existed ... but was DORMANT".

        The exemption is defined by SEMANTICS, not migration order: source_type is nullable
        and reserve_job_slot defaults it to None, so NULL does not prove anything about when
        the row was written. NULL source_type + NULL organization_id means EXPLICIT LEGACY
        AGGREGATE-ONLY funding — no bucket was recorded as charged — so the refund can only
        restore credits_remaining, which job_params already states. That is why it is safe
        without corroboration; a bucketed or organization plan names specific spendable
        balances and is not."""
        plan = self.build(params(7), reserve=[])
        self.assertEqual(plan["total"], 7)
        self.assertEqual(plan["funding"], pr.FUNDING_LEGACY)

    def test_bucketed_job_with_no_reserve_row_FAILS_CLOSED(self):
        """A bucketed job could only have been created by code that ledgers its reserve, so a
        missing row is a broken reservation, not history. Tolerating it would let a modern
        reservation bug pass audit on job_params alone."""
        with self.assertRaises(pr.RefundPlanInvalid):
            self.build(params(50, monthly=20, one_time=30), reserve=[],
                       source_type="monthly")

    def test_one_time_job_with_no_reserve_row_FAILS_CLOSED(self):
        with self.assertRaises(pr.RefundPlanInvalid):
            self.build(params(40), reserve=[], source_type="one_time")

    def test_organization_job_with_no_reserve_row_FAILS_CLOSED(self):
        db = DB()
        ledger = FakeLedger()
        db.add_job("J1", job_params=params(40), source_type="monthly",
                   organization_id="org-9")
        with self.assertRaises(pr.RefundPlanInvalid):
            pr.build_refund_plan(FakeCursor(db), "J1", credit_ledger=ledger)

    def test_a_legacy_shaped_job_WITH_a_reserve_row_still_validates_it(self):
        with self.assertRaises(pr.RefundPlanInvalid):
            self.build(params(7), reserve=[99])

    def test_the_ledger_is_mandatory_there_is_no_bypass(self):
        """credit_ledger has no default: a caller cannot skip the cross-check."""
        db, cur, _ = self.make(params(40), reserve=[40], source_type="one_time")
        with self.assertRaises(TypeError):
            pr.build_refund_plan(cur, "J1")

    # -- rejected -------------------------------------------------------
    def test_reserve_total_mismatch_is_rejected(self):
        with self.assertRaises(pr.RefundPlanInvalid):
            self.build(params(40), reserve=[25], source_type="one_time")

    def test_multiple_identical_reserve_rows_are_ambiguous(self):
        with self.assertRaises(pr.RefundPlanInvalid):
            self.build(params(40), reserve=[40, 40], source_type="one_time")

    def test_multiple_inconsistent_reserve_rows_are_rejected(self):
        with self.assertRaises(pr.RefundPlanInvalid):
            self.build(params(40), reserve=[40, 25], source_type="one_time")

    def test_missing_credit_cost_is_rejected(self):
        with self.assertRaises(pr.RefundPlanInvalid):
            self.build(json.dumps({}))

    def test_malformed_credit_cost_is_rejected(self):
        for bad in ("40", 40.5, True, None, [40], {"a": 1}):
            with self.subTest(value=bad):
                with self.assertRaises(pr.RefundPlanInvalid):
                    self.build(json.dumps({"credit_cost": bad}))

    def test_zero_and_negative_credit_cost_are_rejected(self):
        for bad in (0, -5):
            with self.subTest(value=bad):
                with self.assertRaises(pr.RefundPlanInvalid):
                    self.build(json.dumps({"credit_cost": bad}))

    def test_empty_or_missing_job_params_is_rejected(self):
        for bad in (None, "", "   "):
            with self.subTest(value=bad):
                with self.assertRaises(pr.RefundPlanInvalid):
                    self.build(bad)

    def test_unparseable_job_params_is_rejected_not_guessed(self):
        with self.assertRaises(pr.RefundPlanInvalid):
            self.build("{not json")

    def test_job_params_that_is_not_an_object_is_rejected(self):
        with self.assertRaises(pr.RefundPlanInvalid):
            self.build(json.dumps([1, 2, 3]))

    def test_malformed_monthly_one_time_split_is_rejected(self):
        for monthly, one_time in ((None, 30), (20, None), ("20", 30), (-1, 51), (20, -1)):
            with self.subTest(split=(monthly, one_time)):
                with self.assertRaises(pr.RefundPlanInvalid):
                    self.build(params(50, monthly=monthly, one_time=one_time),
                               source_type="monthly")

    def test_split_that_does_not_sum_to_credit_cost_is_rejected(self):
        """reserve_job_slot guarantees monthly + one_time == credit_cost. A stored allocation
        that breaks it is corrupt, not something to pay out on."""
        with self.assertRaises(pr.RefundPlanInvalid):
            self.build(params(50, monthly=20, one_time=10), source_type="monthly")

    def test_job_without_a_user_is_rejected(self):
        db = DB()
        db.add_job("J1", user_id=None)
        with self.assertRaises(pr.RefundPlanInvalid):
            pr.build_refund_plan(FakeCursor(db), "J1", credit_ledger=FakeLedger())

    # -- the terminal path must not move anything on bad evidence -------
    def test_terminalize_on_bad_evidence_pays_nothing_and_ledgers_nothing(self):
        db, cur, ledger = self.make("{not json")
        db.add_user("11111111-1111-4111-8111-111111111111", 0)
        transitioned, amount, state = pr.terminalize_and_refund(
            cur, "J1", credit_ledger=ledger)
        self.assertTrue(transitioned, "the job must still terminalize, not hang forever")
        self.assertEqual(state, pr.REFUND_PENDING)
        self.assertEqual(amount, 0, "no amount may be invented")
        self.assertEqual(db.users["11111111-1111-4111-8111-111111111111"]["credits_remaining"], 0)
        self.assertEqual(ledger.rows, [])

    def test_terminalize_on_a_reserve_mismatch_pays_nothing(self):
        db, cur, ledger = self.make(params(40), reserve=[25], source_type="one_time")
        db.add_user("11111111-1111-4111-8111-111111111111", 0)
        _, amount, state = pr.terminalize_and_refund(cur, "J1", credit_ledger=ledger)
        self.assertEqual((amount, state), (0, pr.REFUND_PENDING))
        self.assertEqual(db.users["11111111-1111-4111-8111-111111111111"]["credits_remaining"], 0)
        self.assertEqual(ledger.refunds("J1"), [])

    def test_unresolved_marker_is_never_settled_automatically(self):
        db, cur, ledger = self.make("{not json")
        db.add_user("11111111-1111-4111-8111-111111111111", 0)
        pr.terminalize_and_refund(cur, "J1", credit_ledger=ledger)
        self.assertTrue(pr.mark_refund_unresolved(cur, "J1", "unparseable job_params"))
        self.assertEqual(
            pr.compensate_pending_refund(cur, "J1", credit_ledger=ledger), pr.REFUND_PENDING)
        self.assertEqual(db.users["11111111-1111-4111-8111-111111111111"]["credits_remaining"], 0)
        self.assertEqual(ledger.rows, [])
        self.assertTrue(pr.read_refund_pending(cur, "J1")["unresolved"])


class ExistingRefundRowVerification(unittest.TestCase):
    """Item 3: settled means EXACTLY ONE row, right user, right amount. Nothing else."""

    def owe(self, shape="mixed_monthly_and_one_time"):
        spec = SHAPES[shape]
        db = DB()
        ledger = FakeLedger()
        db.add_job("J1", source_type=spec["source_type"], job_params=spec["job_params"])
        seed_reserve(db, ledger)
        cur = FakeCursor(db)
        pr.terminalize_and_refund(cur, "J1", credit_ledger=ledger)
        pr.mark_refund_pending(
            cur, "J1", pr.build_refund_plan(cur, "J1", credit_ledger=ledger))
        db.add_user("11111111-1111-4111-8111-111111111111", 0, subscription_type="monthly")
        return db, cur, ledger, pr.read_refund_pending(cur, "J1")

    def verdict(self, cur, ledger, plan):
        return pr.already_refunded(cur, "J1", ledger, plan=plan)[0]

    def test_zero_rows_is_eligible(self):
        db, cur, ledger, plan = self.owe()
        self.assertEqual(self.verdict(cur, ledger, plan), pr.REFUND_ROWS_NONE)

    def test_one_correct_row_is_settled(self):
        db, cur, ledger, plan = self.owe()
        ledger.add(db, "11111111-1111-4111-8111-111111111111", 50, ledger.REASON_JOB_REFUND, "J1")
        self.assertEqual(self.verdict(cur, ledger, plan), pr.REFUND_ROWS_SETTLED)

    def test_duplicate_refund_rows_are_a_conflict(self):
        db, cur, ledger, plan = self.owe()
        ledger.add(db, "11111111-1111-4111-8111-111111111111", 50, ledger.REASON_JOB_REFUND, "J1")
        ledger.add(db, "11111111-1111-4111-8111-111111111111", 50, ledger.REASON_JOB_REFUND, "J1")
        self.assertEqual(self.verdict(cur, ledger, plan), pr.REFUND_ROWS_CONFLICT)

    def test_one_correct_plus_one_incorrect_row_is_a_conflict(self):
        """The `any(...)` version read this as SETTLED and cleared the marker, permanently
        hiding the bad row."""
        db, cur, ledger, plan = self.owe()
        ledger.add(db, "11111111-1111-4111-8111-111111111111", 50, ledger.REASON_JOB_REFUND, "J1")
        ledger.add(db, "11111111-1111-4111-8111-111111111111", 5, ledger.REASON_JOB_REFUND, "J1")
        self.assertEqual(self.verdict(cur, ledger, plan), pr.REFUND_ROWS_CONFLICT)

    def test_correct_amount_but_wrong_user_is_a_conflict(self):
        db, cur, ledger, plan = self.owe()
        ledger.add(db, "22222222-2222-4222-8222-222222222222", 50, ledger.REASON_JOB_REFUND, "J1")
        self.assertEqual(self.verdict(cur, ledger, plan), pr.REFUND_ROWS_CONFLICT)

    def test_wrong_amount_is_a_conflict(self):
        db, cur, ledger, plan = self.owe()
        ledger.add(db, "11111111-1111-4111-8111-111111111111", 5, ledger.REASON_JOB_REFUND, "J1")
        self.assertEqual(self.verdict(cur, ledger, plan), pr.REFUND_ROWS_CONFLICT)

    def test_rows_for_other_jobs_are_ignored(self):
        db, cur, ledger, plan = self.owe()
        ledger.add(db, "11111111-1111-4111-8111-111111111111", 50, ledger.REASON_JOB_REFUND, "OTHER")
        self.assertEqual(self.verdict(cur, ledger, plan), pr.REFUND_ROWS_NONE)

    def test_reserve_rows_are_not_mistaken_for_refunds(self):
        db, cur, ledger, plan = self.owe()
        ledger.add(db, "11111111-1111-4111-8111-111111111111", -50, ledger.REASON_JOB_RESERVE, "J1")
        self.assertEqual(self.verdict(cur, ledger, plan), pr.REFUND_ROWS_NONE)

    def test_no_plan_supplied_is_treated_as_a_conflict(self):
        db, cur, ledger, _ = self.owe()
        ledger.add(db, "11111111-1111-4111-8111-111111111111", 50, ledger.REASON_JOB_REFUND, "J1")
        self.assertEqual(pr.already_refunded(cur, "J1", ledger)[0], pr.REFUND_ROWS_CONFLICT)

    def test_a_conflict_leaves_the_debt_pending_and_pays_nothing(self):
        db, cur, ledger, plan = self.owe()
        ledger.add(db, "22222222-2222-4222-8222-222222222222", 50, ledger.REASON_JOB_REFUND, "J1")
        self.assertEqual(
            pr.compensate_pending_refund(cur, "J1", credit_ledger=ledger), pr.REFUND_PENDING)
        self.assertEqual(db.users["11111111-1111-4111-8111-111111111111"]["credits_remaining"], 0)
        self.assertIsNotNone(pr.read_refund_pending(cur, "J1"))
        # db.ledger is the authoritative store (seeded rows + recorded rows); the seeded
        # wrong-user row must still be the ONLY refund row.
        self.assertEqual(
            len([r for r in db.ledger
                 if r.job_id == "J1" and r.transaction_type == ledger.REASON_JOB_REFUND]),
            1, "no second payment on a conflict")

    def test_duplicates_block_compensation_rather_than_clearing_the_marker(self):
        db, cur, ledger, plan = self.owe()
        ledger.add(db, "11111111-1111-4111-8111-111111111111", 50, ledger.REASON_JOB_REFUND, "J1")
        ledger.add(db, "11111111-1111-4111-8111-111111111111", 50, ledger.REASON_JOB_REFUND, "J1")
        self.assertEqual(
            pr.compensate_pending_refund(cur, "J1", credit_ledger=ledger), pr.REFUND_PENDING)
        self.assertIsNotNone(pr.read_refund_pending(cur, "J1"),
                             "a double refund must stay visible to an operator")


class MarkerClearIsMandatory(unittest.TestCase):
    """Item 4: a paid refund whose marker survives would be paid AGAIN next tick."""

    def owe(self):
        spec = SHAPES["mixed_monthly_and_one_time"]
        db = DB()
        ledger = FakeLedger()
        db.add_job("J1", source_type=spec["source_type"], job_params=spec["job_params"])
        seed_reserve(db, ledger)
        cur = FakeCursor(db)
        pr.terminalize_and_refund(cur, "J1", credit_ledger=ledger)
        pr.mark_refund_pending(
            cur, "J1", pr.build_refund_plan(cur, "J1", credit_ledger=ledger))
        db.add_user("11111111-1111-4111-8111-111111111111", 0, subscription_type="monthly")
        return db, cur, ledger

    def test_a_failed_marker_clear_raises(self):
        db, cur, ledger = self.owe()
        with self.assertRaises(pr.RefundMarkerNotCleared):
            pr.compensate_pending_refund(_NoClearCursor(db), "J1", credit_ledger=ledger)

    def test_the_raise_lets_the_caller_roll_the_whole_thing_back(self):
        """Modelled the way the reaper does it: the transaction is discarded, so the payment
        is undone and the marker survives for a clean retry."""
        db, cur, ledger = self.owe()
        before_users = json.loads(json.dumps(
            {k: dict(v) for k, v in db.users.items()}))
        before_ledger = len(db.ledger)
        try:
            pr.compensate_pending_refund(_NoClearCursor(db), "J1", credit_ledger=ledger)
        except pr.RefundMarkerNotCleared:
            db.users = before_users                 # the caller's conn.rollback()
            del db.ledger[before_ledger:]
        self.assertEqual(db.users["11111111-1111-4111-8111-111111111111"]["credits_remaining"], 0)
        self.assertEqual(len(db.ledger), before_ledger)
        self.assertIsNotNone(pr.read_refund_pending(cur, "J1"),
                             "the debt survives so a later tick can pay it exactly once")

    def test_an_already_settled_marker_that_will_not_clear_also_raises(self):
        db, cur, ledger = self.owe()
        ledger.add(db, "11111111-1111-4111-8111-111111111111", 50, ledger.REASON_JOB_REFUND, "J1")
        with self.assertRaises(pr.RefundMarkerNotCleared):
            pr.compensate_pending_refund(_NoClearCursor(db), "J1", credit_ledger=ledger)

    def test_the_happy_path_clears_the_marker_and_pays_once(self):
        db, cur, ledger = self.owe()
        self.assertEqual(
            pr.compensate_pending_refund(cur, "J1", credit_ledger=ledger), pr.REFUND_DONE)
        self.assertIsNone(pr.read_refund_pending(cur, "J1"))
        self.assertEqual(
            len([r for r in db.ledger
                 if r.job_id == "J1" and r.transaction_type == ledger.REASON_JOB_REFUND]), 1)
        self.assertEqual(db.users["11111111-1111-4111-8111-111111111111"]["credits_remaining"], 50)
        self.assertEqual(db.users["11111111-1111-4111-8111-111111111111"]["monthly_credits_remaining"], 20)
        self.assertEqual(db.users["11111111-1111-4111-8111-111111111111"]["one_time_credits_remaining"], 30)

    def test_the_reaper_rolls_back_on_a_marker_clear_failure(self):
        """Structural: the compensator loop must catch RefundMarkerNotCleared and roll back,
        never commit."""
        with open(os.path.join(BACKEND_DIR, "function_app.py"), encoding="utf-8") as fh:
            src = fh.read()
        body = src[src.index("def _compensate_pending_refunds("):]
        body = body[:body.index("\ndef ")]
        self.assertIn("RefundMarkerNotCleared", body)
        rollback_at = body.index("conn.rollback()")
        commit_at = body.index("conn.commit()")
        self.assertLess(rollback_at, commit_at,
                        "the rollback must guard the commit, not follow it")


class ModernJobWithNoReserveRowFailsClosed(unittest.TestCase):
    """Item 4: a NEWLY-CREATED bucketed or organization job with no job_reserve row is a
    broken reservation, not history. It must pay nothing, ledger nothing, still terminalize,
    and leave an UNRESOLVED marker that no automated path can act on."""

    def make(self, **job):
        db = DB()
        ledger = FakeLedger()
        job.setdefault("job_params", params(40))
        db.add_job("J1", **job)
        db.add_user("11111111-1111-4111-8111-111111111111", 0, subscription_type="monthly")
        return db, FakeCursor(db), ledger

    def run_terminal_path(self, **job):
        """The production sequence: terminalize -> plan cannot be built -> record the debt as
        unresolved (what function_app._record_refund_debt does)."""
        db, cur, ledger = self.make(**job)
        transitioned, amount, state = pr.terminalize_and_refund(
            cur, "J1", credit_ledger=ledger)
        self.assertTrue(transitioned, "the job must terminalize, not hang forever")
        self.assertEqual(state, pr.REFUND_PENDING)
        self.assertEqual(amount, 0, "no amount may be invented")
        try:
            pr.build_refund_plan(cur, "J1", credit_ledger=ledger)
            self.fail("a modern job with no reserve row must not yield a plan")
        except pr.RefundPlanInvalid as e:
            self.assertTrue(pr.mark_refund_unresolved(cur, "J1", str(e)))
        return db, cur, ledger

    def assert_untouched_and_unresolved(self, db, cur, ledger):
        self.assertEqual(db.users["11111111-1111-4111-8111-111111111111"]["credits_remaining"], 0)
        self.assertEqual(db.users["11111111-1111-4111-8111-111111111111"]["monthly_credits_remaining"], 0)
        self.assertEqual(db.users["11111111-1111-4111-8111-111111111111"]["one_time_credits_remaining"], 0)
        self.assertEqual(db.orgs, {})
        self.assertEqual(
            [r for r in db.ledger if r.transaction_type == ledger.REASON_JOB_REFUND], [])
        marker = pr.read_refund_pending(cur, "J1")
        self.assertTrue(marker["unresolved"])
        self.assertIn("job_reserve", marker["reason"])
        self.assertNotIn("total", marker, "an unresolved marker carries NO amount")
        # and the compensator must refuse to act on it, every time
        for _ in range(3):
            self.assertEqual(
                pr.compensate_pending_refund(cur, "J1", credit_ledger=ledger),
                pr.REFUND_PENDING)
        self.assertEqual(db.users["11111111-1111-4111-8111-111111111111"]["credits_remaining"], 0)
        self.assertEqual(
            [r for r in db.ledger if r.transaction_type == ledger.REASON_JOB_REFUND], [])

    def test_new_monthly_job_with_no_reserve_row(self):
        db, cur, ledger = self.run_terminal_path(
            source_type="monthly",
            job_params=json.dumps({"credit_cost": 40, "monthly_credit_cost": 40,
                                   "one_time_credit_cost": 0}))
        self.assert_untouched_and_unresolved(db, cur, ledger)

    def test_new_one_time_job_with_no_reserve_row(self):
        db, cur, ledger = self.run_terminal_path(source_type="one_time")
        self.assert_untouched_and_unresolved(db, cur, ledger)

    def test_new_organization_job_with_no_reserve_row(self):
        db, cur, ledger = self.run_terminal_path(
            source_type="monthly", organization_id="org-9",
            job_params=json.dumps({"credit_cost": 40, "monthly_credit_cost": 40,
                                   "one_time_credit_cost": 0}))
        self.assert_untouched_and_unresolved(db, cur, ledger)

    def test_a_positive_reserve_row_is_also_no_evidence_of_a_charge(self):
        db = DB()
        ledger = FakeLedger()
        db.add_job("J1", job_params=params(40), source_type="one_time")
        ledger.add(db, "11111111-1111-4111-8111-111111111111", 40, ledger.REASON_JOB_RESERVE, "J1")   # +40, not a debit
        db.add_user("11111111-1111-4111-8111-111111111111", 0)
        cur = FakeCursor(db)
        _, amount, state = pr.terminalize_and_refund(cur, "J1", credit_ledger=ledger)
        self.assertEqual((amount, state), (0, pr.REFUND_PENDING))
        self.assertEqual(db.users["11111111-1111-4111-8111-111111111111"]["credits_remaining"], 0,
                         "a positive reserve must never authorize a refund")


if __name__ == "__main__":
    unittest.main(verbosity=2)
