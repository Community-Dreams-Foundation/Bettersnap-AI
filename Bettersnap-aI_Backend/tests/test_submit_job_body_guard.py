"""B4: /jobs/submit must return a clean 400 (never a 500 stack trace) for a malformed or
non-object body, and for a non-numeric image_count — while a valid object flows into the
existing field/plan validation.

These tests exercise the REAL submit_job handler with the same lightweight offline-stub
pattern as tests/test_dispatch_logic.py, so the suite runs in CI on stdlib alone (no
pyodbc/jwt/azure/torch).

IMPORT-ORDER SAFETY: the offline suites share one process. test_dispatch_logic configures
and asserts its mocks via sys.modules["shared.*"], which only works if function_app is bound
to those same module objects. So this file must NOT create competing stub modules or import
function_app at import time — doing so binds function_app to a DIFFERENT shared.* mock than the
one another suite reconfigures (that split makes e.g. validate_token return an unconfigured
Mock -> ["oid"] raises -> a spurious 401). Instead we DEFER to setUpModule(): by the time it
runs, every test module has been imported, so if another suite already stubbed + imported
function_app we REUSE it untouched; only when this file runs alone do we install our own stubs.
That is why it is safe to list this file before OR after test_dispatch_logic in the same
process (the backend-ci command lists it first on purpose).

The request object is a faithful fake: get_json() == json.loads(body.decode('utf-8')), exactly
what azure-functions 2.1.0 does, so the body-parse path raises the SAME ValueError family
(JSONDecodeError, and UnicodeDecodeError which is also a ValueError) production hits — the
reason `except ValueError` in the handler is sufficient.

Run: python -m unittest tests.test_submit_job_body_guard   (from the backend dir)
"""
import os
import sys
import json
import types
import importlib
import unittest
from unittest import mock

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

# Bound in setUpModule() — never imported at module import time (see IMPORT-ORDER SAFETY).
function_app = None


# ── Faithful fakes (used regardless of which azure.functions stub is active) ──
class _FakeHttpRequest:
    """Faithful to azure-functions 2.1.0: get_json() = json.loads(body.decode('utf-8'))."""
    def __init__(self, body=b"", headers=None):
        self._body = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.headers = headers or {"Authorization": "Bearer test"}

    def get_body(self):
        return self._body

    def get_json(self):
        return json.loads(self._body.decode("utf-8"))


def _req(body):
    if isinstance(body, (dict, list)):
        body = json.dumps(body)
    if isinstance(body, str):
        body = body.encode("utf-8")
    return _FakeHttpRequest(body)


# ── Offline stub harness — installed ONLY when this file runs without another suite having
#    already set function_app up (mirrors tests/test_dispatch_logic.py). ──
def _mod(name, **attrs):
    if name in sys.modules:            # reuse an existing stub; never clobber another suite's
        return sys.modules[name]
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


class _QueueMessage:
    def __init__(self, payload, dequeue_count=1):
        self._body = json.dumps(payload).encode("utf-8")
        self.dequeue_count = dequeue_count

    def get_body(self):
        return self._body


class _NoFaceError(Exception):
    pass


class _DispatchConfigError(Exception):
    pass


def _install_stubs():
    _mod("azure")
    _mod("azure.functions",
         FunctionApp=_FakeFunctionApp, AuthLevel=_AuthLevel,
         HttpResponse=_HttpResponse, HttpRequest=_FakeHttpRequest, QueueMessage=_QueueMessage,
         TimerRequest=type("TimerRequest", (), {}))
    _mod("azure.storage")
    _mod("azure.storage.blob",
         generate_blob_sas=mock.Mock(return_value="sas"), BlobSasPermissions=mock.Mock())
    _mod("shared.auth", validate_token=mock.Mock(return_value={"oid": "user-1"}),
         get_user_id=mock.Mock(return_value="user-1"),
         require_admin=mock.Mock(return_value={"oid": "admin", "email": "admin@test", "name": "Admin", "roles": ["Admin"]}),
         NotAdminError=type("NotAdminError", (Exception,), {}))
    _mod("shared.db", get_db=mock.Mock(), new_connection=mock.Mock())
    _mod("shared.queue_client",
         enqueue_job=mock.Mock(), enqueue_training_job=mock.Mock(),
         _send=mock.Mock(), INFERENCE_QUEUE="inference-jobs", TRAINING_QUEUE="lora-training-jobs")
    _mod("shared.blob",
         upload_blob=mock.Mock(), download_blob=mock.Mock(return_value=b""),
         get_blob_client=mock.Mock())
    _mod("shared.keyvault", get_secret=mock.Mock(return_value="secret"))
    _mod("shared.queue_trigger",
         trigger_container_job=mock.Mock(return_value="exec-123"),
         count_active_job_executions=mock.Mock(return_value=0))
    _mod("shared.crops",
         crop_head_and_shoulders=mock.Mock(return_value=b"jpeg"), NoFaceError=_NoFaceError)
    _mod("shared.training_trigger",
         trigger_training_job=mock.Mock(return_value="train-exec-1"),
         get_execution_status=mock.Mock(return_value="running"))
    _mod("shared.gpu_lease",
         acquire_dispatch_lease=mock.Mock(return_value="owner-1"),
         release_dispatch_lease=mock.Mock(), mark_dispatched=mock.Mock(),
         recent_dispatch_pending=mock.Mock(return_value=False),
         DispatchConfigError=_DispatchConfigError)


def setUpModule():
    """Bind `function_app`. If another offline suite already imported it (shared process),
    REUSE it untouched so their sys.modules-based mock wiring keeps working; otherwise install
    our own stubs and import it (standalone run)."""
    global function_app
    if "function_app" not in sys.modules:
        _install_stubs()
    function_app = importlib.import_module("function_app")
    # Auth must succeed for our handler tests (the body/plan guards are what we exercise).
    # Configure via the SAME module object function_app is bound to — robust whether the stubs
    # are ours or another suite's.
    sys.modules["shared.auth"].get_user_id.return_value = "user-1"


class BodyGuardTests(unittest.TestCase):
    """The body guard runs right after auth and BEFORE any DB access, so these need no DB
    stubs — auth passes (get_user_id -> 'user-1') and the guard is what returns 400."""

    def _assert_object_400(self, body_bytes):
        resp = function_app.submit_job(_FakeHttpRequest(body_bytes))
        self.assertEqual(resp.status_code, 400)
        # dev's submit_job body guard returns this message (feat/quality-gate's parallel guard
        # used a different string; dev is the trunk, so the test asserts dev's message).
        self.assertEqual(json.loads(resp.body)["error"], "invalid JSON body")

    def test_empty_body(self):
        self._assert_object_400(b"")

    def test_malformed_json(self):
        self._assert_object_400(b"{not json")

    def test_null(self):
        self._assert_object_400(b"null")

    def test_array(self):
        self._assert_object_400(b"[]")

    def test_string(self):
        self._assert_object_400(b'"hello"')

    def test_true(self):
        self._assert_object_400(b"true")

    def test_false(self):
        self._assert_object_400(b"false")

    def test_number(self):
        self._assert_object_400(b"3")

    def test_invalid_utf8(self):
        # get_json decodes UTF-8 first; non-UTF-8 bytes raise UnicodeDecodeError, a subclass
        # of ValueError, so the handler's `except ValueError` covers the decode failure too.
        self._assert_object_400(b"\xff\xfe\xff")

    def test_valid_object_passes_into_existing_validation(self):
        # A well-formed object missing required fields must clear the body guard and hit the
        # EXISTING field-level 400 (different message) — proving no over-rejection.
        resp = function_app.submit_job(_req({"gender": "male"}))
        self.assertEqual(resp.status_code, 400)
        err = json.loads(resp.body)["error"]
        self.assertIn("required", err)
        self.assertNotEqual(err, "Request body must be a JSON object")


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return self._row

    def close(self):
        pass


class _FakeConn:
    def __init__(self, row):
        self._row = row

    def cursor(self):
        return _FakeCursor(self._row)

    def commit(self):
        pass

    def close(self):
        pass


class ImageCountValidationTests(unittest.TestCase):
    """Drive the REAL monthly submit_job path (custom_prompt mode skips the catalog checks),
    asserting the actual coercion + clamp — NOT a mirror of the int(...) expression. reserve is
    mocked to succeed and to capture the job_params the handler built, so the CLAMP production
    applies (max(1, min(min_session, credits), min(requested, monthly, credits))) is measured."""

    PLAN_ROW = ("monthly_pro", "ready", 200, 0, None)  # plan_name, lora_status, credits_remaining, one_time_credits_remaining, suspended_at
    MONTHLY_CAP = 40                            # monthly_pro.monthly_images
    MIN_SESSION = 5                             # monthly_pro.min_session_images

    def setUp(self):
        self.reserve = mock.Mock(return_value=types.SimpleNamespace(
            ok=True, job_id="job-123", outbox_id=1, reason=None, status="queued"))
        patches = [
            mock.patch.object(function_app, "get_db", return_value=_FakeConn(self.PLAN_ROW)),
            mock.patch.object(function_app, "reserve_job_slot", self.reserve),
            mock.patch.object(function_app, "outbox_try_send_now", mock.Mock()),
        ]
        for p in patches:
            self.addCleanup(p.stop)
            p.start()

    def _submit(self, image_count):
        body = {"gender": "male", "age_range": "25-30", "hair_color": "black",
                "custom_prompt": "a quiet park bench", "image_count": image_count}
        return function_app.submit_job(_req(body))

    def _delivered_count(self):
        # reserve_job_slot(user_id, input_blob_path, job_params, ...) -> job_params is arg[2].
        return json.loads(self.reserve.call_args.args[2])["image_count"]

    # ── invalid values are rejected by the REAL handler ──
    def test_non_numeric_string_returns_400(self):
        resp = self._submit("abc")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(json.loads(resp.body)["error"], "image_count must be a number")
        self.reserve.assert_not_called()

    def test_wrong_type_list_returns_400(self):
        resp = self._submit([1])   # int([1]) raises TypeError -> caught -> 400
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(json.loads(resp.body)["error"], "image_count must be a number")
        self.reserve.assert_not_called()

    # ── valid values flow through and the REAL clamp bounds them ──
    def test_large_value_clamped_to_monthly_cap(self):
        resp = self._submit(999)
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(self._delivered_count(), self.MONTHLY_CAP)

    def test_in_range_value_preserved(self):
        self._submit(10)
        self.assertEqual(self._delivered_count(), 10)

    def test_below_floor_raised_to_min_session(self):
        self._submit(1)
        self.assertEqual(self._delivered_count(), self.MIN_SESSION)

    def test_negative_clamped_into_valid_range(self):
        self._submit(-5)
        n = self._delivered_count()
        self.assertGreaterEqual(n, 1)
        self.assertEqual(n, self.MIN_SESSION)


if __name__ == "__main__":
    unittest.main(verbosity=2)
