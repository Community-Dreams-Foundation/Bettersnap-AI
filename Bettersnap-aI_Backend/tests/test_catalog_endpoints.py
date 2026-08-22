"""Offline tests for the gender-aware catalog retrieval endpoints
(GET /catalog/{category}/attires and /backgrounds). Uses the same stdlib-only
offline stub pattern as tests/test_dispatch_logic.py — no pyodbc/azure/torch. A fake
get_db returns known catalog rows so we assert the JSON shape, gender filtering,
composed image paths, and the 400/404 guards without a live DB.
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


class _FakeReq:
    def __init__(self, category, gender=None):
        self.route_params = {"category": category}
        self.params = {} if gender is None else {"gender": gender}
        self.headers = {}


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


class _AuthLevel:
    ANONYMOUS = "anonymous"


class _HttpResponse:
    def __init__(self, body="", status_code=200, mimetype=None):
        self.body = body; self.status_code = status_code; self.mimetype = mimetype


def _install_stubs():
    _mod("azure")
    _mod("azure.functions", FunctionApp=_FakeFunctionApp, AuthLevel=_AuthLevel,
         HttpResponse=_HttpResponse, HttpRequest=_FakeReq,
         QueueMessage=type("QM", (), {}), TimerRequest=type("TR", (), {}))
    _mod("azure.storage"); _mod("azure.storage.blob",
         generate_blob_sas=mock.Mock(return_value="sas"), BlobSasPermissions=mock.Mock())
    _mod("shared.auth", validate_token=mock.Mock(return_value={"oid": "u"}),
         get_user_id=mock.Mock(return_value="u"),
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
         NoFaceError=type("NoFaceError", (Exception,), {}))
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


class _Cur:
    """Answers the exact queries the catalog handlers issue."""
    def __init__(self, known=True, attires=None, backgrounds=None):
        self.known = known; self.attires = attires or []; self.backgrounds = backgrounds or []
        self._fetch = None; self._all = []
    def execute(self, sql, *params):
        s = " ".join(sql.split()).lower()
        if "from dbo.catalog_categories" in s:
            self._fetch = (1,) if self.known else None
        elif "from dbo.catalog_attires" in s:
            self._all = self.attires
        elif "from dbo.catalog_backgrounds" in s:
            self._all = self.backgrounds
    def fetchone(self): return self._fetch
    def fetchall(self): return self._all


class _Conn:
    def __init__(self, cur): self._cur = cur
    def cursor(self): return self._cur


def _patch_db(cur):
    return mock.patch.object(function_app, "get_db", return_value=_Conn(cur))


class CatalogAttireEndpoint(unittest.TestCase):
    ROWS = [("business_suit.navy_suit_tie", "Navy Suit & Tie", "navy-suit-tie"),
            ("business_suit.black_suit_tie", "Black Suit & Tie", "black-suit-tie")]

    def test_valid_male_returns_attires_with_image_paths(self):
        with _patch_db(_Cur(known=True, attires=self.ROWS)):
            resp = function_app.get_catalog_attires(_FakeReq("business_suit", "male"))
        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.body)
        self.assertEqual(body["gender"], "male")
        self.assertEqual(len(body["attires"]), 2)
        a0 = body["attires"][0]
        self.assertEqual(a0["ref"], "business_suit.navy_suit_tie")
        self.assertEqual(a0["id"], "navy_suit_tie")
        self.assertEqual(a0["label"], "Navy Suit & Tie")
        self.assertEqual(a0["image"], "/catalog/male/attires/navy-suit-tie_male.jpg")
        # phrase must NOT leak to the client
        self.assertNotIn("phrase", a0)
        self.assertNotIn("prompt_phrase", a0)

    def test_female_gender_in_image_path(self):
        rows = [("business_suit.navy_pantsuit", "Navy Pantsuit", "navy-pantsuit")]
        with _patch_db(_Cur(known=True, attires=rows)):
            resp = function_app.get_catalog_attires(_FakeReq("business_suit", "female"))
        img = json.loads(resp.body)["attires"][0]["image"]
        self.assertEqual(img, "/catalog/female/attires/navy-pantsuit_female.jpg")

    def test_missing_gender_400(self):
        with _patch_db(_Cur(known=True, attires=self.ROWS)):
            resp = function_app.get_catalog_attires(_FakeReq("business_suit", None))
        self.assertEqual(resp.status_code, 400)

    def test_bad_gender_400(self):
        with _patch_db(_Cur(known=True, attires=self.ROWS)):
            resp = function_app.get_catalog_attires(_FakeReq("business_suit", "xyz"))
        self.assertEqual(resp.status_code, 400)

    def test_unknown_category_404(self):
        with _patch_db(_Cur(known=False)):
            resp = function_app.get_catalog_attires(_FakeReq("nope", "male"))
        self.assertEqual(resp.status_code, 404)


class CatalogBackgroundEndpoint(unittest.TestCase):
    ROWS = [("business_suit.executive_office", "Executive Office", "executive-office")]

    def test_backgrounds_default_other_when_gender_absent(self):
        with _patch_db(_Cur(known=True, backgrounds=self.ROWS)):
            resp = function_app.get_catalog_backgrounds(_FakeReq("business_suit", None))
        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.body)
        self.assertEqual(body["gender"], "other")
        self.assertEqual(body["backgrounds"][0]["image"],
                         "/catalog/other/backgrounds/executive-office_other.jpg")

    def test_backgrounds_gender_specific_image(self):
        with _patch_db(_Cur(known=True, backgrounds=self.ROWS)):
            resp = function_app.get_catalog_backgrounds(_FakeReq("business_suit", "male"))
        self.assertEqual(json.loads(resp.body)["backgrounds"][0]["image"],
                         "/catalog/male/backgrounds/executive-office_male.jpg")

    def test_unknown_category_404(self):
        with _patch_db(_Cur(known=False)):
            resp = function_app.get_catalog_backgrounds(_FakeReq("nope", "male"))
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
