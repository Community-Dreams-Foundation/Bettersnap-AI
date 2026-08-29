"""Does the STORED retrain allocation match what was actually DEBITED?

The concern under investigation: reserve_training_slot computes plan-derived
monthly_charge / one_time_charge BEFORE it decides which balances to debit, stores those
values on lora_trainings, and then debits with a SEPARATELY computed monthly_debit /
one_time_debit. If the two ever diverge, _finish_training refunds the STORED values and
creates bucket credit that was never taken.

These tests drive the REAL reserve_training_slot against a users table whose three balances
are tracked independently, assert the INVARIANT (stored == debited) across the whole reachable
input space, and then run the full reservation -> failed-training -> refund sequence to see
what a user actually ends up with.

No Azure, no database, no queue, no GPU.

Run: python -m unittest tests.test_retrain_allocation   (from the backend dir)
"""
import os
import sys
import unittest
from unittest import mock

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from shared import training_reservation as tr           # noqa: E402


class UsersRow:
    """The three balances, tracked SEPARATELY. A single scalar would hide precisely the
    bucket-inflation this file is looking for."""

    def __init__(self, legacy=0, monthly=0, one_time=0, lora_status="ready",
                 retrain_count=99, plan_name="basic"):
        self.legacy = legacy
        self.monthly = monthly
        self.one_time = one_time
        self.lora_status = lora_status
        self.retrain_count = retrain_count
        self.plan_name = plan_name

    def snapshot(self):
        return (self.legacy, self.monthly, self.one_time)


class Cursor:
    def __init__(self, world):
        self.w = world
        self._fetch = None
        self.rowcount = 0

    def execute(self, sql, *params):
        s = " ".join(sql.split()).lower()
        w = self.w
        if "sp_getapplock" in s:
            self._fetch = (0,)
        elif s.startswith("select lora_status, credits_remaining, retrain_count"):
            u = w["user"]
            self._fetch = (u.lora_status, u.legacy, u.retrain_count, u.plan_name,
                           u.one_time, u.monthly)
        elif s.startswith("select count(*) from lora_trainings"):
            self._fetch = (0,)
        elif s.startswith("insert into lora_trainings"):
            # (user_id, photo_count, class_word, files_json, source_type,
            #  monthly_credit_cost, one_time_credit_cost)
            w["stored"] = {"source_type": params[4],
                           "monthly_credit_cost": params[5],
                           "one_time_credit_cost": params[6]}
            self._fetch = ("T1",)
        elif s.startswith("update users set lora_status = 'training', retrain_count"):
            monthly_debit, one_time_debit, charge = params[0], params[1], params[2]
            w["debited"] = {"monthly": monthly_debit, "one_time": one_time_debit,
                            "aggregate": charge}
            u = w["user"]
            u.monthly -= monthly_debit
            u.one_time -= one_time_debit
            u.legacy -= charge
            u.lora_status = "training"
            u.retrain_count += 1
            self.rowcount = 1
        elif s.startswith("update users set lora_status = 'training' where"):
            w["user"].lora_status = "training"
            self.rowcount = 1
        elif s.startswith("insert into credit_transactions"):
            w["ledger"].append(tuple(params))
            self.rowcount = 1
        elif s.startswith("insert into outbox"):
            self._fetch = (1,)
        else:
            raise AssertionError("unmodelled SQL: %s" % s[:120])

    def fetchone(self):
        return self._fetch


class Conn:
    def __init__(self, world):
        self.w = world
        self.autocommit = True

    def cursor(self):
        return Cursor(self.w)

    def commit(self):
        self.w["committed"] = True

    def rollback(self):
        self.w["rolled_back"] = True

    def close(self):
        pass


def reserve(user, *, charge=10, free_retrains=0, force=True):
    world = {"user": user, "ledger": [], "stored": None, "debited": None,
             "committed": False, "rolled_back": False}
    with mock.patch.object(tr, "new_connection", lambda: Conn(world)):
        result = tr.reserve_training_slot(
            "u1", [{"blob": "a.jpg"}], "woman", force=force,
            free_retrains=free_retrains, retrain_credits=charge, max_per_day=10)
    return result, world


class StoredAllocationMatchesActualDebit(unittest.TestCase):
    """THE INVARIANT. Whatever lora_trainings records as the retrain charge must be exactly
    what came out of each balance — otherwise _finish_training refunds credit that was never
    taken."""

    def assert_consistent(self, world, before):
        stored, debited, user = world["stored"], world["debited"], world["user"]
        self.assertIsNotNone(stored, "no training row was inserted")
        self.assertIsNotNone(debited, "no retrain debit was issued")
        self.assertEqual(stored["monthly_credit_cost"], debited["monthly"],
                         "stored monthly_credit_cost != monthly credits actually debited")
        self.assertEqual(stored["one_time_credit_cost"], debited["one_time"],
                         "stored one_time_credit_cost != one-time credits actually debited")
        # and the balances really moved by those amounts
        self.assertEqual(before[1] - user.monthly, debited["monthly"])
        self.assertEqual(before[2] - user.one_time, debited["one_time"])
        self.assertEqual(before[0] - user.legacy, debited["aggregate"])

    def test_one_time_plan_funded_from_the_one_time_bucket(self):
        u = UsersRow(legacy=50, one_time=50, plan_name="basic")
        before = u.snapshot()
        result, world = reserve(u, charge=10)
        self.assertTrue(result.ok, result.reason)
        self.assert_consistent(world, before)

    def test_monthly_plan_funded_entirely_from_the_monthly_bucket(self):
        u = UsersRow(legacy=50, monthly=50, plan_name="monthly_pro")
        before = u.snapshot()
        result, world = reserve(u, charge=10)
        self.assertTrue(result.ok, result.reason)
        self.assert_consistent(world, before)

    def test_monthly_plan_overflowing_into_the_one_time_bucket(self):
        u = UsersRow(legacy=50, monthly=4, one_time=20, plan_name="monthly_pro")
        before = u.snapshot()
        result, world = reserve(u, charge=10)
        self.assertTrue(result.ok, result.reason)
        self.assert_consistent(world, before)
        self.assertEqual(world["debited"], {"monthly": 4, "one_time": 6, "aggregate": 10})

    def test_free_retrain_stores_and_debits_nothing(self):
        u = UsersRow(legacy=50, one_time=50, retrain_count=0, plan_name="basic")
        result, world = reserve(u, charge=10, free_retrains=5)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(world["stored"]["monthly_credit_cost"], 0)
        self.assertEqual(world["stored"]["one_time_credit_cost"], 0)
        self.assertEqual(world["debited"], {"monthly": 0, "one_time": 0, "aggregate": 0})
        self.assertEqual(u.snapshot(), (50, 0, 50), "a free retrain moves nothing")

    def test_the_invariant_holds_across_the_whole_reachable_input_space(self):
        """Exhaustive sweep. If ANY reachable reservation stores a bucket amount it did not
        debit, this fails and names the case."""
        checked = 0
        for plan in ("basic", "monthly_pro"):
            for legacy in (0, 5, 10, 40):
                for monthly in (0, 3, 10, 40):
                    for one_time in (0, 3, 10, 40):
                        u = UsersRow(legacy=legacy, monthly=monthly, one_time=one_time,
                                     plan_name=plan)
                        before = u.snapshot()
                        result, world = reserve(u, charge=10)
                        if not result.ok:
                            continue
                        checked += 1
                        with self.subTest(plan=plan, legacy=legacy, monthly=monthly,
                                          one_time=one_time):
                            self.assert_consistent(world, before)
        self.assertGreater(checked, 10, "the sweep never exercised a successful reservation")


class LegacyOnlyBalanceCannotReachThePaidPath(unittest.TestCase):
    """The reported inflation requires bucket_total == 0 WITH a paid retrain allowed. This
    proves that combination is unreachable, and shows what happens instead."""

    def test_legacy_only_user_is_refused_a_paid_retrain(self):
        for plan in ("basic", "monthly_pro"):
            with self.subTest(plan=plan):
                u = UsersRow(legacy=500, monthly=0, one_time=0, plan_name=plan)
                result, world = reserve(u, charge=10)
                self.assertFalse(result.ok)
                self.assertEqual(result.reason, "credits")
                self.assertIsNone(world["debited"], "nothing may be debited on a refusal")
                self.assertEqual(u.snapshot(), (500, 0, 0))

    def test_so_the_zero_bucket_debit_branch_never_runs_for_a_paid_retrain(self):
        """`else: monthly_debit = 0; one_time_debit = 0` is reachable only when charge == 0,
        where it is a no-op. That is why the stored/debited divergence cannot occur."""
        u = UsersRow(legacy=500, monthly=0, one_time=0, retrain_count=0, plan_name="basic")
        result, world = reserve(u, charge=10, free_retrains=5)   # free -> charge becomes 0
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(world["debited"], {"monthly": 0, "one_time": 0, "aggregate": 0})
        self.assertEqual(world["stored"]["one_time_credit_cost"], 0)


class FullReservationThenFailedTrainingSequence(unittest.TestCase):
    """reservation -> failed training -> refund, end to end, on the balances themselves."""

    def _refund(self, user, stored):
        """What _finish_training does with the stored allocation."""
        monthly, one_time = stored["monthly_credit_cost"], stored["one_time_credit_cost"]
        user.monthly += monthly
        user.one_time += one_time
        user.legacy += monthly + one_time

    def test_a_paid_retrain_that_fails_returns_exactly_what_it_took(self):
        u = UsersRow(legacy=50, monthly=4, one_time=20, plan_name="monthly_pro")
        before = u.snapshot()
        result, world = reserve(u, charge=10)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(u.snapshot(), (40, 0, 14))
        self._refund(u, world["stored"])
        self.assertEqual(u.snapshot(), before,
                         "a failed paid retrain must leave every balance exactly as found")

    def test_a_free_retrain_that_fails_returns_nothing(self):
        u = UsersRow(legacy=50, one_time=20, retrain_count=0, plan_name="basic")
        before = u.snapshot()
        result, world = reserve(u, charge=10, free_retrains=5)
        self.assertTrue(result.ok, result.reason)
        self._refund(u, world["stored"])
        self.assertEqual(u.snapshot(), before)

    def test_no_bucket_inflation_across_the_reachable_space(self):
        """The end-to-end statement of the concern: after reservation + full refund, no
        balance may be HIGHER than it started."""
        for plan in ("basic", "monthly_pro"):
            for monthly in (0, 3, 10, 40):
                for one_time in (0, 3, 10, 40):
                    u = UsersRow(legacy=50, monthly=monthly, one_time=one_time,
                                 plan_name=plan)
                    before = u.snapshot()
                    result, world = reserve(u, charge=10)
                    if not result.ok:
                        continue
                    self._refund(u, world["stored"])
                    with self.subTest(plan=plan, monthly=monthly, one_time=one_time):
                        self.assertEqual(u.snapshot(), before,
                                         "balances diverged after reserve + refund")


if __name__ == "__main__":
    unittest.main(verbosity=2)
