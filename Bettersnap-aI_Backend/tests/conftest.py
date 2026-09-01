"""Canonical Azure/shared stubs for the whole test suite.

WHY THIS FILE EXISTS
Every test module runs in ONE process, and each used to install its own stubs into
sys.modules on import. Whichever module pytest imported first therefore decided which fake
objects the entire suite saw — an invisible dependency on alphabetical filename order.

It broke the moment a module sorting earlier than test_dispatch_logic.py appeared
(test_dashboard_summary.py): 184 tests failed across files that had not changed, while the
new file's own tests passed in isolation. The sharpest edge is that shared/outbox.py binds
`from .queue_client import _send` at IMPORT time, so once outbox is imported it calls that
exact function object forever — any later swap of sys.modules leaves the tests asserting on
one mock while the code calls another, and a send that definitely happened reads as zero
calls.

pytest imports conftest.py before ANY test module, so installing the stubs here makes the
arrangement deterministic and order-independent. The set below is lifted verbatim from
test_dispatch_logic.py, which is the superset the rest of the suite was already written
against. Every module's own _mod() call is now a no-op that returns what is already claimed.
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


def _mod(name, **attrs):
    """Only installs a stub if nothing has claimed this module name yet.

    This USED to overwrite unconditionally, which was safe only while this file happened to
    be imported first. It stopped being true when a test module sorting before it appeared,
    and the failure was vicious: shared/outbox.py binds `from .queue_client import _send` at
    IMPORT time, so once outbox is imported it calls that exact function object forever.
    Overwriting sys.modules afterwards swapped the module the TESTS assert on while the code
    kept calling the original — `_send.assert_called_once()` saw zero calls for a send that
    definitely happened, in a different file, only in full-suite runs.

    Deferring matches test_org_teams.py, which documented this reasoning first.
    """
    if name in sys.modules:
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

    def get_body(self):
        """The real azure.functions.HttpResponse exposes get_body(), not .body. Handlers
        that compose other handlers (dashboard-summary calls subscription_status) read the
        inner response through it, so the fake has to offer the same surface or the test
        passes against an API that does not exist in production."""
        body = self.body
        return body.encode("utf-8") if isinstance(body, str) else body


class _HttpRequest:
    """Mirrors what handlers read off a request. `params` is the query string —
    paginated endpoints read it, and whichever test module wins the _mod() race
    decides which fake the whole suite sees, so both must carry it."""
    def __init__(self, params=None, route_params=None, headers=None):
        self.params = params or {}
        self.route_params = route_params or {}
        self.headers = headers or {}


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


# shared.crops is deliberately NOT stubbed here. test_crops_face_gate.py imports the REAL
# module (FaceTooSmallError and friends) and previously got it only because "crops" sorts
# before "dispatch" — the same accidental ordering this file exists to remove. Claim the
# real module up front so every test sees it regardless of order; the modules that only
# need crop_head_and_shoulders faked patch it per-test instead.
import shared.crops  # noqa: E402,F401
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

