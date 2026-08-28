"""Integration test for the infra-failure reconcile + refund path, exercising the REAL
function_app._fetch_execution_outcome / _reconcile_execution_outcome / _stamp_failure_class /
_mark_failed against an in-memory fake `jobs`+`users` DB and a fake diagnostics blob.

Simulates: reservation -> dispatch -> preflight exit 43 -> reconciliation -> refund ->
repeated reconciliation -> final job state + final credit balance. No Azure, no workload.

Run: python -m unittest tests.test_infra_refund_integration   (from the backend dir)
"""
import json
import os
import sys
import types
import unittest
from unittest import mock

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

# ── in-memory DB + blob the fakes operate on ─────────────────────────────────
STATE = {"jobs": {}, "users": {}, "blobs": {}, "user_updates": 0}


class FakeCursor:
    def __init__(self):
        self.rowcount = 0
        self._fetch = None

    def execute(self, sql, *params):
        s = " ".join(sql.split()).lower()
        J = STATE["jobs"]
        if "select external_execution_id from jobs" in s:
            self._fetch = (J[params[0]].get("external_execution_id"),)
        elif "update jobs set status = 'failed'" in s:
            jid = params[0]
            if J[jid]["status"] not in ("failed", "completed"):
                J[jid]["status"] = "failed"
                self.rowcount = 1
            else:
                self.rowcount = 0
        elif "select job_params, user_id, source_type from jobs" in s:
            jid = params[0]
            self._fetch = (J[jid]["job_params"], J[jid]["user_id"], J[jid].get("source_type"))
        elif "update users set credits_remaining" in s:
            refund, jid = params[0], params[1]
            uid = J[jid]["user_id"]
            STATE["users"][uid] += refund
            STATE["user_updates"] += 1     # count actual credit mutations
        elif "select job_params from jobs" in s:
            self._fetch = (J[params[0]]["job_params"],)
        elif "update jobs set job_params = ?" in s:
            newparams, jid = params[0], params[1]
            J[jid]["job_params"] = newparams
        else:
            self._fetch = None

    def fetchone(self):
        return self._fetch


class FakeConn:
    def cursor(self):
        return FakeCursor()

    def commit(self):
        pass

    def close(self):
        pass


def fake_new_connection():
    return FakeConn()


def fake_download_blob(container, name):
    if name in STATE["blobs"]:
        return STATE["blobs"][name]
    raise FileNotFoundError(name)


def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


# Install the azure/shared stubs and import function_app ONLY when we are the first importer
# (i.e. running this file standalone). Under `unittest discover` test_dispatch_logic loads
# first and already imported function_app under its stubs, so we reuse that and DO NOT stub
# again — re-stubbing here would replace sibling mocks like sys.modules["shared.queue_client"]
# with fresh never-called ones and break test_dispatch_logic (its asserts read sys.modules
# directly). We override ONLY new_connection + download_blob per-test via mock.patch.object.
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
    _mod("shared.auth", validate_token=mock.Mock(), get_user_id=mock.Mock(return_value="user-1"),
         require_admin=mock.Mock(return_value={"oid": "admin", "email": "admin@test", "name": "Admin", "roles": ["Admin"]}),
         NotAdminError=type("NotAdminError", (Exception,), {}))
    _mod("shared.db", get_db=mock.Mock(), new_connection=fake_new_connection)
    _mod("shared.queue_client", enqueue_job=mock.Mock(), enqueue_training_job=mock.Mock(),
         _send=mock.Mock(), INFERENCE_QUEUE="inference-jobs", TRAINING_QUEUE="lora-training-jobs")
    _mod("shared.blob", upload_blob=mock.Mock(), download_blob=fake_download_blob,
         get_blob_client=mock.Mock())
    _mod("shared.keyvault", get_secret=mock.Mock(return_value="secret"))
    _mod("shared.queue_trigger", trigger_container_job=mock.Mock(return_value="e"),
         count_active_job_executions=mock.Mock(return_value=0))
    _mod("shared.crops", crop_head_and_shoulders=mock.Mock(return_value=b"jpeg"),
         NoFaceError=type("NoFaceError", (Exception,), {}))
    _mod("shared.training_trigger", trigger_training_job=mock.Mock(return_value="t"),
         get_execution_status=mock.Mock(return_value="running"))
    _mod("shared.gpu_lease", acquire_dispatch_lease=mock.Mock(return_value="o"),
         release_dispatch_lease=mock.Mock(), mark_dispatched=mock.Mock(),
         recent_dispatch_pending=mock.Mock(return_value=False),
         DispatchConfigError=type("DispatchConfigError", (Exception,), {}))

import function_app  # noqa: E402

import function_app  # noqa: E402  (cached; bound to test_dispatch_logic's stubs)


def _preflight_blob(exec_id, exit_code, reason):
    return json.dumps({"execution_id": exec_id,
                       "result": {"ok": False, "exit_code": exit_code, "reason": reason}}).encode()


# exec_reconcile is now WIRED into dev's reaper: for each stuck job the reaper calls
# _fetch_execution_outcome -> _reconcile_execution_outcome (classify -> stamp + _mark_failed on
# ACTION_REFUND), falling back to a labelled fail+refund when no execution outcome is readable.
class InfraRefundIntegrationTests(unittest.TestCase):
    def setUp(self):
        STATE["jobs"].clear(); STATE["users"].clear(); STATE["blobs"].clear()
        STATE["user_updates"] = 0
        # Override ONLY the two functions we need, scoped to this test (auto-restored),
        # regardless of what the initial import bound them to.
        self._patchers = [
            mock.patch.object(function_app, "new_connection", fake_new_connection),
            mock.patch.object(function_app, "download_blob", fake_download_blob),
        ]
        for p in self._patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patchers])
        # reservation already happened: user had 100, a 40-credit job was reserved -> 60 left.
        STATE["users"]["u1"] = 60
        STATE["jobs"]["J1"] = {"status": "processing", "user_id": "u1",
                               "external_execution_id": "exec-J1",
                               "job_params": json.dumps({"credit_cost": 40})}
        STATE["blobs"]["preflight/exec-J1.json"] = _preflight_blob(
            "exec-J1", 43, "GPU hardware present but torch.cuda unavailable")

    def _reconcile_once(self):
        exec_data = function_app._fetch_execution_outcome("J1")
        self.assertIsNotNone(exec_data, "diag blob should be read")
        self.assertEqual(exec_data["exit_code"], 43)
        return function_app._reconcile_execution_outcome("J1", exec_data, now=0)

    def test_full_flow_refund_exactly_once(self):
        # ── first reconciliation: fail + refund
        decision = self._reconcile_once()
        self.assertEqual(decision["failure_class"], "infra_gpu_unusable")
        self.assertTrue(decision["is_infra"])
        self.assertEqual(STATE["jobs"]["J1"]["status"], "failed")
        self.assertEqual(STATE["users"]["u1"], 100, "credits restored to pre-reservation")
        self.assertEqual(STATE["user_updates"], 1, "exactly one credit mutation")
        # class + reason + execution id preserved, and NOT labelled customer/model failure
        fail = json.loads(STATE["jobs"]["J1"]["job_params"])["_failure"]
        self.assertEqual(fail["class"], "infra_gpu_unusable")
        self.assertTrue(fail["is_infra"])
        self.assertEqual(fail["execution_id"], "exec-J1")
        self.assertIn("torch.cuda", fail["reason"])

        # ── repeated reconciliation (reaper retries / duplicate event): NO second refund
        self._reconcile_once()
        self._reconcile_once()
        self.assertEqual(STATE["users"]["u1"], 100, "still 100 — no double refund")
        self.assertEqual(STATE["user_updates"], 1, "still exactly one credit mutation")
        self.assertEqual(STATE["jobs"]["J1"]["status"], "failed")

    def test_success_blob_does_not_refund(self):
        STATE["blobs"]["preflight/exec-J1.json"] = _preflight_blob("exec-J1", 0, "ok")
        # a passing preflight would not normally write a fail record, but prove the guard:
        exec_data = function_app._fetch_execution_outcome("J1")
        decision = function_app._reconcile_execution_outcome("J1", exec_data, now=0)
        self.assertEqual(decision["action"], function_app.exec_reconcile.ACTION_NONE)
        self.assertEqual(STATE["users"]["u1"], 60, "no refund on success")
        self.assertEqual(STATE["user_updates"], 0)

    def test_fetch_returns_none_when_no_blob(self):
        STATE["blobs"].clear()
        self.assertIsNone(function_app._fetch_execution_outcome("J1"))


class ReaperWiringTests(unittest.TestCase):
    """Prove the classifier is actually invoked from the live reaper path (not dormant)."""
    def setUp(self):
        with open(os.path.join(BACKEND_DIR, "function_app.py"), encoding="utf-8") as f:
            self.src = f.read()

    def test_reaper_calls_fetch_and_reconcile(self):
        reaper_body = self.src[self.src.index("def reaper("):]
        self.assertIn("_fetch_execution_outcome(job_id)", reaper_body)
        self.assertIn("_reconcile_execution_outcome(job_id", reaper_body)

    def test_reconcile_uses_mark_failed_and_exec_reconcile(self):
        self.assertIn("from shared import exec_reconcile", self.src)
        body = self.src[self.src.index("def _reconcile_execution_outcome("):]
        self.assertIn("classify(exec_data, now)", body)
        self.assertIn("mark_failed(job_id)", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
