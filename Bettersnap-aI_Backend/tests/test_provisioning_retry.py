"""Offline tests for the bounded provisioning-retry state machine (shared/provisioning_retry).

Everything here runs against an in-memory fake SQL Server that MODELS THE GUARDS RATHER THAN
THE SYNTAX: each UPDATE's WHERE clause is evaluated for real, and rowcount is set from it, so a
test can only pass if the production statement's predicates actually hold. That is the point —
these are atomicity and idempotency tests, and a fake that ignored the WHERE clause would prove
nothing.

No Azure, no database, no queue, no GPU.

Run: python -m unittest tests.test_provisioning_retry   (from the backend dir)
"""
import json
import os
import uuid
import sys
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from shared import provisioning_retry as pr        # noqa: E402


# ── a fake cursor that honours WHERE clauses ─────────────────────────────────
def sql_guid_eq(left, right):
    """Model SQL Server's `WHERE user_id = ?` on a UNIQUEIDENTIFIER column.

    The engine compares uniqueidentifier as a BINARY type, so the predicate is
    case-INSENSITIVE: an uppercase stored value matches a lowercase parameter. The fake used
    exact string equality, which made a mixed-case owner look like a different person and hid
    the very asymmetry that broke verify_fused_link on the real engine. Modelling the engine
    here is what lets the offline suite reproduce it.
    """
    try:
        return uuid.UUID(str(left)) == uuid.UUID(str(right))
    except (AttributeError, TypeError, ValueError):
        return str(left) == str(right)   # non-GUID test ids ("11111111-1111-4111-8111-111111111111") still compare exactly


class LedgerRow:
    """One credit_transactions row, with every column tracked SEPARATELY.

    transaction_id matters because already_refunded now selects it, and user_id must be
    independent of job_id so a row credited to the WRONG user is distinguishable from a
    correct one.
    """

    __slots__ = ("transaction_id", "user_id", "amount", "transaction_type", "job_id")

    def __init__(self, transaction_id, user_id, amount, transaction_type, job_id):
        self.transaction_id = transaction_id
        self.user_id = user_id
        self.amount = int(amount)
        self.transaction_type = transaction_type
        self.job_id = job_id

    def __repr__(self):
        return ("LedgerRow(%r, user=%r, amount=%r, type=%r, job=%r)"
                % (self.transaction_id, self.user_id, self.amount,
                   self.transaction_type, self.job_id))


class FakeLedger:
    REASON_JOB_REFUND = "job_refund"
    REASON_JOB_RESERVE = "job_reserve"
    REASON_RETRAIN_REFUND = "retrain_refund"

    def __init__(self):
        self.rows = []

    def record(self, cur, user_id, amount, transaction_type, job_id=None):
        if int(amount) == 0:
            return
        row = LedgerRow("tx-%d" % (len(cur.db.ledger) + 1), user_id, amount,
                        transaction_type, job_id)
        self.rows.append(row)
        # The fake DB must SEE the row: already_refunded and the reserve cross-check are both
        # SELECTs, so a ledger the cursor cannot read would make every guard vacuous.
        cur.db.ledger.append(row)

    def add(self, db, user_id, amount, transaction_type, job_id):
        """Seed a row directly (for rows a test wants to pre-exist)."""
        row = LedgerRow("tx-%d" % (len(db.ledger) + 1), user_id, amount,
                        transaction_type, job_id)
        db.ledger.append(row)
        return row

    def refunds(self, job_id=None):
        return [r for r in self.rows
                if r.transaction_type == self.REASON_JOB_REFUND
                and (job_id is None or r.job_id == job_id)]


class DB:
    """Mutable world the fake cursor operates on."""

    def __init__(self):
        self.jobs = {}
        self.trainings = {}
        self.users = {}
        self.orgs = {}          # (user_id, org_id) -> credits
        self.org_members = set()  # (user_id, org_id) rows that EXIST
        self.outbox = []
        self.ledger = []        # credit_transactions rows the fake cursor can query
        self.credit_mutations = 0

    def add_user(self, user_id, credits=0, monthly=0, one_time=0,
                 subscription_type="one_time"):
        """A users row with the THREE balances tracked independently. A single scalar would
        hide exactly the bucket defect these tests exist to catch."""
        self.users[user_id] = {"credits_remaining": credits,
                               "monthly_credits_remaining": monthly,
                               "one_time_credits_remaining": one_time,
                               "subscription_type": subscription_type}
        return self.users[user_id]

    def bal(self, user_id):
        return self.users[user_id]["credits_remaining"]

    def add_reserve(self, ledger, job_id="J1", user_id=None):
        """The single strictly-negative job_reserve row a properly-charged job has. Bucketed
        and organization-funded jobs now REQUIRE one; legacy aggregate-only jobs do not."""
        row = self.jobs[job_id]
        total = json.loads(row["job_params"])["credit_cost"]
        return ledger.add(self, user_id or row["user_id"], -total,
                          ledger.REASON_JOB_RESERVE, job_id)

    def add_member(self, user_id, org_id, credits=0):
        """An organization_members row that EXISTS. Refund rowcount now depends on this: a
        member who left the org has no row, and the production code must notice."""
        self.org_members.add((user_id, org_id))
        self.orgs[(user_id, org_id)] = credits

    def add_job(self, job_id, **kw):
        row = {"job_id": job_id, "user_id": "11111111-1111-4111-8111-111111111111", "status": "processing",
               "external_execution_id": None, "job_params": json.dumps({"credit_cost": 40}),
               "organization_id": None, "source_type": None, "created_at": 0,
               "provisioning_attempts": 0, "provisioning_execution_ids": None,
               "first_terminal_observed_at": None, "dispatched_at": 1}
        row.update(kw)
        self.jobs[job_id] = row
        return row

    def add_training(self, training_id, **kw):
        row = {"training_id": training_id, "user_id": "11111111-1111-4111-8111-111111111111", "status": "training",
               "external_execution_id": None, "fused_job_id": None,
               "provisioning_attempts": 0, "provisioning_execution_ids": None,
               "first_terminal_observed_at": None}
        row.update(kw)
        self.trainings[training_id] = row
        return row


class FakeCursor:
    """Interprets the exact statements provisioning_retry issues, honouring every predicate."""

    def __init__(self, db):
        self.db = db
        self.rowcount = 0
        self._fetch = None

    # -- helpers ----------------------------------------------------------
    def _job(self, jid):
        return self.db.jobs.get(str(jid))

    def _train(self, tid):
        return self.db.trainings.get(str(tid))

    def _credit_user(self, uid, aggregate, monthly=0, one_time=0):
        """A users UPDATE matches 1 row only if that user exists. Each BUCKET is applied
        separately, so a path that restores the aggregate but forgets a bucket fails a test
        instead of silently passing."""
        row = self.db.users.get(uid)
        if row is None:
            self.rowcount = 0
            return
        row["credits_remaining"] += aggregate
        # Mirrors the production CASE: a lapsed subscription gets no monthly bucket back.
        if monthly and row.get("subscription_type") == "monthly":
            row["monthly_credits_remaining"] += monthly
        row["one_time_credits_remaining"] += one_time
        self.db.credit_mutations += 1
        self.rowcount = 1

    def execute(self, sql, *params):
        s = " ".join(sql.split()).lower()
        self.rowcount = 0
        self._fetch = None
        self._rows = []
        J = self.db.jobs

        # ---- reads -------------------------------------------------------
        if s.startswith("select status, user_id, job_params, provisioning_attempts"):
            jid, ex = str(params[0]), str(params[1])
            r = self._job(jid)
            if r and str(r["external_execution_id"]) == ex:
                self._fetch = (r["status"], r["user_id"], r["job_params"],
                               r["provisioning_attempts"], r["provisioning_execution_ids"])
        elif s.startswith("select status, provisioning_attempts, provisioning_execution_ids from jobs"):
            jid, ex = str(params[0]), str(params[1])
            r = self._job(jid)
            if r and str(r["external_execution_id"]) == ex:
                self._fetch = (r["status"], r["provisioning_attempts"],
                               r["provisioning_execution_ids"])
        elif s.startswith("select job_params, user_id, source_type from jobs"):
            r = self._job(params[0])
            self._fetch = (r["job_params"], r["user_id"], r["source_type"]) if r else None
        elif s.startswith("select source_type from jobs"):
            r = self._job(params[0])
            self._fetch = (r["source_type"],) if r else None
        elif s.startswith("select subscription_type from users"):
            r = self.db.users.get(params[0])
            self._fetch = (r["subscription_type"],) if r else None
        elif s.startswith("select organization_id from jobs"):
            r = self._job(params[0])
            self._fetch = (r["organization_id"],) if r else None
        elif s.startswith("select user_id, status from jobs"):
            r = self._job(params[0])
            self._fetch = (r["user_id"], r["status"]) if r else None
        elif s.startswith("select fused_job_id, user_id from lora_trainings"):
            r = self._train(params[0])
            self._fetch = (r["fused_job_id"], r["user_id"]) if r else None
        elif s.startswith("select fused_job_id from lora_trainings"):
            r = self._train(params[0])
            self._fetch = (r["fused_job_id"],) if r else None
        elif s.startswith("select status, user_id, fused_job_id, provisioning_attempts"):
            tid, ex = str(params[0]), str(params[1])
            r = self._train(tid)
            if r and str(r["external_execution_id"]) == ex:
                self._fetch = (r["status"], r["user_id"], r["fused_job_id"],
                               r["provisioning_attempts"], r["provisioning_execution_ids"])
        elif s.startswith("select provisioning_attempts, provisioning_execution_ids from lora_trainings"):
            tid, ex = str(params[0]), str(params[1])
            r = self._train(tid)
            if r and str(r["external_execution_id"]) == ex:
                self._fetch = (r["provisioning_attempts"], r["provisioning_execution_ids"])
        elif s.startswith("select top 1 job_id from jobs"):
            uid = params[0]
            elig = [r for r in J.values()
                    if sql_guid_eq(r["user_id"], uid)
                    and r["status"] == "waiting_lora"]
            # ORDER BY created_at, job_id — the tie-break under test.
            elig.sort(key=lambda r: (r["created_at"], str(r["job_id"])))
            self._fetch = (elig[0]["job_id"],) if elig else None

        # ---- writes ------------------------------------------------------
        elif s.startswith("update jobs set external_execution_id = ? where job_id = ? and status = ?"):
            ex, jid, expected = params
            r = self._job(jid)
            if (r and r["status"] == expected
                    and r["external_execution_id"] is None):
                r["external_execution_id"] = ex
                self.rowcount = 1
        elif s.startswith("update jobs set external_execution_id = ?, first_terminal_observed_at = null"):
            new_ex, jid, stale = params
            r = self._job(jid)
            if (r and str(r["external_execution_id"]) == str(stale)
                    and r["status"] not in ("completed", "failed")):
                r["external_execution_id"] = new_ex
                r["first_terminal_observed_at"] = None
                self.rowcount = 1
        elif s.startswith("update lora_trainings set status = 'training', external_execution_id"):
            ex, tid, expected = params
            r = self._train(tid)
            if (r and r["status"] == expected
                    and r["external_execution_id"] is None):
                r.update(status="training", external_execution_id=ex)
                self.rowcount = 1
        elif s.startswith("select status, external_execution_id from jobs"):
            r = self._job(params[0])
            self._fetch = (r["status"], r["external_execution_id"]) if r else None
        elif s.startswith("select status, external_execution_id from lora_trainings"):
            r = self._train(params[0])
            self._fetch = (r["status"], r["external_execution_id"]) if r else None
        elif s.startswith("select status from jobs"):
            jid, ex = str(params[0]), str(params[1])
            r = self._job(jid)
            if r and str(r["external_execution_id"]) == ex:
                self._fetch = (r["status"],)
        elif s.startswith("update jobs set status = 'queued', external_execution_id = null"):
            attempts, hist, jid, ex, status = params
            r = self._job(jid)
            if (r and str(r["external_execution_id"]) == str(ex) and r["status"] == status):
                r.update(status="queued", external_execution_id=None,
                         first_terminal_observed_at=None, dispatched_at=None,
                         provisioning_attempts=attempts, provisioning_execution_ids=hist)
                self.rowcount = 1
        elif s.startswith("update jobs set provisioning_attempts"):
            attempts, hist, jid, ex = params
            r = self._job(jid)
            if r and str(r["external_execution_id"]) == str(ex):
                r.update(provisioning_attempts=attempts, provisioning_execution_ids=hist)
                self.rowcount = 1
        elif s.startswith("update jobs set status = 'failed'"):
            r = self._job(params[0])
            if r and r["status"] not in ("failed", "completed"):
                r["status"] = "failed"
                self.rowcount = 1
        elif s.startswith("update jobs set first_terminal_observed_at"):
            jid, ex = params
            r = self._job(jid)
            if (r and str(r["external_execution_id"]) == str(ex)
                    and r["first_terminal_observed_at"] is None
                    and r["status"] not in ("completed", "failed")):
                r["first_terminal_observed_at"] = "T1"
                self.rowcount = 1
        elif s.startswith("update jobs set status = 'processing'"):
            if len(params) == 2:
                jid, uid = params
            else:
                jid, uid = params[0], None
            r = self._job(jid)
            if r and r["status"] == "waiting_lora" and (uid is None
                                                        or sql_guid_eq(r["user_id"], uid)):
                r.update(status="processing", dispatched_at=1)
                self.rowcount = 1
        elif s.startswith("update jobs set status = 'waiting_lora'"):
            jid, uid = params
            r = self._job(jid)
            if r and r["status"] == "processing" and sql_guid_eq(r["user_id"], uid):
                r.update(status="waiting_lora", dispatched_at=None)
                self.rowcount = 1
        elif s.startswith("select job_params, user_id, source_type, organization_id from jobs"):
            r = self._job(params[0])
            self._fetch = ((r["job_params"], r["user_id"], r["source_type"],
                            r["organization_id"]) if r else None)
        elif s.startswith("update users set monthly_credits_remaining = monthly_credits_remaining + ?"):
            monthly, one_time, aggregate, uid = params
            self._credit_user(uid, aggregate, monthly=monthly, one_time=one_time)
        elif s.startswith("update users set credits_remaining = credits_remaining + ? where user_id = ?"):
            self._credit_user(params[1], params[0])
        elif s.startswith("update organization_members set credits_remaining = credits_remaining + ? where user_id = ? and organization_id = ?"):
            amount, uid, org = params
            key = (uid, org)
            if key not in self.db.org_members:
                self.rowcount = 0
                return
            self.db.orgs[key] = self.db.orgs.get(key, 0) + amount
            self.db.credit_mutations += 1
            self.rowcount = 1
        elif s.startswith("select transaction_id, user_id, amount from credit_transactions"):
            jid, kind = params
            self._rows = [(r.transaction_id, r.user_id, r.amount) for r in self.db.ledger
                          if r.job_id == jid and r.transaction_type == kind]
        elif s.startswith("select amount from credit_transactions"):
            jid, kind = params
            self._rows = [(r.amount,) for r in self.db.ledger
                          if r.job_id == jid and r.transaction_type == kind]
        elif s.startswith("select job_params from jobs"):
            r = self._job(params[0])
            self._fetch = (r["job_params"],) if r else None
        elif s.startswith("update jobs set job_params = ?"):
            newparams, jid = params
            r = self._job(jid)
            if r:
                r["job_params"] = newparams
                self.rowcount = 1
        elif s.startswith("select user_id, organization_id, source_type from jobs"):
            r = self._job(params[0])
            self._fetch = (r["user_id"], r["organization_id"], r["source_type"]) if r else None
        elif s.startswith("update organization_members set credits_remaining"):
            amount, jid, org = params
            uid = self._job(jid)["user_id"]
            key = (uid, org)
            # rowcount 0 when the membership row does not exist -- the member left the org.
            if key not in self.db.org_members:
                self.rowcount = 0
                return
            self.db.orgs[key] = self.db.orgs.get(key, 0) + amount
            self.db.credit_mutations += 1
            self.rowcount = 1
        elif s.startswith("update lora_trainings set status = 'queued'"):
            attempts, hist, tid, ex, status = params
            r = self._train(tid)
            if r and str(r["external_execution_id"]) == str(ex) and r["status"] == status:
                r.update(status="queued", external_execution_id=None,
                         first_terminal_observed_at=None,
                         provisioning_attempts=attempts, provisioning_execution_ids=hist)
                self.rowcount = 1
        elif s.startswith("update lora_trainings set provisioning_attempts"):
            attempts, hist, tid, ex = params
            r = self._train(tid)
            if r and str(r["external_execution_id"]) == str(ex):
                r.update(provisioning_attempts=attempts, provisioning_execution_ids=hist)
                self.rowcount = 1
        elif s.startswith("update lora_trainings set fused_job_id"):
            jid, tid = params
            r = self._train(tid)
            if r and r["fused_job_id"] is None:
                r["fused_job_id"] = jid
                self.rowcount = 1
        elif s.startswith("update lora_trainings set first_terminal_observed_at"):
            tid, ex = params
            r = self._train(tid)
            if (r and str(r["external_execution_id"]) == str(ex)
                    and r["first_terminal_observed_at"] is None
                    and r["status"] not in ("completed", "failed")):
                r["first_terminal_observed_at"] = "T1"
                self.rowcount = 1
        else:
            raise AssertionError("unmodelled SQL in test fake: %s" % s[:120])

    def fetchone(self):
        return self._fetch

    def fetchall(self):
        return getattr(self, "_rows", [])


def fake_outbox_add(cur, queue_name, payload):
    cur.db.outbox.append({"queue": queue_name, "payload": payload, "delivered": False})
    return len(cur.db.outbox) - 1


# ── attempt semantics ─────────────────────────────────────────────────────────
class AttemptSemantics(unittest.TestCase):
    """provisioning_attempts == number of DISTINCT ACA executions handled.
    MAX_PROVISIONING_EXECUTIONS is the TOTAL execution budget, initial run included."""

    def test_budget_of_three_allows_exactly_three_executions(self):
        history = None
        plans = []
        for i in range(1, 5):
            try:
                plan, history, attempts = pr.plan_attempt(
                    history, "exec-%d" % i, attempts=None, max_executions=3)
            except pr.HistoryCorrupt:
                break
            plans.append((plan, attempts))
            history = pr.dump_history(history)
        self.assertEqual(plans[:3], [(pr.PLAN_RETRY, 1), (pr.PLAN_RETRY, 2),
                                     (pr.PLAN_EXHAUSTED, 3)])

    def test_budget_of_one_never_retries(self):
        plan, _, attempts = pr.plan_attempt(None, "exec-1", max_executions=1)
        self.assertEqual((plan, attempts), (pr.PLAN_EXHAUSTED, 1))

    def test_attempts_always_equals_history_length(self):
        for n in range(0, 4):
            hist = pr.dump_history(["e%d" % i for i in range(n)])
            plan, new_hist, attempts = pr.plan_attempt(hist, "new", attempts=n,
                                                       max_executions=10)
            self.assertEqual(attempts, len(new_hist))
            self.assertEqual(attempts, n + 1)

    def test_counter_drift_is_corruption_not_a_free_retry(self):
        hist = pr.dump_history(["e1", "e2"])
        with self.assertRaises(pr.HistoryCorrupt):
            pr.plan_attempt(hist, "e3", attempts=0, max_executions=3)


class HistoryParsing(unittest.TestCase):
    def test_null_and_empty_are_an_empty_history(self):
        self.assertEqual(pr.parse_history(None), [])
        self.assertEqual(pr.parse_history(""), [])
        self.assertEqual(pr.parse_history("   "), [])

    def test_malformed_values_fail_closed(self):
        for bad in ("not json", "{}", '"a string"', "[1,2]", '["", "b"]', "[null]",
                    '["a", "a"]', "42"):
            with self.assertRaises(pr.HistoryCorrupt, msg=bad):
                pr.parse_history(bad)

    def test_malformed_history_is_never_silently_reset(self):
        """The dangerous failure mode: treating corruption as [] restores the whole budget
        and could re-dispatch a row that already burned every allowed A100 start."""
        with self.assertRaises(pr.HistoryCorrupt):
            pr.plan_attempt("{oops", "exec-9")

    def test_missing_execution_id_is_corruption(self):
        with self.assertRaises(pr.HistoryCorrupt):
            pr.plan_attempt(None, None)


# ── first terminal observation ────────────────────────────────────────────────
class FirstTerminalObservation(unittest.TestCase):
    def setUp(self):
        self.db = DB()
        self.cur = FakeCursor(self.db)
        self.db.add_job("J1", external_execution_id="e1")

    def test_stamps_once(self):
        self.assertTrue(pr.stamp_first_terminal(self.cur, "jobs", "J1", "e1"))
        self.assertEqual(self.db.jobs["J1"]["first_terminal_observed_at"], "T1")

    def test_duplicate_observation_does_not_overwrite(self):
        pr.stamp_first_terminal(self.cur, "jobs", "J1", "e1")
        self.db.jobs["J1"]["first_terminal_observed_at"] = "ORIGINAL"
        self.assertFalse(pr.stamp_first_terminal(self.cur, "jobs", "J1", "e1"))
        self.assertEqual(self.db.jobs["J1"]["first_terminal_observed_at"], "ORIGINAL",
                         "the FIRST observation must survive; age must stay monotonic")

    def test_stale_execution_cannot_stamp_the_new_attempt(self):
        """After a retry the row is on a different execution. An in-flight observation of the
        OLD one must not touch the new attempt's clock."""
        self.db.jobs["J1"]["external_execution_id"] = "e2"
        self.assertFalse(pr.stamp_first_terminal(self.cur, "jobs", "J1", "e1"))
        self.assertIsNone(self.db.jobs["J1"]["first_terminal_observed_at"])

    def test_terminal_row_is_not_stamped(self):
        self.db.jobs["J1"]["status"] = "failed"
        self.assertFalse(pr.stamp_first_terminal(self.cur, "jobs", "J1", "e1"))

    def test_stamping_never_mutates_credits_or_state(self):
        pr.stamp_first_terminal(self.cur, "jobs", "J1", "e1")
        self.assertEqual(self.db.credit_mutations, 0)
        self.assertEqual(self.db.jobs["J1"]["status"], "processing")
        self.assertEqual(self.db.outbox, [])

    def test_unknown_table_is_rejected(self):
        with self.assertRaises(ValueError):
            pr.stamp_first_terminal(self.cur, "users", "J1", "e1")


# ── inference retry ───────────────────────────────────────────────────────────
class InferenceRetry(unittest.TestCase):
    def setUp(self):
        self.db = DB()
        self.cur = FakeCursor(self.db)
        self.ledger = FakeLedger()
        self.db.add_user("11111111-1111-4111-8111-111111111111", 60)
        self.db.add_job("J1", external_execution_id="e1", status="processing",
                        first_terminal_observed_at="T1")

    def retry(self, exec_id="e1", **kw):
        return pr.retry_job(self.cur, "J1", exec_id, outbox_add=fake_outbox_add,
                            queue_name="inference-jobs", **kw)

    def test_confirmed_pre_container_retry_with_budget(self):
        r = self.retry()
        j = self.db.jobs["J1"]
        self.assertEqual(r["plan"], pr.PLAN_RETRY)
        self.assertEqual(j["status"], "queued")
        self.assertIsNone(j["external_execution_id"])
        self.assertIsNone(j["first_terminal_observed_at"])
        self.assertEqual(j["provisioning_attempts"], 1)
        self.assertEqual(json.loads(j["provisioning_execution_ids"]), ["e1"])
        self.assertEqual(len(self.db.outbox), 1)
        self.assertEqual(self.db.outbox[0]["queue"], "inference-jobs")
        self.assertEqual(self.db.outbox[0]["payload"]["job_id"], "J1")

    def test_execution_id_and_timestamp_reset_atomically(self):
        """Both are cleared by the SAME statement as the status change — a retry that kept
        either would either re-observe a dead execution or start already timed out."""
        self.retry()
        j = self.db.jobs["J1"]
        self.assertEqual((j["external_execution_id"], j["first_terminal_observed_at"]),
                         (None, None))
        self.assertEqual(j["status"], "queued")

    def test_no_refund_between_attempts(self):
        self.retry()
        self.assertEqual(self.db.bal("11111111-1111-4111-8111-111111111111"), 60, "the reservation stands across a retry")
        self.assertEqual(self.db.credit_mutations, 0)
        self.assertEqual(self.ledger.rows, [])

    def test_user_facing_state_stays_in_progress(self):
        self.retry()
        self.assertNotIn(self.db.jobs["J1"]["status"], ("failed", "completed"))

    def test_same_execution_reconciled_twice_does_nothing_the_second_time(self):
        self.retry()
        self.db.jobs["J1"].update(status="processing", external_execution_id="e1")
        r2 = self.retry()
        self.assertEqual(r2["plan"], pr.PLAN_ALREADY_HANDLED)
        self.assertEqual(self.db.jobs["J1"]["provisioning_attempts"], 1,
                         "duplicate reconcile must not consume budget")
        self.assertEqual(len(self.db.outbox), 1, "and must not enqueue a second retry")

    def test_duplicate_outbox_delivery_cannot_retry_again(self):
        """At-least-once delivery: the same retry message arriving twice reaches a job that is
        already 'queued', which is not a retryable state."""
        self.retry()
        r2 = self.retry()          # row is 'queued' with no execution id now
        self.assertEqual(r2["plan"], pr.PLAN_ALREADY_HANDLED)
        self.assertEqual(len(self.db.outbox), 1)

    def test_two_concurrent_reconcilers_produce_one_retry(self):
        """Both read the pre-transition row; only the one whose UPDATE matches wins."""
        cur_a, cur_b = FakeCursor(self.db), FakeCursor(self.db)
        ra = pr.retry_job(cur_a, "J1", "e1", outbox_add=fake_outbox_add,
                          queue_name="inference-jobs")
        rb = pr.retry_job(cur_b, "J1", "e1", outbox_add=fake_outbox_add,
                          queue_name="inference-jobs")
        plans = sorted([ra["plan"], rb["plan"]])
        self.assertEqual(plans, sorted([pr.PLAN_RETRY, pr.PLAN_ALREADY_HANDLED]))
        self.assertEqual(self.db.jobs["J1"]["provisioning_attempts"], 1)
        self.assertEqual(len(self.db.outbox), 1)

    def test_exact_retry_limit_boundary(self):
        """MAX=3 total executions => exactly 2 retries, then exhaustion. Counted explicitly."""
        retries = 0
        for i in range(1, 10):
            self.db.jobs["J1"].update(status="processing",
                                      external_execution_id="e%d" % i)
            r = self.retry("e%d" % i)
            if r["plan"] == pr.PLAN_RETRY:
                retries += 1
                continue
            self.assertEqual(r["plan"], pr.PLAN_EXHAUSTED)
            self.assertEqual(i, 3, "the THIRD execution must be the one that exhausts")
            break
        self.assertEqual(retries, 2)
        self.assertEqual(len(self.db.outbox), 2, "no outbox row on the exhausting pass")

    def test_terminal_job_is_not_retryable(self):
        self.db.jobs["J1"]["status"] = "failed"
        self.assertEqual(self.retry()["plan"], pr.PLAN_ALREADY_HANDLED)
        self.assertEqual(len(self.db.outbox), 0)

    def test_stale_execution_is_not_retryable(self):
        self.db.jobs["J1"]["external_execution_id"] = "e2"
        r = self.retry("e1")
        self.assertEqual(r["plan"], pr.PLAN_ALREADY_HANDLED)
        self.assertEqual(self.db.jobs["J1"]["provisioning_attempts"], 0)

    def test_malformed_history_raises_rather_than_retrying(self):
        self.db.jobs["J1"]["provisioning_execution_ids"] = "{corrupt"
        with self.assertRaises(pr.HistoryCorrupt):
            self.retry()
        self.assertEqual(self.db.jobs["J1"]["status"], "processing",
                         "nothing may be written on a corrupt history")
        self.assertEqual(len(self.db.outbox), 0)

    def test_crash_before_commit_leaves_nothing(self):
        """The caller's transaction is the unit. Modelled by discarding the cursor's writes:
        a crash before commit means the row is untouched and the NEXT pass retries cleanly."""
        snapshot = dict(self.db.jobs["J1"])
        scratch = DB()
        scratch.jobs["J1"] = dict(snapshot)
        pr.retry_job(FakeCursor(scratch), "J1", "e1", outbox_add=fake_outbox_add,
                     queue_name="inference-jobs")
        # the real DB never saw it
        self.assertEqual(self.db.jobs["J1"], snapshot)
        self.assertEqual(len(self.db.outbox), 0)
        r = self.retry()
        self.assertEqual(r["plan"], pr.PLAN_RETRY)
        self.assertEqual(self.db.jobs["J1"]["provisioning_attempts"], 1)

    def test_committed_outbox_with_worker_crash_afterwards_is_delivered_once(self):
        """State + message committed together. A crash before the fast-path send leaves the
        message pending in the outbox; the dispatcher delivers it, and the redelivery cannot
        produce a second retry."""
        r = self.retry()
        self.assertEqual(len(self.db.outbox), 1)
        self.assertFalse(self.db.outbox[0]["delivered"])
        self.db.outbox[0]["delivered"] = True      # dispatcher backstop
        again = self.retry()
        self.assertEqual(again["plan"], pr.PLAN_ALREADY_HANDLED)
        self.assertEqual(len(self.db.outbox), 1)
        self.assertEqual(r["message"]["job_id"], "J1")

    def test_no_direct_queue_send_in_the_module(self):
        with open(os.path.join(BACKEND_DIR, "shared", "provisioning_retry.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        for forbidden in ("enqueue_job", "enqueue_training_job", "queue_client", "_send("):
            self.assertNotIn(forbidden, src,
                             "retry scheduling must go through the outbox only")


# ── exhaustion + refund routing ───────────────────────────────────────────────
class Exhaustion(unittest.TestCase):
    def setUp(self):
        self.db = DB()
        self.cur = FakeCursor(self.db)
        self.ledger = FakeLedger()
        self.db.add_user("11111111-1111-4111-8111-111111111111", 60)

    def _job(self, **kw):
        defaults = {"external_execution_id": "e3", "status": "processing",
                    "provisioning_attempts": 2,
                    "provisioning_execution_ids": pr.dump_history(["e1", "e2"])}
        defaults.update(kw)
        self.db.add_job("J1", **defaults)
        if defaults.get("source_type") or defaults.get("organization_id"):
            self.db.add_reserve(self.ledger)

    def exhaust(self):
        return pr.exhaust_job(self.cur, "J1", "e3", credit_ledger=self.ledger)

    def test_exhaustion_refunds_exactly_once(self):
        self._job()
        transitioned, refund, attempts, state = self.exhaust()
        self.assertTrue(transitioned)
        self.assertEqual(refund, 40)
        self.assertEqual(attempts, 3)
        self.assertEqual(state, pr.REFUND_DONE)
        self.assertEqual(self.db.bal("11111111-1111-4111-8111-111111111111"), 100)
        self.assertEqual(self.db.credit_mutations, 1)
        self.assertEqual(len(self.ledger.rows), 1)
        self.assertEqual(self.db.jobs["J1"]["status"], "failed")
        self.assertEqual(len(self.db.outbox), 0, "no retry outbox row on exhaustion")

    def test_second_exhaustion_pass_refunds_nothing(self):
        self._job()
        self.exhaust()
        self.db.jobs["J1"]["external_execution_id"] = "e3"
        transitioned, refund, _, state = self.exhaust()
        self.assertFalse(transitioned)
        self.assertEqual(refund, 0)
        self.assertEqual(state, pr.REFUND_NONE)
        self.assertEqual(self.db.bal("11111111-1111-4111-8111-111111111111"), 100)
        self.assertEqual(self.db.credit_mutations, 1)
        self.assertEqual(len(self.ledger.rows), 1, "no duplicate ledger row")

    def test_terminalization_and_refund_share_one_cursor(self):
        """Requirement: not 'fail here, refund there'. Both happen on the cursor handed in,
        so the caller's single commit covers them."""
        self._job()
        self.exhaust()
        self.assertEqual(self.db.jobs["J1"]["status"], "failed")
        self.assertEqual(self.db.bal("11111111-1111-4111-8111-111111111111"), 100)

    def test_final_execution_id_is_recorded(self):
        self._job()
        self.exhaust()
        self.assertEqual(json.loads(self.db.jobs["J1"]["provisioning_execution_ids"]),
                         ["e1", "e2", "e3"])
        self.assertEqual(self.db.jobs["J1"]["provisioning_attempts"], 3)

    def test_organization_credits_route_to_the_org_that_paid(self):
        self._job(organization_id="org-9", source_type="monthly")
        self.db.add_member("11111111-1111-4111-8111-111111111111", "org-9")
        self.exhaust()
        self.assertEqual(self.db.orgs[("11111111-1111-4111-8111-111111111111", "org-9")], 40)
        self.assertEqual(self.db.bal("11111111-1111-4111-8111-111111111111"), 60, "personal balance untouched for an org job")

    def test_personal_one_time_credits_route_to_the_user(self):
        self._job(source_type="one_time")
        self.exhaust()
        self.assertEqual(self.db.bal("11111111-1111-4111-8111-111111111111"), 100)
        self.assertEqual(self.db.orgs, {})

    def test_monthly_split_refund(self):
        self.db.users["11111111-1111-4111-8111-111111111111"]["subscription_type"] = "monthly"
        self._job(source_type="monthly",
                  job_params=json.dumps({"credit_cost": 40, "monthly_credit_cost": 30,
                                         "one_time_credit_cost": 10}))
        _, refund, _, _ = self.exhaust()
        self.assertEqual(refund, 40)
        self.assertEqual(self.db.bal("11111111-1111-4111-8111-111111111111"), 100)

    def test_already_terminal_job_is_left_alone(self):
        self._job(status="failed")
        transitioned, refund, _, state = self.exhaust()
        self.assertFalse(transitioned)
        self.assertEqual(refund, 0)
        self.assertEqual(state, pr.REFUND_NONE)
        self.assertEqual(self.db.credit_mutations, 0)


# ── fused train_infer linkage ─────────────────────────────────────────────────
class FusedAllocation(unittest.TestCase):
    def setUp(self):
        self.db = DB()
        self.cur = FakeCursor(self.db)
        self.db.add_training("T1", user_id="11111111-1111-4111-8111-111111111111")

    def test_deterministic_created_at_job_id_tie_break(self):
        """Two parked jobs sharing created_at: the job_id tie-break decides, every time."""
        self.db.add_job("B", user_id="11111111-1111-4111-8111-111111111111", status="waiting_lora", created_at=5)
        self.db.add_job("A", user_id="11111111-1111-4111-8111-111111111111", status="waiting_lora", created_at=5)
        jid, _ = pr.allocate_fused_job(self.cur, "T1", "11111111-1111-4111-8111-111111111111")
        self.assertEqual(jid, "A")

    def test_older_created_at_still_wins_over_job_id(self):
        self.db.add_job("A", user_id="11111111-1111-4111-8111-111111111111", status="waiting_lora", created_at=9)
        self.db.add_job("Z", user_id="11111111-1111-4111-8111-111111111111", status="waiting_lora", created_at=1)
        jid, _ = pr.allocate_fused_job(self.cur, "T1", "11111111-1111-4111-8111-111111111111")
        self.assertEqual(jid, "Z")

    def test_first_allocation_persists_the_link_and_claims_the_job(self):
        self.db.add_job("J1", user_id="11111111-1111-4111-8111-111111111111", status="waiting_lora")
        jid, why = pr.allocate_fused_job(self.cur, "T1", "11111111-1111-4111-8111-111111111111")
        self.assertEqual(jid, "J1")
        self.assertEqual(self.db.trainings["T1"]["fused_job_id"], "J1")
        self.assertEqual(self.db.jobs["J1"]["status"], "processing")
        self.assertEqual(why, "allocated")

    def test_existing_link_is_reused_never_reselected(self):
        self.db.add_job("J1", user_id="11111111-1111-4111-8111-111111111111", status="waiting_lora", created_at=1)
        self.db.add_job("J2", user_id="11111111-1111-4111-8111-111111111111", status="waiting_lora", created_at=0)
        self.db.trainings["T1"]["fused_job_id"] = "J1"
        jid, why = pr.allocate_fused_job(self.cur, "T1", "11111111-1111-4111-8111-111111111111")
        self.assertEqual(jid, "J1", "J2 is older but the persisted link wins")
        self.assertEqual(self.db.jobs["J1"]["status"], "processing")
        self.assertEqual(self.db.jobs["J2"]["status"], "waiting_lora")
        self.assertIn("reused", why)

    def test_no_parked_job_is_plain_training(self):
        jid, why = pr.allocate_fused_job(self.cur, "T1", "11111111-1111-4111-8111-111111111111")
        self.assertIsNone(jid)
        self.assertEqual(why, "no parked job")

    def test_another_users_job_is_never_selected(self):
        self.db.add_job("J1", user_id="33333333-3333-4333-8333-333333333333", status="waiting_lora")
        jid, _ = pr.allocate_fused_job(self.cur, "T1", "11111111-1111-4111-8111-111111111111")
        self.assertIsNone(jid)
        self.assertEqual(self.db.jobs["J1"]["status"], "waiting_lora")

    def test_two_concurrent_allocations_cannot_bind_one_training_twice(self):
        self.db.add_job("J1", user_id="11111111-1111-4111-8111-111111111111", status="waiting_lora", created_at=1)
        self.db.add_job("J2", user_id="11111111-1111-4111-8111-111111111111", status="waiting_lora", created_at=2)
        cur_a, cur_b = FakeCursor(self.db), FakeCursor(self.db)
        first, _ = pr.allocate_fused_job(cur_a, "T1", "11111111-1111-4111-8111-111111111111")
        self.assertEqual(first, "J1")
        # B now sees the link and reuses it rather than claiming J2.
        second, why = pr.allocate_fused_job(cur_b, "T1", "11111111-1111-4111-8111-111111111111")
        self.assertEqual(second, "J1")
        self.assertEqual(self.db.jobs["J2"]["status"], "waiting_lora")
        self.assertIn("reused", why)

    def test_unique_index_violation_shape_raises_for_rollback(self):
        """If the guarded link UPDATE matches 0 rows (another allocator won between our SELECT
        and our UPDATE), we must RAISE so the caller rolls the job claim back too — never
        commit a 'processing' job with no link."""
        self.db.add_job("J1", user_id="11111111-1111-4111-8111-111111111111", status="waiting_lora")

        class Racing(FakeCursor):
            def execute(self, sql, *params):
                if " ".join(sql.split()).lower().startswith(
                        "update lora_trainings set fused_job_id"):
                    self.rowcount = 0     # someone else bound it first
                    self._fetch = None
                    return
                return super().execute(sql, *params)

        with self.assertRaises(pr.FusedLinkConflict):
            pr.allocate_fused_job(Racing(self.db), "T1", "11111111-1111-4111-8111-111111111111")

    def test_historical_null_link_is_not_guessed_from_user_id_on_retry(self):
        """Allocation may select; RETRY may not. A NULL link on the retry path is ineligible,
        never an invitation to go find a job by user_id/status."""
        self.db.add_job("J9", user_id="11111111-1111-4111-8111-111111111111", status="waiting_lora")
        self.db.trainings["T1"].update(external_execution_id="e1", fused_job_id=None)
        r = pr.retry_fused_training(self.cur, "T1", "e1", outbox_add=fake_outbox_add,
                                    queue_name="lora-training-jobs")
        self.assertEqual(r["fused_job_id"], None)
        self.assertEqual(self.db.jobs["J9"]["status"], "waiting_lora",
                         "an unrelated parked job must never be touched")


class FusedRetry(unittest.TestCase):
    def setUp(self):
        self.db = DB()
        self.cur = FakeCursor(self.db)
        self.db.add_training("T1", user_id="11111111-1111-4111-8111-111111111111", external_execution_id="e1",
                             fused_job_id="J1", first_terminal_observed_at="T1")
        self.db.add_job("J1", user_id="11111111-1111-4111-8111-111111111111", status="processing")

    def retry(self, exec_id="e1"):
        return pr.retry_fused_training(self.cur, "T1", exec_id, outbox_add=fake_outbox_add,
                                       queue_name="lora-training-jobs")

    def test_retry_retains_the_link_and_parks_the_same_job(self):
        r = self.retry()
        t = self.db.trainings["T1"]
        self.assertEqual(r["plan"], pr.PLAN_RETRY)
        self.assertEqual(t["fused_job_id"], "J1", "the link must survive the retry")
        self.assertEqual(t["status"], "queued")
        self.assertIsNone(t["external_execution_id"])
        self.assertIsNone(t["first_terminal_observed_at"])
        self.assertEqual(t["provisioning_attempts"], 1)
        self.assertEqual(self.db.jobs["J1"]["status"], "waiting_lora")
        self.assertEqual(len(self.db.outbox), 1)
        self.assertEqual(self.db.outbox[0]["queue"], "lora-training-jobs")
        self.assertEqual(self.db.outbox[0]["payload"],
                         {"training_id": "T1", "user_id": "11111111-1111-4111-8111-111111111111"})

    def test_next_dispatch_reclaims_only_the_same_job(self):
        self.retry()
        self.db.add_job("J2", user_id="11111111-1111-4111-8111-111111111111", status="waiting_lora", created_at=-99)
        jid, why = pr.allocate_fused_job(self.cur, "T1", "11111111-1111-4111-8111-111111111111")
        self.assertEqual(jid, "J1", "an older parked job must NOT be substituted")
        self.assertEqual(self.db.jobs["J2"]["status"], "waiting_lora")
        self.assertIn("reused", why)

    def test_retry_cannot_select_a_second_waiting_job(self):
        self.db.add_job("J2", user_id="11111111-1111-4111-8111-111111111111", status="waiting_lora")
        self.retry()
        self.assertEqual(self.db.jobs["J2"]["status"], "waiting_lora")
        self.assertEqual(self.db.trainings["T1"]["fused_job_id"], "J1")

    def test_missing_linked_job_fails_closed(self):
        self.db.trainings["T1"]["fused_job_id"] = "GONE"
        r = self.retry()
        self.assertEqual(r["plan"], pr.PLAN_EXHAUSTED)
        self.assertEqual(r["link_error"], pr.LINK_NOT_FOUND)
        self.assertEqual(len(self.db.outbox), 0)
        self.assertEqual(self.db.trainings["T1"]["status"], "training", "no transition")

    def test_wrong_user_linked_job_fails_closed(self):
        self.db.jobs["J1"]["user_id"] = "22222222-2222-4222-8222-222222222222"
        r = self.retry()
        self.assertEqual(r["plan"], pr.PLAN_EXHAUSTED)
        self.assertEqual(r["link_error"], pr.LINK_WRONG_USER)
        self.assertEqual(self.db.jobs["J1"]["status"], "processing")

    def test_terminal_linked_job_fails_closed(self):
        self.db.jobs["J1"]["status"] = "completed"
        r = self.retry()
        self.assertEqual(r["plan"], pr.PLAN_EXHAUSTED)
        self.assertEqual(r["link_error"], pr.LINK_TERMINAL)

    def test_unexpected_state_linked_job_fails_closed(self):
        self.db.jobs["J1"]["status"] = "queued"
        r = self.retry()
        self.assertEqual(r["plan"], pr.PLAN_EXHAUSTED)
        self.assertEqual(r["link_error"], pr.LINK_UNEXPECTED_STATE)

    def test_duplicate_reconcile_does_not_retry_twice(self):
        self.retry()
        self.db.trainings["T1"].update(status="training", external_execution_id="e1")
        self.db.jobs["J1"]["status"] = "processing"
        r2 = self.retry()
        self.assertEqual(r2["plan"], pr.PLAN_ALREADY_HANDLED)
        self.assertEqual(self.db.trainings["T1"]["provisioning_attempts"], 1)
        self.assertEqual(len(self.db.outbox), 1)

    def test_two_concurrent_fused_reconcilers_produce_one_retry(self):
        cur_a, cur_b = FakeCursor(self.db), FakeCursor(self.db)
        ra = pr.retry_fused_training(cur_a, "T1", "e1", outbox_add=fake_outbox_add,
                                     queue_name="lora-training-jobs")
        rb = pr.retry_fused_training(cur_b, "T1", "e1", outbox_add=fake_outbox_add,
                                     queue_name="lora-training-jobs")
        # The loser's own SELECT is scoped to external_execution_id, which the winner has
        # already cleared — so it never even reaches the attempt planner. That is the
        # STRONGER outcome: the loser writes nothing at all.
        self.assertEqual(sorted([ra["plan"], rb["plan"]]),
                         sorted([pr.PLAN_RETRY, pr.PLAN_ALREADY_HANDLED]))
        self.assertEqual(self.db.trainings["T1"]["provisioning_attempts"], 1)
        self.assertEqual(len(self.db.outbox), 1)

    def test_terminal_training_is_not_retryable(self):
        self.db.trainings["T1"]["status"] = "failed"
        self.assertEqual(self.retry()["plan"], pr.PLAN_ALREADY_HANDLED)

    def test_plain_training_without_a_link_still_retries(self):
        self.db.trainings["T1"]["fused_job_id"] = None
        r = self.retry()
        self.assertEqual(r["plan"], pr.PLAN_RETRY)
        self.assertEqual(self.db.trainings["T1"]["status"], "queued")
        self.assertEqual(len(self.db.outbox), 1)

    def test_exhaustion_records_the_attempt_once(self):
        self.db.trainings["T1"].update(
            provisioning_attempts=2, provisioning_execution_ids=pr.dump_history(["a", "b"]),
            external_execution_id="e3")
        recorded, attempts = pr.record_training_attempt(self.cur, "T1", "e3")
        self.assertTrue(recorded)
        self.assertEqual(attempts, 3)
        again, attempts2 = pr.record_training_attempt(self.cur, "T1", "e3")
        self.assertFalse(again)
        self.assertEqual(attempts2, 3)
        self.assertEqual(json.loads(
            self.db.trainings["T1"]["provisioning_execution_ids"]), ["a", "b", "e3"])


class FusedExhaustion(unittest.TestCase):
    """Only the LINKED job is terminalized; unrelated jobs of the same user are untouched."""

    def setUp(self):
        self.db = DB()
        self.cur = FakeCursor(self.db)
        self.ledger = FakeLedger()
        self.db.add_user("11111111-1111-4111-8111-111111111111", 60)
        self.db.add_training("T1", user_id="11111111-1111-4111-8111-111111111111", external_execution_id="e3",
                             fused_job_id="J1", provisioning_attempts=2,
                             provisioning_execution_ids=pr.dump_history(["e1", "e2"]))
        self.db.add_job("J1", user_id="11111111-1111-4111-8111-111111111111", status="processing")
        self.db.add_job("J2", user_id="11111111-1111-4111-8111-111111111111", status="waiting_lora")
        self.db.add_job("J3", user_id="11111111-1111-4111-8111-111111111111", status="processing")

    def test_only_the_linked_job_is_refunded(self):
        ok, _ = pr.verify_fused_link(self.cur, "J1", "11111111-1111-4111-8111-111111111111", pr.FUSED_RECLAIMABLE_STATES)
        self.assertTrue(ok)
        pr.terminalize_and_refund(self.cur, "J1", credit_ledger=self.ledger)
        self.assertEqual(self.db.jobs["J1"]["status"], "failed")
        self.assertEqual(self.db.jobs["J2"]["status"], "waiting_lora")
        self.assertEqual(self.db.jobs["J3"]["status"], "processing")
        self.assertEqual(self.db.credit_mutations, 1)
        self.assertEqual(len(self.ledger.rows), 1)

    def test_link_is_preserved_as_audit_after_exhaustion(self):
        pr.record_training_attempt(self.cur, "T1", "e3")
        pr.terminalize_and_refund(self.cur, "J1", credit_ledger=self.ledger)
        self.assertEqual(self.db.trainings["T1"]["fused_job_id"], "J1",
                         "the link is permanent audit linkage, never cleared")

    def test_double_exhaustion_refunds_once(self):
        pr.terminalize_and_refund(self.cur, "J1", credit_ledger=self.ledger)
        pr.terminalize_and_refund(self.cur, "J1", credit_ledger=self.ledger)
        self.assertEqual(self.db.bal("11111111-1111-4111-8111-111111111111"), 100)
        self.assertEqual(self.db.credit_mutations, 1)
        self.assertEqual(len(self.ledger.rows), 1)

    def test_successful_completion_retains_the_link(self):
        self.db.jobs["J1"]["status"] = "completed"
        ok, reason = pr.verify_fused_link(self.cur, "J1", "11111111-1111-4111-8111-111111111111",
                                           pr.FUSED_RECLAIMABLE_STATES)
        self.assertFalse(ok)
        self.assertEqual(reason, pr.LINK_TERMINAL)
        self.assertEqual(self.db.trainings["T1"]["fused_job_id"], "J1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
