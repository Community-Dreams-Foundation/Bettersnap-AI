"""The canonical RefundPlan: derivation, strict validation, and IMMEDIATE == DELAYED.

THE DEFECT THIS FILE EXISTS FOR
`mark_refund_pending` used to persist only a total, and `compensate_pending_refund` special-
cased one_time and restored nothing but `credits_remaining` for everything else. A delayed
monthly or mixed refund therefore produced:

    ledger says the full refund happened
    credits_remaining restored
    monthly_credits_remaining / one_time_credits_remaining STILL SHORT

i.e. a balance the user cannot spend. Every generation would keep failing on insufficient
bucket credit while the aggregate looked correct.

The fake here tracks all three personal balances plus organization credits INDEPENDENTLY. A
single scalar would hide exactly this class of bug, so there isn't one.

No Azure, no database, no queue, no GPU.

Run: python -m unittest tests.test_refund_plan   (from the backend dir)
"""
import json
import os
import sys
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from shared import provisioning_retry as pr                    # noqa: E402
from tests.test_provisioning_retry import DB, FakeCursor, FakeLedger   # noqa: E402


def params(credit_cost, monthly=None, one_time=None):
    p = {"credit_cost": credit_cost}
    if monthly is not None:
        p["monthly_credit_cost"] = monthly
    if one_time is not None:
        p["one_time_credit_cost"] = one_time
    return json.dumps(p)


# Every funding shape the product can produce, with the balances each one must restore.
# reserve_job_slot guarantees monthly_credit_cost + one_time_credit_cost == credit_cost for
# 'monthly' jobs (job_reservation.py: one_time_debit = credit_cost - monthly_debit).
SHAPES = {
    "one_time_only": dict(
        source_type="one_time", job_params=params(40),
        expect=dict(total=40, aggregate=40, monthly=0, one_time=40)),
    "monthly_only": dict(
        source_type="monthly", job_params=params(30, monthly=30, one_time=0),
        expect=dict(total=30, aggregate=30, monthly=30, one_time=0)),
    "mixed_monthly_and_one_time": dict(
        source_type="monthly", job_params=params(50, monthly=20, one_time=30),
        expect=dict(total=50, aggregate=50, monthly=20, one_time=30)),
    "legacy_aggregate_only": dict(
        source_type=None, job_params=params(7),
        expect=dict(total=7, aggregate=7, monthly=0, one_time=0)),
}


def seed_reserve(db, ledger, job_id="J1", user_id="11111111-1111-4111-8111-111111111111"):
    """A job that was really charged has exactly ONE strictly-negative job_reserve row for
    its credit_cost. Bucketed and organization-funded jobs now REQUIRE it."""
    total = json.loads(db.jobs[job_id]["job_params"])["credit_cost"]
    ledger.add(db, user_id, -total, ledger.REASON_JOB_RESERVE, job_id)


class PlanDerivation(unittest.TestCase):
    def plan_for(self, **job):
        db = DB()
        ledger = FakeLedger()
        db.add_job("J1", **job)
        if job.get("source_type") or job.get("organization_id"):
            seed_reserve(db, ledger)
        return pr.build_refund_plan(FakeCursor(db), "J1", credit_ledger=ledger)

    def test_every_funding_shape_derives_the_right_deltas(self):
        for name, spec in SHAPES.items():
            with self.subTest(shape=name):
                plan = self.plan_for(source_type=spec["source_type"],
                                     job_params=spec["job_params"])
                e = spec["expect"]
                self.assertEqual(plan["total"], e["total"])
                self.assertEqual(plan["aggregate_delta"], e["aggregate"])
                self.assertEqual(plan["monthly_delta"], e["monthly"])
                self.assertEqual(plan["one_time_delta"], e["one_time"])
                self.assertEqual(plan["target"], pr.TARGET_USER)
                self.assertIsNone(plan["organization_id"])

    def test_organization_plan_never_touches_the_personal_buckets(self):
        plan = self.plan_for(organization_id="org-9", source_type="monthly",
                             job_params=params(50, monthly=20, one_time=30))
        self.assertEqual(plan["target"], pr.TARGET_ORG)
        self.assertEqual(plan["organization_id"], "org-9")
        self.assertEqual(plan["aggregate_delta"], 50)
        self.assertEqual((plan["monthly_delta"], plan["one_time_delta"]), (0, 0))

    def test_missing_job_row_yields_no_plan(self):
        self.assertIsNone(pr.build_refund_plan(FakeCursor(DB()), "GONE", credit_ledger=FakeLedger()))

    def test_unparseable_job_params_is_rejected_not_guessed(self):
        """The old code turned this into a ONE-credit refund, silently under-refunding a
        40-credit job and ledgering that as the whole debt."""
        with self.assertRaises(pr.RefundPlanInvalid):
            self.plan_for(job_params="{not json")

    def test_every_derived_plan_passes_its_own_validation(self):
        for name, spec in SHAPES.items():
            with self.subTest(shape=name):
                pr.validate_refund_plan(
                    self.plan_for(source_type=spec["source_type"],
                                  job_params=spec["job_params"]))
        pr.validate_refund_plan(self.plan_for(organization_id="org-1",
                                              job_params=params(9)))


class PlanValidation(unittest.TestCase):
    """Fail closed: a plan that cannot be trusted leaves the debt pending for an operator.
    Paying the wrong bucket is as harmful as not paying."""

    def good(self, **over):
        plan = {"total": 50, "user_id": "11111111-1111-4111-8111-111111111111", "target": pr.TARGET_USER,
                "funding": pr.FUNDING_BUCKETED,
                "organization_id": None, "aggregate_delta": 50,
                "monthly_delta": 20, "one_time_delta": 30}
        plan.update(over)
        return plan

    def test_a_good_plan_validates(self):
        self.assertIsNotNone(pr.validate_refund_plan(self.good()))

    def test_not_an_object(self):
        for bad in (None, 42, "plan", [1, 2]):
            with self.assertRaises(pr.RefundPlanInvalid):
                pr.validate_refund_plan(bad)

    def test_missing_fields(self):
        for field in pr.PLAN_FIELDS:
            plan = self.good()
            plan.pop(field)
            with self.assertRaises(pr.RefundPlanInvalid, msg=field):
                pr.validate_refund_plan(plan)

    def test_non_integer_and_boolean_amounts(self):
        for value in ("40", 40.5, True, None, [40]):
            with self.assertRaises(pr.RefundPlanInvalid, msg=repr(value)):
                pr.validate_refund_plan(self.good(total=value))

    def test_negative_amounts(self):
        for field in ("total", "aggregate_delta", "monthly_delta", "one_time_delta"):
            with self.assertRaises(pr.RefundPlanInvalid, msg=field):
                pr.validate_refund_plan(self.good(**{field: -1}))

    def test_zero_total(self):
        with self.assertRaises(pr.RefundPlanInvalid):
            pr.validate_refund_plan(self.good(total=0))

    def test_buckets_exceeding_the_aggregate(self):
        with self.assertRaises(pr.RefundPlanInvalid):
            pr.validate_refund_plan(self.good(monthly_delta=40, one_time_delta=40))

    def test_split_that_does_not_sum_to_the_total(self):
        with self.assertRaises(pr.RefundPlanInvalid):
            pr.validate_refund_plan(
                self.good(aggregate_delta=50, monthly_delta=20, one_time_delta=10))

    def test_aggregate_smaller_than_total(self):
        with self.assertRaises(pr.RefundPlanInvalid):
            pr.validate_refund_plan(self.good(total=50, aggregate_delta=40,
                                              monthly_delta=0, one_time_delta=10))

    def test_aggregate_larger_than_total(self):
        with self.assertRaises(pr.RefundPlanInvalid):
            pr.validate_refund_plan(self.good(total=50, aggregate_delta=60,
                                              monthly_delta=20, one_time_delta=30))

    def test_partial_one_time_bucket(self):
        """total=50, aggregate=50, monthly=0, one_time=10 — the balances would move 50 while
        only 10 reached a spendable bucket."""
        with self.assertRaises(pr.RefundPlanInvalid):
            pr.validate_refund_plan(self.good(total=50, aggregate_delta=50,
                                              monthly_delta=0, one_time_delta=10))

    def test_legacy_may_not_carry_buckets(self):
        with self.assertRaises(pr.RefundPlanInvalid):
            pr.validate_refund_plan(self.good(funding=pr.FUNDING_LEGACY,
                                              monthly_delta=20, one_time_delta=30))

    def test_legacy_aggregate_only_is_accepted(self):
        pr.validate_refund_plan(self.good(funding=pr.FUNDING_LEGACY, total=7,
                                          aggregate_delta=7,
                                          monthly_delta=0, one_time_delta=0))

    def test_unknown_funding_shape(self):
        with self.assertRaises(pr.RefundPlanInvalid):
            pr.validate_refund_plan(self.good(funding="mystery"))

    def test_bucketed_with_no_buckets_at_all(self):
        with self.assertRaises(pr.RefundPlanInvalid):
            pr.validate_refund_plan(self.good(monthly_delta=0, one_time_delta=0))

    def test_unknown_or_missing_target(self):
        for target in ("wallet", None, ""):
            with self.assertRaises(pr.RefundPlanInvalid):
                pr.validate_refund_plan(self.good(target=target))

    def test_missing_user_id(self):
        for uid in (None, ""):
            with self.assertRaises(pr.RefundPlanInvalid):
                pr.validate_refund_plan(self.good(user_id=uid))

    def test_org_target_without_an_org_id(self):
        with self.assertRaises(pr.RefundPlanInvalid):
            pr.validate_refund_plan(self.good(target=pr.TARGET_ORG, organization_id=None,
                                              monthly_delta=0, one_time_delta=0))

    def test_org_target_touching_personal_buckets(self):
        with self.assertRaises(pr.RefundPlanInvalid):
            pr.validate_refund_plan(self.good(target=pr.TARGET_ORG,
                                              funding=pr.FUNDING_ORGANIZATION,
                                              organization_id="org-1",
                                              monthly_delta=20, one_time_delta=30))

    def test_personal_target_carrying_an_org_id(self):
        with self.assertRaises(pr.RefundPlanInvalid):
            pr.validate_refund_plan(self.good(organization_id="org-1"))


class _Fixture(unittest.TestCase):
    def make(self, shape, subscription_type=None, seed_user=True, org=None):
        spec = SHAPES[shape]
        db = DB()
        if seed_user:
            db.add_user("11111111-1111-4111-8111-111111111111", 0, monthly=0, one_time=0,
                        subscription_type=subscription_type
                        or ("monthly" if spec["source_type"] == "monthly" else "one_time"))
        if org:
            db.add_member("11111111-1111-4111-8111-111111111111", org, credits=0)
        db.add_job("J1", source_type=spec["source_type"],
                   job_params=spec["job_params"], organization_id=org)
        ledger = FakeLedger()
        if spec["source_type"] or org:
            seed_reserve(db, ledger)
        return db, FakeCursor(db), ledger, spec

    def balances(self, db):
        row = db.users.get("11111111-1111-4111-8111-111111111111")
        if row is None:
            return None
        return (row["credits_remaining"], row["monthly_credits_remaining"],
                row["one_time_credits_remaining"])


class DelayedCompensationRestoresEveryBucket(_Fixture):
    """Item 9. THE regression: a delayed monthly or mixed refund must restore the SAME
    spendable buckets an immediate one would."""

    def owe_then_settle(self, shape, **kw):
        db, cur, ledger, spec = self.make(shape, seed_user=False, **kw)
        transitioned, amount, state = pr.terminalize_and_refund(
            cur, "J1", credit_ledger=ledger)
        self.assertTrue(transitioned)
        self.assertEqual(state, pr.REFUND_PENDING, "the target row does not exist yet")
        plan = pr.build_refund_plan(cur, "J1", credit_ledger=ledger)
        self.assertTrue(pr.mark_refund_pending(cur, "J1", plan))
        # the balance row now appears
        db.add_user("11111111-1111-4111-8111-111111111111", 0, subscription_type=kw.get("subscription_type")
                    or ("monthly" if spec["source_type"] == "monthly" else "one_time"))
        if kw.get("org"):
            db.add_member("11111111-1111-4111-8111-111111111111", kw["org"], credits=0)
        result = pr.compensate_pending_refund(cur, "J1", credit_ledger=ledger)
        return db, ledger, result, spec

    def test_one_time_only(self):
        db, ledger, result, spec = self.owe_then_settle("one_time_only")
        self.assertEqual(result, pr.REFUND_DONE)
        self.assertEqual(self.balances(db), (40, 0, 40))
        self.assertEqual(len(ledger.rows), 1)

    def test_monthly_only(self):
        db, ledger, result, spec = self.owe_then_settle("monthly_only")
        self.assertEqual(result, pr.REFUND_DONE)
        self.assertEqual(self.balances(db), (30, 30, 0),
                         "the monthly bucket must be restored, not just the aggregate")

    def test_mixed_monthly_and_one_time(self):
        db, ledger, result, spec = self.owe_then_settle("mixed_monthly_and_one_time")
        self.assertEqual(result, pr.REFUND_DONE)
        self.assertEqual(self.balances(db), (50, 20, 30),
                         "BOTH spendable buckets must be restored")

    def test_legacy_aggregate_only(self):
        db, ledger, result, _ = self.owe_then_settle("legacy_aggregate_only")
        self.assertEqual(result, pr.REFUND_DONE)
        self.assertEqual(self.balances(db), (7, 0, 0),
                         "a legacy job has no buckets to restore")

    def test_lapsed_subscription_refund_stays_pending_for_support_review(self):
        db, ledger, result, _ = self.owe_then_settle(
            "mixed_monthly_and_one_time", subscription_type="one_time")
        self.assertEqual(result, pr.REFUND_PENDING)
        self.assertEqual(self.balances(db), (0, 0, 0),
                         "old monthly units must not be paid into an image balance")

    def test_organization_refund(self):
        db, cur, ledger, _ = self.make("one_time_only", seed_user=False)
        db.jobs["J1"]["organization_id"] = "org-9"
        pr.terminalize_and_refund(cur, "J1", credit_ledger=ledger)
        plan = pr.build_refund_plan(cur, "J1", credit_ledger=ledger)
        pr.mark_refund_pending(cur, "J1", plan)
        db.add_member("11111111-1111-4111-8111-111111111111", "org-9", credits=0)
        self.assertEqual(
            pr.compensate_pending_refund(cur, "J1", credit_ledger=ledger), pr.REFUND_DONE)
        self.assertEqual(db.orgs[("11111111-1111-4111-8111-111111111111", "org-9")], 40)
        self.assertNotIn("11111111-1111-4111-8111-111111111111", db.users, "no personal balance was created or touched")


class DelayedCompensationFailsClosed(_Fixture):
    def test_one_time_job_refund_after_monthly_switch_stays_pending(self):
        db, cur, ledger, _ = self.make("one_time_only", subscription_type="monthly")
        transitioned, amount, state = pr.terminalize_and_refund(
            cur, "J1", credit_ledger=ledger)
        self.assertTrue(transitioned)
        self.assertEqual((amount, state), (40, pr.REFUND_PENDING))
        self.assertEqual(self.balances(db), (0, 0, 0))

    def test_malformed_marker_stays_pending_and_is_not_cleared(self):
        db, cur, ledger, _ = self.make("mixed_monthly_and_one_time", seed_user=False)
        pr.terminalize_and_refund(cur, "J1", credit_ledger=ledger)
        pr.mark_refund_pending(cur, "J1", pr.build_refund_plan(cur, "J1", credit_ledger=ledger))
        # corrupt it in place
        p = json.loads(db.jobs["J1"]["job_params"])
        p["_failure"]["refund_pending"]["monthly_delta"] = -5
        db.jobs["J1"]["job_params"] = json.dumps(p)
        db.add_user("11111111-1111-4111-8111-111111111111", 0, subscription_type="monthly")
        self.assertEqual(
            pr.compensate_pending_refund(cur, "J1", credit_ledger=ledger), pr.REFUND_PENDING)
        self.assertEqual(self.balances(db), (0, 0, 0), "nothing may be paid on a bad plan")
        self.assertEqual(ledger.rows, [])
        self.assertIsNotNone(pr.read_refund_pending(cur, "J1"),
                             "the bad marker must survive for operator review")

    def test_marker_missing_fields_stays_pending(self):
        db, cur, ledger, _ = self.make("one_time_only", seed_user=True)
        pr.terminalize_and_refund(cur, "J1", credit_ledger=ledger)
        p = json.loads(db.jobs["J1"]["job_params"])
        p.setdefault("_failure", {})["refund_pending"] = {"amount": 40}
        db.jobs["J1"]["job_params"] = json.dumps(p)
        self.assertEqual(
            pr.compensate_pending_refund(cur, "J1", credit_ledger=ledger), pr.REFUND_PENDING)

    def test_ledger_amount_mismatch_does_not_clear_the_marker(self):
        """A job_refund row for a DIFFERENT amount means something else was settled. Clearing
        on that evidence would silently abandon the outstanding balance."""
        db, cur, ledger, _ = self.make("mixed_monthly_and_one_time", seed_user=False)
        pr.terminalize_and_refund(cur, "J1", credit_ledger=ledger)
        pr.mark_refund_pending(cur, "J1", pr.build_refund_plan(cur, "J1", credit_ledger=ledger))
        db.add_user("11111111-1111-4111-8111-111111111111", 0, subscription_type="monthly")
        ledger.record(cur, "11111111-1111-4111-8111-111111111111", 5, ledger.REASON_JOB_REFUND, "J1")   # wrong amount
        self.assertEqual(
            pr.compensate_pending_refund(cur, "J1", credit_ledger=ledger), pr.REFUND_PENDING)
        self.assertEqual(self.balances(db), (0, 0, 0), "no payment on top of a wrong row")
        self.assertIsNotNone(pr.read_refund_pending(cur, "J1"))

    def test_matching_ledger_row_clears_the_marker_without_paying_again(self):
        db, cur, ledger, _ = self.make("mixed_monthly_and_one_time", seed_user=False)
        pr.terminalize_and_refund(cur, "J1", credit_ledger=ledger)
        pr.mark_refund_pending(cur, "J1", pr.build_refund_plan(cur, "J1", credit_ledger=ledger))
        db.add_user("11111111-1111-4111-8111-111111111111", 0, subscription_type="monthly")
        ledger.record(cur, "11111111-1111-4111-8111-111111111111", 50, ledger.REASON_JOB_REFUND, "J1")   # the right amount
        self.assertEqual(
            pr.compensate_pending_refund(cur, "J1", credit_ledger=ledger), pr.REFUND_NONE)
        self.assertEqual(self.balances(db), (0, 0, 0))
        self.assertIsNone(pr.read_refund_pending(cur, "J1"))

    def test_target_still_missing_stays_pending_with_the_plan_intact(self):
        db, cur, ledger, _ = self.make("mixed_monthly_and_one_time", seed_user=False)
        pr.terminalize_and_refund(cur, "J1", credit_ledger=ledger)
        pr.mark_refund_pending(cur, "J1", pr.build_refund_plan(cur, "J1", credit_ledger=ledger))
        self.assertEqual(
            pr.compensate_pending_refund(cur, "J1", credit_ledger=ledger), pr.REFUND_PENDING)
        plan = pr.read_refund_pending(cur, "J1")
        self.assertEqual((plan["monthly_delta"], plan["one_time_delta"]), (20, 30))
        self.assertEqual(ledger.rows, [])

    def test_repeated_compensation_pays_exactly_once(self):
        db, cur, ledger, _ = self.make("mixed_monthly_and_one_time", seed_user=False)
        pr.terminalize_and_refund(cur, "J1", credit_ledger=ledger)
        pr.mark_refund_pending(cur, "J1", pr.build_refund_plan(cur, "J1", credit_ledger=ledger))
        db.add_user("11111111-1111-4111-8111-111111111111", 0, subscription_type="monthly")
        results = [pr.compensate_pending_refund(cur, "J1", credit_ledger=ledger)
                   for _ in range(6)]
        self.assertEqual(results[0], pr.REFUND_DONE)
        self.assertTrue(all(r == pr.REFUND_NONE for r in results[1:]))
        self.assertEqual(self.balances(db), (50, 20, 30))
        self.assertEqual(len(ledger.rows), 1)

    def test_no_marker_is_a_no_op(self):
        db, cur, ledger, _ = self.make("one_time_only")
        self.assertEqual(
            pr.compensate_pending_refund(cur, "J1", credit_ledger=ledger), pr.REFUND_NONE)

    def test_mark_refund_pending_reports_whether_it_landed(self):
        db, cur, ledger, _ = self.make("one_time_only")
        plan = pr.build_refund_plan(cur, "J1", credit_ledger=ledger)
        self.assertTrue(pr.mark_refund_pending(cur, "J1", plan))
        db.jobs.clear()
        self.assertFalse(pr.mark_refund_pending(cur, "J1", plan),
                         "a marker that cannot be written must report failure")

    def test_mark_refund_pending_rejects_an_invalid_plan(self):
        db, cur, ledger, _ = self.make("one_time_only")
        with self.assertRaises(pr.RefundPlanInvalid):
            pr.mark_refund_pending(cur, "J1", {"total": 40})


class ImmediateEqualsDelayed(_Fixture):
    """Item 10. For the SAME plan, the two paths must land on identical balances and identical
    ledger entries. This is the property the old code violated."""

    def immediate(self, shape, **kw):
        db, cur, ledger, _ = self.make(shape, **kw)
        pr.terminalize_and_refund(cur, "J1", credit_ledger=ledger)
        return db, ledger

    def delayed(self, shape, **kw):
        db, cur, ledger, spec = self.make(shape, seed_user=False, **kw)
        pr.terminalize_and_refund(cur, "J1", credit_ledger=ledger)
        pr.mark_refund_pending(cur, "J1", pr.build_refund_plan(cur, "J1", credit_ledger=ledger))
        db.add_user("11111111-1111-4111-8111-111111111111", 0, subscription_type=kw.get("subscription_type")
                    or ("monthly" if spec["source_type"] == "monthly" else "one_time"))
        pr.compensate_pending_refund(cur, "J1", credit_ledger=ledger)
        return db, ledger

    def test_balances_and_ledger_match_for_every_shape(self):
        for shape in SHAPES:
            with self.subTest(shape=shape):
                db_i, ledger_i = self.immediate(shape)
                db_d, ledger_d = self.delayed(shape)
                self.assertEqual(self.balances(db_i), self.balances(db_d),
                                 "immediate and delayed balances diverged for %s" % shape)
                self.assertEqual(
                    [(r.amount, r.transaction_type, r.job_id) for r in ledger_i.rows],
                    [(r.amount, r.transaction_type, r.job_id) for r in ledger_d.rows],
                    "immediate and delayed ledger entries diverged for %s" % shape)

    def test_lapsed_subscription_matches_too(self):
        db_i, ledger_i = self.immediate("mixed_monthly_and_one_time",
                                        subscription_type="one_time")
        db_d, ledger_d = self.delayed("mixed_monthly_and_one_time",
                                      subscription_type="one_time")
        self.assertEqual(self.balances(db_i), self.balances(db_d))
        self.assertEqual(len(ledger_i.rows), len(ledger_d.rows))

    def test_organization_matches_too(self):
        db_i, cur_i, ledger_i, _ = self.make("one_time_only", seed_user=False, org="org-9")
        pr.terminalize_and_refund(cur_i, "J1", credit_ledger=ledger_i)

        db_d, cur_d, ledger_d, _ = self.make("one_time_only", seed_user=False)
        db_d.jobs["J1"]["organization_id"] = "org-9"
        pr.terminalize_and_refund(cur_d, "J1", credit_ledger=ledger_d)
        pr.mark_refund_pending(cur_d, "J1", pr.build_refund_plan(cur_d, "J1", credit_ledger=ledger_d))
        db_d.add_member("11111111-1111-4111-8111-111111111111", "org-9", credits=0)
        pr.compensate_pending_refund(cur_d, "J1", credit_ledger=ledger_d)

        self.assertEqual(db_i.orgs[("11111111-1111-4111-8111-111111111111", "org-9")], db_d.orgs[("11111111-1111-4111-8111-111111111111", "org-9")])
        self.assertEqual([(r.amount, r.transaction_type, r.job_id) for r in ledger_i.rows],
                         [(r.amount, r.transaction_type, r.job_id) for r in ledger_d.rows])

    def test_one_application_function_serves_both_paths(self):
        """Structural backstop for the property above: there is exactly ONE place that writes
        a refund balance, so the two paths cannot drift apart again."""
        with open(os.path.join(BACKEND_DIR, "shared", "provisioning_retry.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        code = "\n".join(line.split("#", 1)[0] for line in src.splitlines())
        self.assertEqual(code.count("UPDATE users SET "), 2,
                         "one bucketed UPDATE and one aggregate-only UPDATE, both inside "
                         "apply_refund_plan")
        self.assertEqual(code.count("UPDATE organization_members SET "), 1)
        body = src[src.index("def apply_refund_plan("):src.index("def terminalize_and_refund(")]
        self.assertIn("UPDATE users SET ", body)
        self.assertIn("UPDATE organization_members SET ", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
