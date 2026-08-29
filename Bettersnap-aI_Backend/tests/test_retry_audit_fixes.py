"""Regression tests for every finding in the retry-integration audit.

One class per audit finding, named after it. These are behavioural: they drive the real
functions against fakes that honour WHERE clauses and rowcount, and the multi-tick reaper
tests run the SAME row through repeated reconciliations to prove a terminal state is actually
reached and reached only once.

No Azure, no database, no queue, no GPU.

Run: python -m unittest tests.test_retry_audit_fixes   (from the backend dir)
"""
import datetime
import json
import os
import sys
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from shared import provisioning_retry as pr          # noqa: E402
from shared import queue_trigger as qt               # noqa: E402
from tests.test_provisioning_retry import (          # noqa: E402
    DB, FakeCursor, FakeLedger, fake_outbox_add,
)

UTC = datetime.timezone.utc


def _t(minute):
    return datetime.datetime(2026, 8, 29, 12, minute, 0, tzinfo=UTC)


# ── P0-1: execution discovery is deterministic and skips handled executions ───
class FakeEnv:
    def __init__(self, name, value):
        self.name, self.value = name, value


class FakeExecution:
    def __init__(self, name, job_id, start_time):
        self.name = name
        self.start_time = start_time

        class _C:
            pass
        c = _C()
        c.env = [FakeEnv("JOB_ID", job_id), FakeEnv("MODE", "infer")]

        class _T:
            pass
        t = _T()
        t.containers = [c]
        self.template = t


class FakeExecClient:
    def __init__(self, executions):
        self._x = executions

        class _Jobs:
            def __init__(self, outer):
                self._outer = outer

            def list(self, rg, job_name):
                return list(self._outer._x)

            def get(self, rg, job_name, name):
                for e in self._outer._x:
                    if e.name == name:
                        return e
                raise KeyError(name)
        self.jobs_executions = _Jobs(self)


class P0_1_ExecutionDiscovery(unittest.TestCase):
    """find_execution_for_job used to return whatever the ACA list yielded first. That was
    safe only while one job had one execution; bounded retries break that invariant, and
    adopting a SPENT execution pins the row to a verdict it already had."""

    def find(self, executions, job_id="J1", **kw):
        return qt.find_execution_for_job(job_id, client=FakeExecClient(executions), **kw)

    def test_old_failed_A_and_newer_running_B_returns_B(self):
        got = self.find([FakeExecution("A", "J1", _t(0)), FakeExecution("B", "J1", _t(10))])
        self.assertEqual(got, "B")

    def test_list_order_does_not_decide(self):
        """Same two executions, reversed list order — the answer must not change."""
        got = self.find([FakeExecution("B", "J1", _t(10)), FakeExecution("A", "J1", _t(0))])
        self.assertEqual(got, "B")

    def test_old_handled_A_and_newer_B_skips_A(self):
        got = self.find([FakeExecution("A", "J1", _t(0)), FakeExecution("B", "J1", _t(10))],
                        exclude={"A"})
        self.assertEqual(got, "B")

    def test_all_executions_already_handled_returns_none(self):
        got = self.find([FakeExecution("A", "J1", _t(0)), FakeExecution("B", "J1", _t(10))],
                        exclude={"A", "B"})
        self.assertIsNone(got, "a spent execution must never be re-adopted")

    def test_equal_start_times_are_broken_deterministically_by_name(self):
        same = _t(5)
        a = self.find([FakeExecution("aaa", "J1", same), FakeExecution("zzz", "J1", same)])
        b = self.find([FakeExecution("zzz", "J1", same), FakeExecution("aaa", "J1", same)])
        self.assertEqual(a, b)
        self.assertEqual(a, "zzz")

    def test_missing_start_time_sorts_oldest(self):
        got = self.find([FakeExecution("NOTIME", "J1", None),
                         FakeExecution("B", "J1", _t(1))])
        self.assertEqual(got, "B", "an execution that never started must not beat one that did")

    def test_only_missing_start_times_still_answers_deterministically(self):
        got = self.find([FakeExecution("a", "J1", None), FakeExecution("b", "J1", None)])
        self.assertEqual(got, "b")

    def test_naive_and_aware_start_times_do_not_raise(self):
        naive = datetime.datetime(2026, 8, 29, 12, 30, 0)
        got = self.find([FakeExecution("N", "J1", naive), FakeExecution("A", "J1", _t(0))])
        self.assertEqual(got, "N")

    def test_similarly_identified_unrelated_jobs_are_not_matched(self):
        got = self.find([FakeExecution("X", "J12", _t(30)), FakeExecution("A", "J1", _t(0))])
        self.assertEqual(got, "A", "'J1' must not match the execution of job 'J12'")

    def test_not_before_drops_executions_from_a_previous_attempt(self):
        got = self.find([FakeExecution("OLD", "J1", _t(0)), FakeExecution("NEW", "J1", _t(20))],
                        not_before=_t(10))
        self.assertEqual(got, "NEW")
        self.assertIsNone(self.find([FakeExecution("OLD", "J1", _t(0))], not_before=_t(10)))

    def test_no_matching_execution_returns_none(self):
        self.assertIsNone(self.find([FakeExecution("X", "OTHER", _t(1))]))


# ── P1-1: guarded execution-id persistence ────────────────────────────────────
class P1_1_GuardedExecutionId(unittest.TestCase):
    """A slow attempt A must never overwrite a newer attempt B's execution id."""

    def setUp(self):
        self.db = DB()
        self.cur = FakeCursor(self.db)
        self.db.add_job("J1", status="dispatching", external_execution_id=None)

    def test_fills_an_empty_slot(self):
        outcome, cur_id = pr.record_execution_id(self.cur, "jobs", "J1", "A")
        self.assertEqual(outcome, pr.RECORD_OK)
        self.assertEqual(self.db.jobs["J1"]["external_execution_id"], "A")
        self.assertEqual(cur_id, "A")

    def test_slow_attempt_A_cannot_overwrite_newer_attempt_B(self):
        """The audited race, end to end: A blocks in begin_start; the row is reaped, retried
        and redispatched as B; A finally returns and tries to write its id."""
        pr.record_execution_id(self.cur, "jobs", "J1", "A")            # A recorded
        pr.retry_job(self.cur, "J1", "A", outbox_add=fake_outbox_add,  # reaped + retried
                     queue_name="inference-jobs")
        self.db.jobs["J1"]["status"] = "dispatching"                   # redispatched
        pr.record_execution_id(self.cur, "jobs", "J1", "B")            # B recorded
        outcome, cur_id = pr.record_execution_id(self.cur, "jobs", "J1", "A")   # A returns
        self.assertEqual(outcome, pr.RECORD_STALE)
        self.assertEqual(cur_id, "B")
        self.assertEqual(self.db.jobs["J1"]["external_execution_id"], "B",
                         "the late writer must not clobber the current attempt")

    def test_stale_result_reports_the_current_id_so_the_orphan_can_be_logged(self):
        pr.record_execution_id(self.cur, "jobs", "J1", "B")
        outcome, cur_id = pr.record_execution_id(self.cur, "jobs", "J1", "A")
        self.assertEqual((outcome, cur_id), (pr.RECORD_STALE, "B"))

    def test_missing_row_is_reported_distinctly(self):
        outcome, _ = pr.record_execution_id(self.cur, "jobs", "GONE", "A")
        self.assertEqual(outcome, pr.RECORD_MISSING)

    def test_container_may_have_already_moved_the_row_to_processing(self):
        """The benign case: a fast container start writes 'processing' before the dispatcher
        records the id. The slot is still empty, so the id must still be recorded."""
        self.db.jobs["J1"]["status"] = "processing"
        outcome, _ = pr.record_execution_id(self.cur, "jobs", "J1", "A")
        self.assertEqual(outcome, pr.RECORD_OK)
        self.assertEqual(self.db.jobs["J1"]["external_execution_id"], "A")

    def test_terminal_row_never_accepts_a_late_id(self):
        self.db.jobs["J1"]["status"] = "failed"
        outcome, _ = pr.record_execution_id(self.cur, "jobs", "J1", "A")
        self.assertEqual(outcome, pr.RECORD_STALE)
        self.assertIsNone(self.db.jobs["J1"]["external_execution_id"])

    def test_training_row_is_guarded_and_sets_status(self):
        self.db.add_training("T1", status="dispatching", external_execution_id=None)
        outcome, _ = pr.record_execution_id(self.cur, "lora_trainings", "T1", "A")
        self.assertEqual(outcome, pr.RECORD_OK)
        self.assertEqual(self.db.trainings["T1"]["status"], "training")
        self.assertEqual(self.db.trainings["T1"]["external_execution_id"], "A")
        outcome2, cur2 = pr.record_execution_id(self.cur, "lora_trainings", "T1", "LATE")
        self.assertEqual((outcome2, cur2), (pr.RECORD_STALE, "A"))


# ── P0-2: the ALREADY_HANDLED state is bounded ────────────────────────────────
class P0_2_OrphanLifecycle(unittest.TestCase):
    """A row pinned to an already-reconciled execution used to return ACTION_NONE forever:
    paid, undelivered, unrefunded, never terminal. It must now recover or terminalize."""

    def plan(self, **kw):
        base = dict(status="processing", current_execution_id="A", history=["A"],
                    candidate_execution_id=None, age=None, ceiling=1800)
        base.update(kw)
        return pr.plan_orphan(**base)

    def test_newer_unhandled_execution_is_adopted_not_refunded(self):
        plan, why = self.plan(candidate_execution_id="B", age=99999)
        self.assertEqual(plan, pr.ORPHAN_RECOVER)
        self.assertEqual(why, "B")

    def test_recovery_wins_even_when_the_ceiling_has_expired(self):
        """Refunding a row whose current attempt may be healthy would be premature."""
        plan, _ = self.plan(candidate_execution_id="B", age=10 ** 9)
        self.assertEqual(plan, pr.ORPHAN_RECOVER)

    def test_a_handled_candidate_is_never_adopted(self):
        plan, _ = self.plan(candidate_execution_id="A", history=["A"], age=10)
        self.assertNotEqual(plan, pr.ORPHAN_RECOVER)

    def test_unknown_timestamp_observes_and_never_terminalizes(self):
        plan, why = self.plan(age=None)
        self.assertEqual(plan, pr.ORPHAN_OBSERVE)
        self.assertIn("unknown", why)

    def test_inside_the_ceiling_observes(self):
        self.assertEqual(self.plan(age=1799)[0], pr.ORPHAN_OBSERVE)

    def test_at_and_past_the_ceiling_terminalizes(self):
        self.assertEqual(self.plan(age=1800)[0], pr.ORPHAN_TERMINAL)
        self.assertEqual(self.plan(age=1801)[0], pr.ORPHAN_TERMINAL)

    def test_already_terminal_row_is_left_alone(self):
        self.assertEqual(self.plan(status="failed", age=10 ** 9)[0], pr.ORPHAN_OBSERVE)

    def test_corrupt_history_raises_rather_than_adopting_anything(self):
        with self.assertRaises(pr.HistoryCorrupt):
            pr.plan_orphan(status="processing", current_execution_id="A",
                           history="{corrupt", candidate_execution_id="B", age=1)


class P0_2_MultiTickReaper(unittest.TestCase):
    """MULTI-TICK, not an isolated classifier call: the same row is reconciled repeatedly and
    must reach a terminal state exactly once."""

    def setUp(self):
        self.db = DB()
        self.ledger = FakeLedger()
        self.db.add_user("11111111-1111-4111-8111-111111111111", 60, one_time=60)
        self.db.add_job("J1", status="processing", external_execution_id="A",
                        provisioning_attempts=1,
                        provisioning_execution_ids=pr.dump_history(["A"]),
                        first_terminal_observed_at=None)

    def tick(self, now, candidate=None):
        """One reaper pass over the orphan path, mirroring _reconcile_orphaned_execution."""
        cur = FakeCursor(self.db)
        row = self.db.jobs["J1"]
        history = pr.parse_history(row["provisioning_execution_ids"])
        age = None
        if row["first_terminal_observed_at"] is not None:
            age = now - row["first_terminal_observed_at"]
        plan, why = pr.plan_orphan(
            status=row["status"], current_execution_id=row["external_execution_id"],
            history=history, candidate_execution_id=candidate, age=age, ceiling=1800)
        if plan == pr.ORPHAN_RECOVER:
            pr.adopt_execution(cur, "J1", row["external_execution_id"], why)
            return plan
        if plan == pr.ORPHAN_OBSERVE:
            if row["first_terminal_observed_at"] is None:
                row["first_terminal_observed_at"] = now
            return plan
        pr.terminalize_orphan(cur, "J1", row["external_execution_id"],
                              credit_ledger=self.ledger)
        return plan

    def test_repeated_ticks_reach_a_terminal_state(self):
        self.assertEqual(self.tick(now=0), pr.ORPHAN_OBSERVE)        # stamps the clock
        self.assertEqual(self.tick(now=600), pr.ORPHAN_OBSERVE)
        self.assertEqual(self.tick(now=1799), pr.ORPHAN_OBSERVE)
        self.assertEqual(self.tick(now=1800), pr.ORPHAN_TERMINAL)
        self.assertEqual(self.db.jobs["J1"]["status"], "failed")
        self.assertEqual(self.db.bal("11111111-1111-4111-8111-111111111111"), 100)

    def test_many_ticks_after_terminal_refund_only_once(self):
        self.tick(now=0)
        self.tick(now=1800)
        for extra in (1900, 2000, 5000, 100000):
            self.tick(now=extra)
        self.assertEqual(self.db.bal("11111111-1111-4111-8111-111111111111"), 100, "exactly one refund across every tick")
        self.assertEqual(self.db.credit_mutations, 1)
        self.assertEqual(len(self.ledger.rows), 1)

    def test_the_clock_is_never_reset_by_re_observation(self):
        self.tick(now=0)
        stamped = self.db.jobs["J1"]["first_terminal_observed_at"]
        self.tick(now=900)
        self.assertEqual(self.db.jobs["J1"]["first_terminal_observed_at"], stamped,
                         "a resettable clock would make the ceiling unreachable")

    def test_a_newer_execution_rescues_the_row_instead_of_refunding_it(self):
        self.tick(now=0)
        self.assertEqual(self.tick(now=1800, candidate="B"), pr.ORPHAN_RECOVER)
        self.assertEqual(self.db.jobs["J1"]["external_execution_id"], "B")
        self.assertIsNone(self.db.jobs["J1"]["first_terminal_observed_at"],
                          "the adopted attempt gets its own clock")
        self.assertEqual(self.db.jobs["J1"]["status"], "processing")
        self.assertEqual(self.db.bal("11111111-1111-4111-8111-111111111111"), 60, "no premature refund")

    def test_adopt_is_guarded_so_two_reapers_adopt_once(self):
        a = pr.adopt_execution(FakeCursor(self.db), "J1", "A", "B")
        b = pr.adopt_execution(FakeCursor(self.db), "J1", "A", "C")
        self.assertTrue(a)
        self.assertFalse(b)
        self.assertEqual(self.db.jobs["J1"]["external_execution_id"], "B")

    def test_terminalize_orphan_refuses_a_row_that_moved_on(self):
        self.db.jobs["J1"]["external_execution_id"] = "B"
        transitioned, refund, _state = pr.terminalize_orphan(
            FakeCursor(self.db), "J1", "A", credit_ledger=self.ledger)
        self.assertFalse(transitioned)
        self.assertEqual(refund, 0)
        self.assertEqual(self.db.jobs["J1"]["status"], "processing")


# ── P1-3: refund invariants ───────────────────────────────────────────────────
class P1_3_RefundInvariants(unittest.TestCase):
    """SUPERSEDED CONTRACT. The previous phase raised RefundTargetMissing and rolled the whole
    terminal transaction back. That satisfied "never ledger a refund that did not move" but
    violated "never leave a paid job non-terminal forever" — the row went back to
    processing/dispatching on every tick. The refund UPDATE matches 0 rows, so nothing is
    mutated and there is nothing to undo; the job now terminalizes, the debt is RECORDED, and
    a compensator settles it exactly once."""

    def setUp(self):
        self.db = DB()
        self.cur = FakeCursor(self.db)
        self.ledger = FakeLedger()

    def test_personal_refund_moves_money_and_ledgers_once(self):
        self.db.add_user("11111111-1111-4111-8111-111111111111", 60)
        self.db.add_job("J1", source_type="one_time")
        self.db.add_reserve(self.ledger)
        transitioned, refund, state = pr.terminalize_and_refund(
            self.cur, "J1", credit_ledger=self.ledger)
        self.assertTrue(transitioned)
        self.assertEqual(state, pr.REFUND_DONE)
        self.assertEqual(self.db.bal("11111111-1111-4111-8111-111111111111"), 100)
        self.assertEqual(len(self.ledger.rows), 1)

    def test_org_refund_returns_credits_to_the_org_pool(self):
        self.db.add_job("J1", organization_id="org-9", source_type="monthly")
        self.db.add_reserve(self.ledger)
        self.db.add_member("11111111-1111-4111-8111-111111111111", "org-9", credits=0)
        _, _, state = pr.terminalize_and_refund(self.cur, "J1", credit_ledger=self.ledger)
        self.assertEqual(state, pr.REFUND_DONE)
        self.assertEqual(self.db.orgs[("11111111-1111-4111-8111-111111111111", "org-9")], 40)
        self.assertEqual(self.db.users.get("11111111-1111-4111-8111-111111111111", 0), 0, "personal balance untouched")
        self.assertEqual(self.ledger.rows[0].job_id, "J1",
                         "the ledger row carries the job, which carries the org linkage")
        self.assertEqual(self.ledger.rows[0].user_id, "11111111-1111-4111-8111-111111111111")
        self.assertEqual(self.ledger.rows[0].amount, 40)

    def test_member_who_left_the_org_is_pending_not_ledgered(self):
        self.db.add_job("J1", organization_id="org-9", source_type="monthly")
        self.db.add_reserve(self.ledger)
        # no organization_members row for (u1, org-9)
        transitioned, refund, state = pr.terminalize_and_refund(
            self.cur, "J1", credit_ledger=self.ledger)
        self.assertTrue(transitioned, "the job must NOT be left non-terminal")
        self.assertEqual(state, pr.REFUND_PENDING)
        self.assertEqual(refund, 40)
        self.assertEqual(self.ledger.rows, [],
                         "no ledger row may describe money that never moved")
        self.assertEqual(self.db.credit_mutations, 0)

    def test_missing_user_is_pending_not_ledgered(self):
        self.db.add_job("J1", source_type="one_time", user_id="99999999-9999-4999-8999-999999999999")
        self.db.add_reserve(self.ledger)
        transitioned, _, state = pr.terminalize_and_refund(
            self.cur, "J1", credit_ledger=self.ledger)
        self.assertTrue(transitioned)
        self.assertEqual(state, pr.REFUND_PENDING)
        self.assertEqual(self.ledger.rows, [])

    def test_monthly_and_legacy_targets_are_checked_too(self):
        for source in ("monthly", None):
            db = DB()
            cur = FakeCursor(db)
            ledger = FakeLedger()
            db.add_job("J1", source_type=source, user_id="99999999-9999-4999-8999-999999999999")
            if source:
                db.add_reserve(ledger)
            _, _, state = pr.terminalize_and_refund(cur, "J1", credit_ledger=ledger)
            self.assertEqual(state, pr.REFUND_PENDING, "source_type=%s" % source)
            self.assertEqual(ledger.rows, [])

    def test_exhaustion_reports_pending_rather_than_raising(self):
        self.db.add_job("J1", external_execution_id="e3", status="processing",
                        source_type="one_time", user_id="99999999-9999-4999-8999-999999999999",
                        provisioning_attempts=2,
                        provisioning_execution_ids=pr.dump_history(["e1", "e2"]))
        self.db.add_reserve(self.ledger)
        transitioned, _, _, state = pr.exhaust_job(
            self.cur, "J1", "e3", credit_ledger=self.ledger)
        self.assertTrue(transitioned)
        self.assertEqual(state, pr.REFUND_PENDING)
        self.assertEqual(self.db.jobs["J1"]["status"], "failed")


# ── P0-3: fused exhaustion isolation ──────────────────────────────────────────
class P0_3_FusedExhaustionIsolation(unittest.TestCase):
    """Source-level guarantees plus the composed behaviour that the audit found missing."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BACKEND_DIR, "function_app.py"), encoding="utf-8") as fh:
            cls.src = fh.read()

    def body(self, name):
        start = self.src.index("def %s(" % name)
        rest = self.src[start + 1:]
        nxt = rest.find("\ndef ")
        return rest[:nxt] if nxt != -1 else rest

    def test_exhaustion_disables_the_parked_job_sweep(self):
        self.assertIn("sweep_parked=False", self.body("_exhaust_provisioning_training"))

    def test_ordinary_training_failure_keeps_the_sweep(self):
        watcher = self.body("_watch_one")
        self.assertIn("_finish_training(training_id, user_id, ok=False", watcher)
        self.assertNotIn("sweep_parked", watcher,
                         "the ordinary failure path must keep today's behaviour")

    def test_finish_training_only_queries_parked_jobs_when_sweeping(self):
        body = self.body("_finish_training")
        idx = body.index("if sweep_parked:")
        select = body.index("status = 'waiting_lora'", idx)
        self.assertGreater(select, idx, "the parked query must be inside the sweep guard")

    def test_exhaustion_does_not_let_a_storage_blip_fail_the_lora(self):
        body = self.body("_exhaust_provisioning_training")
        self.assertIn("_identity_adapter_state(", body)
        self.assertIn("_has_completed_training(", body)
        self.assertNotIn("_identity_adapter_exists(", body,
                         "the fail-closed boolean probe must not decide this path")

    def test_exhaustion_no_longer_swallows_errors_before_completion(self):
        body = self.body("_exhaust_provisioning_training")
        self.assertIn("raise", body)
        raise_at = body.index("raise")
        finish_at = body.index("_finish_training(")
        self.assertLess(raise_at, finish_at,
                        "a failed terminalize must abort, not fall through to completion")

    def test_link_is_retained_for_audit(self):
        body = self.body("_exhaust_provisioning_training")
        self.assertNotIn("fused_job_id = NULL", body)
        self.assertNotIn("fused_job_id=None", body)

    def test_adapter_state_is_tri_state(self):
        body = self.body("_identity_adapter_state")
        self.assertIn("return None", body)


class P0_3_ComposedExhaustion(unittest.TestCase):
    """Three parked jobs plus the linked one; only the linked one may be terminalized."""

    def setUp(self):
        self.db = DB()
        self.cur = FakeCursor(self.db)
        self.ledger = FakeLedger()
        self.db.add_user("11111111-1111-4111-8111-111111111111", 0)
        self.db.add_training("T1", user_id="11111111-1111-4111-8111-111111111111", external_execution_id="e3",
                             fused_job_id="LINKED", provisioning_attempts=2,
                             provisioning_execution_ids=pr.dump_history(["e1", "e2"]))
        self.db.add_job("LINKED", user_id="11111111-1111-4111-8111-111111111111", status="processing", source_type="one_time")
        self.db.add_reserve(self.ledger, "LINKED")
        for name in ("PARK1", "PARK2", "PARK3"):
            self.db.add_job(name, user_id="11111111-1111-4111-8111-111111111111", status="waiting_lora", source_type="one_time")

    def exhaust(self):
        pr.record_training_attempt(self.cur, "T1", "e3")
        ok, _ = pr.verify_fused_link(self.cur, "LINKED", "11111111-1111-4111-8111-111111111111", pr.FUSED_RECLAIMABLE_STATES)
        self.assertTrue(ok)
        return pr.terminalize_and_refund(self.cur, "LINKED", credit_ledger=self.ledger)

    def test_three_parked_jobs_survive_untouched(self):
        self.exhaust()
        self.assertEqual(self.db.jobs["LINKED"]["status"], "failed")
        for name in ("PARK1", "PARK2", "PARK3"):
            self.assertEqual(self.db.jobs[name]["status"], "waiting_lora",
                             "%s was collateral damage" % name)
        self.assertEqual(self.db.credit_mutations, 1, "exactly one refund")
        self.assertEqual(len(self.ledger.rows), 1)

    def test_the_link_survives_as_audit(self):
        self.exhaust()
        self.assertEqual(self.db.trainings["T1"]["fused_job_id"], "LINKED")
        self.assertEqual(json.loads(
            self.db.trainings["T1"]["provisioning_execution_ids"]), ["e1", "e2", "e3"])

    def test_repeated_exhaustion_refunds_once(self):
        self.exhaust()
        pr.terminalize_and_refund(self.cur, "LINKED", credit_ledger=self.ledger)
        self.assertEqual(self.db.credit_mutations, 1)
        self.assertEqual(len(self.ledger.rows), 1)


# ── P1-4 / P2: per-item isolation and honest returns ─────────────────────────
class P1_4_PerItemIsolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(BACKEND_DIR, "function_app.py"), encoding="utf-8") as fh:
            cls.src = fh.read()

    def body(self, name):
        start = self.src.index("def %s(" % name)
        rest = self.src[start + 1:]
        nxt = rest.find("\ndef ")
        return rest[:nxt] if nxt != -1 else rest

    def test_reaper_isolates_each_job(self):
        body = self.body("reaper")
        self.assertIn("_reap_one(job_id, now_ts)", body)
        self.assertIn("except Exception", body)

    def test_watcher_isolates_each_training(self):
        body = self.body("training_watcher")
        self.assertIn("_watch_one(", body)
        self.assertIn("except Exception", body)

    def test_reaper_early_reconcile_covers_dispatching(self):
        """A pre-container failure can never reach 'processing', so scoping the early
        reconcile to 'processing' left exactly the retryable class waiting the full window."""
        body = self.body("reaper")
        self.assertIn("status IN ('processing', 'dispatching')", body)

    def test_reaper_recovery_excludes_handled_executions(self):
        body = self.body("reaper")
        self.assertIn("handled, corrupt = _handled_executions(job_id)", body)
        self.assertIn("exclude=handled", body)

    def test_reaper_runs_the_pending_refund_compensator(self):
        self.assertIn("_compensate_pending_refunds()", self.body("reaper"))

    def test_exhaustion_returns_refunded_honestly(self):
        body = self.body("_retry_provisioning_job")
        self.assertIn('"refunded": bool(transitioned)', body)

    def test_exhaustion_records_a_pending_refund_rather_than_dropping_it(self):
        body = self.body("_retry_provisioning_job")
        self.assertIn("provisioning_retry.REFUND_PENDING", body)
        self.assertIn("_record_refund_debt(", body)

    def test_failure_class_is_stamped_in_the_exhaustion_transaction(self):
        body = self.body("_retry_provisioning_job")
        stamp_at = body.index("_stamp_failure_class_tx(")
        commit_at = body.index("conn.commit()", stamp_at)
        self.assertLess(stamp_at, commit_at,
                        "the class must commit with the refund, not after it")

    def test_mark_failed_records_a_pending_refund_and_still_terminalizes(self):
        """The superseded design rolled the transition back, leaving a paid job
        non-terminal forever. It must now terminalize AND record the FULL plan — and roll
        back only in the one case where the obligation itself could not be written down."""
        body = self.body("_mark_failed")
        self.assertIn("REFUND_PENDING", body)
        self.assertIn("_record_refund_debt(", body)
        self.assertIn("MARK_REFUND_PENDING", body)
        self.assertIn("RefundDebtNotRecorded", body)
        self.assertIn("MARK_REFUND_BLOCKED", body)

    def test_record_execution_id_logs_the_orphan_and_does_not_retry(self):
        body = self.body("_record_execution_id")
        self.assertIn("ORPHAN_EXECUTION", body)
        self.assertNotIn("UPDATE jobs SET external_execution_id", body,
                         "the raw blind write must be gone")

    def test_training_retry_age_semantics_are_documented(self):
        self.assertIn("RETRY AGE SEMANTICS", self.body("_watch_one"))


# ── item 6: deployment / schema ordering ──────────────────────────────────────
class SchemaGate(unittest.TestCase):
    def test_runner_declares_every_runtime_required_column(self):
        sys.path.insert(0, os.path.join(BACKEND_DIR, "scripts"))
        import run_migrations                                  # noqa: E402
        required = run_migrations.REQUIRED_RUNTIME_COLUMNS
        self.assertEqual(
            set(required["dbo.jobs"]),
            {"provisioning_attempts", "provisioning_execution_ids",
             "first_terminal_observed_at"})
        self.assertEqual(
            set(required["dbo.lora_trainings"]),
            {"provisioning_attempts", "provisioning_execution_ids",
             "first_terminal_observed_at", "fused_job_id"})

    def test_verify_schema_reports_every_missing_column(self):
        sys.path.insert(0, os.path.join(BACKEND_DIR, "scripts"))
        import run_migrations                                  # noqa: E402

        class NoColumns:
            def __init__(self):
                self._v = None

            def execute(self, sql, *params):
                self._v = (None,)

            def fetchone(self):
                return self._v
        missing = run_migrations.verify_runtime_schema(NoColumns())
        self.assertEqual(len(missing), 7)
        self.assertIn("dbo.lora_trainings.fused_job_id", missing)

    def test_verify_schema_passes_when_every_column_exists(self):
        sys.path.insert(0, os.path.join(BACKEND_DIR, "scripts"))
        import run_migrations                                  # noqa: E402

        class AllColumns:
            def execute(self, sql, *params):
                pass

            def fetchone(self):
                return (4,)
        self.assertEqual(run_migrations.verify_runtime_schema(AllColumns()), [])

    def test_deploy_script_verifies_even_when_migrations_are_skipped(self):
        with open(os.path.join(os.path.dirname(BACKEND_DIR), "deploy.ps1"),
                  encoding="utf-8") as fh:
            ps = fh.read()
        skip_block = ps[ps.index("Migrations SKIPPED"):]
        self.assertIn("--verify-schema", skip_block,
                      "-SkipMigrations must skip APPLYING, never VERIFYING")
        self.assertIn("Refusing to deploy", skip_block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
