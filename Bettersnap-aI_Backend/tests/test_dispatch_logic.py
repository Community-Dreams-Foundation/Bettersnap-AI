"""Unit tests for GPU dispatch / cost-control logic.

These stub the Azure + DB dependencies so the *decision logic* runs locally with
no Azure SQL, queue, or Container Apps access. They prove the deterministic
guarantees:

  - missing lease row  -> FAIL CLOSED (never start a job)
  - duplicate retry    -> does NOT start a second Container Apps job
  - over-cap           -> defers with backoff; after max defers -> failed
  - loss-safe requeue  -> if enqueue fails, the original message is retried (raises)
  - kill switch        -> long pause delay, no dispatch, no defer increment
  - daily cap logic    -> submit returns 429 at/над the cap, 402 on no credits

True *concurrency* guarantees (sp_getapplock serialization, lease atomicity under
parallel callers) need a real SQL Server — see test_concurrency_integration.py.

Run:  python -m unittest tests.test_dispatch_logic   (from the backend dir)
"""
import os
import sys
import json
import types
import unittest
from unittest import mock

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)


# ── Stub heavy deps BEFORE importing function_app ─────────────────────────
def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _FakeFunctionApp:
    def __init__(self, *a, **k):
        pass

    def route(self, *a, **k):
        return lambda fn: fn

    def queue_trigger(self, *a, **k):
        return lambda fn: fn

    def timer_trigger(self, *a, **k):
        return lambda fn: fn


class _AuthLevel:
    ANONYMOUS = "anonymous"


class _HttpResponse:
    def __init__(self, body="", status_code=200, mimetype=None):
        self.body = body
        self.status_code = status_code
        self.mimetype = mimetype


class _HttpRequest:  # not exercised here
    pass


class _QueueMessage:
    def __init__(self, payload: dict, dequeue_count=1):
        self._body = json.dumps(payload).encode("utf-8")
        self.dequeue_count = dequeue_count

    def get_body(self):
        return self._body


# azure.* stubs
_mod("azure")
# TimerRequest is needed by the timer-trigger function annotations. Python 3.11 (the deploy
# target / CI) evaluates those annotations at import; 3.14 defers them (PEP 649), which is why
# a missing stub attr only surfaced on 3.11. Provide every func.* type function_app annotates.
_mod("azure.functions",
     FunctionApp=_FakeFunctionApp, AuthLevel=_AuthLevel,
     HttpResponse=_HttpResponse, HttpRequest=_HttpRequest, QueueMessage=_QueueMessage,
     TimerRequest=type("TimerRequest", (), {}))
_mod("azure.storage")
_mod("azure.storage.blob",
     generate_blob_sas=mock.Mock(return_value="sas"),
     BlobSasPermissions=mock.Mock())

# Stub the heavy LEAF modules so importing function_app never pulls pyodbc / jwt
# / azure-mgmt. NOTE: 'shared' itself and shared.job_reservation are left REAL so
# the tests exercise the real reservation logic (it uses the stubbed shared.db).
_mod("shared.auth", validate_token=mock.Mock(), get_user_id=mock.Mock(return_value="user-1"))
_mod("shared.db", get_db=mock.Mock(), new_connection=mock.Mock())
_mod("shared.queue_client",
     enqueue_job=mock.Mock(), enqueue_training_job=mock.Mock(),
     # The transactional outbox (shared.outbox, imported REAL via job_reservation) pulls
     # _send + the queue-name constants from here, and function_app imports the constants too.
     _send=mock.Mock(), INFERENCE_QUEUE="inference-jobs", TRAINING_QUEUE="lora-training-jobs")
_mod("shared.blob",
     upload_blob=mock.Mock(), download_blob=mock.Mock(return_value=b""),
     get_blob_client=mock.Mock())
_mod("shared.keyvault", get_secret=mock.Mock(return_value="secret"))
_mod("shared.queue_trigger",
     trigger_container_job=mock.Mock(return_value="exec-123"),
     count_active_job_executions=mock.Mock(return_value=0))


# Identity-LoRA training deps. crops pulls opencv and training_trigger pulls
# azure-mgmt, neither of which the decision-logic tests need.
class _NoFaceError(Exception):
    pass


_mod("shared.crops",
     crop_head_and_shoulders=mock.Mock(return_value=b"jpeg"),
     NoFaceError=_NoFaceError)
_mod("shared.training_trigger",
     trigger_training_job=mock.Mock(return_value="train-exec-1"),
     get_execution_status=mock.Mock(return_value="running"))


class _DispatchConfigError(Exception):
    pass


_mod("shared.gpu_lease",
     acquire_dispatch_lease=mock.Mock(return_value="owner-1"),
     release_dispatch_lease=mock.Mock(),
     mark_dispatched=mock.Mock(),
     recent_dispatch_pending=mock.Mock(return_value=False),
     DispatchConfigError=_DispatchConfigError)

import function_app  # noqa: E402


# ── A programmable fake DB connection/cursor ──────────────────────────────
class FakeCursor:
    """Branches on SQL text to return per-test values. Tracks executed SQL."""
    def __init__(self, cfg):
        self.cfg = cfg
        self.rowcount = 0
        self._fetch = None
        self.executed = cfg.setdefault("executed", [])

    def execute(self, sql, *params):
        self.executed.append((" ".join(sql.split()), params))
        self._fetchall = []
        s = sql.lower()
        # simulate a crash mid-operation (e.g. while recording execution id)
        raise_on = self.cfg.get("raise_on")
        if raise_on and raise_on in s:
            raise RuntimeError(f"simulated crash on: {raise_on}")
        if "sp_getapplock" in s:
            self._fetch = (self.cfg.get("applock_rc", 0),)
        # NOTE: must precede the `jobs` variant — the lora_trainings SELECT also
        # contains the substring "select status, external_execution_id".
        elif "from lora_trainings" in s and "select status" in s:
            self._fetch = self.cfg.get(
                "training_row",
                ("queued", None, '[{"blob": "user-1/input/crop_upperbody/img0.jpg"}]', "woman"),
            )
        elif "select status, external_execution_id" in s:
            self._fetch = self.cfg.get("job_row", ("queued", None))
        elif "update jobs set status = 'dispatching'" in s:
            self.rowcount = self.cfg.get("claim_rowcount", 1)
        elif "update lora_trainings set status = 'dispatching'" in s:
            self.rowcount = self.cfg.get("training_claim_rowcount", 1)
        elif "update lora_trainings set status = ?" in s:
            self.rowcount = self.cfg.get("training_finish_rowcount", 1)
        elif "update jobs set status = 'failed'" in s:
            # guarded fail transition; rowcount drives the one-time refund
            self.rowcount = self.cfg.get("fail_rowcount", 1)
        # Plan + identity-LoRA gate lookup in submit_job. submit_job now selects a THIRD
        # column, credits_remaining (prow[2]), so the row must be a 3-tuple.
        elif "select plan_name, lora_status" in s:
            self._fetch = (self.cfg.get("plan_name", "basic"),
                           self.cfg.get("lora_status", "ready"),
                           self.cfg.get("credits", 20))
        # _mark_failed reads the amount actually charged so it refunds the FULL cost.
        elif "select job_params, user_id from jobs" in s:
            self._fetch = (self.cfg.get("job_params",
                                        json.dumps({"credit_cost": 1})), "user-1")
        elif "select job_id, job_params from jobs" in s:
            self._fetch = None
            self._fetchall = self.cfg.get("parked_jobs", [])
        elif "select credits_remaining" in s:
            self._fetch = (self.cfg.get("credits", 20),)
        # #6 purchase-gate: in-flight job count (status IN ...) — MUST precede the generic
        # user-count branch, which the same "count(*) from jobs where user_id" would swallow.
        elif "count(*) from jobs where user_id" in s and "status in" in s:
            self._fetch = (self.cfg.get("jobs_in_flight", 0),)
        elif "select subscription_type, stripe_subscription_id" in s:
            self._fetch = (self.cfg.get("subscription_type"),
                           self.cfg.get("stripe_subscription_id"))
        elif "count(*) from jobs where user_id" in s:
            self._fetch = (self.cfg.get("user_count", 0),)
        elif "count(*) from jobs where created_at" in s:
            self._fetch = (self.cfg.get("global_count", 0),)
        elif "insert into jobs" in s:
            self._fetch = (self.cfg.get("new_job_id", 999),)
        # Transactional outbox row (written in reserve_job_slot / start_training /
        # _finish_training's txn) — OUTPUT INSERTED.outbox_id, so fetchone must return an id.
        elif "insert into outbox" in s:
            self._fetch = (self.cfg.get("new_outbox_id", 12345),)
        elif "select user_id from lora_trainings" in s:
            self._fetch = (self.cfg.get("training_user", "user-1"),)
        else:
            self._fetch = None
        return self

    def fetchall(self):
        return getattr(self, "_fetchall", [])

    def fetchone(self):
        return self._fetch


class FakeConn:
    def __init__(self, cfg):
        self.cfg = cfg
        self.autocommit = True
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return FakeCursor(self.cfg)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


class DispatchTests(unittest.TestCase):
    def setUp(self):
        # reset all shared mocks
        for m in (function_app.enqueue_job,):
            m.reset_mock(); m.side_effect = None
        gl = sys.modules["shared.gpu_lease"]
        qt = sys.modules["shared.queue_trigger"]
        qc = sys.modules["shared.queue_client"]
        for m in (gl.acquire_dispatch_lease, gl.release_dispatch_lease, gl.mark_dispatched,
                  gl.recent_dispatch_pending, qt.trigger_container_job,
                  qt.count_active_job_executions, qc.enqueue_job):
            m.reset_mock(); m.side_effect = None
        gl.acquire_dispatch_lease.return_value = "owner-1"
        gl.recent_dispatch_pending.return_value = False
        qt.trigger_container_job.return_value = "exec-123"
        qt.count_active_job_executions.return_value = 0
        os.environ["GPU_DISPATCH_ENABLED"] = "true"
        self._cfg = {"job_row": ("queued", None), "claim_rowcount": 1}
        self._patch = mock.patch.object(
            function_app, "new_connection", side_effect=lambda: FakeConn(self._cfg))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    # 1a) lease HELD by another instance -> defer (None), never start
    def test_lease_held_defers(self):
        gl = sys.modules["shared.gpu_lease"]
        qt = sys.modules["shared.queue_trigger"]
        gl.acquire_dispatch_lease.return_value = None  # held
        function_app.process_inference_job(_QueueMessage({"job_id": "1", "user_id": "u"}))
        qt.trigger_container_job.assert_not_called()           # never starts an A100
        sys.modules["shared.queue_client"].enqueue_job.assert_called()  # deferred instead

    # 1b) lease row/table MISSING -> DispatchConfigError -> FAIL LOUD, no defer
    def test_lease_config_error_fails_job(self):
        gl = sys.modules["shared.gpu_lease"]
        qt = sys.modules["shared.queue_trigger"]
        qc = sys.modules["shared.queue_client"]
        gl.acquire_dispatch_lease.side_effect = gl.DispatchConfigError("no lease row")
        function_app.process_inference_job(_QueueMessage({"job_id": "1", "user_id": "u"}))
        qt.trigger_container_job.assert_not_called()   # never starts
        qc.enqueue_job.assert_not_called()             # NOT deferred (fail loud)
        self.assertTrue(any("status = 'failed'" in sql
                            for sql, _ in self._cfg["executed"]))

    # 2) duplicate retry -> job already dispatched -> no second job
    def test_duplicate_dispatch_skipped(self):
        qt = sys.modules["shared.queue_trigger"]
        self._cfg["job_row"] = ("processing", "exec-existing")
        function_app.process_inference_job(_QueueMessage({"job_id": "1", "user_id": "u"}))
        qt.trigger_container_job.assert_not_called()

    def test_existing_execution_id_skips_even_if_queued(self):
        qt = sys.modules["shared.queue_trigger"]
        self._cfg["job_row"] = ("queued", "exec-existing")  # has exec id -> skip
        function_app.process_inference_job(_QueueMessage({"job_id": "1", "user_id": "u"}))
        qt.trigger_container_job.assert_not_called()

    # 3) happy path -> starts exactly once, records execution id, releases lease
    def test_happy_path_starts_once(self):
        gl = sys.modules["shared.gpu_lease"]
        qt = sys.modules["shared.queue_trigger"]
        function_app.process_inference_job(_QueueMessage({"job_id": "1", "user_id": "u"}))
        qt.trigger_container_job.assert_called_once()
        gl.mark_dispatched.assert_called_once()
        gl.release_dispatch_lease.assert_called_once()
        # #5A: the queued->dispatching claim stamps dispatched_at, so the reaper can measure
        # the processing deadline from GPU-run start instead of submit time.
        self.assertTrue(any("dispatched_at = getutcdate()" in s.lower()
                            for s, _ in self._cfg["executed"]))

    # 4) over-cap -> defers, does not start
    def test_over_cap_defers(self):
        qt = sys.modules["shared.queue_trigger"]
        qc = sys.modules["shared.queue_client"]
        qt.count_active_job_executions.return_value = 1  # cap is 1
        function_app.process_inference_job(_QueueMessage({"job_id": "1", "user_id": "u"}))
        qt.trigger_container_job.assert_not_called()
        qc.enqueue_job.assert_called_once()
        # backoff delay passed as visibility_timeout
        _, kw = qc.enqueue_job.call_args
        self.assertIn("visibility_timeout", kw)

    # 5) max defers -> marked failed, NOT re-enqueued
    def test_max_defers_marks_failed(self):
        qt = sys.modules["shared.queue_trigger"]
        qc = sys.modules["shared.queue_client"]
        qt.count_active_job_executions.return_value = 1
        payload = {"job_id": "1", "user_id": "u",
                   "defer_count": function_app.MAX_DISPATCH_DEFERS}
        function_app.process_inference_job(_QueueMessage(payload))
        qc.enqueue_job.assert_not_called()  # no more requeue
        # a failed UPDATE was issued
        self.assertTrue(any("status = 'failed'" in sql
                            for sql, _ in self._cfg["executed"]))

    # 6) loss-safe requeue -> enqueue fails => exception propagates (host retries)
    def test_requeue_loss_safe(self):
        qt = sys.modules["shared.queue_trigger"]
        qc = sys.modules["shared.queue_client"]
        qt.count_active_job_executions.return_value = 1
        qc.enqueue_job.side_effect = RuntimeError("queue down")
        with self.assertRaises(RuntimeError):
            function_app.process_inference_job(_QueueMessage({"job_id": "1", "user_id": "u"}))

    # 7) kill switch -> long pause delay, no dispatch, no defer increment
    def test_kill_switch_pauses(self):
        qt = sys.modules["shared.queue_trigger"]
        qc = sys.modules["shared.queue_client"]
        os.environ["GPU_DISPATCH_ENABLED"] = "false"
        function_app.process_inference_job(_QueueMessage({"job_id": "1", "user_id": "u"}))
        qt.trigger_container_job.assert_not_called()
        qc.enqueue_job.assert_called_once()
        _, kw = qc.enqueue_job.call_args
        self.assertEqual(kw.get("visibility_timeout"), function_app.KILL_SWITCH_PAUSE_DELAY)

    # 8) start failure -> claim reverted to 'queued' and re-raised
    def test_start_failure_reverts_and_raises(self):
        qt = sys.modules["shared.queue_trigger"]
        qt.trigger_container_job.side_effect = RuntimeError("ACA 500")
        with self.assertRaises(RuntimeError):
            function_app.process_inference_job(_QueueMessage({"job_id": "1", "user_id": "u"}))
        self.assertTrue(any("status = 'queued'" in sql and "dispatching" in sql
                            for sql, _ in self._cfg["executed"]))

    # 9) CRASH-AFTER-START: A100 started, but recording the execution id crashes.
    #    The start happened exactly once and the exception propagates so the host
    #    retries — it must NOT have started a second job in this invocation.
    def test_crash_while_recording_execution_id(self):
        qt = sys.modules["shared.queue_trigger"]
        self._cfg["raise_on"] = "set external_execution_id"  # crash on the record step only
        with self.assertRaises(RuntimeError):
            function_app.process_inference_job(_QueueMessage({"job_id": "1", "user_id": "u"}))
        qt.trigger_container_job.assert_called_once()  # started exactly once

    # 10) CRASH-AFTER-START retry: the job is now stuck in 'dispatching' (claim
    #     committed, exec id never saved). The retried message must NOT start a
    #     second A100 — idempotency catches status='dispatching'.
    def test_retry_after_crash_does_not_restart(self):
        qt = sys.modules["shared.queue_trigger"]
        self._cfg["job_row"] = ("dispatching", None)  # state left by the crash
        function_app.process_inference_job(_QueueMessage({"job_id": "1", "user_id": "u"}))
        qt.trigger_container_job.assert_not_called()

    # 11) a terminal failure refunds the credit exactly once (guarded transition).
    #     The refund is the FULL amount charged at submit (image_count *
    #     credits_per_image, carried in job_params.credit_cost) — NOT a hardcoded 1.
    #     A 30-image job that fails must return 30 credits, not 1.
    def test_failed_path_refunds_full_credit_cost(self):
        gl = sys.modules["shared.gpu_lease"]
        gl.acquire_dispatch_lease.side_effect = gl.DispatchConfigError("no lease row")
        self._cfg["job_params"] = json.dumps({"credit_cost": 30})
        function_app.process_inference_job(_QueueMessage({"job_id": "1", "user_id": "u"}))
        executed = self._cfg["executed"]
        sqls = [sql for sql, _ in executed]
        self.assertTrue(any("status = 'failed'" in s for s in sqls))          # failed
        refunds = [p for s, p in executed if "credits_remaining + ?" in s]
        self.assertEqual(len(refunds), 1)                                     # exactly once
        self.assertEqual(refunds[0][0], 30)                                   # FULL cost

    # 12) refund is NOT issued when the transition does nothing (already terminal)
    def test_no_refund_when_not_transitioned(self):
        gl = sys.modules["shared.gpu_lease"]
        gl.acquire_dispatch_lease.side_effect = gl.DispatchConfigError("no lease row")
        self._cfg["fail_rowcount"] = 0   # row was already failed/completed
        function_app.process_inference_job(_QueueMessage({"job_id": "1", "user_id": "u"}))
        sqls = [sql for sql, _ in self._cfg["executed"]]
        self.assertFalse(any("credits_remaining + ?" in s for s in sqls))  # no double refund


class DailyCapTests(unittest.TestCase):
    """submit_job cap logic (the SQL serialization itself is integration-tested)."""
    def setUp(self):
        # lora_status defaults to 'ready' — the cap tests are about caps, not the
        # identity gate, and an untrained user is rejected before the caps are reached.
        self._cfg = {"applock_rc": 0, "credits": 200, "lora_status": "ready",
                     "plan_name": "basic"}
        # submit_job -> reserve_job_slot -> shared.job_reservation.new_connection
        self._patch = mock.patch(
            "shared.job_reservation.new_connection",
            side_effect=lambda: FakeConn(self._cfg))
        self._patch.start()
        # submit_job ALSO reads plan_name + lora_status via get_db() before reserving.
        self._patch_db = mock.patch.object(
            function_app, "get_db", side_effect=lambda: FakeConn(self._cfg))
        self._patch_db.start()
        sys.modules["shared.auth"].get_user_id.return_value = "user-1"
        sys.modules["shared.queue_client"].enqueue_job.reset_mock()
        sys.modules["shared.queue_client"]._send.reset_mock()
        sys.modules["shared.queue_client"]._send.side_effect = None

    def tearDown(self):
        self._patch.stop()
        self._patch_db.stop()

    def _req(self):
        r = _HttpRequest()
        r.headers = {"Authorization": "Bearer t"}
        r.get_json = lambda: {
            "gender": "m", "age_range": "25-29", "hair_color": "black",
            "input_blob_path": "inputs/u/in.jpg",
            "attire_ids": ["business_suit.navy_suit_tie"],
            "background_ids": ["business_suit.studio_gray"],
        }
        return r

    def test_per_user_cap_blocks_at_limit(self):
        self._cfg["user_count"] = function_app.PER_USER_DAILY_CAP
        self._cfg["global_count"] = 0
        resp = function_app.submit_job(self._req())
        self.assertEqual(resp.status_code, 429)
        self.assertIn("user", resp.body)

    def test_global_cap_blocks_at_limit(self):
        self._cfg["user_count"] = 0
        self._cfg["global_count"] = function_app.GLOBAL_DAILY_CAP
        resp = function_app.submit_job(self._req())
        self.assertEqual(resp.status_code, 429)
        self.assertIn("global", resp.body)

    def test_no_credits_blocks(self):
        self._cfg["credits"] = 0
        resp = function_app.submit_job(self._req())
        self.assertEqual(resp.status_code, 402)

    def test_applock_timeout_returns_503(self):
        self._cfg["applock_rc"] = -1
        resp = function_app.submit_job(self._req())
        self.assertEqual(resp.status_code, 503)

    def test_happy_submit_enqueues_and_202(self):
        self._cfg.update(user_count=0, global_count=0, new_job_id=777)
        sys.modules["shared.queue_client"].enqueue_job.reset_mock()
        sys.modules["shared.queue_client"]._send.reset_mock()
        sys.modules["shared.queue_client"]._send.side_effect = None
        resp = function_app.submit_job(self._req())
        self.assertEqual(resp.status_code, 202)
        sys.modules["shared.queue_client"]._send.assert_called_once()
        # #6 foundation: the job row is tagged with the product (plan_type) it belongs to.
        job_inserts = [(s, p) for s, p in self._cfg["executed"] if "insert into jobs" in s.lower()]
        self.assertTrue(job_inserts, "no jobs INSERT recorded")
        self.assertIn("source_type", job_inserts[0][0].lower())
        self.assertIsNotNone(job_inserts[0][1][-1])

    def test_send_failure_keeps_job_and_returns_202(self):
        # Transactional outbox: the queue message was written ATOMICALLY with the job + credit
        # charge in reserve_job_slot, so a fast-path send failure is NOT fatal. The job is
        # neither failed nor refunded — the outbox_dispatcher delivers it later — and the API
        # still returns 202 (the work is safely persisted, not an orphan).
        self._cfg.update(user_count=0, global_count=0, new_job_id=555)
        qc = sys.modules["shared.queue_client"]
        qc._send.reset_mock()
        qc._send.side_effect = RuntimeError("queue down")
        try:
            with mock.patch.object(function_app, "_mark_failed") as mf:
                resp = function_app.submit_job(self._req())
            self.assertEqual(resp.status_code, 202)   # success — job persisted, not orphaned
            mf.assert_not_called()                     # NOT refunded; the dispatcher retries
        finally:
            qc._send.side_effect = None


class IdentityLoraGateTests(unittest.TestCase):
    """The gate that stops a user without a trained adapter being handed a STRANGER'S
    face. Without an identity LoRA, txt2img renders base SDXL — a photogenic stranger —
    and the old code uploaded that as the user's headshots."""

    def setUp(self):
        self._cfg = {"applock_rc": 0, "credits": 200, "plan_name": "basic",
                     "new_job_id": 777, "user_count": 0, "global_count": 0}
        self._p1 = mock.patch("shared.job_reservation.new_connection",
                              side_effect=lambda: FakeConn(self._cfg))
        self._p2 = mock.patch.object(function_app, "get_db",
                                     side_effect=lambda: FakeConn(self._cfg))
        self._p3 = mock.patch.object(function_app, "new_connection",
                                     side_effect=lambda: FakeConn(self._cfg))
        self._p1.start(); self._p2.start(); self._p3.start()
        sys.modules["shared.auth"].get_user_id.return_value = "user-1"
        sys.modules["shared.queue_client"].enqueue_job.reset_mock()
        sys.modules["shared.queue_client"]._send.reset_mock()
        sys.modules["shared.queue_client"]._send.side_effect = None

    def tearDown(self):
        self._p1.stop(); self._p2.stop(); self._p3.stop()

    def _req(self):
        r = _HttpRequest()
        r.headers = {"Authorization": "Bearer t"}
        r.get_json = lambda: {
            "gender": "f", "age_range": "25-29", "hair_color": "black",
            "input_blob_path": "inputs/u/in.jpg",
            "attire_ids": ["business_suit.navy_suit_tie"],
            "background_ids": ["business_suit.studio_gray"],
        }
        return r

    # Never trained -> 409 BEFORE any credit is spent. A parked job with no training
    # in flight would wait forever, so this must reject rather than park.
    def test_untrained_user_rejected_and_charged_nothing(self):
        self._cfg["lora_status"] = "none"
        resp = function_app.submit_job(self._req())
        self.assertEqual(resp.status_code, 409)
        sqls = [s.lower() for s, _ in self._cfg.get("executed", [])]
        self.assertFalse(any("insert into jobs" in s for s in sqls))     # no job row
        self.assertFalse(any("credits_remaining -" in s for s in sqls))  # no charge
        sys.modules["shared.queue_client"]._send.assert_not_called()

    # A failed LoRA is equally unusable -> same rejection, not an infinite park.
    def test_failed_lora_rejected(self):
        self._cfg["lora_status"] = "failed"
        self.assertEqual(function_app.submit_job(self._req()).status_code, 409)

    # Training in flight -> ACCEPT, reserve credits, park as 'waiting_lora',
    # and deliberately DO NOT enqueue (the watcher releases it).
    def test_training_in_flight_parks_without_enqueueing(self):
        self._cfg["lora_status"] = "training"
        resp = function_app.submit_job(self._req())
        self.assertEqual(resp.status_code, 202)
        self.assertIn("waiting_lora", resp.body)
        inserts = [p for s, p in self._cfg["executed"] if "insert into jobs" in s.lower()]
        self.assertEqual(inserts[0][1], "waiting_lora")                  # parked status
        sys.modules["shared.queue_client"]._send.assert_not_called()

    def test_ready_lora_dispatches_normally(self):
        self._cfg["lora_status"] = "ready"
        resp = function_app.submit_job(self._req())
        self.assertEqual(resp.status_code, 202)
        inserts = [p for s, p in self._cfg["executed"] if "insert into jobs" in s.lower()]
        self.assertEqual(inserts[0][1], "queued")
        sys.modules["shared.queue_client"]._send.assert_called_once()

    # Training SUCCEEDS -> parked jobs are released IMMEDIATELY (no visibility delay,
    # so no waiting on a backoff timer while the adapter sits there ready).
    def test_training_success_releases_parked_jobs(self):
        self._cfg["parked_jobs"] = [("job-a", "{}"), ("job-b", "{}")]
        function_app._finish_training("t-1", "user-1", ok=True)
        qc = sys.modules["shared.queue_client"]
        self.assertEqual(qc._send.call_count, 2)
        sqls = [s.lower() for s, _ in self._cfg["executed"]]
        self.assertTrue(any("update jobs set status = 'queued'" in s for s in sqls))

    # FIRST training FAILS (no prior adapter) -> parked jobs must NOT hang forever waiting
    # on an adapter that will never exist. They are failed and refunded instead.
    def test_training_failure_fails_and_refunds_parked_jobs(self):
        self._cfg["parked_jobs"] = [("job-a", "{}")]
        self._cfg["job_params"] = json.dumps({"credit_cost": 30})
        with mock.patch.object(function_app, "_identity_adapter_exists", return_value=False):
            function_app._finish_training("t-1", "user-1", ok=False, error="boom")
        sys.modules["shared.queue_client"]._send.assert_not_called()
        executed = self._cfg["executed"]
        self.assertTrue(any("status = 'failed'" in s.lower() for s, _ in executed))
        refunds = [p for s, p in executed if "credits_remaining + ?" in s]
        self.assertEqual(refunds[0][0], 30)     # full cost returned, not 1

    # A failed RETRAIN must NOT strand a user whose previous model still works.
    # The trainer only overwrites the adapter at the very end (after its format gate), so a
    # failed run leaves the old one intact. Marking them 'failed' would 409 them out of
    # /jobs/submit and take away a product they already had, over a failure that damaged
    # nothing. They stay 'ready' and their parked jobs are RELEASED, not refunded.
    def test_failed_retrain_keeps_user_ready_when_prior_adapter_intact(self):
        self._cfg["parked_jobs"] = [("job-a", "{}")]
        with mock.patch.object(function_app, "_identity_adapter_exists", return_value=True):
            function_app._finish_training("t-1", "user-1", ok=False, error="OOM")
        # the RUN is still recorded as failed ...
        executed = self._cfg["executed"]
        finish = [p for s, p in executed if "update lora_trainings set status = ?" in s.lower()]
        self.assertEqual(finish[0][0], "failed")
        # ... but the USER keeps a working model, and their job runs instead of dying.
        user_sets = [p for s, p in executed if "update users set lora_status" in s.lower()]
        self.assertEqual(user_sets[0][0], "ready")
        sys.modules["shared.queue_client"]._send.assert_called_once()

    # A retried training message must never start a SECOND A100 for the same run.
    def test_training_dispatch_is_idempotent(self):
        tt = sys.modules["shared.training_trigger"]
        tt.trigger_training_job.reset_mock()
        gl = sys.modules["shared.gpu_lease"]
        gl.acquire_dispatch_lease.return_value = "owner-1"
        gl.acquire_dispatch_lease.side_effect = None
        self._cfg["training_row"] = ("training", "already-running-exec", "[]", "woman")
        function_app.process_training_job(
            _QueueMessage({"training_id": "t-1", "user_id": "user-1"}))
        tt.trigger_training_job.assert_not_called()


class PlanAffordabilityTests(unittest.TestCase):
    """The registration grant MUST cover one job on the default plan.

    This exact invariant was broken in production: the grant was 20 credits while the
    default plan (Basic) cost 30, so every single registered user was created unable to
    ever generate anything — a permanent 402, with no billing flow to escape it. Nothing
    failed loudly; the users table just filled up with dead accounts. This test is the
    tripwire, so changing a plan's image_count or the grant can never silently do it again.
    """

    def test_registration_grant_covers_one_job_on_the_default_plan(self):
        from shared import plans
        plan = plans.get_plan(plans.DEFAULT_PLAN_KEY)
        cost = plans.credit_cost(plan, plan.image_count)
        self.assertLessEqual(
            cost, plans.REGISTRATION_CREDITS,
            f"a new user gets {plans.REGISTRATION_CREDITS} credits but one job on the "
            f"default plan '{plan.key}' costs {cost} — every signup would 402 forever",
        )
        self.assertTrue(plans.affordable(plan, plans.REGISTRATION_CREDITS))

    def test_default_plan_exists(self):
        from shared import plans
        self.assertIn(plans.DEFAULT_PLAN_KEY, plans.PLANS)


class BillingGateTests(unittest.TestCase):
    """finding #6: ONE active product at a time — a PLAN purchase that collides with an active
    monthly subscription or an in-flight generation is rejected (409) with a clear next step."""

    def setUp(self):
        sys.modules["shared.auth"].validate_token.return_value = {"oid": "user-1"}
        self._cfg = {}
        self._p = mock.patch.object(function_app, "get_db",
                                    side_effect=lambda: FakeConn(self._cfg))
        self._p2 = mock.patch.object(function_app, "new_connection",
                                     side_effect=lambda: FakeConn(self._cfg))
        self._p.start(); self._p2.start()

    def tearDown(self):
        self._p.stop(); self._p2.stop()

    def _req(self, plan="basic", ptype="monthly"):
        r = _HttpRequest()
        r.headers = {"Authorization": "Bearer t"}
        r.get_json = lambda: {"plan": plan, "type": ptype}
        return r

    def test_active_monthly_queues_new_monthly_plan(self):
        self._cfg.update(subscription_type="monthly", stripe_subscription_id="sub_1")
        resp = function_app.create_subscription(self._req(plan="pro", ptype="monthly"))
        self.assertEqual(resp.status_code, 202)          # queued, not rejected
        self.assertIn("queued", resp.body)
        self.assertIn("monthly_active", resp.body)
        # the intent is stored in pending_purchases
        self.assertTrue(any("insert into pending_purchases" in s.lower()
                            for s, _ in self._cfg["executed"]))

    def test_active_monthly_queues_one_time_too(self):
        self._cfg.update(subscription_type="monthly", stripe_subscription_id="sub_1")
        resp = function_app.create_subscription(self._req(plan="basic", ptype="one_time"))
        self.assertEqual(resp.status_code, 202)
        self.assertIn("queued", resp.body)

    def test_generation_in_flight_queues_new_plan(self):
        self._cfg.update(subscription_type=None, stripe_subscription_id=None, jobs_in_flight=1)
        resp = function_app.create_subscription(self._req(plan="basic", ptype="one_time"))
        self.assertEqual(resp.status_code, 202)
        self.assertIn("generation_in_flight", resp.body)

    def test_topup_requires_active_monthly(self):
        # A non-subscriber cannot buy loose credits — they buy a plan.
        self._cfg.update(subscription_type=None, stripe_subscription_id=None)
        resp = function_app.topup_credits(self._req(plan="basic"))
        self.assertEqual(resp.status_code, 409)
        self.assertIn("no_active_monthly", resp.body)

    def test_topup_allowed_for_active_monthly(self):
        self._cfg.update(subscription_type="monthly", stripe_subscription_id="sub_1")
        with mock.patch.object(function_app, "create_topup_checkout",
                               return_value={"url": "http://pay", "id": "cs_1"}):
            resp = function_app.topup_credits(self._req(plan="basic"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("checkout_url", resp.body)


class ReaperTests(unittest.TestCase):
    """finding #5 part A: the reaper must measure the processing/dispatching deadline from
    dispatched_at (when the GPU run started), NOT created_at (submit) — so a healthy job that
    merely waited in the queue is never reaped."""

    def test_reaper_measures_from_dispatched_at_not_created_at(self):
        cfg = {}  # FakeCursor returns no rows -> nothing reaped; we assert the SQL shape
        with mock.patch.object(function_app, "new_connection",
                               side_effect=lambda: FakeConn(cfg)):
            function_app.reaper(None)
        sqls = [s.lower() for s, _ in cfg["executed"]]
        proc = [s for s in sqls if "status = 'processing'" in s]
        disp = [s for s in sqls if "status = 'dispatching'" in s]
        self.assertTrue(proc, "reaper did not scan 'processing'")
        self.assertTrue(disp, "reaper did not scan 'dispatching'")
        # Both scans must age from COALESCE(dispatched_at, created_at)...
        self.assertTrue(all("coalesce(dispatched_at, created_at)" in s for s in proc + disp))
        # ...and must NOT age from the bare submit-time column.
        self.assertFalse(any("and created_at < dateadd" in s for s in sqls),
                         "reaper still measures from created_at (submit time)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
