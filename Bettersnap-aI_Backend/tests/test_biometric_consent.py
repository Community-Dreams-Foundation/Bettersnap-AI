"""Offline tests for the biometric-consent endpoints + the upload/train guard (migration 028).
Same stdlib-only stub pattern as test_catalog_endpoints — no pyodbc/azure/torch. A fake get_db
returns scripted rows so we assert the consent logic, JSON shape, and the 403 guard gating
without a live DB.
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

function_app = None


class _Req:
    def __init__(self, body=None, files=None):
        self.headers = {"Authorization": "Bearer t"}
        self._body = body
        self.files = files if files is not None else {}
    def get_json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _mod(name, **attrs):
    if name in sys.modules:
        return sys.modules[name]
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _FakeFunctionApp:
    def __init__(self, *a, **k): pass
    def route(self, *a, **k): return lambda fn: fn
    def queue_trigger(self, *a, **k): return lambda fn: fn
    def timer_trigger(self, *a, **k): return lambda fn: fn


class _HttpResponse:
    def __init__(self, body="", status_code=200, mimetype=None):
        self.body = body; self.status_code = status_code; self.mimetype = mimetype


def _install_stubs():
    _mod("azure")
    _mod("azure.functions", FunctionApp=_FakeFunctionApp, AuthLevel=type("AL", (), {"ANONYMOUS": "a"}),
         HttpResponse=_HttpResponse, HttpRequest=_Req, QueueMessage=type("QM", (), {}),
         TimerRequest=type("TR", (), {}))
    _mod("azure.storage"); _mod("azure.storage.blob",
         generate_blob_sas=mock.Mock(return_value="s"), BlobSasPermissions=mock.Mock())
    _mod("shared.auth", validate_token=mock.Mock(return_value={"oid": "u"}),
         get_user_id=mock.Mock(return_value="user-1"),
         require_admin=mock.Mock(return_value={"roles": ["Admin"]}),
         NotAdminError=type("NotAdminError", (Exception,), {}))
    _mod("shared.db", get_db=mock.Mock(), new_connection=mock.Mock())
    _mod("shared.queue_client", enqueue_job=mock.Mock(), enqueue_training_job=mock.Mock(),
         _send=mock.Mock(), INFERENCE_QUEUE="q", TRAINING_QUEUE="t")
    _mod("shared.blob", upload_blob=mock.Mock(), download_blob=mock.Mock(return_value=b""),
         get_blob_client=mock.Mock())
    _mod("shared.keyvault", get_secret=mock.Mock(return_value="s"))
    _mod("shared.queue_trigger", trigger_container_job=mock.Mock(return_value="e"),
         count_active_job_executions=mock.Mock(return_value=0))
    _mod("shared.crops", crop_head_and_shoulders=mock.Mock(return_value=b"j"),
         NoFaceError=type("NoFaceError", (Exception,), {}),
         MultipleFacesError=type("MultipleFacesError", (Exception,), {}),
         FaceTooSmallError=type("FaceTooSmallError", (Exception,), {}),
         EyesOccludedError=type("EyesOccludedError", (Exception,), {}))
    _mod("shared.training_trigger", trigger_training_job=mock.Mock(return_value="t"),
         get_execution_status=mock.Mock(return_value="running"))
    _mod("shared.gpu_lease", acquire_dispatch_lease=mock.Mock(return_value="o"),
         release_dispatch_lease=mock.Mock(), mark_dispatched=mock.Mock(),
         recent_dispatch_pending=mock.Mock(return_value=False),
         DispatchConfigError=type("DispatchConfigError", (Exception,), {}))


def setUpModule():
    global function_app
    if "function_app" not in sys.modules:
        _install_stubs()
    function_app = importlib.import_module("function_app")
    sys.modules["shared.auth"].get_user_id.return_value = "user-1"


class _Cur:
    """Answers TOP-1 event / status selects with a scripted latest event."""
    def __init__(self, latest=None):
        self.latest = latest  # None | ("given",...) | ("revoked",...)
        self._fetch = None
        self.inserts = []
    def execute(self, sql, *params):
        s = " ".join(sql.split()).lower()
        if "insert into dbo.biometric_consent" in s:
            self.inserts.append(params)
            self._fetch = None
        elif "select top 1 event, consent_version" in s:   # status
            self._fetch = self.latest
        elif "select top 1 event from dbo.biometric_consent" in s:  # active check
            self._fetch = (self.latest[0],) if self.latest else None
        elif "select top 1 consent_version" in s:
            self._fetch = ("v1.0",)
    def fetchone(self):
        return self._fetch


class _Conn:
    def __init__(self, cur): self._c = cur
    def cursor(self): return self._c
    def commit(self): pass


def _db(cur):
    return mock.patch.object(function_app, "get_db", return_value=_Conn(cur))


class ConsentLogic(unittest.TestCase):
    def test_active_true_when_latest_given(self):
        with _db(_Cur(("given", "v1.0"))):
            self.assertTrue(function_app._biometric_consent_active(_Cur(("given",)), "user-1"))

    def test_active_false_when_revoked_or_none(self):
        self.assertFalse(function_app._biometric_consent_active(_Cur(("revoked",)), "u"))
        self.assertFalse(function_app._biometric_consent_active(_Cur(None), "u"))


class ConsentEndpoints(unittest.TestCase):
    def test_give_requires_version(self):
        with _db(_Cur()):
            resp = function_app.give_biometric_consent(_Req(body={}))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("consent_version", json.loads(resp.body)["error"])

    def test_give_records_event_and_returns_status(self):
        cur = _Cur(("given", "v1.0", "purpose", None, None))
        with _db(cur):
            resp = function_app.give_biometric_consent(_Req(body={"consent_version": "v1.0"}))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(cur.inserts), 1)
        self.assertTrue(json.loads(resp.body)["consent_given"])

    def test_revoke_records_event(self):
        cur = _Cur(("revoked", "v1.0", "purpose", None, None))
        with _db(cur):
            resp = function_app.revoke_biometric_consent(_Req(body={"reason": "user request"}))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(cur.inserts), 1)
        self.assertFalse(json.loads(resp.body)["consent_given"])

    def test_status_none_when_no_record(self):
        with _db(_Cur(None)):
            resp = function_app.biometric_consent_status(_Req())
        self.assertEqual(json.loads(resp.body)["consent_given"], False)


class UploadTrainGuard(unittest.TestCase):
    def _run_upload(self, required, latest):
        with mock.patch.object(function_app, "BIOMETRIC_CONSENT_REQUIRED", required), _db(_Cur(latest)):
            return function_app.upload_photo(_Req(files={}))

    def test_guard_blocks_when_required_and_no_consent(self):
        resp = self._run_upload(required=True, latest=None)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(json.loads(resp.body)["error"], "biometric_consent_required")

    def test_guard_passes_when_required_and_consented(self):
        # Consent active -> guard passes -> falls through to "No photo provided" (400, no file).
        resp = self._run_upload(required=True, latest=("given",))
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn("biometric", str(resp.body))

    def test_guard_inert_when_not_required(self):
        # Default off -> no consent check -> "No photo provided" (400), never 403.
        resp = self._run_upload(required=False, latest=None)
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
