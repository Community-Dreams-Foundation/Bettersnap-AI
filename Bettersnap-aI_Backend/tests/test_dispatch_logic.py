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
import io
import os
import sys
import json
import types
import unittest
from datetime import datetime, timezone
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
_mod("requests", post=mock.Mock(), get=mock.Mock())

# Stub the heavy LEAF modules so importing function_app never pulls pyodbc / jwt
# / azure-mgmt. NOTE: 'shared' itself and shared.job_reservation are left REAL so
# the tests exercise the real reservation logic (it uses the stubbed shared.db).
_mod("shared.auth", validate_token=mock.Mock(), get_user_id=mock.Mock(return_value="user-1"),
     require_admin=mock.Mock(return_value={"oid": "admin", "email": "admin@test", "name": "Admin", "roles": ["Admin"]}),
     NotAdminError=type("NotAdminError", (Exception,), {}))
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
     count_active_job_executions=mock.Mock(return_value=0),
     find_execution_for_job=mock.Mock(return_value=None),
     execution_status=mock.Mock(return_value="running"),
     stop_execution=mock.Mock(return_value=True))


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
     clear_dispatch_pending=mock.Mock(),
     recent_dispatch_pending=mock.Mock(return_value=False),
     DispatchConfigError=_DispatchConfigError)

import function_app  # noqa: E402


class JobRouteIdTests(unittest.TestCase):
    def setUp(self):
        self._db_patcher = mock.patch.object(function_app, "get_db")
        self.get_db = self._db_patcher.start()
        sys.modules["shared.auth"].get_user_id.return_value = "user-1"

    def tearDown(self):
        self._db_patcher.stop()

    @staticmethod
    def _request(job_id):
        req = _HttpRequest()
        req.headers = {"Authorization": "Bearer token"}
        req.route_params = {"job_id": job_id}
        return req

    def test_malformed_id_returns_404_before_sql_for_all_job_routes(self):
        for handler in (
            function_app.job_status,
            function_app.job_result_url,
            function_app.delete_job,
        ):
            with self.subTest(handler=handler.__name__):
                self.get_db.reset_mock()
                response = handler(self._request("not-a-guid"))
                self.assertEqual(response.status_code, 404)
                self.get_db.assert_not_called()

    def test_valid_id_is_canonicalized(self):
        raw = "D85B1407-351D-4694-9392-03ACC5870EB1"
        self.assertEqual(
            function_app._route_job_id(self._request(raw)),
            "d85b1407-351d-4694-9392-03acc5870eb1",
        )


class ProfileEmailValidationTests(unittest.TestCase):
    class Cursor:
        def __init__(self, update_error=None):
            self.update_error = update_error
            self._row = None

        def execute(self, sql, *_params):
            normalized = " ".join(sql.lower().split())
            if normalized.startswith("select user_id from users"):
                self._row = ("user-1",)
            elif normalized.startswith("update users set") and self.update_error:
                raise self.update_error
            return self

        def fetchone(self):
            return self._row

    class Connection:
        def __init__(self, update_error=None):
            self.cur = ProfileEmailValidationTests.Cursor(update_error)
            self.rolled_back = False

        def cursor(self):
            return self.cur

        def commit(self):
            pass

        def rollback(self):
            self.rolled_back = True

    @staticmethod
    def _request(email):
        req = _HttpRequest()
        req.headers = {"Authorization": "Bearer token"}
        req.get_json = lambda: {"email": email}
        return req

    def test_invalid_email_returns_400_before_database_access(self):
        with mock.patch.object(function_app, "get_db") as get_db:
            response = function_app.update_profile(self._request("not-an-email"))

        self.assertEqual(response.status_code, 400)
        get_db.assert_not_called()

    def test_duplicate_email_unique_violation_returns_409(self):
        duplicate = RuntimeError(
            "23000", "Cannot insert duplicate key row in unique index "
            "'UX_users_email' (2601)"
        )
        conn = self.Connection(duplicate)
        with mock.patch.object(function_app, "get_db", return_value=conn):
            response = function_app.update_profile(self._request(" Used@Example.COM "))

        self.assertEqual(response.status_code, 409)
        self.assertIn("already in use", response.body)
        self.assertTrue(conn.rolled_back)

    def test_unrelated_database_error_is_not_hidden_as_conflict(self):
        conn = self.Connection(RuntimeError("database unavailable"))
        with mock.patch.object(function_app, "get_db", return_value=conn):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                function_app.update_profile(self._request("valid@example.com"))

        self.assertTrue(conn.rolled_back)


# ── A programmable fake DB connection/cursor ──────────────────────────────
class UploadServerNamingTests(unittest.TestCase):
    class File:
        def __init__(self, filename, data):
            self.filename = filename
            self._data = data

        def read(self):
            return self._data

    @staticmethod
    def _image_bytes(fmt):
        from io import BytesIO
        from PIL import Image

        output = BytesIO()
        Image.new("RGB", (256, 256), "white").save(output, format=fmt)
        return output.getvalue()

    @classmethod
    def _request(cls, filename, fmt="JPEG"):
        req = _HttpRequest()
        req.headers = {"Authorization": "Bearer token"}
        req.files = {"photo": cls.File(filename, cls._image_bytes(fmt))}
        return req

    def setUp(self):
        function_app.upload_blob.reset_mock()
        function_app.upload_blob.return_value = "https://storage/upload"
        sys.modules["shared.auth"].get_user_id.return_value = "user-1"

    def test_duplicate_client_filenames_get_distinct_server_blob_names(self):
        first = function_app.upload_photo(self._request("image.jpg"))
        second = function_app.upload_photo(self._request("image.jpg"))

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        first_name = json.loads(first.body)["blob_name"]
        second_name = json.loads(second.body)["blob_name"]
        self.assertNotEqual(first_name, second_name)
        for name in (first_name, second_name):
            self.assertRegex(name, r"^user-1/input/[0-9a-f]{32}\.jpg$")
            self.assertNotIn("image.jpg", name)

    def test_slash_name_is_ignored_and_extension_comes_from_verified_bytes(self):
        response = function_app.upload_photo(
            self._request("nested/client/name.jpg", fmt="PNG"))

        self.assertEqual(response.status_code, 200)
        blob_name = json.loads(response.body)["blob_name"]
        self.assertRegex(blob_name, r"^user-1/input/[0-9a-f]{32}\.png$")
        relative_name = blob_name.removeprefix("user-1/input/")
        self.assertNotIn("/", relative_name)
        self.assertNotIn("nested", blob_name)


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
        self.rowcount = 0
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
        elif "where status in ('processing', 'dispatching')" in s:
            # The early reconcile now covers 'dispatching' too: a PRE-CONTAINER failure can
            # never reach 'processing', so the old scope missed exactly the retryable class.
            # Rows are (job_id, execution_id, status).
            self._fetchall = [
                r if len(r) == 3 else (r[0], r[1], "processing")
                for r in self.cfg.get("reaper_processing_with_exec", [])
            ]
        elif "select job_id from jobs where status = 'processing'" in s:
            self._fetchall = self.cfg.get("reaper_processing", [])
        elif "select job_id, external_execution_id from jobs where status = 'dispatching'" in s:
            self._fetchall = self.cfg.get("reaper_dispatching", [])
        elif "select terms_accepted_at from users" in s:
            self._fetch = (self.cfg.get("terms_accepted_at"),)
        elif "update jobs set status = 'dispatching'" in s:
            self.rowcount = self.cfg.get("claim_rowcount", 1)
        elif "update lora_trainings set status = 'dispatching'" in s:
            self.rowcount = self.cfg.get("training_claim_rowcount", 1)
        elif "update lora_trainings set status = ?" in s:
            self.rowcount = self.cfg.get("training_finish_rowcount", 1)
        elif "select 1 from users with (updlock, holdlock)" in s:
            # The owner-existence probe _finish_training now makes BEFORE any users write.
            # Default: the user exists. Set user_missing=True to model an orphaned training.
            self._fetch = None if self.cfg.get("user_missing") else (1,)
        elif ("select monthly_credit_cost, one_time_credit_cost, error" in s
              and "from lora_trainings" in s):
            costs = self.cfg.get("training_credit_costs", (0, 0))
            # now also returns `error`, so the orphan path can preserve the root cause
            self._fetch = tuple(costs) + (self.cfg.get("training_error"),)
        elif "update lora_trainings set status = ?, error = ?, completed_at" in s:
            # The orphan terminal write: error = ? (NOT COALESCE), so the marker lands first.
            self.rowcount = self.cfg.get("training_finish_rowcount", 1)
        elif "update users set lora_status = ?" in s:
            # rowcount is load-bearing: lora_trainings.user_id has NO FK to users, so this
            # UPDATE is one of the places a vanished owner is detected.
            self.rowcount = self.cfg.get("lora_status_rowcount", 1)
        elif "update users set monthly_credits_remaining = monthly_credits_remaining + ?" in s:
            self.rowcount = self.cfg.get("retrain_refund_rowcount", 1)
        elif "update jobs set external_execution_id = ? where job_id = ? and status = ?" in s:
            # Guarded: fills an empty slot only. cfg drives whether this attempt still owns
            # the row, so a test can model a LATE writer losing to a newer attempt.
            self.rowcount = self.cfg.get("record_exec_rowcount", 1)
        elif "select provisioning_execution_ids from jobs" in s:
            self._fetch = (self.cfg.get("provisioning_execution_ids"),)
        elif "select status from jobs with (updlock" in s:
            # terminalize_corrupt_history locks and re-reads the row before failing it.
            self._fetch = (self.cfg.get("corrupt_status", "dispatching"),)
        elif "datediff(second, coalesce(dispatched_at, created_at)" in s:
            self._fetch = (self.cfg.get("corrupt_age_s", 0),)
        elif "job_params like ?" in s:
            self._fetchall = self.cfg.get("pending_refunds", [])
        elif "update jobs set status = 'failed'" in s:
            # guarded fail transition; rowcount drives the one-time refund
            self.rowcount = self.cfg.get("fail_rowcount", 1)
        # Plan + identity-LoRA gate lookup in submit_job.
        elif "select plan_name, lora_status" in s:
            self._fetch = (self.cfg.get("plan_name", "basic"),
                           self.cfg.get("lora_status", "ready"),
                           self.cfg.get("credits", 20),
                           self.cfg.get("one_time_credits", 0),
                           self.cfg.get("suspended_at"))  # None = active (submit_job suspend gate)
        # _mark_failed reads the amount actually charged so it refunds the FULL cost.
        elif "select job_params, user_id, source_type, organization_id from jobs" in s:
            self._fetch = (self.cfg.get("job_params", json.dumps({"credit_cost": 1})),
                           self.cfg.get("job_user_id", "user-1"),
                           self.cfg.get("job_source_type"),
                           self.cfg.get("job_org_id"))
        elif "update users set credits_remaining = credits_remaining + ? where user_id = ?" in s:
            self.rowcount = self.cfg.get("refund_rowcount", 1)
        elif "update users set monthly_credits_remaining" in s:
            self.rowcount = self.cfg.get("refund_rowcount", 1)
        elif "update organization_members set credits_remaining" in s:
            self.rowcount = self.cfg.get("org_refund_rowcount", 1)
        elif "select job_params, user_id, source_type from jobs" in s:
            self._fetch = (self.cfg.get("job_params",
                                        json.dumps({"credit_cost": 1})), "user-1",
                           self.cfg.get("job_source_type"))
        elif "select job_id, job_params from jobs" in s:
            self._fetch = None
            self._fetchall = self.cfg.get("parked_jobs", [])
        elif "lora_status from users with (updlock" in s:
            # Merged reserve_job_slot reads lora_status alone (its own UPDLOCK query).
            self._fetch = (
                self.cfg.get("reservation_lora_status", self.cfg.get("lora_status", "ready")),
            )
        elif "select monthly_credits_remaining, one_time_credits_remaining" in s:
            self._fetch = (
                self.cfg.get("credits", 20),
                self.cfg.get("one_time_credits", 0),
            )
        elif ("select credits_remaining" in s
              or "select monthly_credits_remaining" in s
              or "select one_time_credits_remaining" in s):
            self._fetch = (self.cfg.get("credits", 20),)
        # #6 purchase-gate: in-flight job count (status IN ...) — MUST precede the generic
        # user-count branch, which the same "count(*) from jobs where user_id" would swallow.
        elif ("count(*) from jobs where user_id" in s and "status in" in s
              and "created_at" not in s):
            self._fetch = (self.cfg.get("jobs_in_flight", 0),)
        elif "select subscription_type, stripe_subscription_id" in s:
            if "stripe_checkout_expires_at" in s:
                self._fetch = (
                    self.cfg.get("subscription_type"),
                    self.cfg.get("stripe_subscription_id"),
                    self.cfg.get("stripe_checkout_expires_at"),
                )
            else:
                self._fetch = (self.cfg.get("subscription_type"),
                               self.cfg.get("stripe_subscription_id"))
        elif "select stripe_subscription_id, subscription_type" in s:
            values = [
                self.cfg.get("stripe_subscription_id"),
                self.cfg.get("subscription_type"),
            ]
            if "subscription_renewed_at" in s:
                values.append(self.cfg.get("subscription_renewed_at"))
            self._fetch = tuple(values)
        elif "select subscription_plan, subscription_type, stripe_subscription_id" in s:
            self._fetch = (
                self.cfg.get("subscription_plan", "pro"),
                self.cfg.get("subscription_type"),
                self.cfg.get("stripe_subscription_id"),
                self.cfg.get("subscription_cancel_at"),
            )
        elif ("select credits_monthly_limit, monthly_credits_remaining" in s
              and "one_time_credits_remaining" in s):
            self._fetch = (
                self.cfg.get("credits_monthly_limit", 200),
                self.cfg.get("monthly_credits", 20),
                self.cfg.get("one_time_credits", 0),
            )
        elif "select stripe_customer_id from users" in s:
            customer_id = self.cfg.get("stripe_customer_id")
            self._fetch = (customer_id,) if customer_id else None
        elif "select stripe_checkout_token from users" in s:
            token = self.cfg.get("stripe_checkout_token")
            self._fetch = (token,) if token else None
        elif ("select plan_name from users" in s
              and "subscription_type = 'monthly'" in s):
            self._fetch = (
                self.cfg.get("plan_name", "monthly_pro"),
            ) if self.cfg.get("subscription_type", "monthly") == "monthly" else None
        elif "update users with (updlock, rowlock) set" in s:
            if self.cfg.get("subscription_type") == "monthly" and self.cfg.get("stripe_subscription_id"):
                self.rowcount = 0
            elif self.cfg.get("checkout_reserved"):
                self.rowcount = 0
            else:
                self.cfg["checkout_reserved"] = True
                self.cfg["stripe_checkout_token"] = params[0]
                self.rowcount = 1
        elif ("stripe_checkout_token = null" in s
              and "stripe_checkout_expires_at = null" in s
              and "stripe_checkout_token = ?" in s):
            if self.cfg.get("stripe_checkout_token") == params[1]:
                self.cfg["checkout_reserved"] = False
                self.cfg["stripe_checkout_token"] = None
                self.rowcount = 1
        elif "insert into processed_stripe_events" in s:
            self.rowcount = self.cfg.get("event_claim_rowcount", 1)
        elif "where stripe_subscription_id = ?" in s and "update users set" in s:
            self.rowcount = self.cfg.get("subscription_update_rowcount", 1)
        elif "stripe_checkout_token   = null" in s and "update users set" in s:
            self.rowcount = self.cfg.get("monthly_activation_rowcount", 1)
        elif ("subscription_type = 'one_time'" in s
              and "one_time_credits_remaining" in s
              and "update users set" in s):
            self.rowcount = self.cfg.get("one_time_payment_rowcount", 1)
        # subscription_status: plan/type/credits/quota/renewed/failed/cancel_at
        elif "select subscription_plan, subscription_type" in s:
            self._fetch = self.cfg.get(
                "sub_status_row",
                ("monthly_pro", "monthly", 120, 200, None, None, None, 40, 120),
            )
        elif "select purchase_type, plan_key from pending_purchases" in s:
            self._fetch = self.cfg.get("pending_row")   # None unless a test sets one
        elif "count(*) from jobs where user_id" in s:
            self._fetch = (self.cfg.get("user_count", 0),)
        elif "count(*) from jobs" in s and "user_id" not in s and "created_at" in s:
            self._fetch = (self.cfg.get("global_count", 0),)
        elif "insert into jobs" in s:
            self._fetch = (self.cfg.get("new_job_id", 999),)
        # Transactional outbox row (written in reserve_job_slot / start_training /
        # _finish_training's txn) — OUTPUT INSERTED.outbox_id, so fetchone must return an id.
        elif "insert into outbox" in s:
            self._fetch = (self.cfg.get("new_outbox_id", 12345),)
        elif "insert into processed_stripe_events" in s:
            self.rowcount = self.cfg.get("claim_event_rowcount", 1)
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
                  gl.clear_dispatch_pending,
                  gl.recent_dispatch_pending, qt.trigger_container_job,
                  qt.count_active_job_executions, qt.find_execution_for_job, qc.enqueue_job):
            m.reset_mock(); m.side_effect = None
        gl.acquire_dispatch_lease.return_value = "owner-1"
        gl.recent_dispatch_pending.return_value = False
        qt.trigger_container_job.return_value = "exec-123"
        qt.count_active_job_executions.return_value = 0
        qt.find_execution_for_job.return_value = None
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
        gl = sys.modules["shared.gpu_lease"]
        qt = sys.modules["shared.queue_trigger"]
        self._cfg["job_row"] = ("queued", "exec-existing")  # has exec id -> skip
        function_app.process_inference_job(_QueueMessage({"job_id": "1", "user_id": "u"}))
        qt.trigger_container_job.assert_not_called()
        gl.clear_dispatch_pending.assert_called_once_with("owner-1")

    # 3) happy path -> starts exactly once, records execution id, releases lease
    def test_happy_path_starts_once(self):
        gl = sys.modules["shared.gpu_lease"]
        qt = sys.modules["shared.queue_trigger"]
        function_app.process_inference_job(_QueueMessage({"job_id": "1", "user_id": "u"}))
        qt.trigger_container_job.assert_called_once()
        gl.mark_dispatched.assert_called_once()
        gl.clear_dispatch_pending.assert_called_once_with("owner-1")
        gl.release_dispatch_lease.assert_called_once()
        # #5A: the queued->dispatching claim stamps dispatched_at, so the reaper can measure
        # the processing deadline from GPU-run start instead of submit time.
        self.assertTrue(any("dispatched_at = getutcdate()" in s.lower()
                            for s, _ in self._cfg["executed"]))

    def test_cold_start_is_reserved_before_azure_start(self):
        gl = sys.modules["shared.gpu_lease"]
        qt = sys.modules["shared.queue_trigger"]

        def start_only_after_reservation(*_args, **_kwargs):
            gl.mark_dispatched.assert_called_once_with("owner-1")
            return "exec-123"

        qt.trigger_container_job.side_effect = start_only_after_reservation
        function_app.process_inference_job(
            _QueueMessage({"job_id": "1", "user_id": "u"})
        )
        qt.trigger_container_job.assert_called_once()

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
        sys.modules["shared.gpu_lease"].clear_dispatch_pending.assert_called_once_with(
            "owner-1")
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

    def test_retry_after_start_commit_gap_recovers_execution_id(self):
        qt = sys.modules["shared.queue_trigger"]
        self._cfg["job_row"] = ("dispatching", None)
        qt.find_execution_for_job.return_value = "exec-recovered"
        function_app.process_inference_job(_QueueMessage({"job_id": "1", "user_id": "u"}))
        qt.trigger_container_job.assert_not_called()
        self.assertTrue(any(
            "set external_execution_id" in sql.lower() and params[0] == "exec-recovered"
            for sql, params in self._cfg["executed"]
        ))

    def test_malformed_message_is_not_silently_acknowledged(self):
        with self.assertRaises(ValueError):
            function_app.process_inference_job(_QueueMessage({"user_id": "u"}))

    def test_poison_message_without_job_id_is_not_dropped(self):
        with self.assertRaises(ValueError):
            function_app.handle_poison_job(_QueueMessage({"user_id": "u"}, dequeue_count=3))

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
            "background_ids": ["business_suit.light_gray_studio"],
        }
        return r

    def test_malformed_json_returns_400(self):
        req = self._req()
        req.get_json = mock.Mock(side_effect=ValueError("bad JSON"))
        resp = function_app.submit_job(req)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("invalid JSON body", resp.body)

    def test_oversized_age_range_is_rejected_before_database_access(self):
        req = self._req()
        body = req.get_json()
        body["age_range"] = "2" * (function_app.MAX_PROFILE_ATTRIBUTE_CHARS + 1)
        req.get_json = lambda: body
        self._cfg["executed"] = []

        resp = function_app.submit_job(req)

        self.assertEqual(resp.status_code, 400)
        self.assertIn("age_range", resp.body)
        self.assertEqual(self._cfg["executed"], [])

    def test_oversized_hair_color_is_rejected_before_database_access(self):
        req = self._req()
        body = req.get_json()
        body["hair_color"] = "x" * (function_app.MAX_PROFILE_ATTRIBUTE_CHARS + 1)
        req.get_json = lambda: body
        self._cfg["executed"] = []

        resp = function_app.submit_job(req)

        self.assertEqual(resp.status_code, 400)
        self.assertIn("hair_color", resp.body)
        self.assertEqual(self._cfg["executed"], [])

    def test_non_string_profile_attributes_are_rejected(self):
        req = self._req()
        body = req.get_json()
        body["age_range"] = ["25-29"]
        req.get_json = lambda: body
        self._cfg["executed"] = []

        resp = function_app.submit_job(req)

        self.assertEqual(resp.status_code, 400)
        self.assertIn("must be strings", resp.body)
        self.assertEqual(self._cfg["executed"], [])


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

    def test_daily_caps_exclude_failed_and_waiting_lora_statuses(self):
        self._cfg.update(user_count=0, global_count=0)
        resp = function_app.submit_job(self._req())
        self.assertEqual(resp.status_code, 202)

        cap_queries = [
            sql.lower() for sql, _ in self._cfg["executed"]
            if "count(*) from jobs" in sql.lower() and "created_at" in sql.lower()
        ]
        self.assertEqual(len(cap_queries), 2)
        for sql in cap_queries:
            self.assertIn(
                "status in ('queued', 'dispatching', 'processing', 'completed')", sql)
            self.assertNotIn("waiting_lora", sql)
            self.assertNotIn("failed", sql)

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


class AcceptTermsTests(unittest.TestCase):
    def test_uses_server_utc_time_and_ignores_client_timestamp(self):
        server_time = datetime(2026, 8, 3, 12, 34, 56, tzinfo=timezone.utc)
        cfg = {"terms_accepted_at": server_time}
        req = _HttpRequest()
        req.headers = {"Authorization": "Bearer t"}
        req.get_json = mock.Mock(return_value={"accepted_at": "1999-01-01T00:00:00Z"})

        with mock.patch.object(function_app, "get_db", return_value=FakeConn(cfg)):
            resp = function_app.accept_terms(req)

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(req.get_json.called)
        update_sql, update_params = cfg["executed"][0]
        self.assertIn("terms_accepted_at = GETUTCDATE()", update_sql)
        self.assertEqual(update_params, ("user-1",))
        self.assertNotIn("1999", resp.body)


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
            "attire_ids": ["business_suit.navy_pantsuit"],
            "background_ids": ["business_suit.light_gray_studio"],
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

    def test_training_finishes_between_gate_and_reservation_queues_job(self):
        """Regression: the early handler read says training, but completion wins the
        race before the atomic reservation. The locked re-read must choose queued and
        write an outbox message, never insert an orphaned waiting_lora row."""
        self._cfg["lora_status"] = "training"              # early, stale read
        self._cfg["reservation_lora_status"] = "ready"     # locked transactional read
        resp = function_app.submit_job(self._req())
        self.assertEqual(resp.status_code, 202)
        self.assertIn('"status": "queued"', resp.body)
        inserts = [p for s, p in self._cfg["executed"] if "insert into jobs" in s.lower()]
        self.assertEqual(inserts[0][1], "queued")
        # Merged reserve_job_slot reads lora_status in its OWN UPDLOCK/HOLDLOCK query
        # (split from the credit-balance read) — behaviour is identical, query text changed.
        locked_reads = [s.lower() for s, _ in self._cfg["executed"]
                        if "select lora_status" in s.lower()]
        self.assertTrue(any("updlock" in s and "holdlock" in s for s in locked_reads))
        sys.modules["shared.queue_client"]._send.assert_called_once()

    def test_training_failure_between_gate_and_reservation_does_not_charge(self):
        self._cfg["lora_status"] = "training"              # early, stale read
        self._cfg["reservation_lora_status"] = "failed"    # locked transactional read
        resp = function_app.submit_job(self._req())
        self.assertEqual(resp.status_code, 409)
        sqls = [s.lower() for s, _ in self._cfg["executed"]]
        self.assertFalse(any("insert into jobs" in s for s in sqls))
        self.assertFalse(any("credits_remaining -" in s for s in sqls))
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

    def test_failed_paid_retrain_refunds_original_credit_buckets_once(self):
        self._cfg["training_credit_costs"] = (4, 6)
        with mock.patch.object(function_app, "_identity_adapter_exists", return_value=True):
            function_app._finish_training("t-1", "user-1", ok=False, error="OOM")

        bucket_refunds = [
            params for sql, params in self._cfg["executed"]
            if "monthly_credits_remaining = monthly_credits_remaining + ?" in sql.lower()
        ]
        self.assertEqual(bucket_refunds, [(4, 6, 10, "user-1")])
        ledger_rows = [
            params for sql, params in self._cfg["executed"]
            if "insert into credit_transactions" in sql.lower()
        ]
        self.assertIn(("user-1", 10, "retrain_refund", None), ledger_rows)

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

    def test_monthly_session_minimum_is_5(self):
        # Step 5/#6: a monthly generation session is at least 5 images.
        from shared import plans
        for key in ("monthly_basic", "monthly_pro", "monthly_expert"):
            self.assertEqual(plans.PLANS[key].min_session_images, 5)


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

    def test_status_emits_frontend_field_aliases(self):
        # The frontend reads `monthly_quota` and `renewal_date`; the backend's canonical names
        # are `credits_monthly_limit` and `next_renewal`. Emitting BOTH is what stops the UI
        # rendering a blank quota ("X of  credits") and a missing renewal date.
        self._cfg["sub_status_row"] = (
            "monthly_pro", "monthly", 120, 200, None, None, None, 40, 120,
        )
        resp = function_app.subscription_status(self._req())
        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.body)
        self.assertEqual(body["monthly_quota"], 200)                        # what the UI shows
        self.assertEqual(body["monthly_quota"], body["credits_monthly_limit"])
        self.assertEqual(body["renewal_date"], body["next_renewal"])        # alias present
        self.assertEqual(body["one_time_credits_remaining"], 40)
        self.assertEqual(body["add_on_credits_remaining"], 40)
        self.assertEqual(body["monthly_credits_remaining"], 120)

    def test_one_time_status_hides_stale_monthly_fields(self):
        self._cfg["sub_status_row"] = (
            "basic", "one_time", 230, 20,
            function_app.datetime(2026, 8, 10), None, None, 250, 200,
        )

        response = function_app.subscription_status(self._req())
        body = json.loads(response.body)

        self.assertEqual(body["credits_remaining"], 250)
        self.assertEqual(body["one_time_credits_remaining"], 250)
        self.assertEqual(body["monthly_credits_remaining"], 0)
        self.assertIsNone(body["credits_monthly_limit"])
        self.assertIsNone(body["monthly_quota"])
        self.assertIsNone(body["subscription_renewed_at"])

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


class SubscriptionReliabilityTests(unittest.TestCase):
    def setUp(self):
        sys.modules["shared.auth"].validate_token.return_value = {
            "oid": "user-1",
            "email": "user@example.com",
        }
        sys.modules["shared.auth"].get_user_id.return_value = "user-1"
        self._cfg = {}
        self._get_db = mock.patch.object(
            function_app, "get_db", side_effect=lambda: FakeConn(self._cfg))
        self._new_connection = mock.patch.object(
            function_app, "new_connection", side_effect=lambda: FakeConn(self._cfg))
        self._get_db.start()
        self._new_connection.start()

    def tearDown(self):
        self._get_db.stop()
        self._new_connection.stop()

    def _req(self, body=None):
        req = _HttpRequest()
        req.headers = {"Authorization": "Bearer token"}
        req.get_json = lambda: body or {}
        return req

    def test_b1_billing_portal_returns_stripe_url(self):
        self._cfg["stripe_customer_id"] = "cus_123"
        with mock.patch.object(
            function_app,
            "create_billing_portal",
            return_value={"url": "https://billing.stripe.test/session"},
        ) as create_portal:
            response = function_app.create_subscription_portal(
                self._req({"return_url": "http://localhost:5173/billing"}))

        self.assertEqual(response.status_code, 200)
        self.assertIn("https://billing.stripe.test/session", response.body)
        create_portal.assert_called_once_with(
            "cus_123", "http://localhost:5173/billing", "payment_method_update")

    def test_billing_portal_returns_after_payment_method_update(self):
        from shared import stripe_client

        with mock.patch.object(
            stripe_client, "_post", return_value={"url": "https://billing.stripe.test/session"}
        ) as post:
            stripe_client.create_billing_portal(
                "cus_123", "https://bettersnap.ai/billing", "payment_method_update")

        post.assert_called_once_with("billing_portal/sessions", {
            "customer": "cus_123",
            "return_url": "https://bettersnap.ai/billing",
            "flow_data[type]": "payment_method_update",
            "flow_data[after_completion][type]": "redirect",
            "flow_data[after_completion][redirect][return_url]":
                "https://bettersnap.ai/billing",
        })

    def test_full_billing_portal_shows_saved_methods_and_invoices(self):
        from shared import stripe_client

        with mock.patch.object(stripe_client, "_post", return_value={"url": "portal"}) as post:
            stripe_client.create_billing_portal(
                "cus_123", "https://bettersnap.ai/billing", "manage")

        post.assert_called_once_with("billing_portal/sessions", {
            "customer": "cus_123",
            "return_url": "https://bettersnap.ai/billing",
        })

    def test_b1_payment_failure_is_recorded(self):
        function_app._handle_payment_failed(
            {"subscription": "sub_123"}, "evt_payment_failed")

        executed = self._cfg["executed"]
        self.assertTrue(any(
            "payment_failed_at = coalesce(payment_failed_at, getutcdate())" in sql.lower()
            and params == ("sub_123",)
            for sql, params in executed
        ))

    def test_failed_payment_grace_removes_only_monthly_credits(self):
        function_app.failed_payment_grace_cleanup(mock.Mock())

        cleanup_sql, params = next(
            (sql, params) for sql, params in self._cfg["executed"]
            if "payment_failed_at <= dateadd" in sql.lower()
        )
        normalized = cleanup_sql.lower()
        self.assertIn("monthly_credits_remaining = 0", normalized)
        self.assertIn("credits_remaining = one_time_credits_remaining", normalized)
        self.assertNotIn("one_time_credits_remaining =", normalized)
        self.assertIn("subscription_type = 'monthly'", normalized)
        self.assertEqual(params, (3,))

    def test_local_webhook_secret_overrides_key_vault(self):
        from shared import stripe_client

        payload = b'{"id":"evt_local","type":"invoice.payment_failed"}'
        timestamp = str(int(stripe_client.time.time()))
        signature = stripe_client.hmac.new(
            b"whsec_local",
            f"{timestamp}.{payload.decode()}".encode(),
            stripe_client.hashlib.sha256,
        ).hexdigest()
        with mock.patch.dict(
            os.environ, {"STRIPE_WEBHOOK_SECRET": "whsec_local"}, clear=False
        ), mock.patch.object(stripe_client, "get_secret") as get_secret:
            event = stripe_client.verify_webhook(
                payload, f"t={timestamp},v1={signature}")

        self.assertEqual(event["id"], "evt_local")
        get_secret.assert_not_called()

    def test_webhook_accepts_any_valid_v1_signature_during_rotation(self):
        from shared import stripe_client

        payload = b'{"id":"evt_rotating","type":"invoice.payment_failed"}'
        timestamp = str(int(stripe_client.time.time()))
        valid_signature = stripe_client.hmac.new(
            b"whsec_rotating",
            f"{timestamp}.{payload.decode()}".encode(),
            stripe_client.hashlib.sha256,
        ).hexdigest()
        header = f"t={timestamp},v1={valid_signature},v1=invalid-new-signature"

        with mock.patch.dict(
            os.environ, {"STRIPE_WEBHOOK_SECRET": "whsec_rotating"}, clear=False
        ):
            event = stripe_client.verify_webhook(payload, header)

        self.assertEqual(event["id"], "evt_rotating")

    def test_stripe_upgrade_invoices_proration_and_preserves_cycle_anchor(self):
        from shared import stripe_client

        with mock.patch.object(
            stripe_client, "_price_id", return_value="price_monthly_pro",
        ), mock.patch.object(
            stripe_client, "_post", return_value={"id": "sub_123"},
        ) as post:
            stripe_client.upgrade_subscription("sub_123", "si_123", "pro")

        path, params = post.call_args.args[:2]
        self.assertEqual(path, "subscriptions/sub_123")
        self.assertEqual(params["items[0][id]"], "si_123")
        self.assertEqual(params["items[0][price]"], "price_monthly_pro")
        self.assertEqual(params["proration_behavior"], "always_invoice")
        self.assertEqual(params["payment_behavior"], "pending_if_incomplete")
        self.assertNotIn("billing_cycle_anchor", params)

    def test_cancel_subscription_reads_period_end_from_stripe_items(self):
        self._cfg.update(
            subscription_type="monthly",
            stripe_subscription_id="sub_123",
        )
        stripe_response = {
            "id": "sub_123",
            "cancel_at_period_end": True,
            "items": {
                "data": [
                    {"current_period_end": 1_800_000_000},
                ],
            },
        }
        with mock.patch.object(
            function_app, "cancel_subscription", return_value=stripe_response
        ):
            response = function_app.cancel_user_subscription(self._req())

        self.assertEqual(response.status_code, 200)
        body = json.loads(response.body)
        self.assertIsNotNone(body["cancel_at"])
        self.assertTrue(any(
            "update users set subscription_cancel_at = ?" in sql.lower()
            and params[1] == "user-1"
            for sql, params in self._cfg["executed"]
        ))

    def test_cancel_subscription_refreshes_sparse_stripe_response(self):
        self._cfg.update(
            subscription_type="monthly",
            stripe_subscription_id="sub_123",
        )
        refreshed = {
            "id": "sub_123",
            "cancel_at_period_end": True,
            "items": {"data": [{"current_period_end": 1_800_000_000}]},
        }
        with mock.patch.object(
            function_app, "cancel_subscription", return_value={"id": "sub_123"}
        ), mock.patch.object(
            function_app, "get_subscription", return_value=refreshed
        ) as retrieve:
            response = function_app.cancel_user_subscription(self._req())

        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(json.loads(response.body)["cancel_at"])
        retrieve.assert_called_once_with("sub_123")

    def test_cancel_subscription_falls_back_to_stored_renewal(self):
        renewed_at = function_app.datetime(2026, 8, 3, 17, 32, 29)
        self._cfg.update(
            subscription_type="monthly",
            stripe_subscription_id="sub_123",
            subscription_renewed_at=renewed_at,
        )
        with mock.patch.object(
            function_app, "cancel_subscription", return_value={"id": "sub_123"}
        ), mock.patch.object(
            function_app, "get_subscription", return_value={"id": "sub_123"}
        ):
            response = function_app.cancel_user_subscription(self._req())

        self.assertEqual(response.status_code, 200)
        body = json.loads(response.body)
        self.assertTrue(body["cancel_at"].startswith("2026-09-03T17:32:29"))

    def test_subscription_update_accepts_explicit_cancel_at(self):
        subscription = {
            "id": "sub_123",
            "status": "active",
            "cancel_at": 1_800_000_000,
            "cancel_at_period_end": False,
            "items": {"data": [{
                "price": {"id": "price_pro"},
                "current_period_end": 1_800_000_000,
            }]},
        }
        with mock.patch.object(
            function_app, "monthly_plan_for_price_id", return_value="pro"
        ):
            function_app._handle_subscription_updated(subscription, "evt_cancel_at")

        update_params = next(
            params for sql, params in self._cfg["executed"]
            if "subscription_cancel_at = ?" in sql.lower()
            and "where stripe_subscription_id = ?" in sql.lower()
        )
        self.assertIsNotNone(update_params[3])

    def test_active_subscription_upgrade_adds_only_plan_credit_delta(self):
        self._cfg.update(
            credits_monthly_limit=200,
            monthly_credits=75,
            one_time_credits=250,
        )
        subscription = {
            "id": "sub_123",
            "status": "active",
            "items": {"data": [{"price": {"id": "price_expert"}}]},
            "cancel_at_period_end": False,
        }
        with mock.patch.object(
            function_app, "monthly_plan_for_price_id", return_value="expert"
        ):
            function_app._handle_subscription_updated(subscription, "evt_sub_updated")

        updates = [
            (sql, params) for sql, params in self._cfg["executed"]
            if "credits_monthly_limit" in sql.lower()
            and "update users set" in sql.lower()
            and "where stripe_subscription_id = ?" in sql.lower()
        ]
        self.assertEqual(len(updates), 1)
        sql, params = updates[0]
        self.assertIn("credits_remaining", sql.lower())
        self.assertEqual(params[0], "expert")
        self.assertEqual(params[2], 425)
        self.assertEqual(params[3], 175)
        self.assertEqual(params[4], 300)
        self.assertEqual(params[-1], "sub_123")

    def test_prorated_upgrade_invoice_does_not_reset_spent_credits(self):
        function_app._handle_invoice_paid(
            {
                "subscription": "sub_123",
                "billing_reason": "subscription_update",
            },
            "evt_upgrade_invoice",
        )

        sqls = [sql.lower() for sql, _ in self._cfg["executed"]]
        self.assertFalse(any(
            "monthly_credits_remaining = credits_monthly_limit" in sql
            for sql in sqls
        ))
        self.assertTrue(any("payment_failed_at = null" in sql for sql in sqls))

    def test_upgrade_endpoint_prorates_existing_subscription(self):
        self._cfg.update(
            subscription_plan="basic",
            subscription_type="monthly",
            stripe_subscription_id="sub_123",
        )
        stripe_subscription = {
            "id": "sub_123",
            "items": {"data": [{"id": "si_123"}]},
        }
        with mock.patch.object(
            function_app, "get_subscription", return_value=stripe_subscription,
        ), mock.patch.object(
            function_app,
            "upgrade_subscription",
            return_value={"id": "sub_123", "pending_update": None},
        ) as upgrade:
            response = function_app.upgrade_user_subscription(
                self._req({"plan": "pro"})
            )

        self.assertEqual(response.status_code, 200)
        upgrade.assert_called_once_with("sub_123", "si_123", "pro")

    def test_upgrade_endpoint_rejects_same_or_lower_plan(self):
        self._cfg.update(
            subscription_plan="pro",
            subscription_type="monthly",
            stripe_subscription_id="sub_123",
        )

        response = function_app.upgrade_user_subscription(
            self._req({"plan": "basic"})
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("not_an_upgrade", response.body)

    def test_monthly_checkout_parks_existing_one_time_balance(self):
        function_app._handle_monthly_checkout(
            {
                "metadata": {
                    "user_id": "user-1",
                    "plan": "pro",
                    "checkout_token": "checkout-token",
                },
                "customer": "cus_123",
                "subscription": "sub_123",
            },
            "evt_monthly_checkout",
        )

        activation_sql = next(
            sql for sql, _ in self._cfg["executed"]
            if "monthly_credits_remaining" in sql.lower()
            and "stripe_checkout_token" in sql.lower()
            and "update users set" in sql.lower()
        )
        self.assertIn(
            "one_time_credits_remaining = case when subscription_type = 'monthly'",
            activation_sql.lower(),
        )
        self.assertIn("else credits_remaining end", activation_sql.lower())
        self.assertIn(
            "credits_remaining = ? +",
            activation_sql.lower(),
        )

    def test_repeated_one_time_purchase_rebuilds_total_and_clears_monthly_state(self):
        function_app._handle_onetime_payment(
            {"metadata": {"user_id": "user-1", "plan": "basic"}},
            "evt_one_time_again",
        )

        sql, params = next(
            (sql, params) for sql, params in self._cfg["executed"]
            if "subscription_type = 'one_time'" in sql.lower()
            and "one_time_credits_remaining" in sql.lower()
            and "update users set" in sql.lower()
        )
        normalized = sql.lower()
        self.assertIn(
            "credits_remaining = one_time_credits_remaining + ?",
            normalized,
        )
        self.assertIn("monthly_credits_remaining = 0", normalized)
        self.assertIn("credits_monthly_limit = null", normalized)
        self.assertIn("subscription_renewed_at = null", normalized)
        self.assertEqual(params[2], 30)
        self.assertEqual(params[3], 30)

    def test_subscription_end_clears_monthly_and_restores_one_time_balance(self):
        function_app._handle_subscription_ended(
            {"id": "sub_123", "status": "canceled"},
            "evt_subscription_ended",
        )

        downgrade_sql = next(
            sql for sql, _ in self._cfg["executed"]
            if "monthly_credits_remaining = 0" in sql.lower()
        )
        self.assertIn(
            "credits_remaining = one_time_credits_remaining",
            downgrade_sql.lower(),
        )
        self.assertIn(
            "subscription_type = case when one_time_credits_remaining > 0",
            downgrade_sql.lower(),
        )
        self.assertIn("credits_monthly_limit = null", downgrade_sql.lower())

    def test_invoice_paid_resets_only_monthly_balance(self):
        function_app._handle_invoice_paid(
            {"subscription": "sub_123"},
            "evt_invoice_paid",
        )

        reset_sql = next(
            sql for sql, _ in self._cfg["executed"]
            if "monthly_credits_remaining = credits_monthly_limit" in sql.lower()
        )
        self.assertNotIn("one_time_credits_remaining =", reset_sql.lower())
        self.assertIn(
            "credits_remaining = credits_monthly_limit + one_time_credits_remaining",
            reset_sql.lower(),
        )

    def test_pro_addon_grants_250_separate_addon_credits(self):
        self._cfg["subscription_type"] = "monthly"
        function_app._handle_topup(
            {
                "metadata": {
                    "user_id": "user-1",
                    "plan": "pro",
                },
            },
            "evt_pro_addon",
        )

        addon_updates = [
            (sql, params) for sql, params in self._cfg["executed"]
            if "one_time_credits_remaining = one_time_credits_remaining + ?" in sql.lower()
        ]
        self.assertEqual(len(addon_updates), 1)
        sql, params = addon_updates[0]
        self.assertIn("credits_remaining = credits_remaining + ?", sql.lower())
        self.assertNotIn("monthly_credits_remaining", sql.lower())
        self.assertEqual(params[0], 250)
        self.assertEqual(params[1], 250)
        self.assertEqual(params[2], "pro")
        self.assertEqual(params[3], "monthly_pro")

    def test_monthly_job_spends_monthly_then_addon_credits(self):
        from shared.job_reservation import reserve_job_slot

        self._cfg.update(
            credits=10,
            one_time_credits=15,
            new_job_id=123,
        )
        with mock.patch(
            "shared.job_reservation.new_connection",
            side_effect=lambda: FakeConn(self._cfg),
        ):
            result = reserve_job_slot(
                "user-1",
                "input.jpg",
                json.dumps({"credit_cost": 25}),
                per_user_cap=10,
                global_cap=100,
                credit_cost=25,
                source_type="monthly",
                image_count=5,
                credits_per_image=5,
            )

        self.assertTrue(result.ok)
        debit = next(
            params for sql, params in self._cfg["executed"]
            if "monthly_credits_remaining = monthly_credits_remaining - ?" in sql.lower()
        )
        self.assertEqual(debit[:4], (10, 15, 10, 15))
        debit_sql = next(
            sql for sql, _ in self._cfg["executed"]
            if "monthly_credits_remaining = monthly_credits_remaining - ?" in sql.lower()
        )
        self.assertIn("credits_remaining = credits_remaining - ? - ?", debit_sql.lower())

        inserted_params = next(
            params for sql, params in self._cfg["executed"]
            if "insert into jobs" in sql.lower()
        )
        stored_job_params = json.loads(inserted_params[3])
        self.assertEqual(stored_job_params["monthly_credit_cost"], 10)
        self.assertEqual(stored_job_params["one_time_credit_cost"], 15)

    def test_b3_only_one_monthly_checkout_reservation_is_allowed(self):
        first_token, first_error = function_app._reserve_monthly_checkout("user-1")
        second_token, second_error = function_app._reserve_monthly_checkout("user-1")

        self.assertIsNotNone(first_token)
        self.assertIsNone(first_error)
        self.assertIsNone(second_token)
        self.assertEqual(second_error, "checkout_in_progress")

    def test_b3_provider_failure_releases_checkout_reservation(self):
        with mock.patch.object(
            function_app,
            "create_monthly_checkout",
            side_effect=RuntimeError("Stripe unavailable"),
        ):
            response = function_app.create_subscription(
                self._req({"plan": "pro", "type": "monthly"}))

        self.assertEqual(response.status_code, 502)
        self.assertFalse(self._cfg["checkout_reserved"])

    def test_b3_canceled_completed_checkout_releases_stale_reservation(self):
        self._cfg.update(
            subscription_type="one_time",
            stripe_subscription_id=None,
            checkout_reserved=True,
            stripe_checkout_token="stale-token",
        )
        session = {
            "status": "complete",
            "subscription": "sub_canceled",
            "metadata": {"checkout_token": "stale-token"},
        }
        with mock.patch.object(
            function_app, "find_checkout_session_by_token", return_value=session
        ), mock.patch.object(
            function_app, "get_subscription", return_value={"status": "canceled"}
        ), mock.patch.object(
            function_app, "create_monthly_checkout",
            return_value={"url": "https://checkout.stripe.test/new", "id": "cs_new"},
        ):
            response = function_app.create_subscription(
                self._req({"plan": "pro", "type": "monthly"})
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("https://checkout.stripe.test/new", response.body)
        self.assertNotEqual(self._cfg["stripe_checkout_token"], "stale-token")

    def test_expired_checkout_webhook_releases_reservation(self):
        with mock.patch.object(function_app, "verify_webhook", return_value={
            "id": "evt_expired",
            "type": "checkout.session.expired",
            "data": {"object": {"metadata": {
                "user_id": "user-1",
                "checkout_token": "expired-token",
            }}},
        }), mock.patch.object(function_app, "_release_monthly_checkout") as release:
            request = self._req()
            request.headers = {"Stripe-Signature": "sig"}
            request.get_body = lambda: b"{}"
            response = function_app.stripe_webhook(request)

        self.assertEqual(response.status_code, 200)
        release.assert_called_once_with("user-1", "expired-token")

    def test_unpaid_completed_checkout_does_not_grant(self):
        session = {
            "id": "cs_unpaid",
            "payment_status": "unpaid",
            "metadata": {"payment_type": "one_time"},
        }
        with mock.patch.object(function_app, "verify_webhook", return_value={
            "id": "evt_unpaid",
            "type": "checkout.session.completed",
            "data": {"object": session},
        }), mock.patch.object(function_app, "_handle_onetime_payment") as grant:
            request = self._req()
            request.headers = {"Stripe-Signature": "sig"}
            request.get_body = lambda: b"{}"
            response = function_app.stripe_webhook(request)

        self.assertEqual(response.status_code, 200)
        grant.assert_not_called()

    def test_paid_checkout_routes_all_purchase_types_with_session_claim(self):
        handlers = {
            "one_time": "_handle_onetime_payment",
            "monthly": "_handle_monthly_checkout",
            "topup": "_handle_topup",
        }
        for payment_type, handler_name in handlers.items():
            with self.subTest(payment_type=payment_type):
                session = {
                    "id": f"cs_{payment_type}",
                    "payment_status": "paid",
                    "metadata": {"payment_type": payment_type},
                }
                with mock.patch.object(function_app, "verify_webhook", return_value={
                    "id": f"evt_{payment_type}",
                    "type": "checkout.session.completed",
                    "data": {"object": session},
                }), mock.patch.object(function_app, handler_name) as grant:
                    request = self._req()
                    request.headers = {"Stripe-Signature": "sig"}
                    request.get_body = lambda: b"{}"
                    response = function_app.stripe_webhook(request)

                self.assertEqual(response.status_code, 200)
                grant.assert_called_once_with(
                    session, f"checkout_session:cs_{payment_type}"
                )

    def test_async_payment_success_fulfills_paid_checkout(self):
        session = {
            "id": "cs_delayed",
            "payment_status": "paid",
            "metadata": {"payment_type": "one_time"},
        }
        with mock.patch.object(function_app, "verify_webhook", return_value={
            "id": "evt_async_paid",
            "type": "checkout.session.async_payment_succeeded",
            "data": {"object": session},
        }), mock.patch.object(function_app, "_handle_onetime_payment") as grant:
            request = self._req()
            request.headers = {"Stripe-Signature": "sig"}
            request.get_body = lambda: b"{}"
            response = function_app.stripe_webhook(request)

        self.assertEqual(response.status_code, 200)
        grant.assert_called_once_with(session, "checkout_session:cs_delayed")

    def test_async_monthly_payment_failure_releases_reservation(self):
        session = {
            "id": "cs_failed",
            "payment_status": "unpaid",
            "metadata": {
                "payment_type": "monthly",
                "user_id": "user-1",
                "checkout_token": "checkout-token",
            },
        }
        with mock.patch.object(function_app, "verify_webhook", return_value={
            "id": "evt_async_failed",
            "type": "checkout.session.async_payment_failed",
            "data": {"object": session},
        }), mock.patch.object(function_app, "_release_monthly_checkout") as release:
            request = self._req()
            request.headers = {"Stripe-Signature": "sig"}
            request.get_body = lambda: b"{}"
            response = function_app.stripe_webhook(request)

        self.assertEqual(response.status_code, 200)
        release.assert_called_once_with("user-1", "checkout-token")

    def test_invoice_payment_succeeded_does_not_grant_renewal_credits(self):
        with mock.patch.object(function_app, "verify_webhook", return_value={
            "id": "evt_payment_succeeded",
            "type": "invoice.payment_succeeded",
            "data": {"object": {"subscription": "sub_123"}},
        }), mock.patch.object(function_app, "_handle_invoice_paid") as grant:
            request = self._req()
            request.headers = {"Stripe-Signature": "sig"}
            request.get_body = lambda: b"{}"
            response = function_app.stripe_webhook(request)

        self.assertEqual(response.status_code, 200)
        grant.assert_not_called()

    def test_invoice_paid_is_the_single_renewal_grant_event(self):
        invoice = {"subscription": "sub_123"}
        with mock.patch.object(function_app, "verify_webhook", return_value={
            "id": "evt_invoice_paid",
            "type": "invoice.paid",
            "data": {"object": invoice},
        }), mock.patch.object(function_app, "_handle_invoice_paid") as grant:
            request = self._req()
            request.headers = {"Stripe-Signature": "sig"}
            request.get_body = lambda: b"{}"
            response = function_app.stripe_webhook(request)

        self.assertEqual(response.status_code, 200)
        grant.assert_called_once_with(invoice, "evt_invoice_paid")


class RetentionBlobCaseTests(unittest.TestCase):
    """Blob prefixes are CASE-SENSITIVE. Ids come from SQL uppercase but the pipeline writes
    lowercase paths, so matching only the SQL casing deleted NOTHING — retention reported
    success while every photo and adapter stayed in storage. _delete_blobs must try both."""

    def _fake_container(self, names):
        listed = []

        class FakeContainer:
            def list_blobs(self, name_starts_with=None):
                listed.append(name_starts_with)
                return [types.SimpleNamespace(name=n)
                        for n in names if n.startswith(name_starts_with or "")]

            def delete_blob(self, name):
                pass

        class FakeSvc:
            def get_container_client(self, _):
                return FakeContainer()

        return FakeSvc(), listed

    def test_uppercase_prefix_still_deletes_lowercase_blobs(self):
        # Blobs written lowercase; caller passes the UPPERCASE id straight from SQL.
        svc, listed = self._fake_container(["908a8f2a-dead-beef/input/img0.jpg"])
        with mock.patch.object(function_app, "get_blob_client", return_value=svc):
            n = function_app._delete_blobs("inputs", "908A8F2A-DEAD-BEEF/")
        self.assertEqual(n, 1, "uppercase prefix must still reach the lowercase blobs")
        self.assertIn("908a8f2a-dead-beef/", listed)   # lowercase variant was attempted

    def test_already_lowercase_prefix_is_not_listed_twice(self):
        svc, listed = self._fake_container(["abc/input/img0.jpg"])
        with mock.patch.object(function_app, "get_blob_client", return_value=svc):
            n = function_app._delete_blobs("inputs", "abc/")
        self.assertEqual(n, 1)
        self.assertEqual(len(listed), 1, "no duplicate listing when the prefix is already lower")


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
        # The DEADLINE-based scans (those with a DATEADD window) must age from
        # COALESCE(dispatched_at, created_at). The execution-status early-reconcile scan is
        # deliberately deadline-free (it reconciles on terminal ACA status, not elapsed time),
        # so it is excluded from this check.
        deadline = [s for s in proc + disp if "dateadd" in s]
        self.assertTrue(deadline, "reaper had no deadline-based scan")
        self.assertTrue(all("coalesce(dispatched_at, created_at)" in s for s in deadline))
        # ...and must NOT age from the bare submit-time column.
        self.assertFalse(any("and created_at < dateadd" in s for s in sqls),
                         "reaper still measures from created_at (submit time)")

    def test_reaper_recovers_live_null_execution_instead_of_failing_it(self):
        cfg = {"reaper_dispatching": [("job-7", None)]}
        qt = sys.modules["shared.queue_trigger"]
        qt.find_execution_for_job.reset_mock()
        qt.find_execution_for_job.return_value = "exec-live"
        with mock.patch.object(function_app, "new_connection",
                               side_effect=lambda: FakeConn(cfg)):
            function_app.reaper(None)

        # The write is now GUARDED (status + external_execution_id IS NULL), so it carries the
        # expected status as a third parameter. That guard is what stops a late worker from
        # overwriting a newer attempt's execution id.
        self.assertTrue(any(
            "set external_execution_id" in sql.lower()
            and params[:2] == ("exec-live", "job-7")
            and "external_execution_id is null" in sql.lower()
            for sql, params in cfg["executed"]
        ))
        self.assertFalse(any("update jobs set status = 'failed'" in sql.lower()
                             for sql, _ in cfg["executed"]))

    def test_reaper_recovery_excludes_already_reconciled_executions(self):
        """With bounded retries a job can have several executions. Recovery must never
        re-adopt one that was already reconciled — that pins the row to a spent verdict."""
        cfg = {"reaper_dispatching": [("job-7", None)],
               "provisioning_execution_ids": json.dumps(["exec-spent"])}
        qt = sys.modules["shared.queue_trigger"]
        qt.find_execution_for_job.reset_mock()
        qt.find_execution_for_job.return_value = None   # only the spent one exists
        with mock.patch.object(function_app, "new_connection",
                               side_effect=lambda: FakeConn(cfg)):
            function_app.reaper(None)
        _, kwargs = qt.find_execution_for_job.call_args
        self.assertEqual(kwargs.get("exclude"), {"exec-spent"})

    def test_reaper_terminalizes_a_corrupt_history_row_at_the_ceiling(self):
        """FINITE fail-closed: a corrupt history is never reset and never adopted, but the
        row must not be skipped forever either."""
        cfg = {"reaper_dispatching": [("job-9", None)],
               "provisioning_execution_ids": "{not json",
               "corrupt_age_s": 10 ** 6}
        qt = sys.modules["shared.queue_trigger"]
        qt.find_execution_for_job.reset_mock()
        with mock.patch.object(function_app, "new_connection",
                               side_effect=lambda: FakeConn(cfg)):
            function_app.reaper(None)
        qt.find_execution_for_job.assert_not_called()
        self.assertTrue(any("update jobs set status = 'failed'" in sql.lower()
                            for sql, _ in cfg["executed"]),
                        "the corrupt-history row must terminalize at the ceiling")

    def test_reaper_observes_a_corrupt_history_row_inside_the_ceiling(self):
        cfg = {"reaper_dispatching": [("job-9", None)],
               "provisioning_execution_ids": "{not json",
               "corrupt_age_s": 1}
        qt = sys.modules["shared.queue_trigger"]
        qt.find_execution_for_job.reset_mock()
        with mock.patch.object(function_app, "new_connection",
                               side_effect=lambda: FakeConn(cfg)):
            function_app.reaper(None)
        qt.find_execution_for_job.assert_not_called()
        self.assertFalse(any("update jobs set status = 'failed'" in sql.lower()
                             for sql, _ in cfg["executed"]),
                         "inside the repair window nothing may be terminalized")


class RegistrationTests(unittest.TestCase):
    """M1 regression: POST /users/register must REPORT the same credit balance it
    GRANTS and PERSISTS (REGISTRATION_CREDITS) — never a hardcoded literal. Fails if
    the 201 response value diverges from the granted/persisted amount (e.g. the old
    hardcoded 20)."""

    class _Req:
        def __init__(self, token="Bearer faketoken"):
            self.headers = {"Authorization": token}

    def setUp(self):
        self._cfg = {}
        # register_user uses get_db() (not new_connection) + validate_token().
        function_app.get_db.side_effect = None
        function_app.get_db.return_value = FakeConn(self._cfg)
        function_app.validate_token.side_effect = None
        function_app.validate_token.return_value = {
            "oid": "reg-user-1", "email": "new@example.com", "name": "New User",
        }

    def test_register_response_matches_granted_and_persisted(self):
        resp = function_app.register_user(self._Req())

        # Fresh insert (the 201 path), not the "already exists" 200 path.
        self.assertEqual(resp.status_code, 201)

        # GRANTED / PERSISTED: the users INSERT's credits_remaining arg
        # (user_id, email, name, credits_remaining, plan_name).
        inserts = [p for s, p in self._cfg["executed"] if "insert into users" in s.lower()]
        self.assertEqual(len(inserts), 1, "expected exactly one users INSERT")
        granted = inserts[0][3]
        self.assertEqual(granted, function_app.REGISTRATION_CREDITS)
        self.assertEqual(inserts[0][5], function_app.REGISTRATION_CREDITS)

        # RETURNED: the 201 body must equal the constant AND the granted amount.
        returned = json.loads(resp.body)["credits"]
        self.assertEqual(returned, function_app.REGISTRATION_CREDITS)
        self.assertEqual(
            returned, granted,
            "register response 'credits' must match the granted/persisted balance",
        )


class IdentityMigrationTests(unittest.TestCase):
    """Supabase->Entra self-heal (_migrate_identity) must repoint EVERY user-scoped table to the
    new oid before deleting the old users row, or a returning user strands data (and, for an
    FK-enforced table, the DELETE would FK-block and 500 the whole self-heal). This locks in:
      1. all enforced-FK child tables are repointed unconditionally,
      2. the newer, no-FK tables (lora_trainings/pending_purchases/admin_user_notes) are repointed
         under an OBJECT_ID existence guard (so a partial env without them still migrates),
      3. DELETE FROM users runs LAST (FK-safe order), and commit fires exactly once."""

    class _Cursor:
        def __init__(self):
            self.executed = []   # list of (normalized_sql, params)

        def execute(self, sql, *params):
            self.executed.append((" ".join(sql.split()), params))

    class _Conn:
        def __init__(self):
            self.commits = 0

        def commit(self):
            self.commits += 1

    def _run(self):
        conn, cur = self._Conn(), self._Cursor()
        function_app._migrate_identity(conn, cur, "old-id-123", "new-oid-456", "e@x.com")
        return conn, cur

    def test_enforced_children_repointed_to_new_oid(self):
        _, cur = self._run()
        for tbl in function_app._USER_CHILD_TABLES:
            hits = [(s, p) for s, p in cur.executed
                    if f"update {tbl} set user_id" in s.lower()]
            self.assertEqual(len(hits), 1, f"{tbl} must be repointed exactly once")
            self.assertEqual(hits[0][1], ("new-oid-456", "old-id-123"))

    def test_optional_children_repointed_under_existence_guard(self):
        _, cur = self._run()
        for tbl in function_app._USER_CHILD_TABLES_OPTIONAL:
            hits = [(s, p) for s, p in cur.executed
                    if f"update dbo.{tbl} set user_id" in s.lower()]
            self.assertEqual(len(hits), 1, f"{tbl} must be repointed exactly once")
            # Guarded so a missing table can't fail the migration.
            self.assertIn(f"object_id('dbo.{tbl}', 'u') is not null", hits[0][0].lower())
            self.assertEqual(hits[0][1], ("new-oid-456", "old-id-123"))

    def test_delete_users_runs_after_all_repoints(self):
        _, cur = self._run()
        order = [s.lower() for s, _ in cur.executed]
        del_idx = next(i for i, s in enumerate(order) if s.startswith("delete from users"))
        last_update = max(i for i, s in enumerate(order) if "set user_id" in s)
        self.assertGreater(del_idx, last_update,
                           "DELETE FROM users must run after every child repoint (FK-safe)")

    def test_commits_exactly_once(self):
        conn, _ = self._run()
        self.assertEqual(conn.commits, 1)


class PublicCatalogPrivacyTests(unittest.TestCase):
    class Cursor:
        def __init__(self, rows):
            self.rows = rows
            self.sql = ""

        def execute(self, sql, *_params):
            self.sql = " ".join(sql.lower().split())

        def fetchall(self):
            return self.rows

    class Conn:
        def __init__(self, rows):
            self.cur = PublicCatalogPrivacyTests.Cursor(rows)

        def cursor(self):
            return self.cur

    def test_attires_exclude_internal_blob_path(self):
        conn = self.Conn([("suit", "Navy Suit", "professional")])
        with mock.patch.object(function_app, "get_db", return_value=conn):
            response = function_app.get_attires(None)
        item = json.loads(response.body)["attires"][0]
        self.assertNotIn("blob_path", item)
        self.assertNotIn("blob_path", conn.cur.sql)

    def test_backgrounds_exclude_internal_blob_path(self):
        conn = self.Conn([("studio", "Studio", "professional")])
        with mock.patch.object(function_app, "get_db", return_value=conn):
            response = function_app.get_backgrounds(None)
        item = json.loads(response.body)["backgrounds"][0]
        self.assertNotIn("blob_path", item)
        self.assertNotIn("blob_path", conn.cur.sql)


class SubscriptionDowngradeTests(unittest.TestCase):
    def test_ended_subscription_preserves_one_time_credits(self):
        cfg = {}
        with mock.patch.object(function_app, "new_connection",
                               return_value=FakeConn(cfg)):
            function_app._handle_subscription_ended(
                {"id": "sub-ended", "status": "canceled"}, "evt-ended"
            )

        updates = [
            (sql, params) for sql, params in cfg["executed"]
            if "update users set" in sql.lower() and "credits_monthly_limit" in sql.lower()
        ]
        self.assertEqual(len(updates), 1)
        sql, params = updates[0]
        # Separate-balance downgrade: keep any remaining one-time credits (revert to a one_time
        # account) instead of wiping to the trial/registration limit. Only RETENTION_DAYS + the
        # subscription id bind now.
        self.assertIn("credits_remaining = one_time_credits_remaining", sql.lower())
        self.assertIn("monthly_credits_remaining = 0", sql.lower())
        self.assertEqual(params[0], function_app.RETENTION_DAYS)
        self.assertEqual(params[1], "sub-ended")


class StripePaidGrantTests(unittest.TestCase):
    """A paid entitlement must not be acknowledged after a zero-row update."""

    class _Req:
        headers = {"Stripe-Signature": "sig"}

        @staticmethod
        def get_body():
            return b"{}"

    def test_retryable_paid_grant_returns_500(self):
        event = {
            "id": "evt_paid_unregistered",
            "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_paid_unregistered",
                "payment_status": "paid",
                "metadata": {"payment_type": "one_time"},
            }},
        }
        with mock.patch.object(function_app, "verify_webhook", return_value=event), \
             mock.patch.object(
                 function_app,
                 "_handle_onetime_payment",
                 side_effect=function_app.RetryableStripeWebhookError("no user row"),
             ):
            response = function_app.stripe_webhook(self._Req())
        self.assertEqual(response.status_code, 500)

    def test_invoice_zero_row_rolls_back_and_is_retryable(self):
        class Cursor:
            rowcount = 0

            def execute(self, sql, *params):
                self.rowcount = 1 if "processed_stripe_events" in sql else 0
                return self

        class Conn:
            def __init__(self):
                self.cur = Cursor()
                self.committed = False
                self.rolled_back = False

            def cursor(self):
                return self.cur

            def commit(self):
                self.committed = True

            def rollback(self):
                self.rolled_back = True

            def close(self):
                pass

        conn = Conn()
        with mock.patch.object(function_app, "new_connection", return_value=conn):
            with self.assertRaises(function_app.RetryableStripeWebhookError):
                function_app._handle_invoice_paid(
                    {"subscription": "sub_missing"}, "evt_invoice_missing"
                )
        self.assertTrue(conn.rolled_back)
        self.assertFalse(conn.committed)

    def test_renewal_never_overwrites_balance_with_monthly_limit(self):
        class Cursor:
            rowcount = 0

            def __init__(self):
                self.sql = []

            def execute(self, sql, *params):
                normalized = " ".join(sql.lower().split())
                self.sql.append(normalized)
                self.rowcount = 1
                return self

            def fetchone(self):
                # Feeds the post-reset renewal-ledger lookup (user_id, credits_monthly_limit).
                return ("u-renew", 100)

        class Conn:
            def __init__(self):
                self.cur = Cursor()
                self.committed = False

            def cursor(self):
                return self.cur

            def commit(self):
                self.committed = True

            def rollback(self):
                pass

            def close(self):
                pass

        conn = Conn()
        with mock.patch.object(function_app, "new_connection", return_value=conn):
            function_app._handle_invoice_paid(
                {"subscription": "sub_topup"}, "evt_renew_topup"
            )

        renewal_sql = next(
            sql for sql in conn.cur.sql
            if "update users set" in sql and "credits_monthly_limit" in sql
        )
        # Separate-balance renewal: reset the monthly allowance to the limit, recompute the
        # combined balance, and PRESERVE one-time/top-up credits (in one_time_credits_remaining)
        # — the same "never wipe top-ups on renewal" invariant, expressed for the split balances.
        self.assertIn(
            "credits_remaining = credits_monthly_limit + one_time_credits_remaining",
            renewal_sql,
        )
        self.assertIn("monthly_credits_remaining = credits_monthly_limit", renewal_sql)
        self.assertNotIn(
            "when credits_remaining > credits_monthly_limit", renewal_sql,
        )
        self.assertTrue(conn.committed)


class RetentionCleanupTests(unittest.TestCase):
    class DueCursor:
        def __init__(self, rows):
            self.rows = rows
            self.sql = ""

        def execute(self, sql, *_params):
            self.sql = " ".join(sql.lower().split())
            return self

        def fetchall(self):
            return self.rows

    class DueConnection:
        def __init__(self, rows):
            self.cur = RetentionCleanupTests.DueCursor(rows)

        def cursor(self):
            return self.cur

    class EligibilityCursor:
        def __init__(self, eligible):
            self.eligible = eligible
            self.executed = []
            self._eligibility_query = False

        def execute(self, sql, *params):
            normalized = " ".join(sql.lower().split())
            self.executed.append((normalized, params))
            self._eligibility_query = normalized.startswith(
                "select user_id from users with (updlock, holdlock)"
            )
            return self

        def fetchone(self):
            return ("user-1",) if self._eligibility_query and self.eligible else None

        def fetchall(self):
            return []

    class EligibilityConnection:
        def __init__(self, eligible):
            self.cur = RetentionCleanupTests.EligibilityCursor(eligible)
            self.committed = False
            self.rolled_back = False
            self.closed = False
            self.autocommit = True

        def cursor(self):
            return self.cur

        def commit(self):
            self.committed = True

        def rollback(self):
            self.rolled_back = True

        def close(self):
            self.closed = True

    def test_due_query_preserves_users_with_paid_credit_buckets(self):
        due_conn = self.DueConnection([])
        with mock.patch.object(function_app, "get_db", return_value=due_conn), \
                mock.patch.object(function_app, "new_connection") as new_connection:
            function_app.retention_cleanup(None)

        self.assertIn("isnull(monthly_credits_remaining, 0) <= 0", due_conn.cur.sql)
        self.assertIn("isnull(one_time_credits_remaining, 0) <= 0", due_conn.cur.sql)
        new_connection.assert_not_called()

    def test_active_job_blocks_blob_deletion(self):
        due_conn = self.DueConnection([("user-1",)])
        eligibility_conn = self.EligibilityConnection(eligible=False)
        with mock.patch.object(function_app, "get_db", return_value=due_conn), \
                mock.patch.object(
                    function_app, "new_connection", return_value=eligibility_conn
                ), mock.patch.object(function_app, "_delete_blobs") as delete_blobs:
            function_app.retention_cleanup(None)

        eligibility_sql = eligibility_conn.cur.executed[0][0]
        self.assertIn("status not in ('completed', 'failed')", eligibility_sql)
        self.assertTrue(eligibility_conn.rolled_back)
        self.assertTrue(eligibility_conn.closed)
        self.assertFalse(eligibility_conn.committed)
        delete_blobs.assert_not_called()


class JobStatusReconcileTests(unittest.TestCase):
    """job_status must reconcile a DEAD execution on read, not wait for the reaper.

    A hard-killed container -- OOM SIGKILL, node eviction, a pre-container provisioning
    failure -- never writes its own status, so the row sat at processing/dispatching until
    the 2-minute reaper timer next fired. The customer watched a spinner for up to two
    minutes after their job was already dead, and their refund waited exactly as long.
    """

    def setUp(self):
        self._db = mock.patch.object(function_app, "get_db").start()
        self._uid = mock.patch.object(
            function_app, "get_user_id", return_value="user-1").start()
        self._mark = mock.patch.object(
            function_app, "_mark_failed", return_value="terminalized").start()
        self.addCleanup(mock.patch.stopall)

    def _row(self, status, exec_id="exec-1", output=None):
        self._db.return_value.cursor.return_value.fetchone.return_value = (
            status, output, exec_id)

    def _call(self):
        req = _HttpRequest()
        req.headers = {"Authorization": "Bearer token"}
        req.route_params = {"job_id": "11111111-1111-1111-1111-111111111111"}
        res = function_app.job_status(req)
        return res.status_code, json.loads(res.body)

    def test_dead_execution_is_failed_and_refunded_on_read(self):
        self._row("processing")
        with mock.patch("shared.queue_trigger.execution_status", return_value="Failed"):
            code, body = self._call()
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "failed")
        self._mark.assert_called_once()

    def test_every_dead_state_counts(self):
        for state in ("Stopped", "Failed", "Degraded", "Cancelled"):
            with self.subTest(state=state):
                self._mark.reset_mock()
                self._row("dispatching")
                with mock.patch("shared.queue_trigger.execution_status",
                                return_value=state):
                    _, body = self._call()
                self.assertEqual(body["status"], "failed")
                self._mark.assert_called_once()

    def test_succeeded_execution_is_never_refunded(self):
        """The container writes 'completed' itself, so a Succeeded execution whose row still
        reads 'processing' is a WRITE RACE. Refunding it would hand the user both a refund
        and the delivered images."""
        self._row("processing")
        with mock.patch("shared.queue_trigger.execution_status", return_value="Succeeded"):
            _, body = self._call()
        self.assertEqual(body["status"], "processing")
        self._mark.assert_not_called()

    def test_running_execution_is_left_alone(self):
        self._row("processing")
        with mock.patch("shared.queue_trigger.execution_status", return_value="Running"):
            _, body = self._call()
        self.assertEqual(body["status"], "processing")
        self._mark.assert_not_called()

    def test_control_plane_error_fails_open(self):
        """A status read must never 500 because an ACA call did. Return the stored status
        and leave the job to the reaper, exactly as before."""
        self._row("processing")
        with mock.patch("shared.queue_trigger.execution_status",
                        side_effect=RuntimeError("ARM throttled")):
            code, body = self._call()
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "processing")
        self._mark.assert_not_called()

    def test_terminal_job_never_calls_the_control_plane(self):
        for status in ("completed", "failed"):
            with self.subTest(status=status):
                self._row(status)
                with mock.patch("shared.queue_trigger.execution_status") as ex:
                    _, body = self._call()
                self.assertEqual(body["status"], status)
                ex.assert_not_called()

    def test_job_without_an_execution_id_never_calls_the_control_plane(self):
        self._row("dispatching", exec_id=None)
        with mock.patch("shared.queue_trigger.execution_status") as ex:
            _, body = self._call()
        self.assertEqual(body["status"], "dispatching")
        ex.assert_not_called()

    def test_reaper_and_job_status_share_one_definition_of_dead(self):
        """Two copies of this set would silently drift, and the drift would be invisible
        until a job class stopped being refunded."""
        source = io.open(
            os.path.join(BACKEND_DIR, "function_app.py"), encoding="utf-8").read()
        self.assertEqual(source.count("_DEAD_EXEC_STATES = {"), 1)
        self.assertEqual(
            function_app._DEAD_EXEC_STATES,
            {"stopped", "failed", "degraded", "cancelled"})

if __name__ == "__main__":
    unittest.main(verbosity=2)
