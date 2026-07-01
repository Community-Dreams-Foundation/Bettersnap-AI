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
_mod("azure.functions",
     FunctionApp=_FakeFunctionApp, AuthLevel=_AuthLevel,
     HttpResponse=_HttpResponse, HttpRequest=_HttpRequest, QueueMessage=_QueueMessage)
_mod("azure.storage")
_mod("azure.storage.blob",
     generate_blob_sas=mock.Mock(return_value="sas"),
     BlobSasPermissions=mock.Mock())

# Stub the heavy LEAF modules so importing function_app never pulls pyodbc / jwt
# / azure-mgmt. NOTE: 'shared' itself and shared.job_reservation are left REAL so
# the tests exercise the real reservation logic (it uses the stubbed shared.db).
_mod("shared.auth", validate_token=mock.Mock(), get_user_id=mock.Mock(return_value="user-1"))
_mod("shared.db", get_db=mock.Mock(), new_connection=mock.Mock())
_mod("shared.queue_client", enqueue_job=mock.Mock())
_mod("shared.blob", upload_blob=mock.Mock(), get_blob_client=mock.Mock())
_mod("shared.keyvault", get_secret=mock.Mock(return_value="secret"))
_mod("shared.queue_trigger",
     trigger_container_job=mock.Mock(return_value="exec-123"),
     count_active_job_executions=mock.Mock(return_value=0))
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
        s = sql.lower()
        # simulate a crash mid-operation (e.g. while recording execution id)
        raise_on = self.cfg.get("raise_on")
        if raise_on and raise_on in s:
            raise RuntimeError(f"simulated crash on: {raise_on}")
        if "sp_getapplock" in s:
            self._fetch = (self.cfg.get("applock_rc", 0),)
        elif "select status, external_execution_id" in s:
            self._fetch = self.cfg.get("job_row", ("queued", None))
        elif "update jobs set status = 'dispatching'" in s:
            self.rowcount = self.cfg.get("claim_rowcount", 1)
        elif "update jobs set status = 'failed'" in s:
            # guarded fail transition; rowcount drives the one-time refund
            self.rowcount = self.cfg.get("fail_rowcount", 1)
        elif "select credits_remaining" in s:
            self._fetch = (self.cfg.get("credits", 20),)
        elif "count(*) from jobs where user_id" in s:
            self._fetch = (self.cfg.get("user_count", 0),)
        elif "count(*) from jobs where created_at" in s:
            self._fetch = (self.cfg.get("global_count", 0),)
        elif "insert into jobs" in s:
            self._fetch = (self.cfg.get("new_job_id", 999),)
        else:
            self._fetch = None
        return self

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

    # 11) a terminal failure refunds the credit exactly once (guarded transition)
    def test_failed_path_refunds_credit(self):
        gl = sys.modules["shared.gpu_lease"]
        gl.acquire_dispatch_lease.side_effect = gl.DispatchConfigError("no lease row")
        function_app.process_inference_job(_QueueMessage({"job_id": "1", "user_id": "u"}))
        sqls = [sql for sql, _ in self._cfg["executed"]]
        self.assertTrue(any("status = 'failed'" in s for s in sqls))      # failed
        self.assertTrue(any("credits_remaining + 1" in s for s in sqls))  # refunded

    # 12) refund is NOT issued when the transition does nothing (already terminal)
    def test_no_refund_when_not_transitioned(self):
        gl = sys.modules["shared.gpu_lease"]
        gl.acquire_dispatch_lease.side_effect = gl.DispatchConfigError("no lease row")
        self._cfg["fail_rowcount"] = 0   # row was already failed/completed
        function_app.process_inference_job(_QueueMessage({"job_id": "1", "user_id": "u"}))
        sqls = [sql for sql, _ in self._cfg["executed"]]
        self.assertFalse(any("credits_remaining + 1" in s for s in sqls))  # no double refund


class DailyCapTests(unittest.TestCase):
    """submit_job cap logic (the SQL serialization itself is integration-tested)."""
    def setUp(self):
        self._cfg = {"applock_rc": 0, "credits": 20}
        # submit_job -> reserve_job_slot -> shared.job_reservation.new_connection
        self._patch = mock.patch(
            "shared.job_reservation.new_connection",
            side_effect=lambda: FakeConn(self._cfg))
        self._patch.start()
        sys.modules["shared.auth"].get_user_id.return_value = "user-1"
        sys.modules["shared.queue_client"].enqueue_job.reset_mock()

    def tearDown(self):
        self._patch.stop()

    def _req(self):
        r = _HttpRequest()
        r.headers = {"Authorization": "Bearer t"}
        r.get_json = lambda: {"gender": "m", "age_range": "25-29", "hair_color": "black",
                              "purpose": "linkedin", "background": "white",
                              "input_blob_path": "inputs/u/in.jpg"}
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
        resp = function_app.submit_job(self._req())
        self.assertEqual(resp.status_code, 202)
        sys.modules["shared.queue_client"].enqueue_job.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
