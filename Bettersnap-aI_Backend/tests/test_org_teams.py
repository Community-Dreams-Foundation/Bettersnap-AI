"""Unit tests for the Teams / Organizations layer: org creation, invitations,
accept-invite, and org-aware credit charging in reserve_job_slot.

These stub the Azure + DB dependencies (same convention as test_dispatch_logic.py)
so the *decision logic* runs locally with no Azure SQL, queue, or ACS access. They
prove:

  - creating an org also creates the admin's own membership row (same transaction)
  - inviting more people than seats leaves the excess in "skipped"
  - accepting an invite: rejects invalid/expired/already-used tokens, blocks joining
    a second org, blocks accepting once the org is full, grants credits on success
  - reserve_job_slot: an org member's job charges organization_members.credits_remaining,
    NOT users.credits_remaining, and a job with no org membership still charges the
    personal balance exactly as before
  - a failed job refunds to whichever pool was actually charged

True *concurrency* guarantees (the UPDLOCK/HOLDLOCK race on accept_invitation, the
sp_getapplock serialization in reserve_job_slot) need a real SQL Server under
concurrent load — these tests prove the single-request decision logic is correct,
not that two simultaneous requests can never both win the last seat. Extending
test_concurrency_integration.py with a real-DB race test is a good next step.

Run:  python -m unittest tests.test_org_teams   (from the backend dir)
"""
import os
import sys
import json
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)


# ── Stub heavy deps BEFORE importing function_app (same convention as
#    test_dispatch_logic.py — see that file for why each stub exists) ────────
def _mod(name, **attrs):
    """Only installs a stub if nothing has claimed this module name yet. This
    matters because test files are run TOGETHER in one process (see the test
    docstring, and how CI invokes `python -m unittest tests.test_dispatch_logic
    tests.test_outbox ... tests.test_org_teams`): all test modules are imported
    up front before any test method runs. If this file unconditionally
    overwrote e.g. shared.gpu_lease with a fresh, unconfigured Mock, it would
    silently replace test_dispatch_logic.py's ALREADY-configured stub — and
    that file re-fetches `sys.modules["shared.gpu_lease"]` fresh in its own
    setUp(), so it would pick up OUR blank stub instead of its own. Guarding
    here (rather than trusting import order) makes this file safe to run
    alone, first, last, or anywhere in between."""
    if name in sys.modules:
        return sys.modules[name]
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


#: breakdown_json as teams_quotes stores it for a 10-seat v1 quote. Not optional:
#: reserve_attempt copies it onto the attempt and fulfilment validates against it.
_V1_10_SEAT_BANDS = json.dumps([
    {"from_seat": 1, "to_seat": 9, "unit_price_cents": 3500,
     "seats": 9, "subtotal_cents": 31_500},
    {"from_seat": 10, "to_seat": 24, "unit_price_cents": 3200,
     "seats": 1, "subtotal_cents": 3200},
])

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


class _HttpRequest:
    pass


_mod("azure")
_mod("azure.functions",
     FunctionApp=_FakeFunctionApp, AuthLevel=_AuthLevel,
     HttpResponse=_HttpResponse, HttpRequest=_HttpRequest,
     QueueMessage=type("QueueMessage", (), {}),
     TimerRequest=type("TimerRequest", (), {}))
_mod("azure.storage")
_mod("azure.storage.blob",
     generate_blob_sas=mock.Mock(return_value="sas"),
     BlobSasPermissions=mock.Mock())

_mod("shared.auth",
     validate_token=mock.Mock(return_value={"oid": "admin-1", "email": "admin@acme.com"}),
     get_user_id=mock.Mock(return_value="user-1"))
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

# NOTE: shared.org_credits and shared.job_reservation are left REAL (not stubbed)
# — these tests exist specifically to exercise their real logic, same reasoning
# as job_reservation being left real in test_dispatch_logic.py.
# shared.stripe_client and shared.invite_email are also left real: they only need
# `requests` (already installed) and shared.keyvault (stubbed above), so importing
# them costs nothing and needs no faking.

import function_app  # noqa: E402


# ── A minimal fake HTTP request ───────────────────────────────────────────
class FakeRequest:
    """Stands in for func.HttpRequest. route_params and body are the two things
    every Teams endpoint reads besides the Authorization header."""
    def __init__(self, body=None, route_params=None, auth="Bearer test-token"):
        self._body = body if body is not None else {}
        self.route_params = route_params or {}
        self.headers = {"Authorization": auth}

    def get_json(self):
        return self._body


# ── A programmable fake DB connection/cursor ──────────────────────────────
# Branches on SQL text so each test can hand back exactly the rows it needs,
# without a real database. Add a new `elif` here if you add a query these
# tests need to cover — keep the substrings SHORT and SPECIFIC so a small
# wording change in the real SQL doesn't silently stop matching.
class FakeCursor:
    def __init__(self, cfg):
        self.cfg = cfg
        self.rowcount = 0
        self._fetch = None
        self._fetchall_rows = []
        self.executed = cfg.setdefault("executed", [])

    def execute(self, sql, *params):
        self.executed.append((" ".join(sql.split()), params))
        s = sql.lower()
        self._fetchall_rows = []

        if "sp_getapplock" in s:
            self._fetch = (self.cfg.get("applock_rc", 0),)

        # ── webhook idempotency claim (_claim_event) — rowcount=1 means "claimed
        #    it, proceed"; every _handle_org_payment test needs this to succeed
        #    by default, same as a real first-time webhook delivery would. ──
        elif "insert into processed_stripe_events" in s:
            self.rowcount = self.cfg.get("claim_event_rowcount", 1)

        # ── /me/organization includes pending-payment admin memberships ──
        elif "o.status in ('pending_payment', 'active')" in s:
            self._fetch = self.cfg.get("my_organization_row")

        # ── org membership lookup (shared/org_credits.get_active_membership) ──
        elif "from organization_members m" in s and "join organizations o" in s:
            self._fetch = self.cfg.get("membership_row")  # None or (org_id, credits, membership_id)

        elif "select lora_status from users" in s:
            self._fetch = (self.cfg.get("lora_status", "none"),)

        # ── personal credits (individual pool, no org membership) ──
        elif "select credits_remaining from users" in s:
            self._fetch = (self.cfg.get("personal_credits", 20),)

        # ── daily caps in reserve_job_slot ──
        elif "count(*) from jobs where user_id" in s:
            self._fetch = (self.cfg.get("user_cap_count", 0),)
        elif "count(*) from jobs where created_at" in s:
            self._fetch = (self.cfg.get("global_cap_count", 0),)

        # ── job insert ──
        elif "insert into jobs" in s:
            self._fetch = (self.cfg.get("new_job_id", 999),)

        # ── credit charge (reserve_job_slot) ──
        elif "update organization_members set credits_remaining = credits_remaining -" in s:
            self.rowcount = 1
        elif "update users set credits_remaining = credits_remaining -" in s:
            self.rowcount = 1

        # ── credit refund (_mark_failed) ──
        elif "update organization_members set credits_remaining = credits_remaining +" in s:
            self.rowcount = 1
        elif "update users set credits_remaining = credits_remaining +" in s:
            self.rowcount = 1

        # ── outbox row written alongside the job ──
        elif "insert into outbox" in s:
            self._fetch = (self.cfg.get("new_outbox_id", 12345),)

        # ── create_organization ──
        elif "insert into organizations" in s:
            pass  # no return value read by the caller
        elif "insert into organization_members" in s and "output inserted.membership_id" not in s:
            pass  # create_organization's admin-membership insert (no OUTPUT clause)

        # ── create_org_payment_intent's own org lookup ──
        elif "select admin_user_id, seats_purchased, status from organizations" in s:
            self._fetch = self.cfg.get("payment_intent_org_row")  # (admin_user_id, seats_purchased, status) or None

        # -- org_dashboard_summary --
        # NOTE the ", name," -- this substring is deliberately DIFFERENT from the
        # payment-intent branch above, so the two cannot shadow each other.
        elif "select admin_user_id, name, seats_purchased, status from organizations" in s:
            self._fetch = self.cfg.get("dashboard_org_row")
        elif "coalesce(sum(credits_remaining), 0) from organization_members" in s:
            self._fetch = (self.cfg.get("org_credits", 0),)
        elif "select status, count(*) from jobs where organization_id" in s:
            self._fetchall_rows = self.cfg.get("org_job_counts", [])

        # ── _handle_org_payment: look up who to credit, only if still locked ──
        elif "select admin_user_id, credits_per_seat from organizations" in s and "pending_payment" in s:
            self._fetch = self.cfg.get("webhook_org_row")  # (admin_user_id, credits_per_seat) or None

        # ── _handle_org_payment: the actual unlock — raise admin credits from 0 ──
        elif "update organization_members" in s and "set credits_granted = ?, credits_remaining = ?" in s:
            self.rowcount = self.cfg.get("webhook_membership_rowcount", 1)

        # ── _require_org_admin ──
        elif "from organizations where organization_id = ? and admin_user_id" in s:
            self._fetch = self.cfg.get("org_admin_row")

        # ── organization branding ──
        elif "from organization_branding where organization_id" in s:
            self._fetch = self.cfg.get("branding_row")

        # ── create_invitations: seat accounting ──
        elif "count(*) from organization_members where organization_id" in s:
            self._fetch = (self.cfg.get("active_members", 0),)
        elif "count(*) from invitations where organization_id" in s:
            self._fetch = (self.cfg.get("pending_invites", 0),)
        elif "select lower(i.email) from invitations" in s:
            self._fetchall_rows = [(e,) for e in self.cfg.get("already_invited", [])]
        elif "insert into invitations" in s:
            self._fetch = (self.cfg.get("new_invitation_id", "inv-new"),)

        # ── accept_invitation ──
        elif "from invitations with (updlock, holdlock)" in s:
            self._fetch = self.cfg.get("invite_row")  # (id, org_id, status, expires_at) or None
        elif "update invitations set status = 'expired'" in s:
            pass
        elif "select organization_id from organization_members where user_id" in s:
            self._fetch = self.cfg.get("existing_membership")  # (org_id,) or None
        elif "select seats_purchased, credits_per_seat, status from organizations" in s:
            self._fetch = self.cfg.get("org_row")  # (seats, credits_per_seat, status) or None
        elif "insert into organization_members" in s and "output inserted.membership_id" in s:
            self._fetch = (self.cfg.get("new_membership_id", "member-new"),)
        elif "update invitations set status = 'accepted'" in s:
            pass

        # ── Teams graduated pricing (teams_basic_v1) ──────────────────────
        # Durable quotes. Checkout loads one under a lock and consumes it.
        elif "insert into teams_quotes" in s:
            if self.cfg.get("quote_insert_raises"):
                raise RuntimeError("quote insert failed")
            self.cfg["issued_quote_params"] = params
        elif "from teams_quotes with (updlock, holdlock)" in s:
            self._fetch = self.cfg.get("quote_row")
        elif "update teams_quotes" in s:
            self.rowcount = self.cfg.get("quote_consume_rowcount", 1)

        # The organization's single live checkout attempt.
        elif "from organization_live_checkout l with (updlock, holdlock)" in s:
            self._fetch = self.cfg.get("live_attempt_row")
        elif "insert into organization_live_checkout" in s:
            if self.cfg.get("live_insert_raises"):
                raise RuntimeError("live slot already claimed")
            self.rowcount = 1
        elif "delete from organization_live_checkout" in s:
            self.cfg["live_slot_released"] = True
            self.rowcount = 1

        # Expiry path: locate an attempt by Stripe session id, then retire it.
        elif "select attempt_id, organization_id, status" in s:
            self._fetch = self.cfg.get("attempt_by_session")
        elif "set status = 'expired'" in s:
            self.rowcount = self.cfg.get("expire_rowcount", 1)
        # cancel_org_checkout's admin lookup.
        elif "select admin_user_id from organizations" in s:
            self._fetch = self.cfg.get("cancel_admin_row")

        # Checkout attempts. "attempt_insert_raises" proves the endpoint refuses the
        # checkout when the durable record cannot be written BEFORE Stripe is called.
        elif "insert into organization_checkout_sessions" in s:
            if self.cfg.get("snapshot_insert_raises") or self.cfg.get("attempt_insert_raises"):
                raise RuntimeError("attempt insert failed")
        # _handle_org_payment loads the snapshot it must validate the payment against.
        elif "from organization_checkout_sessions with (updlock, holdlock)" in s:
            self._fetch = self.cfg.get("checkout_snapshot")
        elif "update organization_checkout_sessions" in s:
            if self.cfg.get("promote_raises") and "status = 'pending'" in s:
                raise RuntimeError("promote failed")
            self.rowcount = self.cfg.get("snapshot_update_rowcount", 1)
        # teams_checkout_status' authoritative read.
        elif "from organization_checkout_sessions cs" in s:
            self._fetch = self.cfg.get("checkout_status_row")

        # Legacy allowlist — the ONLY evidence that admits a snapshot-less session.
        elif "from organization_legacy_checkout_allowlist with (updlock, holdlock)" in s:
            self._fetch = self.cfg.get("legacy_allowlist_row")
        elif "update organization_legacy_checkout_allowlist" in s:
            self.rowcount = self.cfg.get("legacy_claim_rowcount", 1)
        # Fulfilment persists the AUTHORISED entitlement onto the org.
        elif "update organizations set credits_per_seat" in s:
            self.rowcount = 1
        elif "update organizations set seats_purchased" in s:
            self.rowcount = 1
        elif "update organizations set updated_at" in s:
            self.rowcount = 1
        elif "update organization_payments" in s:
            self.rowcount = 1

        else:
            self._fetch = None
        return self

    def fetchall(self):
        return self._fetchall_rows

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


def _teams_checkout_on():
    """Enable the Teams checkout kill switch for one test.

    The flag FAILS CLOSED — only the exact string "1" enables checkout — so every test
    that exercises a real checkout path has to turn it on explicitly. That is the point:
    a test which forgets sees 503, not a silent success.
    """
    return mock.patch.dict(os.environ, {"TEAMS_CHECKOUT_ENABLED": "1"})


def _patched(cfg):
    """Patches BOTH new_connection and get_db to return the same fake connection,
    since different endpoints under test use one or the other."""
    conn = FakeConn(cfg)
    p1 = mock.patch.object(function_app, "new_connection", return_value=conn)
    p2 = mock.patch.object(function_app, "get_db", return_value=conn)
    return conn, p1, p2


def _auth_as(admin_user_id="admin-1", email="admin@acme.com"):
    """Explicitly (re)patches BOTH auth functions this module uses, scoped to one
    test via mock.patch's context-manager form. This does NOT rely on the
    module-level _mod("shared.auth", ...) stub above staying untouched — other
    test files imported into the same process (test_dispatch_logic.py etc.) mutate
    that SAME shared mock object's return_value in their own tests, since
    sys.modules is one shared cache for the whole process. Re-patching per test
    keeps this file correct regardless of what order tests run in or what else
    ran before it."""
    return (
        mock.patch.object(function_app, "validate_token",
                           return_value={"oid": admin_user_id, "email": email}),
        mock.patch.object(function_app, "get_user_id", return_value=admin_user_id),
    )


# ═══════════════════════════════════════════════════════════════════════════
# shared/org_credits.py — the function that decides which pool a job spends from
# ═══════════════════════════════════════════════════════════════════════════
class OrgCreditsTests(unittest.TestCase):
    def test_active_member_of_active_org_uses_org_pool(self):
        from shared.org_credits import effective_credits
        cur = FakeCursor({"membership_row": ("org-1", 7, "member-1")})
        credits, org_id = effective_credits(cur, "user-1", personal_credits=50)
        self.assertEqual((credits, org_id), (7, "org-1"))

    def test_no_membership_falls_back_to_personal_pool(self):
        from shared.org_credits import effective_credits
        cur = FakeCursor({"membership_row": None})
        credits, org_id = effective_credits(cur, "user-1", personal_credits=50)
        self.assertEqual((credits, org_id), (50, None))


class MyOrganizationTests(unittest.TestCase):
    def test_pending_payment_workspace_is_visible_to_admin(self):
        cfg = {
            "my_organization_row": (
                "org-1", 0, "member-1", "Acme", "admin-1", 10,
                0, None, "pending_payment", 10,
            ),
            "lora_status": "none",
        }
        conn, p1, p2 = _patched(cfg)
        auth1, auth2 = _auth_as("admin-1")
        p1.start(); p2.start(); auth1.start(); auth2.start()
        try:
            resp = function_app.get_my_organization(FakeRequest())
        finally:
            p1.stop(); p2.stop(); auth1.stop(); auth2.stop()

        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.body)
        self.assertEqual(data["organization"]["organization_id"], "org-1")
        self.assertEqual(data["organization"]["status"], "pending_payment")
        self.assertEqual(data["organization"]["seats_purchased"], 10)
        self.assertTrue(data["organization"]["is_admin"])
        self.assertEqual(data["membership"]["credits_remaining"], 0)

    def test_user_without_pending_or_active_membership_has_no_organization(self):
        cfg = {"my_organization_row": None}
        conn, p1, p2 = _patched(cfg)
        auth1, auth2 = _auth_as("user-1")
        p1.start(); p2.start(); auth1.start(); auth2.start()
        try:
            resp = function_app.get_my_organization(FakeRequest())
        finally:
            p1.stop(); p2.stop(); auth1.stop(); auth2.stop()

        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(json.loads(resp.body)["organization"])


# ═══════════════════════════════════════════════════════════════════════════
# reserve_job_slot — org-aware credit charging (shared/job_reservation.py)
# ═══════════════════════════════════════════════════════════════════════════
class ReserveJobSlotOrgTests(unittest.TestCase):
    def _reserve(self, cfg):
        from shared.job_reservation import reserve_job_slot
        conn = FakeConn(cfg)
        with mock.patch("shared.job_reservation.new_connection", return_value=conn), \
             mock.patch("shared.job_reservation.outbox_add", return_value=12345):
            result = reserve_job_slot(
                user_id="user-1", input_blob_path="", job_params="{}",
                per_user_cap=5, global_cap=25, credit_cost=1,
            )
        return result, conn

    def test_org_member_charges_org_pool_not_personal(self):
        cfg = {
            "applock_rc": 0,
            "membership_row": ("org-1", 7, "member-1"),  # 7 org credits available
            "personal_credits": 999,  # should never even be read
            "new_job_id": 42,
        }
        result, conn = self._reserve(cfg)
        self.assertTrue(result.ok)
        self.assertEqual(result.job_id, 42)
        self.assertTrue(conn.committed)

        executed_sql = [sql.lower() for sql, _ in cfg["executed"]]
        org_charges = [s for s in executed_sql
                       if "update organization_members set credits_remaining = credits_remaining -" in s]
        personal_charges = [s for s in executed_sql
                             if "update users set credits_remaining = credits_remaining -" in s]
        self.assertEqual(len(org_charges), 1, "should charge the org pool exactly once")
        self.assertEqual(len(personal_charges), 0, "must NOT touch the personal pool")

        # The job row itself should have been inserted with the org_id, not NULL.
        insert_call = next(p for sql, p in cfg["executed"] if "insert into jobs" in sql.lower())
        self.assertIn("org-1", insert_call)

    def test_org_member_insufficient_org_credits_blocks(self):
        cfg = {
            "applock_rc": 0,
            "membership_row": ("org-1", 0, "member-1"),  # 0 credits left in the org pool
            "personal_credits": 999,  # plenty personally — must NOT be used as a fallback
        }
        result, conn = self._reserve(cfg)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "credits")
        self.assertTrue(conn.rolled_back)
        # Nothing should have been charged or inserted.
        executed_sql = [sql.lower() for sql, _ in cfg["executed"]]
        self.assertFalse(any("insert into jobs" in s for s in executed_sql))

    def test_no_membership_charges_personal_pool_as_before(self):
        cfg = {
            "applock_rc": 0,
            "membership_row": None,          # not an org member
            "personal_credits": 20,
            "new_job_id": 43,
        }
        result, conn = self._reserve(cfg)
        self.assertTrue(result.ok)
        self.assertTrue(conn.committed)

        executed_sql = [sql.lower() for sql, _ in cfg["executed"]]
        self.assertTrue(any(
            "update users set credits_remaining = credits_remaining -" in s
            for s in executed_sql))
        insert_call = next(p for sql, p in cfg["executed"] if "insert into jobs" in sql.lower())
        self.assertIsNone(insert_call[-1], "organization_id column should be NULL")

    def test_no_membership_insufficient_personal_credits_blocks(self):
        cfg = {"applock_rc": 0, "membership_row": None, "personal_credits": 0}
        result, conn = self._reserve(cfg)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "credits")
        self.assertTrue(conn.rolled_back)


# ═══════════════════════════════════════════════════════════════════════════
# POST /orgs — create_organization
# ═══════════════════════════════════════════════════════════════════════════
class CreateOrganizationTests(unittest.TestCase):
    def test_creates_org_locked_with_admin_membership_at_zero_credits(self):
        """PAYMENT GATING: the org must start unusable, not active. This is the
        actual gate — every other check (payment-intent, _require_org_admin)
        just reads this status/these credits; if creation ever goes back to
        granting real credits immediately, the whole gate silently stops
        mattering again, exactly like it did before this fix."""
        cfg = {}
        conn, p1, p2 = _patched(cfg)
        auth1, auth2 = _auth_as("admin-1")
        p1.start(); p2.start(); auth1.start(); auth2.start()
        try:
            req = FakeRequest(body={"name": "Acme Corp", "seats_purchased": 10})
            resp = function_app.create_organization(req)
        finally:
            p1.stop(); p2.stop(); auth1.stop(); auth2.stop()

        self.assertEqual(resp.status_code, 201)
        self.assertTrue(conn.committed)
        self.assertFalse(conn.rolled_back)
        self.assertEqual(json.loads(resp.body)["status"], "pending_payment")

        org_insert_sql = next(sql for sql, _ in cfg["executed"] if "insert into organizations" in sql.lower())
        self.assertIn("pending_payment", org_insert_sql.lower(), "org must not start active")

        member_insert = next(
            p for sql, p in cfg["executed"] if "insert into organization_members" in sql.lower())
        # (membership_id, organization_id, user_id) then hardcoded 0, 0 in the SQL
        # text itself, not bound params — so the params tuple is just the three IDs.
        self.assertEqual(len(member_insert), 3, "credits are hardcoded 0 in the SQL, not bound")

    def test_missing_name_returns_400_before_touching_the_db(self):
        cfg = {}
        conn, p1, p2 = _patched(cfg)
        auth1, auth2 = _auth_as("admin-1")
        p1.start(); p2.start(); auth1.start(); auth2.start()
        try:
            req = FakeRequest(body={"seats_purchased": 5})
            resp = function_app.create_organization(req)
        finally:
            p1.stop(); p2.stop(); auth1.stop(); auth2.stop()
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(cfg.get("executed", []), [])

    def test_zero_seats_rejected(self):
        cfg = {}
        conn, p1, p2 = _patched(cfg)
        auth1, auth2 = _auth_as("admin-1")
        p1.start(); p2.start(); auth1.start(); auth2.start()
        try:
            req = FakeRequest(body={"name": "Acme", "seats_purchased": 0})
            resp = function_app.create_organization(req)
        finally:
            p1.stop(); p2.stop(); auth1.stop(); auth2.stop()
        self.assertEqual(resp.status_code, 400)


# ═══════════════════════════════════════════════════════════════════════════
# POST /orgs/{org_id}/invitations — create_invitations
# ═══════════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════
# Payment gating — an org must be worthless until Stripe confirms payment
# ═══════════════════════════════════════════════════════════════════════════
GATING_ADMIN = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class PaymentGatingTests(unittest.TestCase):
    def test_payment_intent_blocked_once_already_active(self):
        cfg = {"payment_intent_org_row": (GATING_ADMIN, 10, "active")}
        # Checkout requires a persisted quote; this one is valid so the GATE under test
        # is what decides the outcome.
        _exp = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=30)
        _qid = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
        cfg["quote_row"] = (_qid, GATING_ADMIN, None, 10, 34700, 30,
                            "teams_basic_v1", "teams_basic", "usd", _exp, "issued", None,
                            _V1_10_SEAT_BANDS)
        conn, p1, p2 = _patched(cfg)
        auth1, auth2 = _auth_as(GATING_ADMIN)
        flag = _teams_checkout_on(); flag.start()
        p1.start(); p2.start(); auth1.start(); auth2.start()
        try:
            req = FakeRequest(body={"quote_id": _qid},
                              route_params={"organization_id": "org-1"})
            resp = function_app.create_org_payment_intent(req)
        finally:
            p1.stop(); p2.stop(); auth1.stop(); auth2.stop(); flag.stop()
        self.assertEqual(resp.status_code, 409)

    def test_payment_intent_refused_when_checkout_is_disabled(self):
        """The kill switch must beat every other outcome, including a payable org."""
        cfg = {"payment_intent_org_row": ("admin-1", 10, "pending_payment")}
        conn, p1, p2 = _patched(cfg)
        auth1, auth2 = _auth_as("admin-1")
        # No _teams_checkout_on() — the flag is absent, which must read as DISABLED.
        off = mock.patch.dict(os.environ, {}, clear=False)
        os.environ.pop("TEAMS_CHECKOUT_ENABLED", None)
        off.start(); p1.start(); p2.start(); auth1.start(); auth2.start()
        try:
            resp = function_app.create_org_payment_intent(
                FakeRequest(route_params={"organization_id": "org-1"}))
        finally:
            p1.stop(); p2.stop(); auth1.stop(); auth2.stop(); off.stop()
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(json.loads(resp.body)["error"], "TEAMS_CHECKOUT_DISABLED")

    def test_payment_intent_allowed_while_locked(self):
        cfg = {"payment_intent_org_row": (GATING_ADMIN, 10, "pending_payment")}
        # Checkout requires a persisted quote; this one is valid so the GATE under test
        # is what decides the outcome.
        _exp = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=30)
        _qid = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
        cfg["quote_row"] = (_qid, GATING_ADMIN, None, 10, 34700, 30,
                            "teams_basic_v1", "teams_basic", "usd", _exp, "issued", None,
                            _V1_10_SEAT_BANDS)
        conn, p1, p2 = _patched(cfg)
        auth1, auth2 = _auth_as(GATING_ADMIN)
        flag = _teams_checkout_on(); flag.start()
        checkout_patch = mock.patch.object(
            function_app, "create_org_seats_checkout",
            return_value={"url": "https://stripe.test/checkout", "id": "cs_test_1"})
        checkout_patch.start()
        p1.start(); p2.start(); auth1.start(); auth2.start()
        try:
            req = FakeRequest(body={"quote_id": _qid},
                              route_params={"organization_id": "org-1"})
            resp = function_app.create_org_payment_intent(req)
        finally:
            p1.stop(); p2.stop(); auth1.stop(); auth2.stop()
            checkout_patch.stop(); flag.stop()
        self.assertEqual(resp.status_code, 200)

    def test_invitations_blocked_while_org_still_pending_payment(self):
        """This exercises the SAME gate _require_org_admin already had before this
        fix — it just could never actually fire, because every org was created
        'active' already. Nothing new here except that it's finally reachable."""
        cfg = {"org_admin_row": ("org-1", 10, 10, "pending_payment", "Acme")}
        conn, p1, p2 = _patched(cfg)
        auth1, auth2 = _auth_as("admin-1")
        email_patch = mock.patch.object(function_app, "send_invite_email", return_value=True)
        email_patch.start()
        p1.start(); p2.start(); auth1.start(); auth2.start()
        try:
            req = FakeRequest(body={"emails": ["a@acme.com"]}, route_params={"org_id": "org-1"})
            resp = function_app.create_invitations(req)
        finally:
            p1.stop(); p2.stop(); auth1.stop(); auth2.stop(); email_patch.stop()
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(json.loads(resp.body)["error"], "ORG_NOT_ACTIVE")

    def test_webhook_unlocks_org_and_raises_admin_credits_from_zero(self):
        # Snapshot-backed: a session with no snapshot now fails closed, so fulfilment is
        # exercised through the authorised path it will actually take in production.
        # The last column is breakdown_json: the version's validator proves the bands
        # are contiguous, complete and correctly priced rather than trusting the total.
        cfg = {"webhook_org_row": ("admin-1", 30), "webhook_membership_rowcount": 1,
               "checkout_snapshot": ("org-1", "teams_basic_v1", "teams_basic", 10, 30,
                                     34700, "usd", "pending",
                                     "7c9e6679-7425-40de-944b-e07fc1f90ae7",
                                     json.dumps([
                                         {"from_seat": 1, "to_seat": 9,
                                          "unit_price_cents": 3500, "seats": 9,
                                          "subtotal_cents": 31500},
                                         {"from_seat": 10, "to_seat": 24,
                                          "unit_price_cents": 3200, "seats": 1,
                                          "subtotal_cents": 3200},
                                     ]))}
        conn, p1, p2 = _patched(cfg)
        p1.start(); p2.start()
        try:
            function_app._handle_org_payment(
                {"id": "cs_unlock", "metadata": {"organization_id": "org-1"},
                 "amount_total": 34700, "currency": "usd", "payment_intent": "pi_1"},
                "evt_unlock_1",
            )
        finally:
            p1.stop(); p2.stop()
        self.assertTrue(conn.committed)
        self.assertFalse(conn.rolled_back)

        executed_sql = [sql.lower() for sql, _ in cfg["executed"]]
        self.assertTrue(any("update organizations set status = 'active'" in s for s in executed_sql))

        grant_call = next(
            p for sql, p in cfg["executed"] if "set credits_granted = ?, credits_remaining = ?" in sql.lower())
        self.assertEqual(grant_call, (30, 30, "org-1", "admin-1"),
                          "must raise the admin's own row from 0 to the AUTHORISED grant")

    def test_webhook_no_op_if_org_already_active(self):
        """A duplicate/replayed event landing after the org is already unlocked
        must not re-grant credits — same reasoning as the individual-user
        payment path's own duplicate protection."""
        cfg = {"webhook_org_row": None}  # query filters WHERE status='pending_payment' — no match
        conn, p1, p2 = _patched(cfg)
        p1.start(); p2.start()
        try:
            function_app._handle_org_payment(
                {"metadata": {"organization_id": "org-1"}, "amount_total": 20000,
                 "currency": "usd", "payment_intent": "pi_1"},
                "evt_dup_1",
            )
        finally:
            p1.stop(); p2.stop()
        self.assertTrue(conn.rolled_back)
        self.assertFalse(conn.committed)

    def test_webhook_missing_admin_membership_row_rolls_back_loudly(self):
        cfg = {"webhook_org_row": ("admin-1", 10), "webhook_membership_rowcount": 0}
        conn, p1, p2 = _patched(cfg)
        p1.start(); p2.start()
        try:
            function_app._handle_org_payment(
                {"metadata": {"organization_id": "org-1"}, "amount_total": 20000,
                 "currency": "usd", "payment_intent": "pi_1"},
                "evt_missing_1",
            )
        finally:
            p1.stop(); p2.stop()
        self.assertTrue(conn.rolled_back)
        self.assertFalse(conn.committed)


class CreateInvitationsTests(unittest.TestCase):
    def setUp(self):
        self._email_patch = mock.patch.object(
            function_app, "send_invite_email", return_value=True)
        self._email_mock = self._email_patch.start()
        self.addCleanup(self._email_patch.stop)

        auth1, auth2 = _auth_as("admin-1")
        auth1.start(); auth2.start()
        self.addCleanup(auth1.stop)
        self.addCleanup(auth2.stop)

    def test_invites_within_seat_limit_all_created(self):
        cfg = {
            "org_admin_row": ("org-1", 10, 10, "active", "Acme"),  # 10 seats total
            "active_members": 2,
            "pending_invites": 1,
            "already_invited": [],
        }
        conn, p1, p2 = _patched(cfg)
        p1.start(); p2.start()
        try:
            req = FakeRequest(
                body={"emails": ["a@acme.com", "b@acme.com"]},
                route_params={"org_id": "org-1"},
            )
            resp = function_app.create_invitations(req)
        finally:
            p1.stop(); p2.stop()

        self.assertEqual(resp.status_code, 201)
        data = json.loads(resp.body)
        self.assertEqual(len(data["created"]), 2)
        self.assertEqual(len(data["skipped"]), 0)
        self.assertEqual(self._email_mock.call_count, 2)

    def test_invites_beyond_remaining_seats_are_skipped(self):
        # 10 seats, 9 already taken (members + pending) -> only 1 seat left
        cfg = {
            "org_admin_row": ("org-1", 10, 10, "active", "Acme"),
            "active_members": 8,
            "pending_invites": 1,
            "already_invited": [],
        }
        conn, p1, p2 = _patched(cfg)
        p1.start(); p2.start()
        try:
            req = FakeRequest(
                body={"emails": ["a@acme.com", "b@acme.com", "c@acme.com"]},
                route_params={"org_id": "org-1"},
            )
            resp = function_app.create_invitations(req)
        finally:
            p1.stop(); p2.stop()

        data = json.loads(resp.body)
        self.assertEqual(len(data["created"]), 1, "only the one remaining seat should be used")
        self.assertEqual(len(data["skipped"]), 2)
        self.assertTrue(all(s["reason"] == "NO_SEATS_AVAILABLE" for s in data["skipped"]))

    def test_no_seats_left_at_all_returns_409(self):
        cfg = {
            "org_admin_row": ("org-1", 5, 10, "active", "Acme"),
            "active_members": 5,
            "pending_invites": 0,
            "already_invited": [],
        }
        conn, p1, p2 = _patched(cfg)
        p1.start(); p2.start()
        try:
            req = FakeRequest(body={"emails": ["a@acme.com"]}, route_params={"org_id": "org-1"})
            resp = function_app.create_invitations(req)
        finally:
            p1.stop(); p2.stop()
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(self._email_mock.call_count, 0)

    def test_duplicate_and_already_invited_emails_are_skipped_not_double_counted(self):
        cfg = {
            "org_admin_row": ("org-1", 10, 10, "active", "Acme"),
            "active_members": 0,
            "pending_invites": 1,
            "already_invited": ["a@acme.com"],
        }
        conn, p1, p2 = _patched(cfg)
        p1.start(); p2.start()
        try:
            req = FakeRequest(
                # a@acme.com is already invited; the duplicate + differently-cased
                # copy should both collapse to a single skip, not spend a seat twice
                body={"emails": ["A@Acme.com ", "a@acme.com", "new@acme.com"]},
                route_params={"org_id": "org-1"},
            )
            resp = function_app.create_invitations(req)
        finally:
            p1.stop(); p2.stop()
        data = json.loads(resp.body)
        self.assertEqual(len(data["created"]), 1)
        self.assertEqual(data["created"][0]["email"], "new@acme.com")
        self.assertEqual(len(data["skipped"]), 1)
        self.assertEqual(data["skipped"][0]["reason"], "ALREADY_INVITED")

    def test_non_admin_caller_gets_404_not_403(self):
        # _require_org_admin looks up org_id AND admin_user_id together, so a
        # non-owner gets "not found", never confirming the org exists.
        cfg = {"org_admin_row": None}
        conn, p1, p2 = _patched(cfg)
        p1.start(); p2.start()
        try:
            req = FakeRequest(body={"emails": ["a@acme.com"]}, route_params={"org_id": "org-1"})
            resp = function_app.create_invitations(req)
        finally:
            p1.stop(); p2.stop()
        self.assertEqual(resp.status_code, 404)


# ═══════════════════════════════════════════════════════════════════════════
# POST /invitations/{token}/accept — accept_invitation
# ═══════════════════════════════════════════════════════════════════════════
class OrganizationBrandingTests(unittest.TestCase):
    def setUp(self):
        self.branding_row = (
            None, None, "Studio Neutral", "Business / Corporate", 20,
            "default", datetime.now(timezone.utc).replace(tzinfo=None),
        )

    def test_pending_payment_admin_can_read_branding(self):
        cfg = {
            "org_admin_row": ("org-1", 10, 30, "pending_payment", "Acme"),
            "branding_row": self.branding_row,
        }
        conn, p1, p2 = _patched(cfg)
        auth1, auth2 = _auth_as("admin-1")
        p1.start(); p2.start(); auth1.start(); auth2.start()
        try:
            response = function_app.get_org_branding(
                FakeRequest(route_params={"org_id": "org-1"}))
        finally:
            p1.stop(); p2.stop(); auth1.stop(); auth2.stop()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["branding"]["style_key"], "Studio Neutral")

    def test_pending_payment_admin_can_save_branding(self):
        cfg = {
            "org_admin_row": ("org-1", 10, 30, "pending_payment", "Acme"),
            "branding_row": self.branding_row,
        }
        conn, p1, p2 = _patched(cfg)
        auth1, auth2 = _auth_as("admin-1")
        p1.start(); p2.start(); auth1.start(); auth2.start()
        try:
            response = function_app.set_org_branding(FakeRequest(
                route_params={"org_id": "org-1"},
                body={
                    "style_key": "Studio Neutral",
                    "use_case_key": "Business / Corporate",
                    "max_images_per_member": 20,
                    "enforcement_mode": "default",
                },
            ))
        finally:
            p1.stop(); p2.stop(); auth1.stop(); auth2.stop()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(conn.committed)


class AcceptInvitationTests(unittest.TestCase):
    def _accept(self, cfg, token="tok-abc", accepting_user="user-2"):
        conn, p1, p2 = _patched(cfg)
        auth1, auth2 = _auth_as(accepting_user)
        p1.start(); p2.start(); auth1.start(); auth2.start()
        try:
            req = FakeRequest(route_params={"token": token})
            resp = function_app.accept_invitation(req)
        finally:
            p1.stop(); p2.stop(); auth1.stop(); auth2.stop()
        return resp, conn

    def test_unknown_token_returns_404(self):
        resp, conn = self._accept({"invite_row": None})
        self.assertEqual(resp.status_code, 404)

    def test_already_accepted_token_returns_409(self):
        resp, conn = self._accept({
            "invite_row": ("inv-1", "org-1", "accepted", None),
        })
        self.assertEqual(resp.status_code, 409)

    def test_expired_token_marks_expired_and_returns_410(self):
        from datetime import datetime, timedelta
        past = datetime.utcnow() - timedelta(days=1)
        cfg = {"invite_row": ("inv-1", "org-1", "pending", past)}
        resp, conn = self._accept(cfg)
        self.assertEqual(resp.status_code, 410)
        executed_sql = [sql.lower() for sql, _ in cfg["executed"]]
        self.assertTrue(any("status = 'expired'" in s for s in executed_sql))
        self.assertTrue(conn.committed, "the expiry write itself should still commit")

    def test_already_member_of_a_different_org_returns_409(self):
        from datetime import datetime, timedelta
        future = datetime.utcnow() + timedelta(days=5)
        cfg = {
            "invite_row": ("inv-1", "org-1", "pending", future),
            "existing_membership": ("org-OTHER",),
        }
        resp, conn = self._accept(cfg)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(json.loads(resp.body)["error"], "ALREADY_IN_ANOTHER_ORG")

    def test_re_accepting_own_orgs_invite_is_a_friendly_no_op(self):
        from datetime import datetime, timedelta
        future = datetime.utcnow() + timedelta(days=5)
        cfg = {
            "invite_row": ("inv-1", "org-1", "pending", future),
            "existing_membership": ("org-1",),  # already in THIS org
        }
        resp, conn = self._accept(cfg)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Already a member", json.loads(resp.body)["message"])

    def test_org_full_returns_409(self):
        from datetime import datetime, timedelta
        future = datetime.utcnow() + timedelta(days=5)
        cfg = {
            "invite_row": ("inv-1", "org-1", "pending", future),
            "existing_membership": None,
            "org_row": (5, 10, "active"),  # 5 seats purchased
            "active_members": 5,           # all 5 already filled
        }
        resp, conn = self._accept(cfg)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(json.loads(resp.body)["error"], "NO_SEATS_AVAILABLE")

    def test_successful_accept_grants_credits_and_marks_invite_accepted(self):
        from datetime import datetime, timedelta
        future = datetime.utcnow() + timedelta(days=5)
        cfg = {
            "invite_row": ("inv-1", "org-1", "pending", future),
            "existing_membership": None,
            "org_row": (10, 10, "active"),  # 10 seats, 10 credits/seat
            "active_members": 3,            # room available
            "new_membership_id": "member-99",
        }
        resp, conn = self._accept(cfg)
        self.assertEqual(resp.status_code, 201)
        data = json.loads(resp.body)
        self.assertEqual(data["membership_id"], "member-99")
        self.assertEqual(data["credits_granted"], 10)
        self.assertTrue(conn.committed)

        executed_sql = [sql.lower() for sql, _ in cfg["executed"]]
        self.assertTrue(any("status = 'accepted'" in s for s in executed_sql))


# ===========================================================================
# Teams admin GUID comparison -- regression
# ===========================================================================
# WHY THIS EXISTS
# organizations.admin_user_id is UNIQUEIDENTIFIER. SQL Server renders it UPPERCASE
# through pyodbc; the caller-side id is an Entra oid, which arrives lowercase. Two
# Teams endpoints compare those two values in PYTHON rather than in SQL:
#
#     create_org_payment_intent   function_app.py:510
#     org_dashboard_summary       function_app.py:558
#
# A case-sensitive `!=` there rejects the org's real admin with 403. The fix
# (str(...).lower() on both sides) is already in HEAD; these tests are what keeps it
# there. They were written on the abandoned branch codex/merge-features-team
# (commit ea53384) and never merged, so the runtime fix has shipped with NO
# regression cover at all.
#
# _require_org_admin does NOT need this: it resolves authority in SQL
# ("WHERE organization_id = ? AND admin_user_id = ?"), and SQL Server compares
# uniqueidentifier as a binary type, so that predicate is already case-insensitive.
# The two endpoints above bypass it and compare in Python. That asymmetry is the bug.
#
# These drive the REAL endpoint functions end to end -- no source-text scanning.
UPPER_ADMIN = "ABCDEF01-2345-6789-ABCD-EF0123456789"
LOWER_ADMIN = "abcdef01-2345-6789-abcd-ef0123456789"
OTHER_ADMIN = "11111111-2222-4333-8444-555555555555"


class TeamsAdminGuidCaseTests(unittest.TestCase):
    """Same GUID, different casing, must be the same person."""

    def _payment_intent(self, stored_admin, caller):
        # Checkout now requires a persisted quote. These tests are about WHO the caller
        # is, so the quote is made valid for the caller and the org — the admin check is
        # what must decide the outcome, and it runs before the quote is even loaded.
        from datetime import datetime, timedelta, timezone
        expires = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=30)
        quote_id = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
        cfg = {
            "payment_intent_org_row": (stored_admin, 10, "pending_payment"),
            "quote_row": (quote_id, caller, None, 10, 34700, 30,
                          "teams_basic_v1", "teams_basic", "usd", expires, "issued", None,
                          _V1_10_SEAT_BANDS),
        }
        conn, p1, p2 = _patched(cfg)
        auth1, auth2 = _auth_as(caller)
        # Checkout is enabled here on purpose: these tests are about WHO the caller is,
        # so the kill switch must not be what decides the outcome.
        flag = _teams_checkout_on(); flag.start()
        checkout = mock.patch.object(
            function_app, "create_org_seats_checkout",
            return_value={"url": "https://stripe.test/checkout", "id": "cs_test_case"})
        checkout.start(); p1.start(); p2.start(); auth1.start(); auth2.start()
        try:
            return function_app.create_org_payment_intent(
                FakeRequest(body={"quote_id": quote_id},
                            route_params={"organization_id": "org-1"}))
        finally:
            p1.stop(); p2.stop(); auth1.stop(); auth2.stop()
            checkout.stop(); flag.stop()

    def _dashboard(self, stored_admin, caller):
        cfg = {
            "dashboard_org_row": (stored_admin, "Acme", 10, "active"),
            "active_members": 1,
            "org_credits": 10,
            "org_job_counts": [("completed", 2)],
        }
        conn, p1, p2 = _patched(cfg)
        auth1, auth2 = _auth_as(caller)
        p1.start(); p2.start(); auth1.start(); auth2.start()
        try:
            return function_app.org_dashboard_summary(
                FakeRequest(route_params={"organization_id": "org-1"}))
        finally:
            p1.stop(); p2.stop(); auth1.stop(); auth2.stop()

    def _my_org(self, stored_admin, caller):
        cfg = {
            "my_organization_row": ("org-1", 0, "member-1", "Acme", stored_admin, 10,
                                    0, None, "active", 10),
            "lora_status": "none",
        }
        conn, p1, p2 = _patched(cfg)
        auth1, auth2 = _auth_as(caller)
        p1.start(); p2.start(); auth1.start(); auth2.start()
        try:
            return function_app.get_my_organization(FakeRequest())
        finally:
            p1.stop(); p2.stop(); auth1.stop(); auth2.stop()

    # -- create_org_payment_intent (function_app.py:510) --------------------
    def test_payment_intent_accepts_same_guid_with_different_case(self):
        """THE REGRESSION: uppercase in SQL, lowercase from the token."""
        self.assertEqual(self._payment_intent(UPPER_ADMIN, LOWER_ADMIN).status_code, 200)

    def test_payment_intent_accepts_the_reverse_casing_too(self):
        self.assertEqual(self._payment_intent(LOWER_ADMIN, UPPER_ADMIN).status_code, 200)

    def test_payment_intent_accepts_identical_casing(self):
        self.assertEqual(self._payment_intent(LOWER_ADMIN, LOWER_ADMIN).status_code, 200)

    def test_payment_intent_rejects_a_different_guid(self):
        """Case-insensitivity must not become permissiveness."""
        self.assertEqual(self._payment_intent(UPPER_ADMIN, OTHER_ADMIN).status_code, 403)

    def test_payment_intent_rejects_a_guid_one_hex_digit_apart(self):
        near = LOWER_ADMIN[:-1] + ("8" if LOWER_ADMIN[-1] != "8" else "a")
        self.assertEqual(self._payment_intent(UPPER_ADMIN, near).status_code, 403)

    def test_payment_intent_fails_closed_on_a_malformed_caller_id(self):
        for bad in ("not-a-guid", "", "abcdef01", None):
            with self.subTest(caller=bad):
                self.assertEqual(self._payment_intent(UPPER_ADMIN, bad).status_code, 403)

    def test_payment_intent_fails_closed_on_a_malformed_stored_admin(self):
        self.assertEqual(self._payment_intent("not-a-guid", LOWER_ADMIN).status_code, 403)

    # -- org_dashboard_summary (function_app.py:558) ------------------------
    def test_dashboard_accepts_same_guid_with_different_case(self):
        resp = self._dashboard(UPPER_ADMIN, LOWER_ADMIN)
        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.body)
        self.assertEqual(body["job_status_counts"], {"completed": 2})
        self.assertEqual(body["members_joined"], 1)
        self.assertEqual(body["credits_remaining_total"], 10)

    def test_dashboard_accepts_the_reverse_casing_too(self):
        self.assertEqual(self._dashboard(LOWER_ADMIN, UPPER_ADMIN).status_code, 200)

    def test_dashboard_rejects_a_different_guid(self):
        self.assertEqual(self._dashboard(UPPER_ADMIN, OTHER_ADMIN).status_code, 403)

    def test_dashboard_fails_closed_on_a_malformed_caller_id(self):
        for bad in ("not-a-guid", "", None):
            with self.subTest(caller=bad):
                self.assertEqual(self._dashboard(UPPER_ADMIN, bad).status_code, 403)

    def test_dashboard_404_for_a_missing_org_never_reaches_the_comparison(self):
        """A wrong-org request must not confirm the org exists."""
        cfg = {"dashboard_org_row": None}
        conn, p1, p2 = _patched(cfg)
        auth1, auth2 = _auth_as(LOWER_ADMIN)
        p1.start(); p2.start(); auth1.start(); auth2.start()
        try:
            resp = function_app.org_dashboard_summary(
                FakeRequest(route_params={"organization_id": "org-nope"}))
        finally:
            p1.stop(); p2.stop(); auth1.stop(); auth2.stop()
        self.assertEqual(resp.status_code, 404)

    # -- get_my_organization is_admin flag (function_app.py:6884) -----------
    def test_is_admin_flag_is_true_for_the_same_guid_in_different_case(self):
        """Not in ea53384, but the same comparison and the same failure mode: a
        case-sensitive check here silently demotes the admin to a plain member in the
        dashboard UI -- a wrong answer with a 200, not an error."""
        resp = self._my_org(UPPER_ADMIN, LOWER_ADMIN)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(json.loads(resp.body)["organization"]["is_admin"])

    def test_is_admin_flag_is_false_for_a_different_guid(self):
        resp = self._my_org(UPPER_ADMIN, OTHER_ADMIN)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(json.loads(resp.body)["organization"]["is_admin"])

    def test_is_admin_flag_is_false_for_a_malformed_caller_id(self):
        resp = self._my_org(UPPER_ADMIN, "not-a-guid")
        self.assertFalse(json.loads(resp.body)["organization"]["is_admin"])


class AcceptInvitationOrgIdCaseTests(unittest.TestCase):
    """accept_invitation (function_app.py:6682) compares the caller's existing
    membership org against the invite's org in Python. Same uniqueidentifier casing
    problem: a case-sensitive check turns "re-accepting your own invite" (a friendly
    200 no-op) into ALREADY_IN_ANOTHER_ORG 409, locking a member out of their own org."""

    ORG_UPPER = "0FEEDBEE-1234-4567-89AB-CDEF01234567"
    ORG_LOWER = "0feedbee-1234-4567-89ab-cdef01234567"
    ORG_OTHER = "99999999-8888-4777-8666-555555555555"

    def _accept(self, invite_org, existing_org):
        from datetime import datetime, timedelta
        cfg = {
            "invite_row": ("inv-1", invite_org, "pending",
                           datetime.utcnow() + timedelta(days=5)),
            "existing_membership": (existing_org,),
        }
        conn, p1, p2 = _patched(cfg)
        auth1, auth2 = _auth_as("user-2")
        p1.start(); p2.start(); auth1.start(); auth2.start()
        try:
            return function_app.accept_invitation(
                FakeRequest(route_params={"token": "tok-abc"}))
        finally:
            p1.stop(); p2.stop(); auth1.stop(); auth2.stop()

    def test_re_accepting_own_org_invite_works_across_casing(self):
        resp = self._accept(self.ORG_UPPER, self.ORG_LOWER)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(json.loads(resp.body)["message"], "Already a member")

    def test_a_genuinely_different_org_is_still_409(self):
        resp = self._accept(self.ORG_UPPER, self.ORG_OTHER)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(json.loads(resp.body)["error"], "ALREADY_IN_ANOTHER_ORG")

    def test_a_malformed_existing_org_id_fails_closed_as_a_different_org(self):
        resp = self._accept(self.ORG_UPPER, "not-a-guid")
        self.assertEqual(resp.status_code, 409)


class TheseTestsDriveTheRealFunctions(unittest.TestCase):
    """Guard against this regression cover degenerating into source-text scanning."""

    def test_the_endpoints_under_test_are_the_real_module_functions(self):
        import types
        for name in ("create_org_payment_intent", "org_dashboard_summary",
                     "get_my_organization", "accept_invitation"):
            fn = getattr(function_app, name)
            self.assertIsInstance(fn, types.FunctionType,
                                  "%s must be a real function" % name)
            self.assertEqual(fn.__module__, function_app.__name__)

    def test_the_comparison_is_actually_executed_not_asserted_about(self):
        """Proof the fake reached the real SQL: the endpoint must have issued the
        organizations lookup whose result feeds the comparison."""
        cfg = {"dashboard_org_row": (UPPER_ADMIN, "Acme", 10, "active"),
               "active_members": 0, "org_credits": 0, "org_job_counts": []}
        conn, p1, p2 = _patched(cfg)
        auth1, auth2 = _auth_as(LOWER_ADMIN)
        p1.start(); p2.start(); auth1.start(); auth2.start()
        try:
            resp = function_app.org_dashboard_summary(
                FakeRequest(route_params={"organization_id": "org-1"}))
        finally:
            p1.stop(); p2.stop(); auth1.stop(); auth2.stop()
        self.assertEqual(resp.status_code, 200)
        executed = [sql.lower() for sql, _ in cfg["executed"]]
        self.assertTrue(
            any("from organizations where organization_id" in s for s in executed),
            "the org lookup that feeds the comparison never ran")



if __name__ == "__main__":
    unittest.main()
