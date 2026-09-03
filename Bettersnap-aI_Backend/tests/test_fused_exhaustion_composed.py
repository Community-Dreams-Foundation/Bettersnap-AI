"""BEHAVIOURAL composition tests for fused provisioning exhaustion.

The previous phase asserted this path with source-text inspection, which proves only that a
call exists. These drive the ACTUAL callables:

    _watch_one  ->  classifier  ->  _retry_provisioning_training / _exhaust_provisioning_training
                ->  _finish_training(sweep_parked=False)

against a stateful fake database with a real transaction model (see TxDB): writes land in a
per-connection buffer and become visible to other connections only on commit, rollback discards
them, and every UPDATE reports a rowcount computed from the committed+buffered state.

No Azure, no database, no queue, no GPU.

Run: python -m unittest tests.test_fused_exhaustion_composed   (from the backend dir)
"""
import copy
import json
import os
import sys
import types
import unittest
from unittest import mock

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from shared import provisioning_retry as pr        # noqa: E402


# ── a fake with REAL commit/rollback semantics ───────────────────────────────
class TxDB:
    """Committed state, plus per-connection uncommitted buffers.

    This exists because the earlier fakes applied every write immediately, so they could not
    tell a committed mutation from a rolled-back one and could not detect a ledger row written
    inside a transaction that later aborted.
    """

    def __init__(self):
        self.jobs = {}
        self.trainings = {}
        self.users = {}
        self.org_members = {}         # (user_id, org_id) -> credits
        self.ledger = []
        self.outbox = []
        self.commits = 0
        self.rollbacks = 0

    def snapshot(self):
        return {"jobs": copy.deepcopy(self.jobs),
                "trainings": copy.deepcopy(self.trainings),
                "users": dict(self.users),
                "org_members": dict(self.org_members),
                "ledger": list(self.ledger)}

    def restore(self, snap):
        self.jobs = snap["jobs"]
        self.trainings = snap["trainings"]
        self.users = snap["users"]
        self.org_members = snap["org_members"]
        self.ledger = snap["ledger"]

    def add_user(self, user_id, credits=0, monthly=0, one_time=0,
                 subscription_type="one_time"):
        """Three balances, tracked independently — a single scalar would hide the very
        bucket defect this phase exists to fix."""
        self.users[user_id] = {"credits_remaining": credits,
                               "monthly_credits_remaining": monthly,
                               "one_time_credits_remaining": one_time,
                               "subscription_type": subscription_type}
        return self.users[user_id]

    def bal(self, user_id, field="credits_remaining"):
        return self.users[user_id][field]

    def add_job(self, job_id, **kw):
        row = {"job_id": job_id, "user_id": "11111111-1111-4111-8111-111111111111", "status": "processing",
               "external_execution_id": None,
               "job_params": json.dumps({"credit_cost": 40}),
               "organization_id": None, "source_type": "one_time", "created_at": 0,
               "provisioning_attempts": 0, "provisioning_execution_ids": None,
               "first_terminal_observed_at": None, "dispatched_at": 1,
               "completed_at": None, "output_blob_path": None}
        row.update(kw)
        self.jobs[job_id] = row
        return row

    def add_reserve(self, job_id="J1", user_id=None):
        """The single strictly-negative job_reserve row a properly-charged job has. Bucketed
        and organization-funded jobs now REQUIRE one."""
        row = self.jobs[job_id]
        total = json.loads(row["job_params"])["credit_cost"]
        self.ledger.append((user_id or row["user_id"], -total, "job_reserve", job_id))

    def add_training(self, training_id, **kw):
        row = {"training_id": training_id, "user_id": "11111111-1111-4111-8111-111111111111", "status": "training",
               "external_execution_id": None, "fused_job_id": None,
               "provisioning_attempts": 0, "provisioning_execution_ids": None,
               "first_terminal_observed_at": None, "error": None,
               "monthly_credit_cost": 0, "one_time_credit_cost": 0, "created_at": 0}
        row.update(kw)
        self.trainings[training_id] = row
        return row


class TxCursor:
    """Interprets the statements this path issues, honouring every WHERE clause and rowcount.
    Mutations are staged on the owning connection until commit."""

    def __init__(self, conn):
        self.conn = conn
        self.db = conn.db
        self.rowcount = 0
        self._fetch = None
        self._fetchall = []

    # -- staged view helpers ------------------------------------------------
    def _job(self, jid):
        return self.conn.view_jobs().get(str(jid))

    def _train(self, tid):
        return self.conn.view_trainings().get(str(tid))

    def _credit_user(self, uid, aggregate, monthly=0, one_time=0):
        users = self.conn.view_users()
        row = users.get(uid)
        if row is None:
            self.rowcount = 0
            return
        row = dict(row)
        row["credits_remaining"] += aggregate
        if monthly and row.get("subscription_type") == "monthly":
            row["monthly_credits_remaining"] += monthly
        row["one_time_credits_remaining"] += one_time
        self.conn.stage_user(uid, row)
        self.rowcount = 1

    def execute(self, sql, *params):
        s = " ".join(sql.split()).lower()
        self.rowcount = 0
        self._fetch = None
        self._fetchall = []
        J = self.conn.view_jobs()

        if s.startswith("select fused_job_id from lora_trainings"):
            r = self._train(params[0])
            self._fetch = (r["fused_job_id"],) if r else None
        elif s.startswith("select provisioning_attempts, provisioning_execution_ids from lora_trainings"):
            tid, ex = str(params[0]), str(params[1])
            r = self._train(tid)
            if r and str(r["external_execution_id"]) == ex:
                self._fetch = (r["provisioning_attempts"], r["provisioning_execution_ids"])
        elif s.startswith("update lora_trainings set provisioning_attempts"):
            attempts, hist, tid, ex = params
            r = self._train(tid)
            if r and str(r["external_execution_id"]) == str(ex):
                self.conn.stage_training(tid, provisioning_attempts=attempts,
                                         provisioning_execution_ids=hist)
                self.rowcount = 1
        elif s.startswith("select user_id, status from jobs"):
            r = self._job(params[0])
            self._fetch = (r["user_id"], r["status"]) if r else None
        elif s.startswith("update jobs set status = 'failed'"):
            r = self._job(params[0])
            if r and r["status"] not in ("failed", "completed"):
                self.conn.stage_job(str(params[0]), status="failed", completed_at=1)
                self.rowcount = 1
        elif s.startswith("select job_params, user_id, source_type, organization_id from jobs"):
            r = self._job(params[0])
            self._fetch = ((r["job_params"], r["user_id"], r["source_type"],
                            r["organization_id"]) if r else None)
        elif s.startswith("select source_type from jobs"):
            r = self._job(params[0])
            self._fetch = (r["source_type"],) if r else None
        elif s.startswith("select subscription_type from users"):
            r = self.conn.view_users().get(params[0])
            self._fetch = (r["subscription_type"],) if r else None
        elif s.startswith("update organization_members set credits_remaining"):
            amount, uid, org = params
            members = self.conn.view_members()
            if (uid, org) not in members:
                self.rowcount = 0
            else:
                self.conn.stage_member(uid, org, members[(uid, org)] + amount)
                self.rowcount = 1
        elif s.startswith("update users set monthly_credits_remaining"):
            monthly, one_time, aggregate, uid = params
            self._credit_user(uid, aggregate, monthly=monthly, one_time=one_time)
        elif s.startswith("update users set credits_remaining = credits_remaining + ? where user_id = ?"):
            self._credit_user(params[1], params[0])
        elif s.startswith("select transaction_id, user_id, amount from credit_transactions"):
            jid, kind = params
            self._fetchall = [("tx-%d" % i, row[0], row[1])
                              for i, row in enumerate(self.conn.view_ledger())
                              if row[3] == jid and row[2] == kind]
        elif s.startswith("select amount from credit_transactions"):
            jid, kind = params
            self._fetchall = [(row[1],) for row in self.conn.view_ledger()
                              if row[3] == jid and row[2] == kind]
        elif s.startswith("insert into outbox"):
            self.conn.stage_outbox(tuple(params))
            self._fetch = (len(self.db.outbox) + 1,)
        elif s.startswith("insert into credit_transactions"):
            self.conn.stage_ledger(tuple(params))
            self.rowcount = 1
        elif s.startswith("select job_params from jobs"):
            r = self._job(params[0])
            self._fetch = (r["job_params"],) if r else None
        elif s.startswith("update jobs set job_params = ?"):
            r = self._job(params[1])
            if r:
                self.conn.stage_job(str(params[1]), job_params=params[0])
                self.rowcount = 1
        elif s.startswith("select monthly_credit_cost, one_time_credit_cost, error"):
            r = self._train(params[0])
            # now also returns `error`, so the orphan path can preserve the root cause
            self._fetch = ((r["monthly_credit_cost"], r["one_time_credit_cost"], r["error"])
                           if r else None)
        elif s.startswith("select 1 from users with (updlock, holdlock)"):
            # The existence probe. Asked ONCE, before any write that targets the users row.
            self._fetch = (1,) if params[0] in self.conn.view_users() else None
        elif s.startswith("update lora_trainings set status = ?, error = ?, completed_at"):
            new_status, marker, tid = params
            r = self._train(tid)
            if r and r["status"] not in ("completed", "failed"):
                # error = ? (NOT COALESCE): the marker must land even when a root cause
                # is already recorded.
                self.conn.stage_training(str(tid), status=new_status, error=marker)
                self.rowcount = 1
        elif s.startswith("update lora_trainings set status = ?, error"):
            new_status, err, tid = params
            r = self._train(tid)
            if r and r["status"] not in ("completed", "failed"):
                self.conn.stage_training(str(tid), status=new_status,
                                         error=r["error"] or err)
                self.rowcount = 1
        elif s.startswith("update users set lora_status = ?"):
            # rowcount is load-bearing: lora_trainings.user_id has no FK, so a training can
            # outlive its owner and this UPDATE is one of the places that detects it.
            if params[1] not in self.conn.view_users():
                self.rowcount = 0
            else:
                self.conn.stage_lora_status(params[1], params[0])
                self.rowcount = 1
        elif s.startswith("select job_id, job_params from jobs"):
            uid = params[0]
            self._fetchall = [(k, v["job_params"]) for k, v in sorted(J.items())
                              if v["user_id"] == uid and v["status"] == "waiting_lora"]
        elif s.startswith("update jobs set status = 'queued'"):
            uid = params[0]
            for k, v in J.items():
                if v["user_id"] == uid and v["status"] == "waiting_lora":
                    self.conn.stage_job(k, status="queued")
        elif s.startswith("select top 1 1 from lora_trainings"):
            uid = params[0]
            self._fetch = (1,) if any(
                t["user_id"] == uid and t["status"] == "completed"
                for t in self.conn.view_trainings().values()) else None
        else:
            raise AssertionError("unmodelled SQL: %s" % s[:130])

    def fetchone(self):
        return self._fetch

    def fetchall(self):
        return self._fetchall


class TxConn:
    """One connection = one transaction. Staged writes are invisible until commit()."""

    def __init__(self, db):
        self.db = db
        self._jobs = {}
        self._trainings = {}
        self._users = {}
        self._members = {}
        self._lora = {}
        self._ledger = []
        self._outbox = []
        self.closed = False

    # staged views -------------------------------------------------------
    def view_jobs(self):
        merged = copy.deepcopy(self.db.jobs)
        for k, patch in self._jobs.items():
            merged.setdefault(k, {}).update(patch)
        return merged

    def view_trainings(self):
        merged = copy.deepcopy(self.db.trainings)
        for k, patch in self._trainings.items():
            merged.setdefault(k, {}).update(patch)
        return merged

    def view_users(self):
        merged = dict(self.db.users)
        merged.update(self._users)
        return merged

    def view_ledger(self):
        return list(self.db.ledger) + list(self._ledger)

    def view_members(self):
        merged = dict(self.db.org_members)
        merged.update(self._members)
        return merged

    # staging ------------------------------------------------------------
    def stage_job(self, jid, **kw):
        self._jobs.setdefault(str(jid), {}).update(kw)

    def stage_training(self, tid, **kw):
        self._trainings.setdefault(str(tid), {}).update(kw)

    def stage_user(self, uid, row):
        self._users[uid] = row

    def stage_member(self, uid, org, credits):
        self._members[(uid, org)] = credits

    def stage_lora_status(self, uid, status):
        self._lora[uid] = status

    def stage_ledger(self, row):
        self._ledger.append(row)

    def stage_outbox(self, row):
        self._outbox.append(row)

    def cursor(self):
        return TxCursor(self)

    def commit(self):
        for k, patch in self._jobs.items():
            self.db.jobs.setdefault(k, {}).update(patch)
        for k, patch in self._trainings.items():
            self.db.trainings.setdefault(k, {}).update(patch)
        self.db.users.update(self._users)
        self.db.org_members.update(self._members)
        for uid, st in self._lora.items():
            self.db.users.setdefault("__lora__", {})
            self.db.users["__lora__"] = dict(self.db.users.get("__lora__") or {})
            self.db.users["__lora__"][uid] = st
        self.db.ledger.extend(self._ledger)
        self.db.outbox.extend(self._outbox)
        self.db.commits += 1
        self._clear()

    def rollback(self):
        self.db.rollbacks += 1
        self._clear()

    def _clear(self):
        self._jobs, self._trainings = {}, {}
        self._users, self._members, self._lora = {}, {}, {}
        self._ledger, self._outbox = [], []

    def close(self):
        # An uncommitted transaction that is merely closed must NOT land. This is what makes
        # "no partial mutation after a failure" observable.
        self._clear()
        self.closed = True


class FakeLedgerModule:
    REASON_JOB_REFUND = "job_refund"
    REASON_JOB_RESERVE = "job_reserve"
    REASON_RETRAIN_REFUND = "retrain_refund"

    @staticmethod
    def record(cur, user_id, amount, transaction_type, job_id=None):
        if int(amount) == 0:
            return
        cur.execute("INSERT INTO credit_transactions (user_id, amount, transaction_type, "
                    "job_id) VALUES (?, ?, ?, ?)", user_id, amount, transaction_type, job_id)


# ── function_app under stubs ─────────────────────────────────────────────────
def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


if "function_app" not in sys.modules:
    _mod("azure")
    _mod("azure.functions",
         FunctionApp=type("FunctionApp", (), {
             "__init__": lambda self, *a, **k: None,
             "route": lambda self, *a, **k: (lambda fn: fn),
             "queue_trigger": lambda self, *a, **k: (lambda fn: fn),
             "timer_trigger": lambda self, *a, **k: (lambda fn: fn)}),
         AuthLevel=type("AuthLevel", (), {"ANONYMOUS": "anonymous"}),
         HttpResponse=type("HttpResponse", (), {}), HttpRequest=type("HttpRequest", (), {}),
         QueueMessage=type("QueueMessage", (), {}), TimerRequest=type("TimerRequest", (), {}))
    _mod("azure.storage")
    _mod("azure.storage.blob", generate_blob_sas=mock.Mock(return_value="sas"),
         BlobSasPermissions=mock.Mock())
    _mod("shared.auth", validate_token=mock.Mock(), get_user_id=mock.Mock(return_value="11111111-1111-4111-8111-111111111111"),
         require_admin=mock.Mock(), NotAdminError=type("E", (Exception,), {}))
    _mod("shared.db", get_db=mock.Mock(), new_connection=mock.Mock())
    _mod("shared.queue_client", enqueue_job=mock.Mock(), enqueue_training_job=mock.Mock(),
         _send=mock.Mock(), INFERENCE_QUEUE="inference-jobs",
         TRAINING_QUEUE="lora-training-jobs")
    _mod("shared.blob", upload_blob=mock.Mock(), download_blob=mock.Mock(),
         get_blob_client=mock.Mock())
    _mod("shared.keyvault", get_secret=mock.Mock(return_value="s"))
    _mod("shared.queue_trigger", trigger_container_job=mock.Mock(return_value="e"),
         count_active_job_executions=mock.Mock(return_value=0),
         get_job_execution_outcome=mock.Mock(), get_execution_evidence=mock.Mock(),
         find_execution_for_job=mock.Mock(return_value=None),
         execution_status=mock.Mock(return_value="failed"), stop_execution=mock.Mock())
    _mod("shared.crops", crop_head_and_shoulders=mock.Mock(),
         NoFaceError=type("E", (Exception,), {}))
    _mod("shared.training_trigger", trigger_training_job=mock.Mock(return_value="t"),
         get_execution_status=mock.Mock(return_value="failed"))
    _mod("shared.gpu_lease", acquire_dispatch_lease=mock.Mock(return_value="o"),
         release_dispatch_lease=mock.Mock(), mark_dispatched=mock.Mock(),
         clear_dispatch_pending=mock.Mock(),
         recent_dispatch_pending=mock.Mock(return_value=False),
         DispatchConfigError=type("E", (Exception,), {}))

import function_app  # noqa: E402


class _Base(unittest.TestCase):
    """One training whose GPU replica Azure never backed, one LINKED generation job, and two
    UNRELATED parked jobs that must survive untouched."""

    def setUp(self):
        self.db = TxDB()
        self.db.add_user("11111111-1111-4111-8111-111111111111", 0)
        self.db.add_training("T1", user_id="11111111-1111-4111-8111-111111111111", external_execution_id="e3",
                             fused_job_id="LINKED", provisioning_attempts=2,
                             provisioning_execution_ids=pr.dump_history(["e1", "e2"]))
        self.db.add_job("LINKED", user_id="11111111-1111-4111-8111-111111111111", status="processing")
        self.db.add_reserve("LINKED")
        self.db.add_job("PARK1", user_id="11111111-1111-4111-8111-111111111111", status="waiting_lora")
        self.db.add_reserve("PARK1")
        self.db.add_job("PARK2", user_id="11111111-1111-4111-8111-111111111111", status="waiting_lora")
        self.db.add_reserve("PARK2")
        self._patch = [
            mock.patch.object(function_app, "new_connection", lambda: TxConn(self.db)),
            mock.patch.object(function_app, "credit_ledger", FakeLedgerModule),
        ]
        for p in self._patch:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patch])

    def lora_status(self):
        return (self.db.users.get("__lora__") or {}).get("11111111-1111-4111-8111-111111111111")

    def refunds(self):
        return [r for r in self.db.ledger if r[2] == "job_refund"]

    def exhaust(self, adapter=True):
        with mock.patch.object(function_app, "_identity_adapter_state",
                               side_effect=adapter if callable(adapter)
                               else (lambda _u: adapter)):
            return function_app._exhaust_provisioning_training(
                "T1", "11111111-1111-4111-8111-111111111111", "e3", "GPU could not be provisioned")


class ComposedExhaustion(_Base):
    def test_only_the_linked_job_is_terminalized_and_refunded(self):
        self.db.add_user("11111111-1111-4111-8111-111111111111", 0)
        self.exhaust()
        self.assertEqual(self.db.jobs["LINKED"]["status"], "failed")
        self.assertEqual(self.db.jobs["PARK1"]["status"], "waiting_lora")
        self.assertEqual(self.db.jobs["PARK2"]["status"], "waiting_lora")
        self.assertEqual(len(self.refunds()), 1, "exactly one refund")
        self.assertEqual(self.refunds()[0][3], "LINKED")
        self.assertEqual(self.db.bal("11111111-1111-4111-8111-111111111111"), 40)
        self.assertEqual(self.db.bal("11111111-1111-4111-8111-111111111111", "one_time_credits_remaining"), 40,
                         "the spendable one-time bucket must be restored too")

    def test_training_reaches_the_intended_terminal_state(self):
        self.exhaust()
        self.assertEqual(self.db.trainings["T1"]["status"], "failed")
        self.assertIn("provision", (self.db.trainings["T1"]["error"] or "").lower())

    def test_fused_link_remains_persisted_for_audit(self):
        self.exhaust()
        self.assertEqual(self.db.trainings["T1"]["fused_job_id"], "LINKED")

    def test_the_final_execution_is_recorded_once(self):
        self.exhaust()
        self.assertEqual(
            json.loads(self.db.trainings["T1"]["provisioning_execution_ids"]),
            ["e1", "e2", "e3"])
        self.assertEqual(self.db.trainings["T1"]["provisioning_attempts"], 3)

    def test_repeated_watcher_ticks_do_not_refund_twice(self):
        self.exhaust()
        for _ in range(4):
            self.db.trainings["T1"]["status"] = "training"      # watcher sees it again
            self.exhaust()
        self.assertEqual(len(self.refunds()), 1)
        self.assertEqual(self.db.bal("11111111-1111-4111-8111-111111111111"), 40)
        self.assertEqual(self.db.bal("11111111-1111-4111-8111-111111111111", "one_time_credits_remaining"), 40,
                         "the spendable one-time bucket must be restored too")
        self.assertEqual(self.db.jobs["PARK1"]["status"], "waiting_lora")

    # -- lora_status must not be decided by a storage blip ------------------
    def test_adapter_present_keeps_the_user_ready(self):
        self.exhaust(adapter=True)
        self.assertEqual(self.lora_status(), "ready")

    def test_adapter_absent_marks_failed_so_they_can_retrain(self):
        self.exhaust(adapter=False)
        self.assertEqual(self.lora_status(), "failed")

    def test_storage_unknown_falls_back_to_a_completed_training(self):
        """None means 'storage unreachable'. A user with a completed training demonstrably
        has a model, and a blob outage must not take it away."""
        self.db.add_training("T0", user_id="11111111-1111-4111-8111-111111111111", status="completed")
        self.exhaust(adapter=None)
        self.assertEqual(self.lora_status(), "ready")

    def test_storage_unknown_with_no_history_marks_failed(self):
        self.exhaust(adapter=None)
        self.assertEqual(self.lora_status(), "failed")

    def test_adapter_probe_raising_does_not_abort_the_exhaustion(self):
        def boom(_u):
            raise RuntimeError("blob down")
        with self.assertRaises(RuntimeError):
            self.exhaust(adapter=boom)
        # The linked job was already terminalized in its own committed transaction, and the
        # parked jobs were never touched.
        self.assertEqual(self.db.jobs["LINKED"]["status"], "failed")
        self.assertEqual(self.db.jobs["PARK1"]["status"], "waiting_lora")

    def test_linked_job_terminalization_failure_prevents_finalization(self):
        """If the linked job cannot be terminalized, the training must NOT be finalized —
        otherwise it would complete while its generation sat stranded in 'processing'."""
        original = pr.terminalize_and_refund

        def boom(*a, **kw):
            raise RuntimeError("db exploded")
        with mock.patch.object(pr, "terminalize_and_refund", boom):
            with self.assertRaises(RuntimeError):
                self.exhaust()
        self.assertEqual(pr.terminalize_and_refund, original)
        self.assertEqual(self.db.trainings["T1"]["status"], "training",
                         "the training must stay non-terminal for the next tick")
        self.assertEqual(self.db.jobs["LINKED"]["status"], "processing")
        self.assertEqual(self.refunds(), [])

    def test_wrong_user_link_is_left_alone_and_nothing_else_is_refunded(self):
        self.db.jobs["LINKED"]["user_id"] = "22222222-2222-4222-8222-222222222222"
        self.exhaust()
        self.assertEqual(self.db.jobs["LINKED"]["status"], "processing")
        self.assertEqual(self.refunds(), [])
        self.assertEqual(self.db.jobs["PARK1"]["status"], "waiting_lora")

    def test_org_funded_linked_job_refunds_to_the_org(self):
        self.db.jobs["LINKED"].update(organization_id="org-9", source_type="monthly")
        self.db.org_members[("11111111-1111-4111-8111-111111111111", "org-9")] = 0
        self.exhaust()
        self.assertEqual(self.db.org_members[("11111111-1111-4111-8111-111111111111", "org-9")], 40)
        self.assertEqual(self.db.bal("11111111-1111-4111-8111-111111111111"), 0, "personal balance untouched")

    def test_missing_refund_target_still_terminalizes_and_records_the_debt(self):
        """The link is VALID and the USER exists; what is missing is the organization_members
        row the refund is owed to — the member left the org. (A wholly absent users row is a
        different case: _finish_training now rolls the training back on it, because
        lora_trainings.user_id has no FK and application code is the only guard.)"""
        self.db.jobs["LINKED"].update(organization_id="org-9", source_type="monthly")
        # deliberately NO org_members row for ("11111111-1111-4111-8111-111111111111", "org-9")
        self.exhaust()
        self.assertEqual(self.db.jobs["LINKED"]["status"], "failed")
        self.assertEqual(self.refunds(), [], "no ledger row for money that never moved")
        params = json.loads(self.db.jobs["LINKED"]["job_params"])
        plan = params["_failure"]["refund_pending"]
        # The FULL plan is persisted, not just a total: the compensator has to restore the
        # same buckets the immediate path would have.
        self.assertEqual(plan["total"], 40)
        self.assertEqual(plan["aggregate_delta"], 40)
        self.assertEqual(plan["funding"], pr.FUNDING_ORGANIZATION)
        self.assertEqual(plan["target"], pr.TARGET_ORG)
        self.assertEqual(plan["organization_id"], "org-9")
        self.assertEqual((plan["monthly_delta"], plan["one_time_delta"]), (0, 0),
                         "an organization refund never touches the personal buckets")
        self.assertEqual(plan["user_id"], "11111111-1111-4111-8111-111111111111")
        # the org pool was NOT credited, and no ledger row claims otherwise
        self.assertEqual(self.db.org_members, {})
        self.assertEqual(self.refunds(), [])


class OrdinaryTrainingFailureKeepsTheSweep(_Base):
    """The isolation is SPECIFIC to provisioning exhaustion. A real training failure still
    releases/fails everything parked behind it, exactly as before."""

    def test_application_failure_still_sweeps_parked_jobs(self):
        with mock.patch.object(function_app, "_identity_adapter_exists", lambda _u: False):
            function_app._finish_training("T1", "11111111-1111-4111-8111-111111111111", ok=False, error="training crashed")
        self.assertEqual(self.db.trainings["T1"]["status"], "failed")
        for name in ("PARK1", "PARK2"):
            self.assertEqual(self.db.jobs[name]["status"], "failed",
                             "%s should follow the ordinary sweep" % name)
        self.assertEqual(len(self.refunds()), 2)

    def test_successful_training_releases_parked_jobs(self):
        released = []
        with mock.patch.object(function_app, "outbox_add",
                               lambda cur, q, msg: released.append(msg) or 1), \
             mock.patch.object(function_app, "outbox_try_send_now", lambda *a, **k: True):
            function_app._finish_training("T1", "11111111-1111-4111-8111-111111111111", ok=True)
        self.assertEqual(self.db.trainings["T1"]["status"], "completed")
        for name in ("PARK1", "PARK2"):
            self.assertEqual(self.db.jobs[name]["status"], "queued")
        self.assertEqual(len(released), 2)
        self.assertEqual(self.db.trainings["T1"]["fused_job_id"], "LINKED",
                         "the link is retained after success, for audit")


def refund_rows(db):
    """Only job_refund rows. db.ledger also holds the job_reserve row every properly-charged
    job now has, so a bare len(db.ledger) would count the charge as a refund."""
    return [r for r in db.ledger if r[2] == "job_refund"]


class TransactionModel(unittest.TestCase):
    """Item 5: the fake must actually model commit/rollback, or none of the atomicity claims
    above mean anything. These test the TEST HARNESS."""

    def setUp(self):
        self.db = TxDB()
        self.db.add_user("11111111-1111-4111-8111-111111111111", 0)
        self.db.add_job("J1", user_id="11111111-1111-4111-8111-111111111111", source_type="one_time")
        self.db.add_reserve("J1")

    def test_uncommitted_writes_are_invisible_to_another_connection(self):
        c1 = TxConn(self.db)
        pr.terminalize_and_refund(c1.cursor(), "J1", credit_ledger=FakeLedgerModule)
        c2 = TxConn(self.db)
        self.assertEqual(c2.view_jobs()["J1"]["status"], "processing")
        self.assertEqual(self.db.bal("11111111-1111-4111-8111-111111111111"), 0)
        self.assertEqual(refund_rows(self.db), [])

    def test_commit_makes_the_whole_transaction_visible_at_once(self):
        c1 = TxConn(self.db)
        pr.terminalize_and_refund(c1.cursor(), "J1", credit_ledger=FakeLedgerModule)
        c1.commit()
        self.assertEqual(self.db.jobs["J1"]["status"], "failed")
        self.assertEqual(self.db.bal("11111111-1111-4111-8111-111111111111"), 40)
        self.assertEqual(self.db.bal("11111111-1111-4111-8111-111111111111", "one_time_credits_remaining"), 40,
                         "the spendable one-time bucket must be restored too")
        self.assertEqual(len(refund_rows(self.db)), 1)

    def test_rollback_leaves_no_partial_mutation_and_no_ledger_row(self):
        c1 = TxConn(self.db)
        pr.terminalize_and_refund(c1.cursor(), "J1", credit_ledger=FakeLedgerModule)
        c1.rollback()
        self.assertEqual(self.db.jobs["J1"]["status"], "processing")
        self.assertEqual(self.db.bal("11111111-1111-4111-8111-111111111111"), 0)
        self.assertEqual(refund_rows(self.db), [],
                         "a rolled-back ledger row must not survive")

    def test_closing_without_committing_discards_everything(self):
        c1 = TxConn(self.db)
        pr.terminalize_and_refund(c1.cursor(), "J1", credit_ledger=FakeLedgerModule)
        c1.close()
        self.assertEqual(self.db.jobs["J1"]["status"], "processing")
        self.assertEqual(refund_rows(self.db), [])

    def test_a_second_committed_call_observes_the_first(self):
        c1 = TxConn(self.db)
        pr.terminalize_and_refund(c1.cursor(), "J1", credit_ledger=FakeLedgerModule)
        c1.commit()
        c2 = TxConn(self.db)
        transitioned, refund, state = pr.terminalize_and_refund(
            c2.cursor(), "J1", credit_ledger=FakeLedgerModule)
        c2.commit()
        self.assertFalse(transitioned, "the committed 'failed' state must be observed")
        self.assertEqual(state, pr.REFUND_NONE)
        self.assertEqual(self.db.bal("11111111-1111-4111-8111-111111111111"), 40)
        self.assertEqual(self.db.bal("11111111-1111-4111-8111-111111111111", "one_time_credits_remaining"), 40,
                         "the spendable one-time bucket must be restored too")
        self.assertEqual(len(refund_rows(self.db)), 1)

    def test_rowcount_is_reported_for_a_missing_balance_row(self):
        self.db.add_job("J2", user_id="99999999-9999-4999-8999-999999999999", source_type="one_time")
        self.db.add_reserve("J2")
        c1 = TxConn(self.db)
        _, _, state = pr.terminalize_and_refund(
            c1.cursor(), "J2", credit_ledger=FakeLedgerModule)
        self.assertEqual(state, pr.REFUND_PENDING)
        c1.commit()
        self.assertEqual(refund_rows(self.db), [])


class SequentialInterleaving(unittest.TestCase):
    """NOT a concurrency test. Python drives these calls one after another; what is proven is
    that the GUARDS are correctly ordered and that a second caller observing committed state
    declines. Real concurrency (lock waits, deadlocks, UPDLOCK/HOLDLOCK range behaviour) can
    only be shown against a real SQL Server — see docs/sqlserver_integration_plan.md."""

    def setUp(self):
        self.db = TxDB()
        self.db.add_user("11111111-1111-4111-8111-111111111111", 0)
        self.db.add_job("J1", user_id="11111111-1111-4111-8111-111111111111", source_type="one_time")
        self.db.add_reserve("J1")

    def test_second_caller_after_the_first_commits_refunds_nothing(self):
        c1 = TxConn(self.db)
        pr.terminalize_and_refund(c1.cursor(), "J1", credit_ledger=FakeLedgerModule)
        c1.commit()
        c2 = TxConn(self.db)
        transitioned, _, _ = pr.terminalize_and_refund(
            c2.cursor(), "J1", credit_ledger=FakeLedgerModule)
        c2.commit()
        self.assertFalse(transitioned)
        self.assertEqual(len(refund_rows(self.db)), 1)

    def test_interleaved_uncommitted_callers_both_believe_they_won(self):
        """HONEST NEGATIVE RESULT. With no lock manager, two uncommitted transactions each see
        the pre-state and both transition. Real SQL Server serializes them on the row lock
        taken by the UPDATE. This fake CANNOT prove that, which is exactly why the containerized
        integration test in item 6 exists."""
        c1, c2 = TxConn(self.db), TxConn(self.db)
        t1, _, _ = pr.terminalize_and_refund(c1.cursor(), "J1",
                                             credit_ledger=FakeLedgerModule)
        t2, _, _ = pr.terminalize_and_refund(c2.cursor(), "J1",
                                             credit_ledger=FakeLedgerModule)
        self.assertTrue(t1)
        self.assertTrue(t2, "documented limitation of the in-memory fake, not of the code")


if __name__ == "__main__":
    unittest.main(verbosity=2)
