"""Unit tests for the transactional outbox (finding #4).

Stubs the DB + queue so the outbox LOGIC runs with no pyodbc / azure / real queue:
  - outbox_add writes an INSERT via the CALLER'S cursor (so it commits with the caller's
    txn) and returns the new id.
  - outbox_try_send_now: success -> send + mark delivered; failure -> record + return False,
    and NEVER raises (the state change already committed).
  - outbox_dispatch_pending: sends every undelivered row and marks it delivered; a send
    failure is recorded and skipped, not fatal.

Run:  python -m unittest tests.test_outbox   (from the backend dir)
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


def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


# Stub heavy leaf deps BEFORE importing shared.outbox, so the import needs no pyodbc/azure.
_mod("pyodbc", pooling=False, connect=mock.Mock())
_mod("azure")
_mod("azure.storage")
_mod("azure.storage.queue", QueueClient=mock.Mock(), TextBase64EncodePolicy=mock.Mock())
# Stub shared.keyvault so queue_client's `from .keyvault import get_secret` doesn't pull azure-identity.
_mod("shared.keyvault", get_secret=mock.Mock(return_value="stub"))

from shared import outbox  # noqa: E402


class FakeCursor:
    def __init__(self, fetchall_rows=None, fetchone_row=None):
        self.executed = []           # list of (sql, params)
        self._fetchall = fetchall_rows or []
        self._fetchone = fetchone_row

    def execute(self, sql, *params):
        self.executed.append((sql, params))
        return self

    def fetchall(self):
        return list(self._fetchall)

    def fetchone(self):
        return self._fetchone


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.autocommit = False
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


class OutboxAdd(unittest.TestCase):
    def test_inserts_via_callers_cursor_and_returns_id(self):
        cur = FakeCursor(fetchone_row=[42])
        oid = outbox.outbox_add(cur, "inference-jobs", {"job_id": "j1", "user_id": "u1"})
        self.assertEqual(oid, 42)
        sql, params = cur.executed[-1]
        self.assertIn("INSERT INTO outbox", sql)
        self.assertEqual(params[0], "inference-jobs")
        # payload is JSON-serialized to a string, not passed as a dict
        self.assertIsInstance(params[1], str)
        self.assertIn("job_id", params[1])


class TrySendNow(unittest.TestCase):
    def test_success_sends_then_marks_delivered(self):
        cur = FakeCursor()
        with mock.patch.object(outbox, "new_connection", return_value=FakeConn(cur)), \
             mock.patch.object(outbox, "_send") as m_send:
            ok = outbox.outbox_try_send_now(7, "inference-jobs", {"x": 1})
        self.assertTrue(ok)
        m_send.assert_called_once_with("inference-jobs", {"x": 1}, None)
        self.assertTrue(any("SET delivered_at" in s for s, _ in cur.executed))

    def test_failure_records_and_returns_false_without_raising(self):
        cur = FakeCursor()
        with mock.patch.object(outbox, "new_connection", return_value=FakeConn(cur)), \
             mock.patch.object(outbox, "_send", side_effect=Exception("queue down")):
            ok = outbox.outbox_try_send_now(7, "inference-jobs", {"x": 1})
        self.assertFalse(ok)   # did NOT raise — the state change already committed
        self.assertTrue(any("attempts = attempts + 1" in s for s, _ in cur.executed))

    def test_none_outbox_id_is_a_noop(self):
        with mock.patch.object(outbox, "_send") as m_send:
            self.assertFalse(outbox.outbox_try_send_now(None, "q", {}))
        m_send.assert_not_called()


class DispatchPending(unittest.TestCase):
    def test_sends_all_pending_and_marks_delivered(self):
        pending = [
            (1, "inference-jobs", json.dumps({"job_id": "a"}), None),
            (2, "lora-training-jobs", json.dumps({"training_id": "t"}), None),
        ]
        cur = FakeCursor(fetchall_rows=pending)
        with mock.patch.object(outbox, "new_connection", return_value=FakeConn(cur)), \
             mock.patch.object(outbox, "_send") as m_send:
            sent, seen = outbox.outbox_dispatch_pending()
        self.assertEqual((sent, seen), (2, 2))
        self.assertEqual(m_send.call_count, 2)
        self.assertEqual(len([s for s, _ in cur.executed if "SET delivered_at" in s]), 2)

    def test_send_failure_is_recorded_not_fatal(self):
        pending = [
            (1, "inference-jobs", json.dumps({"job_id": "a"}), None),
            (2, "inference-jobs", json.dumps({"job_id": "b"}), None),
        ]
        cur = FakeCursor(fetchall_rows=pending)
        # first send fails, second succeeds — the failure must not abort the batch
        with mock.patch.object(outbox, "new_connection", return_value=FakeConn(cur)), \
             mock.patch.object(outbox, "_send", side_effect=[Exception("down"), None]):
            sent, seen = outbox.outbox_dispatch_pending()
        self.assertEqual((sent, seen), (1, 2))
        self.assertTrue(any("attempts = attempts + 1" in s for s, _ in cur.executed))
        self.assertTrue(any("SET delivered_at" in s for s, _ in cur.executed))


if __name__ == "__main__":
    unittest.main()
