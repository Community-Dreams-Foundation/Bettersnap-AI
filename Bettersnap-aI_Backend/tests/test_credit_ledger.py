"""Tests for the append-only credit ledger and the paid-not-granted Stripe retry fix.

- credit_ledger.record writes one signed row per balance change and skips zero.
- _handle_onetime_payment returns True + writes a ledger row when the grant applies, and
  returns False + writes NOTHING (rolled back) when the user row doesn't exist yet — so the
  webhook can return 500 and Stripe retries instead of silently losing the payment.

Run: python -m pytest tests/test_credit_ledger.py -q   (from the backend dir)
"""
import os
import sys

import pytest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from shared import credit_ledger  # noqa: E402

# credit_ledger is now WIRED into dev-trunk billing: reserve_job_slot (spend), the retrain
# charge, and every Stripe grant/renewal/topup + the refund path each append a signed ledger
# row in their own transaction. The two integration tests below exercise the one-time grant
# path against dev's ACTUAL handler (which returns None and RAISES for Stripe-retry on a
# missing user, rather than returning a bool).


class RecordingCursor:
    """Captures every execute() so tests can assert what was written."""
    def __init__(self, user_exists=True):
        self.calls = []
        self.rowcount = 0
        self._fetch = None
        self._user_exists = user_exists
        self.ledger_rows = []

    def execute(self, sql, *params):
        s = " ".join(sql.split()).lower()
        self.calls.append((s, params))
        if "insert into credit_transactions" in s:
            # (user_id, amount, transaction_type, job_id)
            self.ledger_rows.append(params)
            self.rowcount = 1
        elif "insert into processed_stripe_events" in s:
            self.rowcount = 1  # event freshly claimed
        elif "update users set" in s and "credits_remaining" in s:
            self.rowcount = 1 if self._user_exists else 0  # 0 = user not registered yet
        else:
            self.rowcount = 1
            self._fetch = None

    def fetchone(self):
        return self._fetch


class RecordingConn:
    def __init__(self, user_exists=True):
        self._cur = RecordingCursor(user_exists)
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return self._cur

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


# ── credit_ledger.record ─────────────────────────────────────────────────────
def test_record_writes_signed_amount():
    cur = RecordingCursor()
    credit_ledger.record(cur, "u1", -4, credit_ledger.REASON_JOB_RESERVE, "job-9")
    assert cur.ledger_rows == [("u1", -4, "job_reserve", "job-9")]


def test_record_skips_zero():
    cur = RecordingCursor()
    credit_ledger.record(cur, "u1", 0, credit_ledger.REASON_JOB_REFUND)
    assert cur.ledger_rows == []  # no-op events don't clutter the ledger


def test_record_defaults_job_id_none():
    cur = RecordingCursor()
    credit_ledger.record(cur, "u1", 30, credit_ledger.REASON_PURCHASE_MONTHLY)
    assert cur.ledger_rows == [("u1", 30, "purchase_monthly", None)]


# ── paid-not-granted retry semantics ─────────────────────────────────────────
def _run_onetime(monkeypatch, user_exists):
    import function_app
    conn = RecordingConn(user_exists=user_exists)
    monkeypatch.setattr(function_app, "new_connection", lambda: conn)
    plan = next(iter(function_app.ONE_TIME_PLANS))
    session = {"metadata": {"user_id": "u-123", "plan": plan, "payment_type": "one_time"}}
    result = function_app._handle_onetime_payment(session, "evt_test_1")
    return result, conn


def test_onetime_grants_and_ledgers_when_user_exists(monkeypatch):
    _, conn = _run_onetime(monkeypatch, user_exists=True)  # dev's handler returns None
    assert conn.committed is True
    assert len(conn._cur.ledger_rows) == 1  # exactly one grant row
    assert conn._cur.ledger_rows[0][2] == "purchase_one_time"


def test_onetime_raises_and_no_ledger_when_user_missing(monkeypatch):
    import function_app
    conn = RecordingConn(user_exists=False)
    monkeypatch.setattr(function_app, "new_connection", lambda: conn)
    plan = next(iter(function_app.ONE_TIME_PLANS))
    session = {"metadata": {"user_id": "u-123", "plan": plan, "payment_type": "one_time"}}
    # Dev raises for a non-2xx Stripe retry (not a return-False) when the user row is absent.
    with pytest.raises(function_app.RetryableStripeWebhookError):
        function_app._handle_onetime_payment(session, "evt_test_1")
    assert conn.rolled_back is True        # event NOT recorded → Stripe retry reprocesses
    assert conn._cur.ledger_rows == []     # nothing granted, nothing ledgered
