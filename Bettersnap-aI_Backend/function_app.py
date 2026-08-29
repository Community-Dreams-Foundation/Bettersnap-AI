import azure.functions as func
import os
import hmac
import json
import logging
import re
import uuid
from uuid import UUID, uuid4
from datetime import datetime, timedelta, timezone
import secrets

# ── GPU cost ceilings (override via app settings) ─────────────────────────
# Deliberately conservative for an unproven A100 product — raise only after
# real cost/runtime data. See COST_CONTROLS.md.
MAX_ACTIVE_GPU_JOBS = int(os.environ.get("MAX_ACTIVE_GPU_JOBS", "1"))
PER_USER_DAILY_CAP  = int(os.environ.get("PER_USER_DAILY_CAP", "5"))
GLOBAL_DAILY_CAP    = int(os.environ.get("GLOBAL_DAILY_CAP", "25"))
# Over-cap back-pressure: exponential backoff (BASE * 2**defer, capped at MAX),
# with a hard defer ceiling after which the job is failed (DISPATCH_TIMEOUT) so a
# stuck cap / broken API can never churn the queue forever.
GPU_BACKPRESSURE_BASE = int(os.environ.get("GPU_BACKPRESSURE_BASE", "30"))
GPU_BACKPRESSURE_MAX  = int(os.environ.get("GPU_BACKPRESSURE_MAX", "600"))
MAX_DISPATCH_DEFERS   = int(os.environ.get("MAX_DISPATCH_DEFERS", "20"))
# Kill-switch pause uses a long, fixed delay (NOT the backoff) so an intentional
# GPU_DISPATCH_ENABLED=false doesn't churn the queue / logs every few seconds.
KILL_SWITCH_PAUSE_DELAY = int(os.environ.get("KILL_SWITCH_PAUSE_DELAY", "900"))


def _utc_iso(dt):
    """Serialise a DB timestamp as ISO-8601 WITH the Z marker. Use for every datetime
    that reaches JSON.

    Every timestamp column is written with GETUTCDATE(), so the value IS UTC — but pyodbc
    hands back a NAIVE datetime, and both str() and .isoformat() then emit it with no
    offset. JavaScript's Date() parses a string without an offset as LOCAL time, so the
    browser took a UTC instant to be local and rendered it unshifted: a customer in EDT
    saw "06:25 PM" on a job they created at 2:25 PM — four hours in the future.

    Fixed HERE rather than by formatting a fixed timezone in the UI, because the frontend
    already calls toLocaleString(undefined, ...) — once the instant is unambiguous, every
    viewer sees it in their OWN timezone, which also survives customers outside EST.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _route_job_id(req):
    """Return a canonical GUID string, or None for a malformed/missing route id."""
    try:
        return str(UUID(req.route_params.get("job_id", "")))
    except (AttributeError, TypeError, ValueError):
        return None


_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s.]+(?:\.[^@\s.]+)+$")


def _normalize_profile_email(value):
    """Validate and canonicalize a client-supplied profile email."""
    if not isinstance(value, str):
        raise ValueError("email must be a string")
    email = value.strip().lower()
    if len(email) > 254 or not _EMAIL_RE.fullmatch(email):
        raise ValueError("invalid email format")
    return email


def _is_duplicate_email_error(exc):
    """Recognize SQL Server unique-index violations specifically involving email."""
    detail = " ".join(str(arg) for arg in getattr(exc, "args", (exc,))).lower()
    return ("2601" in detail or "2627" in detail) and "email" in detail
# Head start for the FUSED train+generate path (MODE=train_infer, see _dispatch_training).
# The dispatcher fuses only if the user's generation job is ALREADY parked in
# 'waiting_lora' when it runs. The frontend calls /train first and /jobs/submit second, and
# this queue message is otherwise sent immediately — so the dispatcher fires within seconds
# and looks BEFORE the job exists, finds nothing, and falls back to plain MODE=train. The
# fused path then almost never triggers in the flow it was built for.
# Holding the message briefly lets /jobs/submit land first. Costs ~25s on a ~34-minute
# training run to save ~4 minutes (one cold start + one queue hop). NOT correctness-critical
# in either direction: if the job still isn't parked we simply don't fuse, which is exactly
# today's behaviour. Set to 0 to disable.
TRAIN_FUSE_HEAD_START = int(os.environ.get("TRAIN_FUSE_HEAD_START", "25"))
# Reaper: auto-fail jobs stuck in 'processing' or 'dispatching' past these thresholds,
# measured from dispatched_at (COALESCE created_at) — NOT submit time — so queue wait
# doesn't count against the deadline (finding #5, part A).
# The 'processing' threshold must exceed the LONGEST a healthy run can legitimately take.
# The GPU job's own replicaTimeout is 7200s = 120 min (job.yaml), so the GPU itself kills
# anything past that; a row still 'processing' beyond ~120 min from dispatch is therefore
# genuinely dead (OOM/SIGKILL left it), not slow. Default 130 keeps the reaper strictly
# less aggressive than the GPU's hard cap, so it can NEVER false-fail a healthy run. (Was
# 45, which — measured from submit — could reap a large job that merely waited in queue.)
REAPER_STUCK_MINUTES       = int(os.environ.get("REAPER_STUCK_MINUTES", "130"))
# How long after submit a user may cancel their own job. Enforced server-side (the UI also
# hides the button past this) so a late cancel on a job that's already deep into generation
# can't be forced. Measured from created_at.
CANCEL_WINDOW_MINUTES      = int(os.environ.get("CANCEL_WINDOW_MINUTES", "5"))
REAPER_DISPATCHING_MINUTES = int(os.environ.get("REAPER_DISPATCHING_MINUTES", "15"))

# ── Identity-LoRA training ────────────────────────────────────────────────
# DreamBooth needs enough angles/expressions to generalize, but the run is a fixed
# 1400 steps regardless of count — so more photos cost quality-nothing and time-nothing.
MIN_TRAINING_PHOTOS = int(os.environ.get("MIN_TRAINING_PHOTOS", "8"))
MAX_TRAINING_PHOTOS = int(os.environ.get("MAX_TRAINING_PHOTOS", "12"))
# Upload validation (0.6). Extension alone proved nothing — /upload accepted any bytes
# with a .jpg name and read the whole request into memory. Enforce a byte cap BEFORE
# decode, then decode with Pillow to confirm it is a real image of the claimed type, cap
# pixel dimensions, and guard against decompression bombs (a tiny file that expands to
# billions of pixels). Limits are generous for phone photos and env-tunable.
MAX_UPLOAD_BYTES  = int(os.environ.get("MAX_UPLOAD_BYTES", str(15 * 1024 * 1024)))   # 15 MB
MIN_UPLOAD_DIM    = int(os.environ.get("MIN_UPLOAD_DIM", "256"))                     # px
MAX_UPLOAD_DIM    = int(os.environ.get("MAX_UPLOAD_DIM", "8192"))                    # px
MAX_UPLOAD_PIXELS = int(os.environ.get("MAX_UPLOAD_PIXELS", str(40_000_000)))        # 40 MP
_ALLOWED_IMAGE_FORMATS = {"JPEG", "PNG", "MPO"}   # MPO = multi-frame JPEG from phones
# User profile attributes are persisted in job_params and interpolated into the
# generation prompt. Bound them before either happens; they remain free text for
# compatibility with existing clients and internationalized hair descriptions.
MAX_PROFILE_ATTRIBUTE_CHARS = 40
# Measured training wall-time is ~51 min (17.6 class-image gen + 28.1 train + startup).
# The watcher fails a run that blows past this, releasing any jobs parked behind it.
TRAINING_STUCK_MINUTES = int(os.environ.get("TRAINING_STUCK_MINUTES", "90"))
# Data retention: N days after the clock starts, the user's BLOBS (photos, LoRA,
# results) are deleted; DB rows are KEPT for analytics. One-time plans start the clock
# on each generation (last-gen + N); monthly plans on subscription end (period-end + N).
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "3"))
# Failed monthly renewals keep their remaining monthly credits for this many days.
# A later invoice.paid restores the normal monthly allowance automatically.
FAILED_PAYMENT_GRACE_DAYS = int(os.environ.get("FAILED_PAYMENT_GRACE_DAYS", "3"))

# BIPA/GDPR: block facial-data processing (upload/train) until server-side biometric consent
# exists. DEFAULT OFF so this can deploy before the migration + frontend consent flow are live;
# flip BIOMETRIC_CONSENT_REQUIRED=1 to enforce once those are in place (else uploads 403).
BIOMETRIC_CONSENT_REQUIRED = os.environ.get("BIOMETRIC_CONSENT_REQUIRED", "0").strip() == "1"
BIOMETRIC_CONSENT_PURPOSE  = "Biometric processing for AI photo generation"


def _write_event(user_id, event_type, target=None, detail=None, ip=None):
    """SOC-2 comprehensive audit trail (migration 029 `audit_log`). Append one event; BEST-EFFORT —
    opens its own autocommit connection and NEVER raises into the request path. Store only refs/ids
    and non-sensitive context in `detail`; never PII or biometric content."""
    try:
        get_db().cursor().execute(
            "INSERT INTO dbo.audit_log (user_id, event_type, target, detail, ip) VALUES (?,?,?,?,?)",
            (str(user_id) if user_id else None), str(event_type)[:48],
            (str(target)[:256] if target is not None else None),
            (json.dumps(detail) if detail is not None else None),
            (str(ip)[:64] if ip else None))
    except Exception as e:
        logging.warning(f"audit_log write failed event={event_type}: {e}")


def _gpu_dispatch_enabled() -> bool:
    # Emergency kill switch (read per-call so it takes effect without a redeploy).
    # A budget action group or an operator flips GPU_DISPATCH_ENABLED=false to
    # halt ALL A100 spend. Budgets only alert; this actually stops dispatch.
    return os.environ.get("GPU_DISPATCH_ENABLED", "true").lower() == "true"
from azure.storage.blob import generate_blob_sas, BlobSasPermissions
from shared.auth import validate_token, get_user_id
try:
    from shared.auth import require_admin, NotAdminError
except ImportError:  # lightweight test/legacy auth stubs may not expose admin helpers
    class NotAdminError(Exception):
        pass

    def require_admin(token):
        raise NotAdminError("Admin authorization is unavailable")
from shared.db import get_db, new_connection
from shared.job_reservation import reserve_job_slot
from shared.training_reservation import reserve_training_slot
from shared import credit_ledger
from shared import exec_reconcile
from shared.queue_client import enqueue_job, INFERENCE_QUEUE, TRAINING_QUEUE
from shared.outbox import outbox_add, outbox_try_send_now
from shared.blob import upload_blob, download_blob, get_blob_client
from shared.keyvault import get_secret
from shared.plans import (
    get_plan, credit_cost, public_plans, REGISTRATION_CREDITS, DEFAULT_PLAN_KEY,
    FREE_RETRAINS, RETRAIN_CREDITS, MAX_TRAININGS_PER_DAY, plan_key_for,
)
from shared import catalog
from shared.stripe_client import (
    ONE_TIME_PLANS, MONTHLY_PLANS,
    create_onetime_checkout, create_monthly_checkout, create_org_checkout,
    create_topup_checkout,
    cancel_subscription, find_checkout_session_by_token, get_subscription,
    reactivate_subscription, create_billing_portal, upgrade_subscription,
    monthly_plan_for_price_id, subscription_period_end, verify_webhook,
)
from shared.org_credits import effective_credits, get_active_membership
from shared.invite_email import send_invite_email

# ── Teams / Organizations (one-time-purchase model) ───────────────────────
ORG_CREDITS_PER_SEAT = int(os.environ.get("ORG_CREDITS_PER_SEAT", "10"))
ORG_PRICE_PER_SEAT_CENTS = int(os.environ.get("ORG_PRICE_PER_SEAT_CENTS", "2000"))

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# ── Health Check ──────────────────────────────────────────
@app.route(route="health", methods=["GET"])
def health(req: func.HttpRequest) -> func.HttpResponse:
    """Liveness, plus a readiness probe for the face-gate dependency.

    OpenCV is the one dependency that can fail in a way nothing else catches: a bad
    wheel imports cleanly but returns a HOLLOW module, so `import cv2` succeeds and the
    first real call dies with AttributeError. That surfaced only as a 500 on /train.
    Loading the cascade here turns a silent, deploy-shaped breakage into something a
    smoke test (or an uptime check) sees immediately.
    """
    face_gate = "ok"
    try:
        from shared.crops import _FRONTAL, _PROFILE
        # Verify EVERY cascade the detector relies on actually loaded (a hollow cv2
        # wheel yields empty classifiers). Covers the two frontal cascades + profile.
        if any(c.empty() for c in (*_FRONTAL, _PROFILE)):
            face_gate = "cascade_empty"
    except Exception as e:
        face_gate = f"unavailable: {type(e).__name__}: {e}"

    status = 200 if face_gate == "ok" else 503
    return func.HttpResponse(
        json.dumps({"status": "OK" if status == 200 else "DEGRADED",
                    "face_gate": face_gate}),
        mimetype="application/json", status_code=status)

# ── User Registration ─────────────────────────────────────
# Tables whose user_id has an ENFORCED FK to users.user_id (see sys.foreign_keys). These
# always exist and MUST be repointed in an identity migration, or the closing DELETE FROM
# users FK-blocks and the whole self-heal rolls back (the returning user 500s anyway).
_USER_CHILD_TABLES = ("jobs", "lora_models", "credit_transactions", "subscriptions")

# Additional user-scoped tables that carry a user_id but have NO enforced FK to users (so the
# DELETE would not block on them) — yet a returning user's rows here still belong to them and
# must follow the migration or they strand under the old id. pending_purchases in particular can
# strand a paid-but-uncommitted purchase. These tables post-date the baseline, so repoint each
# only IF it exists (OBJECT_ID guard) — a fresh/partial environment without them must not fail
# the migration. Names are hardcoded constants, never user input, so the f-string is injection-safe.
_USER_CHILD_TABLES_OPTIONAL = ("lora_trainings", "pending_purchases", "admin_user_notes")


def _migrate_identity(conn, cursor, old_user_id, new_oid, email):
    """Supabase→Entra split-identity self-heal.

    A returning user signs in under a NEW Entra oid, but their account row already exists under
    an OLD id keyed to the same email (created pre-migration). A plain INSERT then 500s on the
    UX_users_email_real unique index and locks them out forever (measured: 4 users, 29 failed
    logins). Instead, MOVE the account row AND every child row to the new oid so future oid
    lookups match and their history/credits follow them.

    FK-safe order, one transaction: free the old email → copy the row under the new id
    (preserving credits/plan/subscription/stripe state) → repoint children → delete old row.
    """
    cursor.execute("UPDATE users SET email = NULL WHERE user_id = ?", old_user_id)
    cursor.execute("""
        INSERT INTO users (user_id, email, full_name, auth_provider, subscription_tier,
            subscription_start, subscription_end, grace_period_end, credits_remaining, created_at,
            plan_name, lora_status, retrain_count, retention_expires_at, subscription_plan,
            subscription_type, stripe_customer_id, stripe_subscription_id, credits_monthly_limit,
            subscription_renewed_at, terms_accepted_at, payment_failed_at, subscription_cancel_at,
            stripe_checkout_token, stripe_checkout_expires_at, one_time_credits_remaining,
            monthly_credits_remaining, one_time_plan, one_time_plan_name)
        SELECT ?, ?, full_name, auth_provider, subscription_tier,
            subscription_start, subscription_end, grace_period_end, credits_remaining, created_at,
            plan_name, lora_status, retrain_count, retention_expires_at, subscription_plan,
            subscription_type, stripe_customer_id, stripe_subscription_id, credits_monthly_limit,
            subscription_renewed_at, terms_accepted_at, payment_failed_at, subscription_cancel_at,
            stripe_checkout_token, stripe_checkout_expires_at, one_time_credits_remaining,
            monthly_credits_remaining, one_time_plan, one_time_plan_name
        FROM users WHERE user_id = ?
    """, new_oid, email, old_user_id)
    for tbl in _USER_CHILD_TABLES:
        cursor.execute(f"UPDATE {tbl} SET user_id = ? WHERE user_id = ?", new_oid, old_user_id)
    # Optional user-scoped tables: repoint only if present, so an environment missing a newer
    # table (e.g. pending_purchases/admin_user_notes) still migrates cleanly instead of erroring.
    for tbl in _USER_CHILD_TABLES_OPTIONAL:
        cursor.execute(
            f"IF OBJECT_ID('dbo.{tbl}', 'U') IS NOT NULL "
            f"UPDATE dbo.{tbl} SET user_id = ? WHERE user_id = ?",
            new_oid, old_user_id)
    cursor.execute("DELETE FROM users WHERE user_id = ?", old_user_id)
    conn.commit()


@app.route(route="users/register", methods=["POST"])
def register_user(req: func.HttpRequest) -> func.HttpResponse:
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        payload = validate_token(token)
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)

    # oid = Entra object ID — the SAME claim get_user_id() returns, so
    # registration and every later lookup key off one identity. (Was payload["sub"],
    # a per-app pairwise subject → users were created under sub but looked up by
    # oid, a silent split-identity 404 on the first post-register call.)
    user_id = payload["oid"]
    # Best-effort profile fields. Entra External ID may name these differently
    # (email can arrive under a different claim; display name is `name` vs
    # `preferred_username`). Defaults keep this crash-free; the log below dumps
    # the actual claim KEYS (not values — no PII/token contents) on a genuine
    # first registration so we can confirm the real names from a live token
    # instead of guessing.
    # Entra tokens often carry the address in preferred_username/upn rather than `email`
    # (social/CIAM tokens may omit it entirely). Fall back through them, and store NULL —
    # NOT "" — when there's genuinely no address. An empty string collides on the email
    # uniqueness rule (only one "" allowed), which is exactly what 500'd every email-less
    # signup; NULL is excluded from the (now filtered) unique index, so many can coexist.
    email = (payload.get("email") or payload.get("preferred_username")
             or payload.get("upn") or None)
    name = payload.get("name", "")

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT user_id FROM users WHERE user_id = ?", user_id)
    if cursor.fetchone():
        _write_event(user_id, "auth.login")
        return func.HttpResponse(
            json.dumps({"message": "User already exists"}),
            mimetype="application/json",
            status_code=200
        )

    # Not found by oid. Before INSERTing, check for a RETURNING user whose auth id changed
    # (Supabase→Entra migration): the SAME email already exists under a DIFFERENT (old) user_id.
    # A blind INSERT here 500s on UX_users_email_real and permanently locks them out — the
    # split-identity bug measured on real users. Migrate their account to this oid instead.
    if email:
        cursor.execute("SELECT user_id FROM users WHERE email = ?", email)
        prior = cursor.fetchone()
        if prior and str(prior[0]).lower() != str(user_id).lower():
            logging.warning(
                f"Identity migration: email already under old id={prior[0]}; moving account to "
                f"Entra oid={user_id} (returning user, auth id changed)."
            )
            _migrate_identity(conn, cursor, prior[0], user_id, email)
            _write_event(user_id, "auth.identity_migrated", detail={"from_id": str(prior[0])})
            return func.HttpResponse(
                json.dumps({"message": "User migrated to current identity"}),
                mimetype="application/json",
                status_code=200
            )

    logging.info(
        f"First registration for oid={user_id}; token claim keys="
        f"{sorted(payload.keys())} (email_present={'email' in payload}, "
        f"name_present={'name' in payload})"
    )
    # plan_name is set EXPLICITLY rather than left to the column default ('basic'): the
    # default plan's credit expectations don't match the free-trial grant, so leaving it
    # to the default created new users on the wrong plan. New users start on the free
    # trial plan, which REGISTRATION_CREDITS is sized to cover (see shared/plans.py).
    cursor.execute("""
        INSERT INTO users (
            user_id, email, full_name, credits_remaining, plan_name,
            one_time_credits_remaining, one_time_plan, one_time_plan_name
        )
        VALUES (?, ?, ?, ?, ?, ?, 'trial', ?)
    """, user_id, email, name, REGISTRATION_CREDITS, DEFAULT_PLAN_KEY,
         REGISTRATION_CREDITS, DEFAULT_PLAN_KEY)
    conn.commit()

    _write_event(user_id, "auth.register")
    return func.HttpResponse(
        json.dumps({"message": "User registered", "credits": REGISTRATION_CREDITS}),
        mimetype="application/json",
        status_code=201
    )


# ── Organizations: Create ──────────────────────────────────────────────
@app.route(route="orgs", methods=["POST"])
def create_organization(req: func.HttpRequest) -> func.HttpResponse:
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        payload = validate_token(token)
        admin_user_id = payload["oid"]
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)

    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON", status_code=400)

    name = (body.get("name") or "").strip()
    seats_purchased = body.get("seats_purchased")

    if not name:
        return func.HttpResponse(
            json.dumps({"error": "name is required"}),
            mimetype="application/json", status_code=400)
    try:
        seats_purchased = int(seats_purchased)
        if seats_purchased < 1:
            raise ValueError
    except (TypeError, ValueError):
        return func.HttpResponse(
            json.dumps({"error": "seats_purchased must be a positive integer"}),
            mimetype="application/json", status_code=400)

    organization_id = str(uuid.uuid4())

    conn = new_connection()
    try:
        cur = conn.cursor()
        # LOCKED BY DEFAULT: status starts 'pending_payment', not 'active'. This is
        # the actual payment gate — every other Teams endpoint either checks this
        # status directly (payment-intent) or goes through _require_org_admin, which
        # already rejects anything not 'active' with 403 ORG_NOT_ACTIVE. That check
        # existed before but was never actually reachable, because this row used to
        # be inserted as 'active' immediately — nothing ever had a reason to be
        # rejected. Flipping the starting status is what actually turns the gate on.
        cur.execute(
            """INSERT INTO organizations
                (organization_id, name, admin_user_id, seats_purchased, credits_per_seat, status)
               VALUES (?, ?, ?, ?, ?, 'pending_payment')""",
            organization_id, name, admin_user_id, seats_purchased, ORG_CREDITS_PER_SEAT,
        )
        # The admin's membership row is still created now, not deferred to the
        # webhook — but with ZERO credits. This keeps GET /me/organization working
        # immediately (the admin's own dashboard needs to find *something* to show a
        # "complete payment" screen against), while reserve_job_slot's existing
        # insufficient-credits check already blocks any real generation attempt,
        # with no new logic needed there. The webhook below is what raises these
        # from 0 to the real amount once payment actually succeeds.
        cur.execute(
            """INSERT INTO organization_members
                (membership_id, organization_id, user_id, invitation_id,
                 credits_granted, credits_remaining, status)
               VALUES (?, ?, ?, NULL, 0, 0, 'active')""",
            str(uuid.uuid4()), organization_id, admin_user_id,
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        logging.error(f"create_organization failed for admin={admin_user_id}: {e}")
        return func.HttpResponse(
            json.dumps({"error": "Could not create organization"}),
            mimetype="application/json", status_code=500)
    finally:
        conn.close()

    return func.HttpResponse(
        json.dumps({
            "organization_id": organization_id,
            "name": name,
            "seats_purchased": seats_purchased,
            "credits_per_seat": ORG_CREDITS_PER_SEAT,
            "status": "pending_payment",
        }),
        mimetype="application/json", status_code=201)


# ── Organizations: Create Stripe payment (one-time, N seats) ────────────
@app.route(route="orgs/{organization_id}/payment-intent", methods=["POST"])
def create_org_payment_intent(req: func.HttpRequest) -> func.HttpResponse:
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        payload = validate_token(token)
        user_id = payload["oid"]
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)

    organization_id = req.route_params.get("organization_id")
    try:
        body = req.get_json()
    except ValueError:
        body = {}
    success_url = body.get("success_url", "https://bettersnap.ai/orgs/success")
    cancel_url = body.get("cancel_url", "https://bettersnap.ai/orgs/cancel")
    email = (payload.get("email") or payload.get("preferred_username")
             or payload.get("upn") or "")

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT admin_user_id, seats_purchased, status FROM organizations WHERE organization_id = ?",
        organization_id,
    )
    row = cur.fetchone()
    if not row:
        return func.HttpResponse("Organization not found", status_code=404)
    admin_user_id, seats_purchased, status = row
    if admin_user_id != user_id:
        return func.HttpResponse("Forbidden", status_code=403)
    # Payable exactly once, while locked. Already-active means it was paid
    # already (or, before this fix, was active from the moment of creation and
    # this check never actually did anything) — either way, re-paying isn't valid.
    if status != "pending_payment":
        return func.HttpResponse(
            json.dumps({"error": f"Organization is '{status}', not payable"}),
            mimetype="application/json", status_code=409)

    try:
        session = create_org_checkout(
            organization_id, email, seats_purchased,
            ORG_PRICE_PER_SEAT_CENTS, success_url, cancel_url,
        )
    except Exception as e:
        logging.error(f"Stripe org checkout failed for org={organization_id}: {e}")
        return func.HttpResponse(
            json.dumps({"error": "Payment provider error"}),
            mimetype="application/json", status_code=502)

    return func.HttpResponse(
        json.dumps({"checkout_url": session["url"], "session_id": session["id"]}),
        mimetype="application/json", status_code=200)


# ── Organizations: Dashboard summary ─────────────────────────────────────
@app.route(route="orgs/{organization_id}/dashboard-summary", methods=["GET"])
def org_dashboard_summary(req: func.HttpRequest) -> func.HttpResponse:
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        payload = validate_token(token)
        user_id = payload["oid"]
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)

    organization_id = req.route_params.get("organization_id")

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT admin_user_id, name, seats_purchased, status FROM organizations WHERE organization_id = ?",
        organization_id,
    )
    row = cur.fetchone()
    if not row:
        return func.HttpResponse("Organization not found", status_code=404)
    admin_user_id, name, seats_purchased, status = row
    if admin_user_id != user_id:
        return func.HttpResponse("Forbidden", status_code=403)

    cur.execute(
        "SELECT COUNT(*) FROM organization_members WHERE organization_id = ? AND status = 'active'",
        organization_id,
    )
    members_joined = cur.fetchone()[0]

    cur.execute(
        "SELECT COALESCE(SUM(credits_remaining), 0) FROM organization_members "
        "WHERE organization_id = ? AND status = 'active'",
        organization_id,
    )
    credits_remaining_total = cur.fetchone()[0]

    cur.execute(
        "SELECT status, COUNT(*) FROM jobs WHERE organization_id = ? GROUP BY status",
        organization_id,
    )
    job_status_counts = {r[0]: r[1] for r in cur.fetchall()}

    return func.HttpResponse(
        json.dumps({
            "organization_id": organization_id,
            "name": name,
            "status": status,
            "seats_purchased": seats_purchased,
            "members_joined": members_joined,
            "credits_remaining_total": credits_remaining_total,
            "job_status_counts": job_status_counts,
        }),
        mimetype="application/json", status_code=200)


# ── User Profile (alias for /profiles/me — used by frontend) ─────────────
@app.route(route="users/profile", methods=["GET"])
def user_profile_alias(req: func.HttpRequest) -> func.HttpResponse:
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        user_id = get_user_id(token)
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT user_id, email, full_name, credits_remaining FROM users WHERE user_id = ?",
        user_id,
    )
    row = cursor.fetchone()
    if not row:
        return func.HttpResponse("User not found", status_code=404)

    return func.HttpResponse(
        json.dumps({
            "user_id": row[0],
            "email": row[1],
            "full_name": row[2],
            "credits_remaining": row[3],
            "credits": row[3],  # alias so frontend can read either field
        }),
        mimetype="application/json",
        status_code=200,
    )

# ── User Credits ──────────────────────────────────────────
@app.route(route="users/credits", methods=["GET"])
def user_credits(req: func.HttpRequest) -> func.HttpResponse:
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        user_id = get_user_id(token)
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT credits_remaining, one_time_credits_remaining, monthly_credits_remaining "
        "FROM users WHERE user_id = ?",
        user_id,
    )
    row = cursor.fetchone()
    if not row:
        return func.HttpResponse("User not found", status_code=404)

    # Separate-balance model: spendable credits live in the one_time/monthly BUCKET columns —
    # that is what reserve_job_slot actually debits. The legacy `credits_remaining` column is
    # stale for any user who purchased/renewed after the split (e.g. a one-time top-up lands in
    # one_time_credits_remaining, leaving credits_remaining at 0), which made the dashboard show
    # 0 despite a paid balance. Report the effective bucket total; fall back to the legacy column
    # only when both buckets are empty (pre-migration users whose balance never moved to a bucket).
    legacy = int(row[0] or 0)
    one_time = int(row[1] or 0)
    monthly = int(row[2] or 0)
    bucket_total = one_time + monthly
    effective = bucket_total if bucket_total > 0 else legacy

    return func.HttpResponse(
        json.dumps({
            "credits_remaining": effective,
            "one_time_credits_remaining": one_time,
            "monthly_credits_remaining": monthly,
        }),
        mimetype="application/json",
        status_code=200
    )

# ── Reset model (the "New LoRA" path of the one-time re-purchase flow) ──────
@app.route(route="users/model", methods=["DELETE"])
def delete_user_model(req: func.HttpRequest) -> func.HttpResponse:
    """Wipe the caller's trained model so they can build a brand-new one.

    This is the backend for the 'New model' choice a one-time user makes after buying another
    pack: it deletes the adapter blob and resets lora_status -> 'none' + retrain_count -> 0, so
    the next /train is a fresh FIRST train (free, included in the pack) rather than a charged
    retrain (FREE_RETRAINS=1 would otherwise bill the 2nd model). Scoped strictly to the caller's
    own user_id — a user can only reset THEIR model. Idempotent: resetting an already-'none'
    account is a no-op that still returns 200. Generation is unaffected until they retrain."""
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        user_id = get_user_id(token)
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)

    # Adapter blobs live under the LOWERCASED user_id prefix (training writes lowercase; the
    # generation lookup is case-sensitive — see _delete_blobs's case note / the lora-case fix).
    n_lora = _delete_blobs("lora-weights", f"identity/{user_id.lower()}/")
    conn = get_db()
    conn.cursor().execute(
        "UPDATE users SET lora_status = 'none', retrain_count = 0 WHERE user_id = ?",
        user_id,
    )
    logging.info(f"delete_user_model: user={user_id} deleted adapter_blobs={n_lora}")
    _write_event(user_id, "model.delete", detail={"deleted_adapters": n_lora})
    return func.HttpResponse(
        json.dumps({"status": "reset", "lora_status": "none", "deleted_adapters": n_lora}),
        mimetype="application/json", status_code=200,
    )

# ── Profile: Get ──────────────────────────────────────────
# Reads the caller's profile straight off the EXISTING users table (keyed on the
# Entra oid = users.user_id). No separate profiles table — that would duplicate
# email / full_name / credits_remaining and drift.
@app.route(route="profiles/me", methods=["GET"])
def get_profile(req: func.HttpRequest) -> func.HttpResponse:
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        user_id = get_user_id(token)
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT user_id, email, full_name, credits_remaining, plan_name, "
        "ISNULL(lora_status, 'none'), ISNULL(retrain_count, 0) "
        "FROM users WHERE user_id = ?",
        user_id,
    )
    row = cursor.fetchone()
    # Fresh-start: no row until the user has registered. register_user is the ONLY
    # path that creates a users row (and grants the initial credits).
    if not row:
        return func.HttpResponse("User not found", status_code=404)

    # Resolve the plan so the client gets its limits (max_attires / max_backgrounds /
    # category_rule) + image_count alongside the raw plan key — same source the
    # backend enforces against, so client and server never disagree.
    plan = get_plan(row[4])
    return func.HttpResponse(
        json.dumps({
            "user_id": row[0],
            "email": row[1],
            "full_name": row[2],
            "credits_remaining": row[3],
            "plan_name": plan.key,
            "plan": {
                "key": plan.key, "name": plan.name, "image_count": plan.image_count,
                "max_attires": plan.max_attires, "max_backgrounds": plan.max_backgrounds,
                "category_rule": plan.category_rule, "plan_type": plan.plan_type,
                "credits_per_image": plan.credits_per_image,
            },
            # The app routes on this at load: 'none'/'failed' -> training flow,
            # 'training' -> progress screen, 'ready' -> generation. Served here so the
            # client needs ONE call to decide where the user belongs.
            "lora_status": row[5],
            "retrain": {
                "count": row[6],
                "free_left": max(0, FREE_RETRAINS - int(row[6] or 0)),
                "cost": 0 if int(row[6] or 0) < FREE_RETRAINS else RETRAIN_CREDITS,
            },
        }),
        mimetype="application/json",
        status_code=200,
    )

# ── Profile: Update ───────────────────────────────────────
# PATCH the caller's own users row. ONLY display_name (-> full_name) and email are
# client-writable. credits_remaining is NEVER read from the body (it moves only via
# reserve_job_slot -1 / _mark_failed +1), and user_id is always the token oid.
# PATCH never CREATES a row — credits originate solely in register_user, so a
# missing row returns 404 ("register first") rather than minting one here.
@app.route(route="profiles/me", methods=["PATCH"])
def update_profile(req: func.HttpRequest) -> func.HttpResponse:
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        user_id = get_user_id(token)
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)

    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON body", status_code=400)
    if not isinstance(body, dict):
        return func.HttpResponse("Invalid JSON body", status_code=400)

    # Build the SET clause from whichever writable fields were actually sent, so a
    # PATCH can touch one field without clobbering the other. Anything not in this
    # allow-list (notably credits_remaining) is ignored.
    updates = []
    params = []
    if "display_name" in body:
        updates.append("full_name = ?")
        params.append(body.get("display_name"))
    if "email" in body:
        try:
            email = _normalize_profile_email(body.get("email"))
        except ValueError as exc:
            return func.HttpResponse(
                json.dumps({"error": str(exc)}),
                mimetype="application/json", status_code=400)
        updates.append("email = ?")
        params.append(email)

    if not updates:
        return func.HttpResponse(
            "No updatable fields provided (display_name, email)", status_code=400
        )

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT user_id FROM users WHERE user_id = ?", user_id)
    if not cursor.fetchone():
        return func.HttpResponse("User not found — register first", status_code=404)

    params.append(user_id)
    try:
        cursor.execute(f"UPDATE users SET {', '.join(updates)} WHERE user_id = ?", params)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        if _is_duplicate_email_error(exc):
            return func.HttpResponse(
                json.dumps({"error": "Email address is already in use"}),
                mimetype="application/json", status_code=409)
        raise

    cursor.execute(
        "SELECT user_id, email, full_name, credits_remaining FROM users WHERE user_id = ?",
        user_id,
    )
    row = cursor.fetchone()
    return func.HttpResponse(
        json.dumps({
            "user_id": row[0],
            "email": row[1],
            "full_name": row[2],
            "credits_remaining": row[3],
        }),
        mimetype="application/json",
        status_code=200,
    )

# ── Terms: Status ────────────────────────────────────────
@app.route(route="users/terms-status", methods=["GET"])
def terms_status(req: func.HttpRequest) -> func.HttpResponse:
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        user_id = get_user_id(token)
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT terms_accepted_at FROM users WHERE user_id = ?", user_id)
    row = cursor.fetchone()
    if not row:
        return func.HttpResponse("User not found", status_code=404)

    return func.HttpResponse(
        json.dumps({"terms_accepted_at": _utc_iso(row[0])}),
        mimetype="application/json",
        status_code=200,
    )

# ── Terms: Accept ─────────────────────────────────────────
@app.route(route="users/accept-terms", methods=["POST"])
def accept_terms(req: func.HttpRequest) -> func.HttpResponse:
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        user_id = get_user_id(token)
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET terms_accepted_at = GETUTCDATE() WHERE user_id = ?",
        user_id,
    )
    conn.commit()
    cursor.execute("SELECT terms_accepted_at FROM users WHERE user_id = ?", user_id)
    row = cursor.fetchone()
    accepted_at = _utc_iso(row[0]) if row else None

    return func.HttpResponse(
        json.dumps({"terms_accepted_at": accepted_at}),
        mimetype="application/json",
        status_code=200,
    )

# ── Biometric Consent (BIPA / GDPR) ───────────────────────
# Append-only consent log (migration 028): each POST records a 'given' or 'revoked' EVENT; the
# user's CURRENT consent is the latest event. Required server-side before any facial-data
# processing (see the upload/train guard, gated by BIOMETRIC_CONSENT_REQUIRED). The row history
# is the audit trail (version, purpose, policy version, timestamp, revocation reason).
def _biometric_consent_active(cursor, user_id) -> bool:
    """True iff the user's most-recent biometric-consent event is 'given'."""
    cursor.execute(
        "SELECT TOP 1 event FROM dbo.biometric_consent WHERE user_id = ? "
        "ORDER BY created_at DESC, consent_id DESC", user_id)
    row = cursor.fetchone()
    return bool(row) and row[0] == "given"


def _biometric_consent_status(cursor, user_id) -> dict:
    cursor.execute(
        "SELECT TOP 1 event, consent_version, consent_purpose, policy_version, created_at "
        "FROM dbo.biometric_consent WHERE user_id = ? ORDER BY created_at DESC, consent_id DESC", user_id)
    row = cursor.fetchone()
    if not row:
        return {"consent_given": False, "event": None}
    return {"consent_given": row[0] == "given", "event": row[0], "consent_version": row[1],
            "consent_purpose": row[2], "policy_version": row[3], "updated_at": _utc_iso(row[4])}


@app.route(route="users/biometric-consent", methods=["GET"])
def biometric_consent_status(req: func.HttpRequest) -> func.HttpResponse:
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        user_id = get_user_id(token)
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)
    cursor = get_db().cursor()
    return func.HttpResponse(json.dumps(_biometric_consent_status(cursor, user_id)),
                             mimetype="application/json", status_code=200)


@app.route(route="users/biometric-consent", methods=["POST"])
def give_biometric_consent(req: func.HttpRequest) -> func.HttpResponse:
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        user_id = get_user_id(token)
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)
    try:
        body = req.get_json() or {}
    except ValueError:
        body = {}
    version = body.get("consent_version")
    if not version:
        return func.HttpResponse(json.dumps({"error": "consent_version is required"}),
                                 mimetype="application/json", status_code=400)
    conn = get_db(); cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO dbo.biometric_consent (user_id, event, consent_version, consent_purpose, policy_version) "
        "VALUES (?, 'given', ?, ?, ?)",
        user_id, str(version)[:32], BIOMETRIC_CONSENT_PURPOSE,
        (str(body.get("policy_version"))[:32] if body.get("policy_version") else None))
    conn.commit()
    _write_event(user_id, "consent.biometric_given", detail={"version": str(version)[:32]})
    return func.HttpResponse(json.dumps(_biometric_consent_status(cursor, user_id)),
                             mimetype="application/json", status_code=200)


@app.route(route="users/biometric-consent/revoke", methods=["POST"])
def revoke_biometric_consent(req: func.HttpRequest) -> func.HttpResponse:
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        user_id = get_user_id(token)
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)
    try:
        body = req.get_json() or {}
    except ValueError:
        body = {}
    conn = get_db(); cursor = conn.cursor()
    cursor.execute("SELECT TOP 1 consent_version FROM dbo.biometric_consent WHERE user_id = ? "
                   "ORDER BY created_at DESC, consent_id DESC", user_id)
    r = cursor.fetchone()
    cursor.execute(
        "INSERT INTO dbo.biometric_consent (user_id, event, consent_version, consent_purpose, reason) "
        "VALUES (?, 'revoked', ?, ?, ?)",
        user_id, (r[0] if r else "unknown"), BIOMETRIC_CONSENT_PURPOSE,
        (str(body.get("reason"))[:512] if body.get("reason") else None))
    conn.commit()
    _write_event(user_id, "consent.biometric_revoked")
    return func.HttpResponse(json.dumps(_biometric_consent_status(cursor, user_id)),
                             mimetype="application/json", status_code=200)


# ── Upload Photo ──────────────────────────────────────────
@app.route(route="upload", methods=["POST"])
def upload_photo(req: func.HttpRequest) -> func.HttpResponse:
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        user_id = get_user_id(token)
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)

    # BIPA/GDPR guard: no facial-data upload without active server-side biometric consent.
    # Env-gated (default off) so it can't break uploads before the consent flow + migration are live.
    if BIOMETRIC_CONSENT_REQUIRED and not _biometric_consent_active(get_db().cursor(), user_id):
        return func.HttpResponse(
            json.dumps({"error": "biometric_consent_required",
                        "message": "Explicit biometric consent is required before uploading photos."}),
            mimetype="application/json", status_code=403)

    file = req.files.get("photo")
    if not file:
        return func.HttpResponse("No photo provided", status_code=400)

    # ── 0.6 upload validation ────────────────────────────────────────────────
    # Read ONCE, cap size before doing any decode work, then verify the bytes are a
    # real image of an allowed type within sane dimensions. Client filename and MIME are
    # deliberately ignored: both are client-controlled, while a real decode cannot be spoofed.
    import io as _io
    from PIL import Image as _Image, UnidentifiedImageError as _UnidentifiedImageError

    data = file.read()
    if not data:
        return func.HttpResponse("Empty file", status_code=400)
    if len(data) > MAX_UPLOAD_BYTES:
        return func.HttpResponse(
            json.dumps({"error": f"Photo too large ({len(data)//1024//1024} MB); "
                                 f"max {MAX_UPLOAD_BYTES//1024//1024} MB."}),
            mimetype="application/json", status_code=400)
    try:
        with _Image.open(_io.BytesIO(data)) as _im:
            fmt = (_im.format or "").upper()
            w, h = _im.size
            _im.verify()   # catches truncated/corrupt payloads; consumes the object
    except _UnidentifiedImageError:
        return func.HttpResponse("File is not a readable image", status_code=400)
    except _Image.DecompressionBombError:
        return func.HttpResponse("Image rejected (decompression-bomb guard)", status_code=400)
    except Exception:
        return func.HttpResponse("File is not a readable image", status_code=400)

    if fmt not in _ALLOWED_IMAGE_FORMATS:
        return func.HttpResponse(
            json.dumps({"error": f"Unsupported image format {fmt or 'unknown'}; "
                                 f"use JPEG or PNG."}),
            mimetype="application/json", status_code=400)
    if w * h > MAX_UPLOAD_PIXELS or w > MAX_UPLOAD_DIM or h > MAX_UPLOAD_DIM:
        return func.HttpResponse(
            json.dumps({"error": f"Image too large ({w}x{h}); max {MAX_UPLOAD_DIM}px per side "
                                 f"and {MAX_UPLOAD_PIXELS//1_000_000} MP."}),
            mimetype="application/json", status_code=400)
    if w < MIN_UPLOAD_DIM or h < MIN_UPLOAD_DIM:
        return func.HttpResponse(
            json.dumps({"error": f"Image too small ({w}x{h}); min {MIN_UPLOAD_DIM}px per side."}),
            mimetype="application/json", status_code=400)

    # Never use the client filename as a storage key. Phones commonly upload every
    # photo as image.jpg (silent overwrite), and slashes create nested paths that the
    # training-photo scanner deliberately excludes. Derive the extension from the
    # verified bytes and make every upload a distinct, flat leaf.
    server_ext = "png" if fmt == "PNG" else "jpg"  # JPEG and MPO are JPEG-family
    blob_name = f"{user_id}/input/{uuid4().hex}.{server_ext}"
    url = upload_blob("inputs", blob_name, data)

    # Canonical convention: input_blob_path is "<container>/<blob>" so the
    # inference container resolves it without assuming a container name.
    # Clients must submit this exact value as input_blob_path to /jobs/submit.
    input_blob_path = f"inputs/{blob_name}"

    _write_event(user_id, "photo.upload", target=blob_name)
    return func.HttpResponse(
        json.dumps({"url": url, "blob_name": blob_name, "input_blob_path": input_blob_path}),
        mimetype="application/json",
        status_code=200
    )

# ── Identity-LoRA training ────────────────────────────────
def _list_training_photos(user_id: str) -> list:
    """The user's RAW uploads: files sitting DIRECTLY under inputs/<user_id>/input/.

    Anything nested one level deeper is excluded, and that is deliberate rather than
    just tidy: `input/` already accumulates non-photo material in sub-prefixes — our own
    generated crops (crop_upperbody/), and at least one account has a template/ folder.
    Filtering only on file extension would sweep a template PNG into the training set as
    if it were the user's face. A raw upload is a leaf under input/ (that is exactly what
    /upload writes: "<user_id>/input/<filename>"), so anything with a further '/' in its
    relative path is by definition not one.
    """
    container = get_blob_client().get_container_client("inputs")
    prefix = f"{user_id}/input/"
    photos = []
    for b in container.list_blobs(name_starts_with=prefix):
        rel = b.name[len(prefix):]
        if "/" in rel:                        # nested: crop_upperbody/, template/, ...
            continue
        if not rel.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            continue
        photos.append(b.name)
    return sorted(photos)


def _purge_stale_training_photos(user_id: str, keep: set):
    """Delete raw photos under this user's input/ prefix that are NOT part of the set we are
    about to train on.

    /upload appends and never cleans up. Without this, every upload session piles on top of
    the last, and the next training silently trains on a MIX of photo sets — two different
    people's faces blended into one adapter if the folder was ever reused. Scoped strictly
    to leaf files directly under <user_id>/input/, so it can never touch crops, templates, or
    anything belonging to another user.
    """
    try:
        container = get_blob_client().get_container_client("inputs")
        prefix = f"{user_id}/input/"
        removed = 0
        for b in container.list_blobs(name_starts_with=prefix):
            rel = b.name[len(prefix):]
            if "/" in rel:                       # nested (crop_upperbody/, ...) — never touch
                continue
            if b.name in keep:
                continue
            container.delete_blob(b.name)
            removed += 1
        if removed:
            logging.info(f"purged {removed} stale training photo(s) for user={user_id}")
    except Exception as e:
        # Non-fatal: a failed purge means clutter, not a wrong model — this session's
        # training set is already pinned to the explicit list.
        logging.warning(f"could not purge stale photos for user={user_id}: {e}")


@app.route(route="train", methods=["POST"])
def start_training(req: func.HttpRequest) -> func.HttpResponse:
    """Kick off this user's identity-LoRA training.

    EVERYTHING is computed here — nothing is hand-typed. The user_id comes from the
    auth token and is the SINGLE source of truth: it selects the input photos AND
    (inside the trainer) the adapter output path identity/<user_id>/. There is no
    request field, env var, or config that can point those at different people.
    """
    # Authenticate FIRST, before importing anything heavy. shared.crops pulls OpenCV,
    # and an import failure there used to crash the handler before the auth check even
    # ran — so an unauthenticated caller got a 500 (leaking that something is broken)
    # instead of a clean 401. Cheap checks first, always.
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        user_id = get_user_id(token)
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)

    # BIPA/GDPR guard: training turns facial data into a persisted per-user model — require
    # active server-side biometric consent (env-gated, default off).
    if BIOMETRIC_CONSENT_REQUIRED and not _biometric_consent_active(get_db().cursor(), user_id):
        return func.HttpResponse(
            json.dumps({"error": "biometric_consent_required",
                        "message": "Explicit biometric consent is required before training."}),
            mimetype="application/json", status_code=403)

    from shared.crops import (crop_head_and_shoulders, NoFaceError,
                              MultipleFacesError, FaceTooSmallError,
                              EyesOccludedError)

    try:
        body = req.get_json()
    except ValueError:
        body = {}
    gender = body.get("gender")
    force = bool(body.get("force"))

    # ── Don't start a second run for a user who already has one ──────────────
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT lora_status, credits_remaining, retrain_count, plan_name, "
        "one_time_credits_remaining, monthly_credits_remaining FROM users WHERE user_id = ?",
        user_id,
    )
    row = cur.fetchone()
    if not row:
        return func.HttpResponse("User not found", status_code=404)
    lora_status = (row[0] or "none").strip()
    retrain_count = int(row[2] or 0)
    # Effective spendable = the buckets (what the retrain charge actually debits), NOT the legacy
    # credits_remaining column — else a fully-funded one-time account (balance in one_time bucket,
    # legacy 0) is wrongly told "Not enough credits to retrain". Mirrors reserve_training_slot.
    _bucket_total = int(row[4] or 0) + int(row[5] or 0)
    credits = _bucket_total if _bucket_total > 0 else int(row[1] or 0)
    if lora_status == "training":
        return func.HttpResponse(
            json.dumps({"status": "training", "message": "Training already in progress"}),
            mimetype="application/json", status_code=409)
    if lora_status == "ready" and not force:
        return func.HttpResponse(
            json.dumps({"status": "ready",
                        "message": "You already have a trained model. Pass force=true to retrain.",
                        "retrain_cost": 0 if retrain_count < FREE_RETRAINS else RETRAIN_CREDITS,
                        "free_retrains_left": max(0, FREE_RETRAINS - retrain_count)}),
            mimetype="application/json", status_code=409)

    # ── Retrain metering ─────────────────────────────────────────────────────
    # A retrain is ~32 min of A100 AND (MAX_ACTIVE_GPU_JOBS=1) blocks the queue for every
    # other user for that whole time — the most expensive action in the product. First one
    # is free; after that it costs credits. Charged HERE, before any GPU is touched.
    is_retrain = lora_status in ("ready", "failed") and force
    retrain_cost = 0
    if is_retrain and retrain_count >= FREE_RETRAINS:
        retrain_cost = RETRAIN_CREDITS
        if credits < retrain_cost:
            return func.HttpResponse(
                json.dumps({"error": "Not enough credits to retrain your model.",
                            "required": retrain_cost, "credits_remaining": credits}),
                mimetype="application/json", status_code=402)

    # Backstop: even a well-funded account cannot monopolise the single GPU.
    cur.execute(
        "SELECT COUNT(*) FROM lora_trainings "
        "WHERE user_id = ? AND created_at >= CAST(GETUTCDATE() AS DATE)",
        user_id,
    )
    if int(cur.fetchone()[0]) >= MAX_TRAININGS_PER_DAY:
        return func.HttpResponse(
            json.dumps({"error": "Daily training limit reached. Try again tomorrow.",
                        "limit": MAX_TRAININGS_PER_DAY}),
            mimetype="application/json", status_code=429)

    # ── Which photos? THIS SESSION'S, not "whatever is in the folder" ────────
    #
    # THE BUG THIS FIXES: /upload APPENDS to inputs/<user_id>/input/ and nothing ever
    # clears it. /train used to list the whole folder, so a user's SECOND upload trained on
    # their old photos AND their new ones, mixed together. On a real account that means a
    # retrain silently blends two different photo sets; on the test account it meant a new
    # user's model would have been trained on the previous occupant's face. It also breaks
    # the count check (6 stale + 8 new = 14 > MAX -> 400 with no way for the user to
    # understand why).
    #
    # The client knows exactly which blobs it just uploaded (/upload returns blob_name), so
    # it now sends them explicitly. Every path is still verified to live under THIS user's
    # own prefix, so a caller cannot name someone else's photos.
    requested = body.get("photos") or []
    if requested:
        prefix = f"{user_id}/".lower()
        stray = [p for p in requested if not str(p).lower().startswith(prefix)]
        if stray:
            logging.error(f"REJECTED: user={user_id} named photos outside their prefix: {stray[:3]}")
            return func.HttpResponse(
                json.dumps({"error": "Invalid photo paths."}),
                mimetype="application/json", status_code=400)
        photos = [str(p) for p in requested]
    else:
        # Fallback for the ops/curl path, which has no session context.
        photos = _list_training_photos(user_id)

    if not (MIN_TRAINING_PHOTOS <= len(photos) <= MAX_TRAINING_PHOTOS):
        return func.HttpResponse(
            json.dumps({
                "error": f"Upload between {MIN_TRAINING_PHOTOS} and {MAX_TRAINING_PHOTOS} photos "
                         f"to train your model.",
                "uploaded": len(photos),
                "min": MIN_TRAINING_PHOTOS, "max": MAX_TRAINING_PHOTOS,
            }),
            mimetype="application/json", status_code=400)

    # ── Crop + FACE GATE ─────────────────────────────────────────────────────
    # Every photo must contain a detectable face. A faceless photo is REJECTED here,
    # in milliseconds, before a single GPU-second is spent — never silently centre-
    # cropped into the training set, which would poison the adapter and only surface
    # ~51 minutes of A100 later as a bad likeness. Nothing is uploaded unless ALL
    # photos pass, so a rejected batch leaves no half-written crop set behind.
    crops, rejected = [], []
    for i, blob_name in enumerate(photos):
        try:
            crops.append(crop_head_and_shoulders(download_blob("inputs", blob_name)))
        except NoFaceError:
            rejected.append({"photo": os.path.basename(blob_name), "index": i + 1,
                             "code": "FACE_NOT_FOUND", "reason": "no face detected"})
        except MultipleFacesError:
            rejected.append({"photo": os.path.basename(blob_name), "index": i + 1,
                             "code": "MULTIPLE_FACES", "reason": "more than one face"})
        except EyesOccludedError as e:
            # Eyes carry more identity than any other feature. A set dominated by
            # sunglasses trains an adapter with no eyes to reproduce.
            rejected.append({"photo": os.path.basename(blob_name), "index": i + 1,
                             "code": "EYES_OCCLUDED",
                             "reason": "your eyes are covered in this photo",
                             "eye_ratio": e.ratio})
        except FaceTooSmallError as e:
            # Detected fine, but too few pixels to crop without heavy upscaling — the
            # adapter would learn interpolation mush as this user's skin. Tell them the
            # actionable thing (get closer), not the pixel count.
            rejected.append({"photo": os.path.basename(blob_name), "index": i + 1,
                             "code": "FACE_TOO_SMALL",
                             "reason": "you're too far away in this photo",
                             "face_px": e.face_px, "required_px": e.required_px})
        except ValueError as e:
            rejected.append({"photo": os.path.basename(blob_name), "index": i + 1,
                             "code": "NOT_AN_IMAGE", "reason": str(e)})

    if rejected:
        names = ", ".join(str(r["index"]) for r in rejected)
        plural = len(rejected) > 1
        # Lead with the dominant reason so the user knows what to DO. "Too far away" is
        # the common real-world case (measured: 6 of 9 photos in a real upload set) and
        # needs different advice from "no face found" — telling someone their clearly-
        # visible face "isn't clear" when they simply stood too far back is unactionable.
        codes = {r["code"] for r in rejected}
        if codes == {"FACE_TOO_SMALL"}:
            msg = (f"In photo{'s' if plural else ''} {names} you're too far away — "
                   f"please use closer shots where your face fills more of the frame.")
        elif codes == {"EYES_OCCLUDED"}:
            msg = (f"Photo{'s' if plural else ''} {names} "
                   f"{'have' if plural else 'has'} your eyes covered — "
                   f"please use photos without sunglasses.")
        elif codes == {"MULTIPLE_FACES"}:
            msg = (f"Photo{'s' if plural else ''} {names} "
                   f"{'have' if plural else 'has'} more than one person — "
                   f"please use solo photos.")
        else:
            msg = (f"Photo{'s' if plural else ''} {names} "
                   f"{'don' if plural else 'doesn'}'t work for training — "
                   f"please replace {'them' if plural else 'it'}.")
        return func.HttpResponse(
            json.dumps({"error": msg, "rejected": rejected}),
            mimetype="application/json", status_code=400)

    # ── Upload crops + build FILES_JSON programmatically ──────────────────────
    # Paths are RELATIVE to the `inputs` container (no container prefix) because the
    # trainer joins them against INPUT_CONTAINER=inputs. Captions are omitted entirely:
    # this is DreamBooth, identity keys off --instance_prompt, and the trainer reads and
    # discards the caption field. Hand-typing them was pure waste.
    files = []
    for i, data in enumerate(crops):
        rel = f"{user_id}/{catalog.CROP_SUBDIR}/img{i}.jpg"
        upload_blob("inputs", rel, data)
        files.append({"blob": rel})

    # Purge raw photos from any PREVIOUS session. /upload appends and never cleans up, so
    # without this a user's folder accumulates every set they have ever uploaded — and the
    # next training (or the ops fallback that lists the folder) would blend them together.
    # Only runs once this session's photos have been cropped and safely written.
    _purge_stale_training_photos(user_id, keep=set(photos))

    cword = catalog.class_word(gender)

    # Insert the run, flip the user to 'training', and CHARGE the retrain in ONE
    # transaction — so a crash between them can't take credits without starting a run,
    # or start a run without charging.
    # Serialized retrain reservation: insert + flip to 'training' + bucket-aware charge
    # in one app-locked transaction.
    reserved = reserve_training_slot(
        user_id, files, cword, force, FREE_RETRAINS, RETRAIN_CREDITS,
        MAX_TRAININGS_PER_DAY,
    )
    if not reserved.ok:
        if reserved.reason == "busy":
            return func.HttpResponse("Service busy, please retry", status_code=503)
        if reserved.reason == "user_missing":
            return func.HttpResponse("User not found", status_code=404)
        if reserved.reason == "already_training":
            return func.HttpResponse(
                json.dumps({"status": "training", "message": "Training already in progress"}),
                mimetype="application/json", status_code=409)
        if reserved.reason == "force_required":
            return func.HttpResponse(
                json.dumps({"status": "ready", "message": "Pass force=true to retrain."}),
                mimetype="application/json", status_code=409)
        if reserved.reason == "credits":
            return func.HttpResponse(
                json.dumps({"error": "Not enough credits to retrain your model.",
                            "required": RETRAIN_CREDITS}),
                mimetype="application/json", status_code=402)
        return func.HttpResponse(
            json.dumps({"error": "Daily training limit reached. Try again tomorrow.",
                        "limit": MAX_TRAININGS_PER_DAY}),
            mimetype="application/json", status_code=429)

    training_id = reserved.training_id
    is_retrain = reserved.retrain
    retrain_cost = reserved.credits_charged
    train_msg = reserved.message
    outbox_tid = reserved.outbox_id

    # Delayed on purpose — see TRAIN_FUSE_HEAD_START. Gives /jobs/submit time to park the
    # generation job so _dispatch_training can fuse it into this same container. The
    # transactional-outbox guarantee is unaffected: the row is already committed, and
    # outbox_dispatch_pending still backstops a failed send.
    # Reservation already committed (training row + credit charge + outbox message) — the
    # request has succeeded and outbox_dispatch_pending backstops delivery, so a fast-path
    # hiccup here must NOT 500 a training that already started. Guard and return 202.
    try:
        outbox_try_send_now(outbox_tid, TRAINING_QUEUE, train_msg,
                            visibility_timeout=TRAIN_FUSE_HEAD_START or None)
    except Exception as e:
        logging.warning(
            f"training outbox fast-path failed for training_id={training_id} "
            f"(non-fatal; the dispatcher will deliver): {e}"
        )
    logging.info(
        f"training queued: training_id={training_id} user={user_id} "
        f"photos={len(files)} class_word={cword} head_start={TRAIN_FUSE_HEAD_START}s"
    )

    return func.HttpResponse(
        json.dumps({"training_id": str(training_id), "status": "training",
                    "photos": len(files), "class_word": cword,
                    "retrain": is_retrain, "credits_charged": retrain_cost,
                    # Warm class-image cache -> ~32 min; a cold cache (first user of a
                    # gender) still pays the ~17.6 min class-image build on top.
                    "estimated_minutes": 32}),
        mimetype="application/json", status_code=202)


@app.route(route="train/status", methods=["GET"])
def training_status(req: func.HttpRequest) -> func.HttpResponse:
    """Poll target for the frontend while the LoRA builds."""
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        user_id = get_user_id(token)
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT lora_status FROM users WHERE user_id = ?", user_id)
    row = cur.fetchone()
    if not row:
        return func.HttpResponse("User not found", status_code=404)

    cur.execute("""
        SELECT TOP 1 training_id, status, photo_count, error, created_at, completed_at
        FROM lora_trainings WHERE user_id = ? ORDER BY created_at DESC
    """, user_id)
    t = cur.fetchone()

    payload = {"lora_status": (row[0] or "none").strip()}
    if t:
        payload["training"] = {
            "training_id": str(t[0]), "status": t[1], "photos": t[2],
            "error": t[3],
            "created_at": _utc_iso(t[4]),
            "completed_at": _utc_iso(t[5]),
        }
    return func.HttpResponse(json.dumps(payload), mimetype="application/json", status_code=200)


# ── Submit Job ────────────────────────────────────────────
@app.route(route="jobs/submit", methods=["POST"])
def submit_job(req: func.HttpRequest) -> func.HttpResponse:
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        user_id = get_user_id(token)
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)

    try:
        body = req.get_json()
        if not isinstance(body, dict):
            raise ValueError("JSON body must be an object")
    except Exception:
        return func.HttpResponse(
            json.dumps({"error": "invalid JSON body"}),
            mimetype="application/json", status_code=400)
    gender = body.get("gender")
    age_range = body.get("age_range")
    hair_color = body.get("hair_color")
    input_blob_path = body.get("input_blob_path")
    # GLOBAL cross-category selections (category-qualified refs, e.g.
    # "business_suit.navy_suit_tie"). custom_prompt (scene-only text) is the
    # custom_scene mode; when present the attire/background menus are skipped.
    attire_ids = body.get("attire_ids") or []
    background_ids = body.get("background_ids") or []
    custom_prompt = (body.get("custom_prompt") or "").strip()

    # input_blob_path is NOT required. This is txt2img: identity comes entirely from the
    # user's trained LoRA, and there is no source photo to seed from — the field is a
    # leftover from the old img2img flow. Requiring it forced the client to invent a dummy
    # value for something the pipeline never reads. Kept as an optional passthrough (it is
    # still stored on the job row for provenance).
    if not all([gender, age_range, hair_color]):
        return func.HttpResponse(
            json.dumps({"error": "gender, age_range and hair_color are required"}),
            mimetype="application/json", status_code=400)
    if not isinstance(age_range, str) or not isinstance(hair_color, str):
        return func.HttpResponse(
            json.dumps({"error": "age_range and hair_color must be strings"}),
            mimetype="application/json", status_code=400)
    age_range = age_range.strip()
    hair_color = hair_color.strip()
    if not age_range or not hair_color:
        return func.HttpResponse(
            json.dumps({"error": "age_range and hair_color cannot be blank"}),
            mimetype="application/json", status_code=400)
    too_long = [
        name for name, value in (("age_range", age_range), ("hair_color", hair_color))
        if len(value) > MAX_PROFILE_ATTRIBUTE_CHARS
    ]
    if too_long:
        return func.HttpResponse(
            json.dumps({
                "error": "Profile attribute too long",
                "fields": too_long,
                "max_length": MAX_PROFILE_ATTRIBUTE_CHARS,
            }),
            mimetype="application/json", status_code=400)
    input_blob_path = input_blob_path or ""

    # ── Resolve the user's plan (image_count + selection limits live in shared/plans) ──
    conn0 = get_db()
    cur0 = conn0.cursor()
    cur0.execute(
        "SELECT plan_name, lora_status, credits_remaining, "
        "one_time_credits_remaining, suspended_at FROM users WHERE user_id = ?",
        user_id,
    )
    prow = cur0.fetchone()
    if not prow:
        return func.HttpResponse("User not found", status_code=404)
    # A super-admin can suspend an account; a suspended user cannot start new generations.
    if prow[4] is not None:
        return func.HttpResponse(
            json.dumps({"error": "account_suspended",
                        "message": "This account is suspended. Contact support."}),
            mimetype="application/json", status_code=403)
    plan = get_plan(prow[0])
    lora_status = (prow[1] or "none").strip()
    # A Teams seat spends the org pool, so the image-count budget below must be
    # computed against THAT balance, not the personal one. reserve_job_slot resolves
    # membership again under its lock — this read is only for sizing the request.
    credits_remaining, org_id = effective_credits(cur0, user_id, int(prow[2] or 0))
    # The Teams pool is exclusive while membership is active; never add personal
    # top-up credits when sizing an organization-funded request.
    one_time_credits_remaining = 0 if org_id else int(prow[3] or 0)

    # ── Identity-LoRA gate ───────────────────────────────────────────────────
    # Without the user's adapter, txt2img has NOTHING carrying their identity and
    # main.py would render base SDXL — a photogenic stranger. So:
    #   ready    → dispatch normally.
    #   training → ACCEPT and PARK (below): reserve credits, insert 'waiting_lora', do
    #              NOT enqueue. The training watcher enqueues it the instant the adapter
    #              lands, so there is no polling lag and no defer-backoff churn.
    #   none/failed → REJECT here, BEFORE reserving credits. A parked job with no
    #              training in flight would wait forever; tell them to train instead.
    if lora_status not in ("ready", "training"):
        return func.HttpResponse(
            json.dumps({
                "error": "Your model isn't trained yet. Upload your photos and start training first.",
                "lora_status": lora_status,
                "train_endpoint": "/api/train",
            }),
            mimetype="application/json", status_code=409)

    # ── Validate selections + enforce plan limits ─────────────────────────────
    is_custom = bool(custom_prompt)
    if is_custom:
        if len(custom_prompt) > 400:
            return func.HttpResponse("Custom scene text too long (max 400 chars)", status_code=400)
    else:
        if not attire_ids or not background_ids:
            return func.HttpResponse(
                "Select at least one attire and one background", status_code=400)
        # Every ref must exist in the catalog (drops typos / stale ids). Attires are
        # gender-specific, so an attire ref is valid only if it exists for THIS user's gender
        # (a male ref submitted for a female user, or vice-versa, is rejected). Backgrounds
        # are shared across genders.
        bad = ([a for a in attire_ids if not catalog.valid_attire_ref(a, gender)] +
               [b for b in background_ids if not catalog.valid_background_ref(b)])
        if bad:
            return func.HttpResponse(
                json.dumps({"error": "Unknown attire/background ids", "invalid": bad}),
                mimetype="application/json", status_code=400)
        # Count limits.
        if len(attire_ids) > plan.max_attires:
            return func.HttpResponse(
                json.dumps({"error": f"{plan.name} allows up to {plan.max_attires} attires",
                            "limit": plan.max_attires}),
                mimetype="application/json", status_code=403)
        if len(background_ids) > plan.max_backgrounds:
            return func.HttpResponse(
                json.dumps({"error": f"{plan.name} allows up to {plan.max_backgrounds} backgrounds",
                            "limit": plan.max_backgrounds}),
                mimetype="application/json", status_code=403)
        # Type rule: single_type plans (Basic) cannot mix professional + personal.
        if plan.category_rule == "single_type":
            types = {catalog.ref_type(r) for r in (attire_ids + background_ids)}
            types.discard(None)
            if len(types) > 1:
                return func.HttpResponse(
                    json.dumps({"error": f"{plan.name} cannot mix professional and personal; "
                                         "pick all selections from one type",
                                "types": sorted(types)}),
                    mimetype="application/json", status_code=403)

    # One-time = the whole pack in ONE batch. Monthly = a per-session slice so the monthly
    # credit pool spreads across MANY generations: default to the plan's session size, let
    # the client request more, clamp to the monthly quota and to what credits can pay for.
    if plan.plan_type == "monthly":
        # Coerce defensively: a non-numeric image_count ("abc") or wrong type ([1]) would
        # raise on int() and — past the body guard — surface as a 500 on the most-hit
        # endpoint. Fall back to a clean 400 instead.
        try:
            requested = int(body.get("image_count") or plan.min_session_images)
        except (TypeError, ValueError):
            return func.HttpResponse(
                json.dumps({"error": "image_count must be a number"}),
                mimetype="application/json", status_code=400)
        max_by_credits = (
            credits_remaining + one_time_credits_remaining
        ) // plan.credits_per_image
        # Honor the min-session floor normally, but NEVER floor above what the user's remaining
        # credits can pay for — otherwise a user with less than one full session's worth of
        # credits could never spend them (every submit asked for the floor and 402'd, stranding
        # the credits). Keep at least 1 so a genuinely broke user still gets an honest 402.
        upper = min(requested, plan.monthly_images, max_by_credits)
        floor = min(plan.min_session_images, max_by_credits)
        image_count = max(1, floor, upper)
    else:
        image_count = plan.image_count
    cost = credit_cost(plan, image_count)

    job_params = json.dumps({
        "gender": gender,
        "age_range": age_range,
        "hair_color": hair_color,
        "attire_ids": attire_ids,
        "background_ids": background_ids,
        "custom_prompt": custom_prompt,
        "image_count": image_count,
        "credit_cost": cost,            # for the refund path on terminal failure
        "plan_name": plan.key,
        "input_blob_path": input_blob_path,
    })

    # Atomic credits + daily-cap check + insert + decrement (serialized across
    # all instances via sp_getapplock). See shared/job_reservation.py. Daily caps
    # exist because credits alone don't bound spend: multi-account abuse,
    # duplicate-job bugs, or a test account can all flood the GPU. credit_cost is
    # image_count * plan.credits_per_image — the counter tracks images, not jobs.
    # 'waiting_lora' rows are reserved (credits taken, cap counted) but deliberately
    # NOT enqueued — see the identity-LoRA gate above.
    result = reserve_job_slot(
        user_id, input_blob_path, job_params, PER_USER_DAILY_CAP, GLOBAL_DAILY_CAP,
        credit_cost=cost,
        # Resolve ready vs training AGAIN under the reservation transaction's UPDLOCK/HOLDLOCK
        # (dev's race fix) — the earlier read is only for validation/prompt planning. With
        # resolve_lora_status set, reserve_job_slot picks queued vs waiting_lora internally, so
        # we do NOT pass initial_status. image_count/credits_per_image drive the separate-balance
        # credit model (features-stripe).
        resolve_lora_status=True,
        image_count=image_count,
        credits_per_image=plan.credits_per_image,
        # Tag the job with the product it belongs to (finding #6 foundation): drives the
        # purchase gate, per-product retention, and LoRA lifecycle. plan.plan_type is
        # 'one_time' | 'monthly'.
        source_type=plan.plan_type,
    )
    if not result.ok:
        if result.reason == "lora_not_ready":
            return func.HttpResponse(
                json.dumps({
                    "error": "Your model is no longer training or ready. Start training first.",
                    "train_endpoint": "/api/train",
                }),
                mimetype="application/json", status_code=409,
            )
        if result.reason == "credits":
            return func.HttpResponse(
                json.dumps({"error": "Insufficient credits", "required": cost,
                            "images": image_count}),
                mimetype="application/json", status_code=402)
        if result.reason == "busy":
            return func.HttpResponse(
                json.dumps({"error": "Service busy, please retry"}),
                mimetype="application/json", status_code=503,
            )
        scope = "user" if result.reason == "user_cap" else "global"
        limit = PER_USER_DAILY_CAP if scope == "user" else GLOBAL_DAILY_CAP
        msg = ("Daily limit reached for your account" if scope == "user"
               else "Service is at daily capacity, please try again tomorrow")
        return func.HttpResponse(
            json.dumps({"error": msg, "scope": scope, "limit": limit}),
            mimetype="application/json", status_code=429,
        )

    job_id = result.job_id
    parked = result.status == "waiting_lora"

    # One-time plans: (re)start the retention clock at each generation (last-gen + N days).
    # Monthly plans keep retention_expires_at NULL until the subscription ends. Best-effort
    # — a miss here just means the hourly cleanup won't pick this user up yet.
    if plan.plan_type == "one_time":
        try:
            rconn = new_connection()
            try:
                rconn.cursor().execute(
                    "UPDATE users SET retention_expires_at = DATEADD(DAY, ?, GETUTCDATE()) "
                    "WHERE user_id = ?", RETENTION_DAYS, user_id)
                rconn.commit()
            finally:
                rconn.close()
        except Exception as e:
            logging.warning(f"retention window not set for user={user_id}: {e}")

    if parked:
        logging.info(
            f"job_id={job_id} parked as 'waiting_lora' (user={user_id} is still training); "
            f"the training watcher will release it"
        )
        return func.HttpResponse(
            json.dumps({"job_id": str(job_id), "status": "waiting_lora",
                        "message": "We're still building your model. "
                                   "Your photos will start automatically when it's ready."}),
            mimetype="application/json", status_code=202)

    # Transactional outbox (finding #4): reserve_job_slot already wrote the queue message
    # into the outbox IN THE SAME TRANSACTION as the job row + credit charge, so the send can
    # no longer be lost. Fast-path it now; if the queue is briefly down the outbox_dispatcher
    # delivers it — the job is neither failed nor refunded, it just starts a little later.
    # Once the reservation has COMMITTED (job row + credit charge + outbox message, atomically),
    # the request has succeeded — the job WILL run, because the outbox_dispatcher delivers the
    # queue message even if this fast-path send (or its bookkeeping) hiccups. So this tail must
    # never turn a committed job into a 500: a false failure makes the user retry and double-pay,
    # and (as seen in prod) shows "Submit failed" for a job that generates all its images.
    # outbox_try_send_now already swallows send failures, but its _mark_delivered/_record_failure
    # DB writes can still raise — guard the whole call and always return 202.
    try:
        outbox_try_send_now(
            result.outbox_id, INFERENCE_QUEUE,
            {"job_id": str(job_id), "user_id": str(user_id), "job_params": job_params},
        )
    except Exception as e:
        logging.warning(
            f"outbox fast-path failed for job_id={job_id} (non-fatal; the dispatcher will "
            f"deliver the queued message): {e}"
        )

    return func.HttpResponse(
        json.dumps({"job_id": str(job_id), "status": "queued"}),
        mimetype="application/json",
        status_code=202
    )

# ── Job Status ────────────────────────────────────────────
@app.route(route="jobs/{job_id}/status", methods=["GET"])
def job_status(req: func.HttpRequest) -> func.HttpResponse:
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        user_id = get_user_id(token)
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)

    job_id = _route_job_id(req)
    if job_id is None:
        return func.HttpResponse("Not found", status_code=404)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT status, output_blob_path FROM jobs WHERE job_id = ? AND user_id = ?",
        job_id, user_id
    )
    row = cursor.fetchone()
    if not row:
        return func.HttpResponse("Not found", status_code=404)

    return func.HttpResponse(
        json.dumps({"status": row[0], "output_blob_path": row[1]}),
        mimetype="application/json",
        status_code=200
    )

# ── Get Result URL (SAS) ──────────────────────────────────
@app.route(route="jobs/{job_id}/result-url", methods=["GET"])
def job_result_url(req: func.HttpRequest) -> func.HttpResponse:
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        user_id = get_user_id(token)
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)

    job_id = _route_job_id(req)
    if job_id is None:
        return func.HttpResponse("Not found", status_code=404)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT status, output_blob_path FROM jobs WHERE job_id = ? AND user_id = ?",
        job_id, user_id
    )
    row = cursor.fetchone()
    if not row:
        return func.HttpResponse("Not found", status_code=404)
    if row[0] != "completed":
        return func.HttpResponse(
            json.dumps({"error": "Job not completed"}),
            mimetype="application/json",
            status_code=400
        )

    # The inference container stores output_blob_path as json.dumps([...]) — a
    # JSON array of the 4 result blobs (results/<job>/headshot_N.png). The old
    # code fed that raw string into generate_blob_sas as a single blob_name, so
    # the SAS pointed at a non-existent blob literally named '["results/..."]'
    # and every download 404'd. Parse the array and mint one SAS per image.
    # Backward-compatible: a legacy single-path string still yields one URL.
    raw = row[1]
    try:
        blob_paths = json.loads(raw)
        if not isinstance(blob_paths, list):
            blob_paths = [raw]
    except (TypeError, ValueError):
        blob_paths = [raw]
    blob_paths = [p for p in blob_paths if p]
    if not blob_paths:
        return func.HttpResponse(
            json.dumps({"error": "No output blobs recorded for this job"}),
            mimetype="application/json",
            status_code=404
        )

    blob_client = get_blob_client()
    account_name = blob_client.account_name
    account_key = get_secret("storage-account-key")
    expiry = datetime.now(timezone.utc) + timedelta(hours=2)

    urls = []
    for blob_name in blob_paths:
        sas_token = generate_blob_sas(
            account_name=account_name,
            container_name="outputs",
            blob_name=blob_name,
            account_key=account_key,
            permission=BlobSasPermissions(read=True),
            expiry=expiry
        )
        urls.append(
            f"https://{account_name}.blob.core.windows.net/outputs/{blob_name}?{sas_token}"
        )

    return func.HttpResponse(
        # `urls` is the full set; `url` kept for any client still reading a single
        # field (it gets the first image) so this change doesn't break callers.
        json.dumps({"urls": urls, "url": urls[0], "count": len(urls)}),
        mimetype="application/json",
        status_code=200
    )

# ── Delete Job ────────────────────────────────────────────
# Owner-only hard delete: remove the jobs row AND its result blobs
# (outputs/results/<job_id>/*). 404 if the job doesn't exist, 403 if it isn't the
# caller's. Route is /jobs/{job_id} (NOT under a reserved prefix like admin/*).
@app.route(route="jobs/{job_id}/cancel", methods=["POST"])
def cancel_job(req: func.HttpRequest) -> func.HttpResponse:
    """Cancel the caller's own in-flight job so it reflects 'failed' + refunds WITHIN SECONDS,
    instead of waiting up to ~10 min for the reaper. Stops the running container (frees the single
    A100) and marks the job failed + refunds SYNCHRONOUSLY via _mark_failed (guarded, exactly-once).
    A job already 'completed'/'failed' is a no-op (idempotent)."""
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        user_id = get_user_id(token)
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)

    job_id = _route_job_id(req)
    if job_id is None:
        return func.HttpResponse("Not found", status_code=404)

    conn = get_db()
    cursor = conn.cursor()
    # Ownership gate: 404 if missing, 403 if not the caller's job. age_sec = seconds since submit,
    # computed in SQL (both created_at and GETUTCDATE are UTC) to avoid client/server clock skew.
    cursor.execute(
        "SELECT user_id, status, external_execution_id, "
        "DATEDIFF(SECOND, created_at, GETUTCDATE()) FROM jobs WHERE job_id = ?", job_id)
    row = cursor.fetchone()
    if not row:
        return func.HttpResponse("Not found", status_code=404)
    if row[0] != user_id:
        return func.HttpResponse("Forbidden", status_code=403)
    status, exec_id, age_sec = (row[1] or "").strip(), row[2], int(row[3] or 0)

    if status in ("completed", "failed"):
        # Nothing to cancel; report current state so the client just refreshes.
        return func.HttpResponse(
            json.dumps({"job_id": str(job_id), "status": status, "cancelled": False}),
            mimetype="application/json", status_code=200)

    # Cancel window: only within the first CANCEL_WINDOW_MINUTES after submit. Past that the job is
    # committed (deep into training/generation), so a late cancel is rejected server-side.
    if age_sec > CANCEL_WINDOW_MINUTES * 60:
        return func.HttpResponse(
            json.dumps({"error": "cancel_window_expired",
                        "message": f"Jobs can only be cancelled within {CANCEL_WINDOW_MINUTES} "
                                   f"minutes of starting.",
                        "window_minutes": CANCEL_WINDOW_MINUTES, "age_seconds": age_sec}),
            mimetype="application/json", status_code=409)

    # 1) Free the GPU immediately: best-effort stop the live container (if its execution is known).
    if exec_id:
        try:
            from shared.queue_trigger import stop_execution
            stop_execution(exec_id)
        except Exception as e:
            logging.warning(f"cancel_job: stop_execution failed for job_id={job_id}: {e}")

    # 2) Mark failed + refund SYNCHRONOUSLY (guarded transition, one-time refund).
    _mark_failed(job_id)

    logging.info(f"cancel_job: user={user_id} cancelled job_id={job_id} (was '{status}')")
    return func.HttpResponse(
        json.dumps({"job_id": str(job_id), "status": "failed", "cancelled": True}),
        mimetype="application/json", status_code=200)


@app.route(route="jobs/{job_id}", methods=["DELETE"])
def delete_job(req: func.HttpRequest) -> func.HttpResponse:
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        user_id = get_user_id(token)
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)

    job_id = _route_job_id(req)
    if job_id is None:
        return func.HttpResponse("Not found", status_code=404)
    conn = get_db()
    cursor = conn.cursor()

    # Ownership gate: distinguish missing (404) from not-yours (403). Fetch the
    # owner rather than filtering by user_id so a wrong owner is a 403, not a 404.
    cursor.execute("SELECT user_id FROM jobs WHERE job_id = ?", job_id)
    row = cursor.fetchone()
    if not row:
        return func.HttpResponse("Not found", status_code=404)
    if row[0] != user_id:
        return func.HttpResponse("Forbidden", status_code=403)

    # Authoritative delete FIRST, committed, THEN blob cleanup. Ordering is
    # deliberate: a leftover blob (orphaned storage, no DB pointer) is far cheaper
    # than the reverse — a live jobs row pointing at already-deleted images. If the
    # row delete/commit fails, nothing external has been touched yet and the whole
    # operation is cleanly retryable.
    cursor.execute("DELETE FROM jobs WHERE job_id = ?", job_id)
    conn.commit()

    # Best-effort blob cleanup, AFTER the commit. The row is already gone, so a blob
    # error here must NOT 500 the request — the delete genuinely succeeded, and a
    # retry would now 404. Any orphaned blobs (results/<job_id>/*) can be swept
    # separately; log and return success. A blob already gone is a no-op.
    try:
        blob_client = get_blob_client()
        container = blob_client.get_container_client("outputs")
        for blob in container.list_blobs(name_starts_with=f"results/{job_id}/"):
            container.delete_blob(blob.name)
    except Exception as e:
        logging.warning(f"job {job_id} row deleted but blob cleanup failed: {e}")

    return func.HttpResponse(status_code=204)

# ── User Jobs History ─────────────────────────────────────
@app.route(route="users/jobs", methods=["GET"])
def user_jobs(req: func.HttpRequest) -> func.HttpResponse:
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        user_id = get_user_id(token)
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT job_id, status, job_type, category, output_blob_path, created_at
        FROM jobs
        WHERE user_id = ?
        ORDER BY created_at DESC
    """, user_id)
    rows = cursor.fetchall()

    def _paths(v):
        """output_blob_path is stored as a JSON STRING (main.py json.dumps's the list), but
        the API contract — and every client — expects a real array. Returning the raw column
        meant the dashboard's "Photos Generated" count could never work: it can't count the
        images in an opaque string. Parse it here, once, at the boundary."""
        if not v:
            return []
        if isinstance(v, list):
            return v
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, list) else [parsed]
        except (TypeError, ValueError):
            return [v]          # legacy rows that stored a bare path

    jobs = [
        {
            "job_id": str(r[0]),
            "status": r[1],
            "job_type": r[2],
            "category": r[3],
            "output_blob_path": _paths(r[4]),
            "created_at": _utc_iso(r[5])
        }
        for r in rows
    ]

    return func.HttpResponse(
        json.dumps({"jobs": jobs}),
        mimetype="application/json",
        status_code=200
    )

# ── Get Attires ───────────────────────────────────────────
@app.route(route="attires", methods=["GET"])
def get_attires(req: func.HttpRequest) -> func.HttpResponse:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT attire_id, name, category FROM attires WHERE is_active = 1")
    rows = cursor.fetchall()

    attires = [
        {"id": r[0], "name": r[1], "category": r[2]}
        for r in rows
    ]

    return func.HttpResponse(
        json.dumps({"attires": attires}),
        mimetype="application/json",
        status_code=200
    )

# ── Get Backgrounds ───────────────────────────────────────
@app.route(route="backgrounds", methods=["GET"])
def get_backgrounds(req: func.HttpRequest) -> func.HttpResponse:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT background_id, name, category FROM backgrounds WHERE is_active = 1")
    rows = cursor.fetchall()

    backgrounds = [
        {"id": r[0], "name": r[1], "category": r[2]}
        for r in rows
    ]

    return func.HttpResponse(
        json.dumps({"backgrounds": backgrounds}),
        mimetype="application/json",
        status_code=200
    )

# ── Catalog (categories + attire/background options) ──────
# The SINGLE source of truth for the frontend pickers — served from shared/catalog
# so the UI never hardcodes options (and can't drift from the inference prompts).
# Each option carries a category-qualified `ref` for the global cross-category picker.
@app.route(route="catalog", methods=["GET"])
def get_catalog(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps({"categories": catalog.public_catalog()}),
        mimetype="application/json",
        status_code=200,
    )


# ── Catalog: gender-aware attire/background fetch (from the DB catalog_* tables) ──
# After the UI picks a category (and knows the user's gender), it pulls the gender-
# specific attire list and the background list from these DB-backed endpoints. Public
# (no token) like GET /catalog. Prompt phrases are NOT exposed — they're internal to
# generation; the client only needs ref + label + image.
_CATALOG_KINDS = {"attires": "catalog_attires", "backgrounds": "catalog_backgrounds"}


def _catalog_gender(g):
    """Normalize to the attire-data gender key: male | female | other. None if invalid."""
    g = (g or "").strip().lower()
    if g in ("male", "man", "m"):
        return "male"
    if g in ("female", "woman", "f"):
        return "female"
    if g in ("other", "neutral", "nonbinary", "non-binary", "prefer not to say"):
        return "other"
    return None


def _catalog_image(gender, kind, image_key):
    """Root-relative image path served from the frontend's /public/catalog folder."""
    return f"/catalog/{gender}/{kind}/{image_key}_{gender}.jpg" if image_key else None


def _category_exists(cur, category):
    cur.execute("SELECT 1 FROM dbo.catalog_categories WHERE category_key = ? AND is_active = 1", category)
    return cur.fetchone() is not None


@app.route(route="catalog/{category}/attires", methods=["GET"])
def get_catalog_attires(req: func.HttpRequest) -> func.HttpResponse:
    category = req.route_params.get("category")
    gender = _catalog_gender(req.params.get("gender"))
    if gender is None:
        return func.HttpResponse(
            json.dumps({"error": "gender query param is required: male | female | other"}),
            mimetype="application/json", status_code=400)
    conn = get_db()
    cur = conn.cursor()
    if not _category_exists(cur, category):
        return func.HttpResponse(json.dumps({"error": "unknown category"}),
                                 mimetype="application/json", status_code=404)
    cur.execute(
        "SELECT ref, label, image_key FROM dbo.catalog_attires "
        "WHERE category_key = ? AND gender = ? AND is_active = 1 ORDER BY sort_order",
        category, gender)
    items = [{"ref": r[0], "id": r[0].split(".", 1)[1], "label": r[1], "gender": gender,
              "image": _catalog_image(gender, "attires", r[2])} for r in cur.fetchall()]
    return func.HttpResponse(
        json.dumps({"category": category, "gender": gender, "attires": items}),
        mimetype="application/json", status_code=200)


@app.route(route="catalog/{category}/backgrounds", methods=["GET"])
def get_catalog_backgrounds(req: func.HttpRequest) -> func.HttpResponse:
    category = req.route_params.get("category")
    # Backgrounds are shared across genders; gender only selects the rendered image variant.
    gender = _catalog_gender(req.params.get("gender") or "other")
    if gender is None:
        return func.HttpResponse(
            json.dumps({"error": "gender must be one of: male | female | other"}),
            mimetype="application/json", status_code=400)
    conn = get_db()
    cur = conn.cursor()
    if not _category_exists(cur, category):
        return func.HttpResponse(json.dumps({"error": "unknown category"}),
                                 mimetype="application/json", status_code=404)
    cur.execute(
        "SELECT ref, label, image_key FROM dbo.catalog_backgrounds "
        "WHERE category_key = ? AND is_active = 1 ORDER BY sort_order",
        category)
    items = [{"ref": r[0], "id": r[0].split(".", 1)[1], "label": r[1],
              "image": _catalog_image(gender, "backgrounds", r[2])} for r in cur.fetchall()]
    return func.HttpResponse(
        json.dumps({"category": category, "gender": gender, "backgrounds": items}),
        mimetype="application/json", status_code=200)


# ── Plans (pricing + selection limits) ────────────────────
# Served from shared/plans so the frontend renders prices/limits and enforces the
# same max_attires / max_backgrounds / category_rule the backend enforces.
@app.route(route="plans", methods=["GET"])
def get_plans(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps({"plans": public_plans()}),
        mimetype="application/json",
        status_code=200,
    )

# ── Ops: manual repair endpoints ─────────────────────────
# "admin" is a reserved prefix in Azure Functions and routes under it never
# register — renamed to "ops". Guarded by ADMIN_API_KEY.
def _admin_authorized(req: func.HttpRequest) -> bool:
    # ADMIN_API_KEY must be a long random value stored ONLY in app settings /
    # Key Vault, never logged. Constant-time compare avoids timing leaks. Auth is
    # checked before any handler touches the DB or returns job data.
    key = os.environ.get("ADMIN_API_KEY")
    presented = req.headers.get("X-Admin-Key", "")
    return bool(key) and hmac.compare_digest(presented, key)


@app.route(route="ops/stuck-dispatch", methods=["GET"])
def admin_stuck_dispatch(req: func.HttpRequest) -> func.HttpResponse:
    if not _admin_authorized(req):
        return func.HttpResponse("Forbidden", status_code=403)
    minutes = int(req.params.get("older_than_min", "15"))
    conn = get_db()
    cursor = conn.cursor()
    # Surface BOTH stuck dispatch states:
    #   'dispatching' — backend crashed after claim, before/around GPU start.
    #   'processing'  — the A100 container was OOM-SIGKILLed (exit 137) mid-run,
    #                   so it never wrote a terminal status. This is the row that
    #                   otherwise hangs forever (no reaper caught it before).
    cursor.execute(
        "SELECT job_id, user_id, status, external_execution_id, created_at "
        "FROM jobs WHERE status IN ('dispatching', 'processing') "
        "AND COALESCE(dispatched_at, created_at) < DATEADD(MINUTE, ?, GETUTCDATE())",
        -minutes,
    )
    jobs = [
        {"job_id": str(r[0]), "user_id": r[1], "status": r[2],
         "external_execution_id": r[3], "created_at": _utc_iso(r[4])}
        for r in cursor.fetchall()
    ]
    return func.HttpResponse(json.dumps({"stuck": jobs}), mimetype="application/json")


@app.route(route="ops/jobs/{job_id}/requeue", methods=["POST"])
def admin_requeue(req: func.HttpRequest) -> func.HttpResponse:
    if not _admin_authorized(req):
        return func.HttpResponse("Forbidden", status_code=403)
    job_id = req.route_params.get("job_id")
    # Manual repair ONLY. Operator must FIRST confirm via /admin/stuck-dispatch
    # (and the Container Apps executions view) that no live A100 execution exists
    # for this job — otherwise requeueing could double-start. We only touch jobs
    # still in 'dispatching'.
    conn = new_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM jobs WHERE job_id = ? AND status = 'dispatching'", job_id)
        row = cur.fetchone()
        if row is None:
            return func.HttpResponse(
                json.dumps({"error": "not a stuck 'dispatching' job", "job_id": job_id}),
                mimetype="application/json", status_code=409,
            )
        user_id = row[0]
        cur.execute(
            "UPDATE jobs SET status = 'queued', external_execution_id = NULL "
            "WHERE job_id = ? AND status = 'dispatching'",
            job_id,
        )
        conn.commit()
    finally:
        conn.close()
    enqueue_job({"job_id": str(job_id), "user_id": str(user_id), "job_params": ""})
    return func.HttpResponse(json.dumps({"requeued": job_id}), mimetype="application/json")


@app.route(route="ops/jobs/{job_id}/fail", methods=["POST"])
def admin_fail_job(req: func.HttpRequest) -> func.HttpResponse:
    """Fail + refund a job stuck in 'processing' (the OOM-SIGKILL case the
    container can't self-report). Use this — NOT requeue — for a 'processing'
    row: the A100 already ran and the credit was spent, so requeueing would
    double-charge a second GPU run. _mark_failed is the guarded fail+refund, so
    a job that somehow already finished is left untouched (no spurious refund).

    Operator MUST first confirm via the Container Apps executions view that no
    live A100 execution exists for this job before calling this.
    """
    if not _admin_authorized(req):
        return func.HttpResponse("Forbidden", status_code=403)
    job_id = req.route_params.get("job_id")
    conn = new_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT status FROM jobs WHERE job_id = ? AND status IN ('dispatching', 'processing')",
            job_id,
        )
        if cur.fetchone() is None:
            return func.HttpResponse(
                json.dumps({"error": "not a stuck 'dispatching'/'processing' job", "job_id": job_id}),
                mimetype="application/json", status_code=409,
            )
    finally:
        conn.close()
    _mark_failed(job_id)   # guarded transition + one-time credit refund
    return func.HttpResponse(json.dumps({"failed": job_id, "refunded": True}),
                             mimetype="application/json")


# ── Queue Trigger ─────────────────────────────────────────
@app.queue_trigger(arg_name="msg", queue_name="inference-jobs", connection="AzureWebJobsStorage")
def process_inference_job(msg: func.QueueMessage):
    from shared.queue_trigger import (
        trigger_container_job, count_active_job_executions, find_execution_for_job,
    )
    from shared.queue_client import enqueue_job
    from shared.gpu_lease import (
        acquire_dispatch_lease, release_dispatch_lease,
        mark_dispatched, clear_dispatch_pending,
        recent_dispatch_pending, DispatchConfigError,
    )
    # The host already base64-decodes the transport (messageEncoding=base64,
    # the extension-bundle default), so get_body() returns the raw JSON.
    payload = json.loads(msg.get_body().decode("utf-8"))
    if not isinstance(payload, dict) or not payload.get("job_id") or not payload.get("user_id"):
        # Raising keeps the message retryable and ultimately visible in the poison
        # queue; acknowledging it here would silently lose the associated job.
        logging.error(f"malformed inference queue message: {payload!r}")
        raise ValueError("inference queue message requires job_id and user_id")
    job_id = payload["job_id"]
    user_id = payload["user_id"]

    # 1) Emergency kill switch. Re-enqueue with a LONG fixed delay and WITHOUT
    #    incrementing defer_count, so a deliberate pause neither times jobs out
    #    nor churns the queue. Loss-safe: enqueue happens before we return (the
    #    message is only completed by returning), so if enqueue raises, the host
    #    retries the original instead of losing the job.
    if not _gpu_dispatch_enabled():
        enqueue_job(payload, visibility_timeout=KILL_SWITCH_PAUSE_DELAY)
        logging.warning(f"GPU_DISPATCH_ENABLED=false; paused job_id={job_id}")
        return

    # 2) Global dispatch lease. Serializes check-then-start across ALL scaled-out
    #    instances (the real fix for the race host.json/batchSize can't close).
    #    - HELD by another instance (None) -> defer and retry.
    #    - lease row/table MISSING (DispatchConfigError) -> a deploy/config error:
    #      FAIL the job loudly (DISPATCH_CONFIG_ERROR) and stop. Never start a job
    #      without the lease; never defer forever on a broken deploy.
    try:
        owner = acquire_dispatch_lease()
    except DispatchConfigError as e:
        logging.error(
            f"DISPATCH_CONFIG_ERROR: {e}; failing job_id={job_id} without GPU start. "
            f"ALERT: apply migration 001 / check the lease table."
        )
        _mark_failed(job_id)
        return
    if owner is None:
        _defer_job(payload, job_id)
        return

    # The lease is held ONLY around the small critical section below
    # (idempotency check -> cap check -> claim -> start -> record). It is NOT held
    # during inference, which runs in the separate container.
    try:
        conn = new_connection()
        try:
            cur = conn.cursor()

            # 3) Dispatch idempotency. A retried message (e.g. after a crash)
            #    must never start a SECOND A100 for the same job_id. If the job
            #    already has an execution id or has moved past 'queued', skip.
            cur.execute(
                "SELECT status, external_execution_id FROM jobs WHERE job_id = ?",
                job_id,
            )
            row = cur.fetchone()
            if row is None:
                logging.error(f"job_id={job_id} not found in DB; dropping message")
                return
            status, exec_id = row[0], row[1]
            if status == "dispatching" and not exec_id:
                exec_id = find_execution_for_job(job_id)
                if exec_id:
                    _record_execution_id(job_id, exec_id)
                    logging.warning(
                        f"job_id={job_id} recovered execution_id={exec_id} after dispatch commit gap"
                    )
            if exec_id or status in ("dispatching", "processing", "completed", "failed"):
                # A prior attempt may have persisted the execution id and then crashed
                # before clearing the short API-visibility reservation. Retries repair it.
                if exec_id or status in ("processing", "completed", "failed"):
                    clear_dispatch_pending(owner)
                logging.info(
                    f"job_id={job_id} already dispatched/terminal "
                    f"(status={status}, execution_id={exec_id}); not starting again"
                )
                return

            # 4) Active-job cap from the executions API (+ grace bump for a
            #    just-started job the API may not list yet). Over cap -> defer;
            #    job stays 'queued' so it retries cleanly.
            active = count_active_job_executions()
            if recent_dispatch_pending():
                active += 1
            if active >= MAX_ACTIVE_GPU_JOBS:
                _defer_job(payload, job_id)
                return

            # 5) Claim the job atomically: only the writer that flips queued ->
            #    dispatching proceeds (guards against any double-claim).
            cur.execute(
                # Stamp dispatched_at HERE (the queued -> dispatching claim): it marks when the
                # GPU run begins, so the reaper can measure the processing deadline from it and
                # not penalise a job for queue wait (finding #5, part A).
                "UPDATE jobs SET status = 'dispatching', dispatched_at = GETUTCDATE() "
                "WHERE job_id = ? AND status = 'queued'",
                job_id,
            )
            claimed = cur.rowcount == 1
            conn.commit()
            if not claimed:
                logging.info(f"job_id={job_id} claim lost (concurrent); skipping")
                return
        finally:
            conn.close()

        # 6) Reserve capacity BEFORE the blocking Container Apps start call.
        #    The short ownership lease may expire during A100 cold-start, but
        #    last_dispatch_at remains visible to recent_dispatch_pending().
        mark_dispatched(owner)

        # Start the GPU job. If the start fails, revert the claim so the job
        #    can be retried (no A100 was started -> no double-spend), then
        #    re-raise so the queue retries the message.
        try:
            execution_id = trigger_container_job(job_id, user_id)
        except Exception:
            logging.exception(f"start failed for job_id={job_id}; reverting claim to 'queued'")
            _revert_claim(job_id)
            clear_dispatch_pending(owner)
            raise

        _record_execution_id(job_id, execution_id)
        clear_dispatch_pending(owner)
        logging.info(f"Started job_id={job_id} execution_id={execution_id} (active before={active})")
    finally:
        release_dispatch_lease(owner)


def _mark_failed(job_id: str):
    """Fail a job and refund its credit — exactly once.

    The credit was spent at submit (reserve_job_slot). Any terminal failure that
    is the user's-no-fault — dispatch config error, dispatch timeout, poison —
    should return it. The refund is tied to the ACTUAL state transition
    (WHERE status NOT IN ('failed','completed') + rowcount check), so retries,
    poison + timeout both firing, or the container ALSO failing the same job can
    never refund twice. completed jobs are never touched (no refund on success).
    """
    conn = new_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE jobs SET status = 'failed', completed_at = GETUTCDATE() "
            "WHERE job_id = ? AND status NOT IN ('failed', 'completed')",
            job_id,
        )
        transitioned = cur.rowcount == 1
        refund = 0
        if transitioned:
            # Refund the FULL amount spent at submit (image_count * credits_per_image),
            # stored as credit_cost in job_params. Falls back to 1 for any legacy job
            # written before per-image charging. Tied to the same state-transition
            # rowcount guard above, so it can never double-refund.
            cur.execute(
                "SELECT job_params, user_id, source_type FROM jobs WHERE job_id = ?",
                job_id,
            )
            r = cur.fetchone()
            source_type = r[2] if r and len(r) > 2 else None
            refund = 1
            if r and r[0]:
                try:
                    refund = max(1, int(json.loads(r[0]).get("credit_cost", 1)))
                except (TypeError, ValueError):
                    refund = 1
            # Refund to whichever pool was charged. jobs.organization_id was written at
            # reserve time, so this stays correct even if the person has since left the
            # org — the credits go back to the org that paid for them.
            cur.execute("SELECT organization_id FROM jobs WHERE job_id = ?", job_id)
            orow = cur.fetchone()
            job_org_id = orow[0] if orow else None
            if job_org_id:
                cur.execute(
                    "UPDATE organization_members SET credits_remaining = credits_remaining + ? "
                    "WHERE user_id = (SELECT user_id FROM jobs WHERE job_id = ?) "
                    "AND organization_id = ?",
                    refund, job_id, job_org_id,
                )
            elif source_type == "monthly":
                monthly_refund = refund
                one_time_refund = 0
                if r and r[0]:
                    try:
                        refund_params = json.loads(r[0])
                        monthly_refund = max(
                            0, int(refund_params.get("monthly_credit_cost", refund))
                        )
                        one_time_refund = max(
                            0, int(refund_params.get("one_time_credit_cost", 0))
                        )
                    except (TypeError, ValueError):
                        monthly_refund = refund
                        one_time_refund = 0
                cur.execute(
                    "UPDATE users SET "
                    "monthly_credits_remaining = monthly_credits_remaining + "
                    "CASE WHEN subscription_type = 'monthly' THEN ? ELSE 0 END, "
                    "one_time_credits_remaining = one_time_credits_remaining + ?, "
                    "credits_remaining = credits_remaining + ? + ? "
                    "WHERE user_id = (SELECT user_id FROM jobs WHERE job_id = ?)",
                    monthly_refund, one_time_refund,
                    monthly_refund, one_time_refund, job_id,
                )
            elif source_type == "one_time":
                cur.execute(
                    "UPDATE users SET credits_remaining = credits_remaining + ?, "
                    "one_time_credits_remaining = one_time_credits_remaining + ? "
                    "WHERE user_id = (SELECT user_id FROM jobs WHERE job_id = ?)",
                    refund, refund, job_id,
                )
            else:
                cur.execute(
                    "UPDATE users SET credits_remaining = credits_remaining + ? "
                    "WHERE user_id = (SELECT user_id FROM jobs WHERE job_id = ?)",
                    refund, job_id,
                )
            # Ledger the refund (positive), in the SAME transaction as the state transition +
            # balance restore so it can never double-count. r[1] is the job's user_id (guard
            # the read: if the row is unexpectedly gone we still refund via the subquery UPDATE
            # above, we just can't attribute a ledger row).
            if r and len(r) > 1 and r[1]:
                credit_ledger.record(cur, r[1], refund, credit_ledger.REASON_JOB_REFUND, job_id)
        conn.commit()
        logging.info(
            f"job_id={job_id} -> failed (transitioned={transitioned}, "
            f"credits_refunded={refund if transitioned else 0})"
        )
    finally:
        conn.close()


def _stamp_failure_class(job_id, failure_class, reason, execution_id):
    """Record WHY a job failed, idempotently, inside job_params JSON (no schema change).

    This is what stops an infrastructure failure from being silently indistinguishable from
    a customer/model failure: it preserves the diagnostic reason + the ACA execution id.
    Idempotent — if a `_failure` stamp already exists it is left untouched, so reconciliation
    retries never rewrite the first (authoritative) reason."""
    conn = new_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT job_params FROM jobs WHERE job_id = ?", job_id)
        row = cur.fetchone()
        if not row:
            return
        try:
            params = json.loads(row[0]) if row[0] else {}
        except (TypeError, ValueError):
            params = {}
        if isinstance(params.get("_failure"), dict):
            return  # already stamped
        params["_failure"] = {
            "class": failure_class,
            "reason": reason,
            "execution_id": execution_id,
            "is_infra": failure_class in exec_reconcile.INFRA_CLASSES,
        }
        cur.execute("UPDATE jobs SET job_params = ? WHERE job_id = ?",
                    json.dumps(params), job_id)
        conn.commit()
    finally:
        conn.close()


def _fetch_execution_outcome(job_id):
    """Read the whole container execution status from ACA, then optionally enrich it with
    preflight diagnostics. A preflight exit 0 is never treated as container success."""
    try:
        from shared.queue_trigger import get_job_execution_outcome

        conn = new_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT external_execution_id FROM jobs WHERE job_id = ?", job_id)
            r = cur.fetchone()
        finally:
            conn.close()
        exec_id = r[0] if r else None
        if not exec_id:
            return None
        outcome = get_job_execution_outcome(str(exec_id))
        outcome.setdefault("events", [])
        try:
            raw = download_blob(
                "diagnostics", f"preflight/{str(exec_id).replace('/', '_')}.json")
            diag = json.loads(raw)
            result = diag.get("result") or {}
            outcome["preflight_exit_code"] = result.get("exit_code")
            outcome["diagnostic_reason"] = result.get("reason")
        except Exception:
            pass
        return outcome
    except Exception:
        logging.exception(f"could not fetch ACA execution outcome for job_id={job_id}")
        return None


def _list_delivered_results(job_id):
    """Return delivered image blob paths, [] when definitively empty, or None on storage error."""
    try:
        container = get_blob_client().get_container_client("outputs")
        paths = [
            str(blob.name) for blob in container.list_blobs(name_starts_with=f"results/{job_id}/")
            if str(blob.name).lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
        ]
        return sorted(paths)
    except Exception:
        logging.exception(f"could not verify delivered outputs for job_id={job_id}")
        return None


def _recover_completed_job(job_id, result_paths):
    """Recover a stale processing row when ACA succeeded and output blobs exist."""
    conn = new_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE jobs SET status = 'completed', output_blob_path = ?, "
            "completed_at = COALESCE(completed_at, GETUTCDATE()) "
            "WHERE job_id = ? AND status NOT IN ('completed', 'failed')",
            json.dumps(result_paths), job_id,
        )
        transitioned = cur.rowcount == 1
        conn.commit()
        return transitioned
    finally:
        conn.close()


def _reconcile_execution_outcome(job_id, exec_data, now, *,
                                 mark_failed=None, stamp=None, classify=None,
                                 list_results=None, recover_completed=None):
    """Classify one execution outcome and, when a refund is owed, stamp the class+reason and
    fail+refund EXACTLY ONCE. The one-refund guarantee is _mark_failed's rowcount guard
    (status NOT IN ('failed','completed')); stamping is separately idempotent. Deps are
    injectable so this is unit-tested with no DB/Azure."""
    mark_failed = mark_failed or _mark_failed
    stamp = stamp or _stamp_failure_class
    classify = classify or exec_reconcile.classify_execution
    list_results = list_results or _list_delivered_results
    recover_completed = recover_completed or _recover_completed_job
    decision = classify(exec_data, now)
    if decision["action"] == exec_reconcile.ACTION_REFUND:
        stamp(job_id, decision["failure_class"], decision["reason"], decision["execution_id"])
        mark_failed(job_id)   # idempotent; refunds only on the real state transition
    elif decision["action"] == exec_reconcile.ACTION_VERIFY_DELIVERY:
        result_paths = list_results(job_id)
        if result_paths is None:
            return {
                **decision,
                "action": exec_reconcile.ACTION_NONE,
                "failure_class": exec_reconcile.CLASS_PENDING,
                "reason": "ACA succeeded but output storage could not be checked; retrying",
            }
        if result_paths:
            recover_completed(job_id, result_paths)
            return {**decision, "action": exec_reconcile.ACTION_RECOVER,
                    "reason": f"recovered {len(result_paths)} delivered image(s)"}
        missing = {
            **decision,
            "action": exec_reconcile.ACTION_REFUND,
            "failure_class": exec_reconcile.CLASS_DELIVERY_MISSING,
            "reason": "ACA execution succeeded but no result images were delivered",
        }
        stamp(job_id, missing["failure_class"], missing["reason"], missing["execution_id"])
        mark_failed(job_id)
        return missing
    return decision


def _revert_claim(job_id: str):
    conn = new_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE jobs SET status = 'queued' WHERE job_id = ? AND status = 'dispatching'",
            job_id,
        )
        conn.commit()
    finally:
        conn.close()


def _record_execution_id(job_id: str, execution_id):
    conn = new_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE jobs SET external_execution_id = ? WHERE job_id = ?",
            execution_id, job_id,
        )
        conn.commit()
    finally:
        conn.close()


def _defer_job(payload: dict, job_id: str):
    """Re-enqueue an over-cap (or lease-busy) job with exponential backoff. After
    MAX_DISPATCH_DEFERS attempts, FAIL it (logged DISPATCH_TIMEOUT) so a stuck cap
    or broken executions API can never churn the queue forever.

    Loss-safe: enqueue_job runs BEFORE this returns. process_inference_job only
    completes the message by returning normally, so if enqueue_job raises the
    exception propagates and the host retries the original message — the job is
    never lost."""
    from shared.queue_client import enqueue_job

    defer_count = int(payload.get("defer_count", 0))
    if defer_count >= MAX_DISPATCH_DEFERS:
        logging.error(
            f"DISPATCH_TIMEOUT: job_id={job_id} exceeded {MAX_DISPATCH_DEFERS} "
            f"dispatch deferrals; marking failed"
        )
        # status stays 'failed' for UI compatibility; DISPATCH_TIMEOUT reason is
        # in logs. TODO: add a failure_code column with the schema-drift fix.
        # _mark_failed also refunds the credit (guarded, once) — a job that never
        # got to run shouldn't cost the user.
        _mark_failed(job_id)
        return

    delay = min(GPU_BACKPRESSURE_BASE * (2 ** defer_count), GPU_BACKPRESSURE_MAX)
    payload["defer_count"] = defer_count + 1
    enqueue_job(payload, visibility_timeout=delay)
    logging.info(
        f"GPU at cap; deferred job_id={job_id} "
        f"(defer {defer_count + 1}/{MAX_DISPATCH_DEFERS}) for {delay}s"
    )


# ── Identity-LoRA training dispatch ───────────────────────────────────────────
# Deliberately mirrors process_inference_job beat for beat, and takes the SAME
# dispatch lease and the SAME active-job cap — training and inference run on one
# shared A100 profile, so they must serialize against each other, not just against
# themselves. (count_active_job_executions now spans both job names.)
def _record_training_error(training_id: str, err: str):
    """Persist the ROOT CAUSE of a dispatch failure on the training row.

    Without this, a dispatch that throws is retried 3x and then poisoned, and the only
    thing ever written is the poison handler's "exceeded retry limit" — which says
    nothing about WHY. That is exactly what happened on the first live run, and App
    Insights sampling/ingestion lag made the real exception invisible for the entire
    debug session. The DB is the one place we control, so the cause goes there.

    Keeps the FIRST error (WHERE error IS NULL): the root cause is more useful than the
    retry-limit message that lands last.
    """
    try:
        conn = new_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE lora_trainings SET error = ? WHERE training_id = ? AND error IS NULL",
                err[:1000], training_id,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logging.exception("could not persist training error (non-fatal)")


@app.queue_trigger(arg_name="msg", queue_name="lora-training-jobs", connection="AzureWebJobsStorage")
def process_training_job(msg: func.QueueMessage):
    payload = json.loads(msg.get_body().decode("utf-8"))
    training_id = payload["training_id"]
    user_id = payload["user_id"]
    try:
        _dispatch_training(payload, training_id, user_id)
    except Exception as e:
        logging.exception(f"training dispatch FAILED training_id={training_id}")
        _record_training_error(training_id, f"{type(e).__name__}: {e}")
        raise


def _dispatch_training(payload: dict, training_id: str, user_id: str):
    from shared.queue_trigger import count_active_job_executions
    from shared.training_trigger import trigger_training_job
    from shared.queue_client import enqueue_training_job
    from shared.gpu_lease import (
        acquire_dispatch_lease, release_dispatch_lease,
        mark_dispatched, clear_dispatch_pending,
        recent_dispatch_pending, DispatchConfigError,
    )

    if not _gpu_dispatch_enabled():
        enqueue_training_job(payload, visibility_timeout=KILL_SWITCH_PAUSE_DELAY)
        logging.warning(f"GPU_DISPATCH_ENABLED=false; paused training_id={training_id}")
        return

    try:
        owner = acquire_dispatch_lease()
    except DispatchConfigError as e:
        logging.error(
            f"DISPATCH_CONFIG_ERROR: {e}; failing training_id={training_id}. "
            f"ALERT: apply migration 001 / check the lease table."
        )
        _fail_training(training_id, "dispatch config error")
        return
    if owner is None:
        _defer_training(payload, training_id)
        return

    try:
        conn = new_connection()
        try:
            cur = conn.cursor()

            # Dispatch idempotency: a retried message must never start a SECOND A100
            # for the same training run.
            cur.execute(
                "SELECT status, external_execution_id, files_json, class_word "
                "FROM lora_trainings WHERE training_id = ?",
                training_id,
            )
            row = cur.fetchone()
            if row is None:
                logging.error(f"training_id={training_id} not found; dropping message")
                return
            status, exec_id, files_json, class_word = row[0], row[1], row[2], row[3]
            if exec_id or status in ("dispatching", "training", "completed", "failed"):
                if exec_id or status in ("training", "completed", "failed"):
                    clear_dispatch_pending(owner)
                logging.info(
                    f"training_id={training_id} already dispatched/terminal "
                    f"(status={status}, execution_id={exec_id}); not starting again"
                )
                return

            active = count_active_job_executions()
            if recent_dispatch_pending():
                active += 1
            if active >= MAX_ACTIVE_GPU_JOBS:
                _defer_training(payload, training_id)
                return

            cur.execute(
                "UPDATE lora_trainings SET status = 'dispatching' "
                "WHERE training_id = ? AND status = 'queued'",
                training_id,
            )
            claimed = cur.rowcount == 1
            conn.commit()
            if not claimed:
                logging.info(f"training_id={training_id} claim lost (concurrent); skipping")
                return

            # ── FUSED train+generate (MODE=train_infer) ──────────────────────────────
            # A user who just paid sees ONE action ("make my headshots"), not a training
            # run and then a generation run. If they already parked a job behind this
            # training, hand it to the SAME container: it trains, then generates, with one
            # cold start and one queue hop instead of two (~4 min of a ~45 min journey).
            #
            # CLAIMING IT HERE IS WHAT MAKES THIS SAFE. _finish_training releases EVERY job
            # still in 'waiting_lora' for this user when training completes; a fused job left
            # there would be enqueued a second time and generated twice for one payment.
            # Moving it to 'processing' inside this same transaction removes it from that
            # query, so no change to _finish_training is needed. 'processing' is also what
            # the reaper watches (REAPER_STUCK_MINUTES), so a fused run that dies after start
            # is still recovered and refunded by the existing safety net.
            #
            # Oldest parked job only: fusing carries exactly one job, and any others stay
            # parked and release normally when training finishes.
            fused_job_id = None
            try:
                cur.execute(
                    "SELECT TOP 1 job_id FROM jobs WHERE user_id = ? AND status = 'waiting_lora' "
                    "ORDER BY created_at",
                    user_id,
                )
                row = cur.fetchone()
                if row:
                    candidate = str(row[0])
                    cur.execute(
                        "UPDATE jobs SET status = 'processing', dispatched_at = GETUTCDATE() "
                        "WHERE job_id = ? AND status = 'waiting_lora'",
                        candidate,
                    )
                    if cur.rowcount == 1:
                        fused_job_id = candidate
                    conn.commit()
            except Exception:
                # Fusing is an OPTIMISATION. If the claim fails for any reason, fall back to
                # plain MODE=train — the job stays parked and _finish_training releases it
                # exactly as it does today. Never let this break the working path.
                logging.exception(
                    f"fused-job claim failed for training_id={training_id}; using MODE=train")
                fused_job_id = None
                try:
                    conn.rollback()
                except Exception:
                    pass
        finally:
            conn.close()

        # Training and inference share the same A100 cap, so training needs the
        # same pre-start reservation across lease expiry and API visibility lag.
        mark_dispatched(owner)
        try:
            # user_id is passed ONCE and drives both the input photo paths and the
            # adapter output path. trigger_training_job re-verifies every file lives
            # under this user's prefix before it will start.
            execution_id = trigger_training_job(
                user_id, json.loads(files_json), class_word, job_id=fused_job_id)
        except Exception as e:
            logging.exception(f"training start failed for training_id={training_id}")
            # No A100 was started, so UN-CLAIM the fused job before anything else: we moved
            # it to 'processing' in anticipation of a container that never ran. Left there it
            # would sit until the reaper timed it out (~130 min) even though the training
            # itself is about to be retried. Back to 'waiting_lora' means the retry can fuse
            # it again, or _finish_training releases it normally.
            if fused_job_id:
                try:
                    c0 = new_connection()
                    try:
                        cc = c0.cursor()
                        cc.execute(
                            "UPDATE jobs SET status = 'waiting_lora', dispatched_at = NULL "
                            "WHERE job_id = ? AND status = 'processing'",
                            fused_job_id,
                        )
                        c0.commit()
                    finally:
                        c0.close()
                except Exception:
                    logging.exception(
                        f"could not un-claim fused job {fused_job_id}; reaper will recover it")
            # Revert to 'queued' so the queue retry can pick it up again — unless the payload
            # itself is bad (ValueError from the prefix guard), which will never succeed and
            # must fail loudly instead of looping.
            if isinstance(e, ValueError):
                _fail_training(training_id, str(e))
                return
            conn3 = new_connection()
            try:
                c3 = conn3.cursor()
                c3.execute(
                    "UPDATE lora_trainings SET status = 'queued' "
                    "WHERE training_id = ? AND status = 'dispatching'",
                    training_id,
                )
                conn3.commit()
            finally:
                conn3.close()
            clear_dispatch_pending(owner)
            raise

        conn4 = new_connection()
        try:
            c4 = conn4.cursor()
            c4.execute(
                "UPDATE lora_trainings SET status = 'training', external_execution_id = ? "
                "WHERE training_id = ?",
                execution_id, training_id,
            )
            conn4.commit()
        finally:
            conn4.close()
        clear_dispatch_pending(owner)
        logging.info(
            f"Started training_id={training_id} user={user_id} "
            f"execution_id={execution_id} (active before={active})"
        )
    finally:
        release_dispatch_lease(owner)


def _defer_training(payload: dict, training_id: str):
    """Same exponential back-pressure as _defer_job. A training run that can never get
    a GPU is failed rather than churning the queue forever."""
    from shared.queue_client import enqueue_training_job

    defer_count = int(payload.get("defer_count", 0))
    if defer_count >= MAX_DISPATCH_DEFERS:
        logging.error(
            f"DISPATCH_TIMEOUT: training_id={training_id} exceeded {MAX_DISPATCH_DEFERS} "
            f"deferrals; marking failed"
        )
        _fail_training(training_id, "could not get a GPU (dispatch timeout)")
        return

    delay = min(GPU_BACKPRESSURE_BASE * (2 ** defer_count), GPU_BACKPRESSURE_MAX)
    payload["defer_count"] = defer_count + 1
    enqueue_training_job(payload, visibility_timeout=delay)
    logging.info(
        f"GPU at cap; deferred training_id={training_id} "
        f"(defer {defer_count + 1}/{MAX_DISPATCH_DEFERS}) for {delay}s"
    )


def _identity_adapter_exists(user_id: str) -> bool:
    """Does this user already have a usable identity LoRA in blob storage?

    Asked on a FAILED training run to decide whether that failure actually cost them
    anything. The trainer writes the adapter only at the very end (after its format gate),
    so a failed run leaves any previous adapter untouched. Fails CLOSED (False) if blob
    storage can't be reached — better to mark them 'failed' and let them retrain than to
    claim they have a model we can't confirm.
    """
    try:
        blob = get_blob_client().get_blob_client(
            container="lora-weights",
            blob=f"identity/{user_id}/adapter_model.safetensors",
        )
        return bool(blob.exists())
    except Exception as e:
        logging.warning(f"could not check adapter for user={user_id}: {e}")
        return False


def _finish_training(training_id: str, user_id: str, ok: bool, error: str = None):
    """Terminal transition for a training run, and the release of everything parked
    behind it. Guarded by the status check + rowcount so it runs EXACTLY once even if
    the watcher and a retry both see the same completed execution.

    On success: lora_status='ready', and every job the user parked in 'waiting_lora'
    flips to 'queued' and is enqueued IMMEDIATELY (visibility 0) — no polling lag.
    On failure: lora_status='failed', and the parked jobs are failed + refunded rather
    than left waiting for an adapter that will never arrive.
    """
    conn = new_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT ISNULL(monthly_credit_cost, 0), ISNULL(one_time_credit_cost, 0) "
            "FROM lora_trainings WITH (UPDLOCK, HOLDLOCK) WHERE training_id = ?",
            training_id,
        )
        charge_row = cur.fetchone()
        monthly_retrain_charge = int(charge_row[0] or 0) if charge_row else 0
        one_time_retrain_charge = int(charge_row[1] or 0) if charge_row else 0
        # COALESCE: never clobber a root cause already recorded by _record_training_error.
        # The poison handler's "exceeded retry limit" arrives LAST and is the least useful
        # message; the first error written is the one that explains the failure.
        cur.execute(
            "UPDATE lora_trainings SET status = ?, error = COALESCE(error, ?), "
            "completed_at = GETUTCDATE() "
            "WHERE training_id = ? AND status NOT IN ('completed', 'failed')",
            "completed" if ok else "failed", (error[:1000] if error else None), training_id,
        )
        transitioned = cur.rowcount == 1
        if not transitioned:
            conn.commit()
            return

        # A FAILED RETRAIN MUST NOT STRAND A USER WHO ALREADY HAD A WORKING MODEL.
        # The trainer overwrites the adapter only at the very END, after its format gate —
        # so when a run fails, any PREVIOUS adapter is still sitting in blob, untouched and
        # perfectly usable. Blindly setting lora_status='failed' would 409 that user out of
        # /jobs/submit and take away a product they already had, over a failure that damaged
        # nothing. So on failure, ask blob storage whether an adapter still exists and keep
        # them 'ready' if it does.
        recovered = not ok and _identity_adapter_exists(user_id)
        if recovered:
            logging.warning(
                f"training FAILED for user={user_id} but a previous adapter is intact; "
                f"keeping lora_status='ready' (their existing model still works)"
            )
        new_status = "ready" if (ok or recovered) else "failed"
        cur.execute(
            "UPDATE users SET lora_status = ? WHERE user_id = ?", new_status, user_id)

        if not ok and (monthly_retrain_charge or one_time_retrain_charge):
            retrain_refund = monthly_retrain_charge + one_time_retrain_charge
            cur.execute(
                "UPDATE users SET "
                "monthly_credits_remaining = monthly_credits_remaining + ?, "
                "one_time_credits_remaining = one_time_credits_remaining + ?, "
                "credits_remaining = credits_remaining + ? WHERE user_id = ?",
                monthly_retrain_charge, one_time_retrain_charge,
                retrain_refund, user_id,
            )
            credit_ledger.record(
                cur, user_id, retrain_refund, credit_ledger.REASON_RETRAIN_REFUND)

        # `ok` stays the TRUTH about the training run (for the row + the log). `usable` is
        # the separate question of whether the user has an adapter to render with, which is
        # what parked jobs actually depend on. Conflating the two would either strand the
        # user or log a failed run as "completed".
        usable = ok or recovered
        cur.execute(
            "SELECT job_id, job_params FROM jobs WHERE user_id = ? AND status = 'waiting_lora'",
            user_id,
        )
        parked = [(str(r[0]), r[1]) for r in cur.fetchall()]
        released = []   # (outbox_id, message) for the fast-path send after commit
        if usable and parked:
            cur.execute(
                "UPDATE jobs SET status = 'queued' WHERE user_id = ? AND status = 'waiting_lora'",
                user_id,
            )
            # Transactional outbox (finding #4): each waiting_lora -> queued release gets its
            # queue message written IN THIS transaction, so a queue outage after commit can't
            # leave a parked job stuck 'queued' with no message (the reaper won't recover it).
            for job_id, job_params in parked:
                msg = {"job_id": job_id, "user_id": str(user_id), "job_params": job_params}
                released.append((outbox_add(cur, INFERENCE_QUEUE, msg), msg))
        conn.commit()
    finally:
        conn.close()

    logging.info(
        f"training_id={training_id} user={user_id} -> {'completed' if ok else 'failed'}"
        f"{'' if ok else f' ({error})'}; lora_status={new_status} "
        f"{'(RECOVERED: prior adapter intact) ' if recovered else ''}parked_jobs={len(parked)}"
    )

    if usable:
        # Fast-path each release (visibility 0, so the GPU picks them up at once); whatever
        # doesn't send now, the outbox_dispatcher delivers on its next tick.
        for outbox_id, msg in released:
            # Per-job guard: a fast-path failure on ONE parked job must not abort releasing the
            # rest (the outbox_dispatcher backstops any that don't send here).
            try:
                outbox_try_send_now(outbox_id, INFERENCE_QUEUE, msg)
            except Exception as e:
                logging.warning(
                    f"release fast-path failed for job_id={msg['job_id']} "
                    f"(non-fatal; dispatcher will deliver): {e}"
                )
            logging.info(f"released parked job_id={msg['job_id']} for user={user_id}")
    else:
        # Never leave a user waiting on an adapter that will never exist.
        # _mark_failed refunds their credits (guarded, once).
        for job_id, _ in parked:
            _mark_failed(job_id)


def _fail_training(training_id: str, error: str):
    conn = new_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM lora_trainings WHERE training_id = ?", training_id)
        row = cur.fetchone()
    finally:
        conn.close()
    if row:
        _finish_training(training_id, str(row[0]), ok=False, error=error)


# ── Poison handler ────────────────────────────────────────────────────────
# After maxDequeueCount (3) failed processing attempts the host auto-moves a
# message to "<queue>-poison". Mark the job failed so a bad message stops
# re-entering the GPU queue and isn't silently lost.
@app.queue_trigger(arg_name="msg", queue_name="inference-jobs-poison", connection="AzureWebJobsStorage")
def handle_poison_job(msg: func.QueueMessage):
    try:
        payload = json.loads(msg.get_body().decode("utf-8"))
        job_id = payload.get("job_id")
    except Exception:
        logging.error(f"POISON: unparseable message dropped: {msg.get_body()!r}")
        return

    if not job_id:
        # There is no safe DB row to update without an identifier. Do not acknowledge
        # and discard the evidence: fail this trigger so operators retain visibility.
        logging.critical(f"POISON: message missing job_id: {payload!r}")
        raise ValueError("poison inference message missing job_id")

    logging.error(
        f"POISON: job_id={job_id} exceeded retry limit (dequeue_count={msg.dequeue_count}); "
        f"marking failed"
    )
    if job_id:
        # Guarded fail + one-time credit refund (same helper as every other
        # failure path) so a poisoned message doesn't silently cost the user.
        _mark_failed(job_id)


# ── Timer-trigger reaper ──────────────────────────────────────────────────────
# Runs every 10 minutes. Finds jobs stuck in 'processing' past the wall-time ceiling
# (REAPER_STUCK_MINUTES, default 130 min), measured from dispatched_at (COALESCE
# created_at) so queue wait doesn't count — 130 sits just above the GPU job's own
# 120-min replicaTimeout, so a still-'processing' row past it is genuinely dead, never a
# slow-but-healthy run (finding #5). Also reaps 'dispatching' rows the dispatcher crashed
# mid-claim (REAPER_DISPATCHING_MINUTES, default 15 min).
# Both paths call _mark_failed: guarded transition + one-time credit refund.
# An OOM SIGKILL (exit 137) leaves the row in 'processing' because the process
# is killed before it can write — this reaper is the ONLY thing that clears it.
@app.timer_trigger(schedule="0 */2 * * * *", arg_name="timer", run_on_startup=False)
def reaper(timer: func.TimerRequest):
    from shared.queue_trigger import find_execution_for_job, execution_status
    conn = new_connection()
    try:
        cur = conn.cursor()
        # Measure from dispatched_at (when the GPU run started), COALESCE to created_at for
        # rows written before migration 015 — so queue wait doesn't count against the
        # deadline and a healthy job that merely waited isn't reaped (finding #5, part A).
        cur.execute(
            "SELECT job_id FROM jobs WHERE status = 'processing' "
            "AND COALESCE(dispatched_at, created_at) < DATEADD(MINUTE, ?, GETUTCDATE())",
            -REAPER_STUCK_MINUTES,
        )
        stuck = [str(r[0]) for r in cur.fetchall()]

        cur.execute(
            "SELECT job_id, external_execution_id FROM jobs WHERE status = 'dispatching' "
            "AND COALESCE(dispatched_at, created_at) < DATEADD(MINUTE, ?, GETUTCDATE())",
            -REAPER_DISPATCHING_MINUTES,
        )
        dispatching = [(str(r[0]), r[1]) for r in cur.fetchall()]

        # EARLY reconcile: a 'processing' job whose ACA execution has ALREADY gone terminal
        # (killed / crashed / operator-stopped) is definitively dead — don't make it wait out the
        # full REAPER_STUCK_MINUTES healthy-run window. Bounded to rows with a known execution id;
        # the per-execution status read (an ACA call) is done below, after the DB conn closes.
        cur.execute(
            "SELECT job_id, external_execution_id FROM jobs "
            "WHERE status = 'processing' AND external_execution_id IS NOT NULL"
        )
        processing_with_exec = [(str(r[0]), r[1]) for r in cur.fetchall()]
    finally:
        conn.close()

    # A terminal-but-not-successful execution means the container is gone without delivering —
    # fail+refund now. 'Succeeded' is deliberately excluded: the container writes 'completed'
    # itself, so a Succeeded execution whose row still reads 'processing' is a write race, NOT a
    # refund case (refunding it would hand the user both a refund AND the delivered images).
    _DEAD_EXEC_STATES = {"stopped", "failed", "degraded", "cancelled"}
    _stuck_seen = set(stuck)
    for job_id, exec_id in processing_with_exec:
        if job_id in _stuck_seen:
            continue
        state = (execution_status(exec_id) or "").lower()
        if state in _DEAD_EXEC_STATES:
            logging.warning(
                f"REAPER: processing job_id={job_id} has terminal execution status='{state}' "
                f"— reconciling early (not waiting REAPER_STUCK_MINUTES)"
            )
            stuck.append(job_id)
            _stuck_seen.add(job_id)

    for job_id, exec_id in dispatching:
        if not exec_id:
            recovered = find_execution_for_job(job_id)
            if recovered:
                _record_execution_id(job_id, recovered)
                logging.warning(
                    f"REAPER: recovered live execution_id={recovered} for job_id={job_id}; not failing"
                )
                continue
        stuck.append(job_id)

    now_ts = datetime.now(timezone.utc).timestamp()
    for job_id in stuck:
        logging.warning(f"REAPER: reconciling stuck job_id={job_id}")
        exec_data = _fetch_execution_outcome(job_id)   # ACA execution status + optional preflight detail
        if exec_data is not None:
            # Classify (GPU-preflight exit 42/43/44, startup stall, app failure, operator stop,
            # or a genuinely-complete/pending job) and fail+refund EXACTLY ONCE with the class
            # recorded — so an infra failure is never mislabelled as a customer/model failure,
            # and a job whose container actually delivered (exit 0) is not spuriously refunded.
            decision = _reconcile_execution_outcome(job_id, exec_data, now_ts)
            logging.warning(f"REAPER: job_id={job_id} class={decision['failure_class']} "
                            f"infra={decision['is_infra']} action={decision['action']}")
        else:
            # Safety net (dev's original fail+refund behaviour + a class): stuck past the timeout
            # with no readable outcome -> fail+refund once, labelled an unclassified infra timeout.
            _stamp_failure_class(job_id, "infra_timeout",
                                 "reaper timeout; execution outcome unavailable", None)
            _mark_failed(job_id)


# ── Outbox dispatcher ─────────────────────────────────────────────────────────
# The retrying half of the transactional outbox (finding #4). submit_job, start_training,
# and training-completion each write a queue message into the outbox ATOMICALLY with their
# state change, then fast-path the send. Anything the fast-path could NOT deliver (queue
# briefly down, or the process died before/after the send) is picked up here and sent, then
# marked delivered — so a committed job/training can never be left with no queue message.
# At-least-once delivery, which the queue consumers already tolerate (dispatch guards on
# external_execution_id + status). Runs every minute; on the fixed-tier DB the scan (a
# filtered index over only the undelivered rows) is negligible.
@app.timer_trigger(schedule="0 */1 * * * *", arg_name="timer", run_on_startup=False)
def outbox_dispatcher(timer: func.TimerRequest):
    from shared.outbox import outbox_dispatch_pending
    outbox_dispatch_pending()


# ── Training poison handler ───────────────────────────────────────────────────
@app.queue_trigger(arg_name="msg", queue_name="lora-training-jobs-poison",
                   connection="AzureWebJobsStorage")
def handle_poison_training(msg: func.QueueMessage):
    try:
        payload = json.loads(msg.get_body().decode("utf-8"))
        training_id = payload.get("training_id")
    except Exception:
        logging.error(f"POISON(train): unparseable message dropped: {msg.get_body()!r}")
        return

    logging.error(
        f"POISON(train): training_id={training_id} exceeded retry limit "
        f"(dequeue_count={msg.dequeue_count}); marking failed"
    )
    if training_id:
        # Fails the run AND releases anything parked behind it (failing + refunding
        # those jobs), so a poisoned training message can't strand a paying user.
        _fail_training(training_id, "training message exceeded retry limit")


# ── Training watcher ──────────────────────────────────────────────────────────
# Runs every MINUTE, not every 10, and this interval is the whole point.
#
# The adapter is uploaded by a throwaway GPU container that holds no SQL credentials,
# so SOMETHING has to notice it finished and flip lora_status. If that something ran on
# the 10-minute reaper cadence, a user could sit staring at a finished LoRA for up to 10
# minutes before their photos even started. Polling the ACA executions API once a minute
# for the handful of in-flight runs is cheap (one SELECT + one list call, and only while
# a training is actually running), and it collapses that dead time to ~60s.
#
# _finish_training does the rest: flips lora_status and enqueues every parked job with
# ZERO visibility delay. Nothing waits on a backoff timer.
@app.timer_trigger(schedule="0 */1 * * * *", arg_name="timer", run_on_startup=False)
def training_watcher(timer: func.TimerRequest):
    from shared.training_trigger import get_execution_status

    conn = new_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT training_id, user_id, external_execution_id,
                   DATEDIFF(MINUTE, created_at, GETUTCDATE()) AS age_min
            FROM lora_trainings
            WHERE status IN ('dispatching', 'training')
        """)
        inflight = [(str(r[0]), str(r[1]), r[2], int(r[3] or 0)) for r in cur.fetchall()]
    finally:
        conn.close()

    if not inflight:
        return

    for training_id, user_id, execution_id, age_min in inflight:
        # Hard timeout: a run past the ceiling is failed so the jobs parked behind it are
        # released (failed + refunded) instead of waiting forever on a dead container.
        if age_min > TRAINING_STUCK_MINUTES:
            logging.warning(
                f"TRAINING_TIMEOUT: training_id={training_id} age={age_min}min "
                f"exceeds {TRAINING_STUCK_MINUTES}min; failing"
            )
            _finish_training(training_id, user_id, ok=False,
                             error=f"training exceeded {TRAINING_STUCK_MINUTES} minutes")
            continue

        if not execution_id:
            continue  # still dispatching; nothing to poll yet

        try:
            status = get_execution_status(execution_id)
        except Exception as e:
            logging.warning(f"watcher: could not read execution {execution_id}: {e}")
            continue

        if status == "succeeded":
            # The trainer's own FORMAT GATE already refused to upload a broken adapter
            # (it reloads base SDXL and checks the adapter actually activates), so a
            # 'succeeded' execution means a usable adapter is in blob storage.
            _finish_training(training_id, user_id, ok=True)
        elif status in ("failed", "degraded", "cancelled", "stopped"):
            _finish_training(training_id, user_id, ok=False,
                             error=f"training execution {status}")


# ── Subscriptions: Plans ──────────────────────────────────
@app.route(route="subscriptions/plans", methods=["GET"])
def list_plans(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps({
            # Field names + units MUST match the frontend (Billing.tsx reads
            # discounted_cents/original_cents for one-time and price_cents for monthly,
            # then divides by 100). Emitting *_price_usd here made every price render as
            # $NaN. Send integer CENTS under the exact keys the frontend consumes.
            "one_time": [
                {
                    "plan": plan,
                    "images": info["images"],
                    "credits": info["credits"],
                    "original_cents": info["original_cents"],
                    "discounted_cents": info["discounted_cents"],
                }
                for plan, info in ONE_TIME_PLANS.items()
            ],
            "monthly": [
                {
                    "plan": plan,
                    "images": info["images"],
                    "credits": info["credits"],
                    "price_cents": info["price_cents"],
                }
                for plan, info in MONTHLY_PLANS.items()
            ],
        }),
        mimetype="application/json",
        status_code=200,
    )


# ── Subscriptions: Status ─────────────────────────────────
def _add_one_month(dt):
    """Add one calendar month, clamping the day for shorter months (Jan 31 -> Feb 28/29).
    Matches Stripe's monthly billing cadence far better than a fixed +30 days."""
    import calendar
    m = dt.month + 1
    y = dt.year + (m - 1) // 12
    m = (m - 1) % 12 + 1
    return dt.replace(year=y, month=m, day=min(dt.day, calendar.monthrange(y, m)[1]))


@app.route(route="subscriptions/status", methods=["GET"])
def subscription_status(req: func.HttpRequest) -> func.HttpResponse:
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        user_id = get_user_id(token)
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT subscription_plan, subscription_type, credits_remaining, "
        "credits_monthly_limit, subscription_renewed_at, payment_failed_at, "
        "subscription_cancel_at, one_time_credits_remaining, "
        "monthly_credits_remaining FROM users WHERE user_id = ?",
        user_id,
    )
    row = cursor.fetchone()
    if not row:
        return func.HttpResponse("User not found", status_code=404)

    # Queued (pay-at-activation) plan purchase, if any (finding #6): the frontend shows it and
    # lets the user complete checkout once their current product ends. null when nothing queued.
    cursor.execute(
        "SELECT purchase_type, plan_key FROM pending_purchases "
        "WHERE user_id = ? AND status = 'pending'",
        user_id)
    prow = cursor.fetchone()
    queued_purchase = {"type": prow[0], "plan": prow[1]} if prow else None

    # Compute the NEXT renewal so the frontend can show "renews {date}". Stripe bills on a
    # MONTHLY cadence (same day-of-month each cycle), so add one calendar month — NOT 30 days,
    # which drifts ~5 days/year off the real billing date. Only meaningful for active monthly.
    next_renewal = None
    if row[1] == "monthly" and row[4]:
        next_renewal = str(_add_one_month(row[4]))

    return func.HttpResponse(
        json.dumps({
            # Frontend (Billing.tsx / getSubscriptionStatus) reads subscription_plan /
            # subscription_type. Emitting "plan"/"type" made the badge always show "Free"
            # even after a paid plan updated the row. Send the keys the frontend consumes;
            # keep plan/type as aliases so any other caller keeps working.
            "subscription_plan": row[0],
            "subscription_type": row[1],
            "plan": row[0],
            "type": row[1],
            "credits_remaining": row[2] if row[1] == "monthly" else row[7],
            "one_time_credits_remaining": row[7],
            "add_on_credits_remaining": row[7],
            "monthly_credits_remaining": row[8] if row[1] == "monthly" else 0,
            "credits_monthly_limit": row[3] if row[1] == "monthly" else None,
            # ALIAS: Billing.tsx / Dashboard.tsx read `monthly_quota` — a key the backend never
            # sent, so "X of Y credits" rendered with a BLANK Y. Emit both names so the existing
            # UI works with no coordinated frontend release.
            "monthly_quota": row[3] if row[1] == "monthly" else None,
            "subscription_renewed_at": _utc_iso(row[4]) if row[1] == "monthly" else None,
            "next_renewal": next_renewal,
            # ALIAS: Dashboard.tsx / Onboarding.tsx read `renewal_date` — same drift, which is
            # why "renews on {date}" never appeared.
            "renewal_date": next_renewal,
            # Dunning: non-null means the latest monthly renewal charge FAILED and Stripe is
            # retrying — the UI should prompt "update your card". Cleared on the next success.
            "payment_failed": bool(row[5]),
            "payment_failed_at": _utc_iso(row[5]),
            # Cancellation: non-null means a period-end cancel is scheduled — the UI shows
            # "cancels on {cancel_at}" and offers Reactivate (POST /subscriptions/reactivate).
            "cancel_pending": bool(row[6]),
            "cancel_at": _utc_iso(row[6]),
            # Queued (pay-at-activation) plan purchase, if any — complete it when the current
            # product ends. null when nothing is queued.
            "queued_purchase": queued_purchase,
        }),
        mimetype="application/json",
        status_code=200,
    )


@app.route(route="subscriptions/portal", methods=["POST"])
def create_subscription_portal(req: func.HttpRequest) -> func.HttpResponse:
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        user_id = get_user_id(token)
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)

    try:
        body = req.get_json()
    except ValueError:
        body = {}
    return_url = (
        body.get("return_url")
        or os.environ.get("FRONTEND_URL", "https://bettersnap.ai") + "/billing"
    )
    mode = body.get("mode", "payment_method_update")
    if mode not in ("payment_method_update", "manage"):
        return func.HttpResponse(
            json.dumps({"error": "Invalid billing portal mode"}),
            mimetype="application/json",
            status_code=400,
        )

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT stripe_customer_id FROM users WHERE user_id = ?",
        user_id,
    )
    row = cursor.fetchone()
    if not row or not row[0]:
        return func.HttpResponse(
            json.dumps({"error": "No Stripe customer found for this account"}),
            mimetype="application/json",
            status_code=409,
        )

    try:
        session = create_billing_portal(row[0], return_url, mode)
    except Exception as e:
        logging.error(f"Stripe billing portal failed for user {user_id}: {e}")
        return func.HttpResponse(
            json.dumps({"error": "Payment provider error"}),
            mimetype="application/json",
            status_code=502,
        )

    return func.HttpResponse(
        json.dumps({"portal_url": session["url"]}),
        mimetype="application/json",
        status_code=200,
    )


def _queue_pending_purchase(user_id: str, purchase_type: str, plan: str):
    """Record a queued plan purchase (finding #6, pay-at-activation): the user completes
    checkout when their current product ends. One PENDING row per user — a new queue
    supersedes the prior one."""
    conn = new_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM pending_purchases WHERE user_id = ? AND status = 'pending'", user_id)
        cur.execute(
            # plan_key, not "plan" — `plan` is a reserved keyword in SQL Server.
            "INSERT INTO pending_purchases (user_id, purchase_type, plan_key) VALUES (?, ?, ?)",
            user_id, purchase_type, plan)
        conn.commit()
    finally:
        conn.close()


def _clear_pending_purchase(cur, user_id: str):
    """Mark a user's queued purchase 'done' once they actually complete a purchase. Uses the
    CALLER'S cursor so it commits with the grant/activation. Safe no-op if none is pending."""
    cur.execute(
        "UPDATE pending_purchases SET status = 'done' WHERE user_id = ? AND status = 'pending'",
        user_id)


def _reserve_monthly_checkout(user_id: str) -> tuple[str | None, str | None]:
    """Atomically allow only one live monthly Checkout Session per account."""
    checkout_token = uuid.uuid4().hex
    conn = new_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE users WITH (UPDLOCK, ROWLOCK) SET
                stripe_checkout_token      = ?,
                stripe_checkout_expires_at = DATEADD(MINUTE, 32, GETUTCDATE())
            WHERE user_id = ?
              AND NOT (subscription_type = 'monthly' AND stripe_subscription_id IS NOT NULL)
              AND (stripe_checkout_token IS NULL
                   OR stripe_checkout_expires_at IS NULL
                   OR stripe_checkout_expires_at <= GETUTCDATE())""",
            checkout_token, user_id,
        )
        if cur.rowcount == 1:
            conn.commit()
            return checkout_token, None

        cur.execute(
            "SELECT subscription_type, stripe_subscription_id, stripe_checkout_expires_at "
            "FROM users WITH (UPDLOCK, ROWLOCK) WHERE user_id = ?",
            user_id,
        )
        row = cur.fetchone()
        conn.rollback()
        if not row:
            return None, "user_not_found"
        if row[0] == "monthly" and row[1]:
            return None, "monthly_active"
        return None, "checkout_in_progress"
    finally:
        conn.close()


def _release_monthly_checkout(user_id: str, checkout_token: str):
    conn = new_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE users SET
                stripe_checkout_token = NULL,
                stripe_checkout_expires_at = NULL
            WHERE user_id = ? AND stripe_checkout_token = ?""",
            user_id, checkout_token,
        )
        conn.commit()
    finally:
        conn.close()


def _recover_stale_monthly_checkout(user_id: str) -> tuple[bool, str | None]:
    """Clear an abandoned reservation, or identify a paid subscription still activating."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT stripe_checkout_token FROM users WHERE user_id = ?",
        user_id,
    )
    row = cur.fetchone()
    checkout_token = row[0] if row else None
    if not checkout_token:
        return False, None

    try:
        session = find_checkout_session_by_token(checkout_token)
    except Exception as e:
        logging.warning(f"Could not inspect Stripe checkout token for user {user_id}: {e}")
        return False, None
    if not session:
        return False, None

    if session.get("status") == "expired":
        _release_monthly_checkout(user_id, checkout_token)
        return True, None

    if session.get("status") != "complete":
        return False, None

    subscription_id = session.get("subscription")
    if not subscription_id:
        return False, None
    try:
        subscription = get_subscription(subscription_id)
    except Exception as e:
        logging.warning(f"Could not inspect Stripe subscription {subscription_id}: {e}")
        return False, None

    if subscription.get("status") in ("active", "trialing", "past_due"):
        return False, "monthly_active"
    if subscription.get("status") in ("canceled", "unpaid", "incomplete_expired"):
        _release_monthly_checkout(user_id, checkout_token)
        return True, None
    return False, None


# ── Subscriptions: Create Checkout ────────────────────────
@app.route(route="subscriptions/create", methods=["POST"])
def create_subscription(req: func.HttpRequest) -> func.HttpResponse:
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        payload = validate_token(token)
        user_id = payload["oid"]
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)

    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON", status_code=400)

    plan          = body.get("plan", "")
    payment_type  = body.get("type", "")  # "one_time" or "monthly"
    success_url   = body.get("success_url", "https://bettersnap.ai/subscription/success")
    cancel_url    = body.get("cancel_url",  "https://bettersnap.ai/subscription/cancel")
    # Entra tokens don't always carry an `email` claim — the address is often in
    # preferred_username or upn. Fall back through them; if none is an address, we pass
    # nothing and Stripe collects the email on its hosted page (see _maybe_email).
    email         = (payload.get("email") or payload.get("preferred_username")
                     or payload.get("upn") or "")

    # ── Purchase gate (finding #6): ONE active product at a time ─────────────────
    # Stops the state corruption where a one-time purchase clobbers an active monthly sub
    # (leaving it uncancellable) or a second monthly orphans the first. Reject a PLAN purchase
    # that collides with the user's current product, with a clear next step. Adding credits to
    # an active monthly is a SEPARATE endpoint (not gated here). NOTE: a later step turns the
    # 'monthly_active' reject into a QUEUE (activate the plan when the current one ends).
    gconn = get_db()
    gcur = gconn.cursor()
    gcur.execute(
        "SELECT subscription_type, stripe_subscription_id FROM users WHERE user_id = ?",
        user_id,
    )
    grow = gcur.fetchone()
    active_monthly = bool(grow and grow[0] == "monthly" and grow[1])
    gcur.execute(
        "SELECT COUNT(*) FROM jobs WHERE user_id = ? "
        "AND status IN ('queued', 'waiting_lora', 'dispatching', 'processing')",
        user_id,
    )
    generation_in_flight = int(gcur.fetchone()[0]) > 0

    # Validate the requested plan/type up front — needed before we can QUEUE or checkout.
    valid_combo = ((payment_type == "one_time" and plan in ONE_TIME_PLANS) or
                   (payment_type == "monthly" and plan in MONTHLY_PLANS))
    if not valid_combo:
        return func.HttpResponse(
            json.dumps({"error": "type must be 'one_time' or 'monthly', and plan one of "
                                 "basic, pro, expert."}),
            mimetype="application/json", status_code=400)

    if active_monthly or generation_in_flight:
        # QUEUE it (pay-at-activation): record the intent now; the user completes checkout when
        # their current product ends and they're idle again — we do NOT charge at queue time.
        # One pending row per user (a new queue supersedes the prior one).
        _queue_pending_purchase(user_id, payment_type, plan)
        if active_monthly:
            state, msg = "monthly_active", (
                "You already have an active monthly plan — we've queued this to start when your "
                "plan ends. Meanwhile, use 'Add credits' for more images now.")
        else:
            state, msg = "generation_in_flight", (
                "Your current generation is still running — we've queued this to start when it "
                "finishes.")
        return func.HttpResponse(
            json.dumps({"status": "queued", "billing_state": state,
                        "queued": {"type": payment_type, "plan": plan}, "message": msg}),
            mimetype="application/json", status_code=202)

    if payment_type == "one_time":
        if plan not in ONE_TIME_PLANS:
            return func.HttpResponse(
                json.dumps({"error": "Invalid plan. Choose: basic, pro, expert"}),
                mimetype="application/json", status_code=400,
            )
        try:
            session = create_onetime_checkout(user_id, email, plan, success_url, cancel_url)
        except Exception as e:
            logging.error(f"Stripe one-time checkout failed: {e}")
            return func.HttpResponse(
                json.dumps({"error": "Payment provider error"}),
                mimetype="application/json", status_code=502,
            )

    elif payment_type == "monthly":
        if plan not in MONTHLY_PLANS:
            return func.HttpResponse(
                json.dumps({"error": "Invalid plan. Choose: basic, pro, expert"}),
                mimetype="application/json", status_code=400,
            )
        checkout_token, reservation_error = _reserve_monthly_checkout(user_id)
        if not checkout_token and reservation_error == "checkout_in_progress":
            recovered, recovered_state = _recover_stale_monthly_checkout(user_id)
            if recovered_state:
                reservation_error = recovered_state
            elif recovered:
                checkout_token, reservation_error = _reserve_monthly_checkout(user_id)
        if not checkout_token:
            messages = {
                "monthly_active": "You already have an active monthly subscription.",
                "checkout_in_progress": "A monthly checkout is already in progress. Complete it or try again shortly.",
                "user_not_found": "User not found.",
            }
            return func.HttpResponse(
                json.dumps({
                    "error": messages[reservation_error],
                    "billing_state": reservation_error,
                }),
                mimetype="application/json",
                status_code=404 if reservation_error == "user_not_found" else 409,
            )
        expires_at = int((datetime.now(timezone.utc) + timedelta(minutes=31)).timestamp())
        try:
            session = create_monthly_checkout(
                user_id, email, plan, success_url, cancel_url,
                checkout_token, expires_at,
            )
        except Exception as e:
            try:
                _release_monthly_checkout(user_id, checkout_token)
            except Exception:
                logging.exception(
                    f"Failed to release monthly checkout reservation for user {user_id}")
            logging.error(f"Stripe monthly checkout failed: {e}")
            return func.HttpResponse(
                json.dumps({"error": "Payment provider error"}),
                mimetype="application/json", status_code=502,
            )
    else:
        return func.HttpResponse(
            json.dumps({"error": "type must be 'one_time' or 'monthly'"}),
            mimetype="application/json", status_code=400,
        )

    return func.HttpResponse(
        json.dumps({"checkout_url": session["url"], "session_id": session["id"]}),
        mimetype="application/json",
        status_code=200,
    )


# ── Subscriptions: Upgrade active monthly plan ─────────────
@app.route(route="subscriptions/upgrade", methods=["POST"])
def upgrade_user_subscription(req: func.HttpRequest) -> func.HttpResponse:
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        user_id = get_user_id(token)
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)

    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON", status_code=400)
    target_plan = body.get("plan", "")
    if target_plan not in MONTHLY_PLANS:
        return func.HttpResponse(
            json.dumps({"error": "Invalid monthly plan. Choose: basic, pro, expert"}),
            mimetype="application/json",
            status_code=400,
        )

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT subscription_plan, subscription_type, stripe_subscription_id, "
        "subscription_cancel_at FROM users WHERE user_id = ?",
        user_id,
    )
    row = cur.fetchone()
    if not row or row[1] != "monthly" or not row[2]:
        return func.HttpResponse(
            json.dumps({"error": "No active monthly subscription to upgrade"}),
            mimetype="application/json",
            status_code=409,
        )

    current_plan = str(row[0] or "").removeprefix("monthly_")
    plan_order = tuple(MONTHLY_PLANS)
    if current_plan not in plan_order:
        return func.HttpResponse(
            json.dumps({"error": "Current monthly plan could not be identified"}),
            mimetype="application/json",
            status_code=409,
        )
    if plan_order.index(target_plan) <= plan_order.index(current_plan):
        return func.HttpResponse(
            json.dumps({
                "error": "Choose a higher monthly plan to upgrade",
                "billing_state": "not_an_upgrade",
            }),
            mimetype="application/json",
            status_code=409,
        )
    if row[3]:
        return func.HttpResponse(
            json.dumps({
                "error": "Resume the subscription before upgrading it",
                "billing_state": "cancel_pending",
            }),
            mimetype="application/json",
            status_code=409,
        )

    stripe_subscription_id = row[2]
    try:
        subscription = get_subscription(stripe_subscription_id)
        items = subscription.get("items", {}).get("data", [])
        subscription_item_id = items[0].get("id") if items else None
        if not subscription_item_id:
            raise ValueError("Stripe subscription has no subscription item")
        result = upgrade_subscription(
            stripe_subscription_id,
            subscription_item_id,
            target_plan,
        )
    except Exception as e:
        logging.error(
            f"Stripe monthly upgrade failed: user={user_id} "
            f"subscription={stripe_subscription_id} target={target_plan}: {e}"
        )
        return func.HttpResponse(
            json.dumps({"error": "Payment provider error while upgrading the plan"}),
            mimetype="application/json",
            status_code=502,
        )

    if result.get("pending_update"):
        return func.HttpResponse(
            json.dumps({
                "error": "The prorated upgrade payment needs attention. Update your payment method and try again.",
                "billing_state": "payment_required",
            }),
            mimetype="application/json",
            status_code=409,
        )

    return func.HttpResponse(
        json.dumps({
            "message": "Plan upgraded; Stripe charged the prorated difference",
            "plan": target_plan,
            "billing_state": "upgrade_processing",
        }),
        mimetype="application/json",
        status_code=200,
    )


# ── Subscriptions: Add credits (top-up for an active monthly plan) ──
# The counterpart to the create_subscription gate (finding #6): while a monthly plan is
# active, the user does NOT buy another plan — they add credits, generated from their
# EXISTING model (no retrain). Only an active monthly account may top up.
@app.route(route="subscriptions/credits/topup", methods=["POST"])
def topup_credits(req: func.HttpRequest) -> func.HttpResponse:
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        payload = validate_token(token)
        user_id = payload["oid"]
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)

    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON", status_code=400)

    pack        = body.get("pack") or body.get("plan", "")
    success_url = body.get("success_url", "https://bettersnap.ai/subscription/success")
    cancel_url  = body.get("cancel_url",  "https://bettersnap.ai/subscription/cancel")
    email       = (payload.get("email") or payload.get("preferred_username")
                   or payload.get("upn") or "")

    if pack not in ONE_TIME_PLANS:
        return func.HttpResponse(
            json.dumps({"error": "Invalid credit pack. Choose: basic, pro, expert"}),
            mimetype="application/json", status_code=400)

    # Gate: top-ups are ONLY for an active monthly plan. A non-subscriber buys a plan, not
    # loose credits — otherwise the credit-rate ambiguity #6 warned about creeps back in.
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT subscription_type, stripe_subscription_id FROM users WHERE user_id = ?",
        user_id)
    row = cur.fetchone()
    if not (row and row[0] == "monthly" and row[1]):
        return func.HttpResponse(
            json.dumps({"error": "Credit top-ups are only available on an active monthly plan.",
                        "billing_state": "no_active_monthly"}),
            mimetype="application/json", status_code=409)

    try:
        session = create_topup_checkout(user_id, email, pack, success_url, cancel_url)
    except Exception as e:
        logging.error(f"Stripe top-up checkout failed: {e}")
        return func.HttpResponse(
            json.dumps({"error": "Payment provider error"}),
            mimetype="application/json", status_code=502)

    return func.HttpResponse(
        json.dumps({"checkout_url": session["url"], "session_id": session["id"]}),
        mimetype="application/json", status_code=200)


# ── Subscriptions: Cancel ─────────────────────────────────
@app.route(route="subscriptions/cancel", methods=["POST"])
def cancel_user_subscription(req: func.HttpRequest) -> func.HttpResponse:
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        user_id = get_user_id(token)
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT stripe_subscription_id, subscription_type, subscription_renewed_at "
        "FROM users WHERE user_id = ?",
        user_id,
    )
    row = cursor.fetchone()
    if not row:
        return func.HttpResponse("User not found", status_code=404)

    stripe_sub_id, sub_type, renewed_at = row[0], row[1], row[2]
    if sub_type != "monthly" or not stripe_sub_id:
        return func.HttpResponse(
            json.dumps({"error": "No active monthly subscription to cancel"}),
            mimetype="application/json", status_code=400,
        )

    try:
        result = cancel_subscription(stripe_sub_id)
    except Exception as e:
        logging.error(f"Stripe cancel failed for user {user_id}: {e}")
        return func.HttpResponse(
            json.dumps({"error": "Payment provider error"}),
            mimetype="application/json", status_code=502,
        )

    # Record the scheduled cancellation (exact date from Stripe's current_period_end) so the
    # UI can show "cancels on {date}" while the plan is still active, and so it can be undone.
    cancel_at = None
    ts = subscription_period_end(result)
    if not ts:
        try:
            ts = subscription_period_end(get_subscription(stripe_sub_id))
        except Exception as e:
            logging.warning(
                f"Stripe subscription refresh failed for user {user_id}: {e}"
            )
    if ts:
        cancel_at = datetime.fromtimestamp(int(ts), tz=timezone.utc).replace(tzinfo=None)
    elif renewed_at:
        cancel_at = _add_one_month(renewed_at)
        logging.warning(
            f"Stripe omitted period end for subscription {stripe_sub_id}; "
            f"using stored renewal date {cancel_at}."
        )

    if not cancel_at:
        return func.HttpResponse(
            json.dumps({
                "error": "Cancellation was scheduled, but its effective date could not be determined."
            }),
            mimetype="application/json",
            status_code=502,
        )

    cursor.execute(
        "UPDATE users SET subscription_cancel_at = ? WHERE user_id = ?",
        cancel_at, user_id,
    )

    return func.HttpResponse(
        json.dumps({
            "message": "Subscription will cancel at end of billing period",
            "cancel_at": _utc_iso(cancel_at),
        }),
        mimetype="application/json",
        status_code=200,
    )


# ── Subscriptions: Reactivate (undo a pending cancellation) ──
@app.route(route="subscriptions/reactivate", methods=["POST"])
def reactivate_user_subscription(req: func.HttpRequest) -> func.HttpResponse:
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        user_id = get_user_id(token)
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT stripe_subscription_id, subscription_type, subscription_cancel_at "
        "FROM users WHERE user_id = ?",
        user_id,
    )
    row = cursor.fetchone()
    if not row:
        return func.HttpResponse("User not found", status_code=404)

    stripe_sub_id, sub_type, cancel_at = row[0], row[1], row[2]
    if sub_type != "monthly" or not stripe_sub_id:
        return func.HttpResponse(
            json.dumps({"error": "No monthly subscription to reactivate"}),
            mimetype="application/json", status_code=400,
        )
    if not cancel_at:
        return func.HttpResponse(
            json.dumps({"error": "Subscription is not scheduled to cancel"}),
            mimetype="application/json", status_code=400,
        )

    try:
        reactivate_subscription(stripe_sub_id)
    except Exception as e:
        logging.error(f"Stripe reactivate failed for user {user_id}: {e}")
        return func.HttpResponse(
            json.dumps({"error": "Payment provider error"}),
            mimetype="application/json", status_code=502,
        )

    cursor.execute(
        "UPDATE users SET subscription_cancel_at = NULL WHERE user_id = ?", user_id)
    return func.HttpResponse(
        json.dumps({"message": "Subscription reactivated — it will keep renewing"}),
        mimetype="application/json",
        status_code=200,
    )


# ── Stripe Webhook ────────────────────────────────────────
class RetryableStripeWebhookError(Exception):
    """A valid paid event whose entitlement could not be applied yet."""


@app.route(route="webhooks/stripe", methods=["POST"])
def stripe_webhook(req: func.HttpRequest) -> func.HttpResponse:
    sig_header = req.headers.get("Stripe-Signature", "")
    if not sig_header:
        return func.HttpResponse("Missing Stripe-Signature", status_code=400)

    try:
        event = verify_webhook(req.get_body(), sig_header)
    except ValueError as e:
        logging.warning(f"Stripe webhook invalid: {e}")
        return func.HttpResponse("Invalid signature", status_code=400)

    event_type = event.get("type", "")
    event_id   = event.get("id", "")
    logging.info(f"Stripe event: {event_type} ({event_id})")
    if not event_id:
        # No id means we cannot dedup this event; refuse rather than risk a double-grant.
        logging.error(f"Stripe event has no id; skipping to stay idempotent: type={event_type}")
        return func.HttpResponse("OK", status_code=200)

    try:
        if event_type in (
            "checkout.session.completed",
            "checkout.session.async_payment_succeeded",
        ):
            _fulfill_paid_checkout(event["data"]["object"], event_id)

        elif event_type == "checkout.session.async_payment_failed":
            session = event["data"]["object"]
            metadata = session.get("metadata", {})
            if metadata.get("payment_type") == "monthly":
                user_id = metadata.get("user_id")
                checkout_token = metadata.get("checkout_token")
                if user_id and checkout_token:
                    _release_monthly_checkout(user_id, checkout_token)
            logging.warning(
                f"Stripe Checkout payment failed asynchronously: session={session.get('id')}"
            )

        elif event_type == "checkout.session.expired":
            # Release a monthly-checkout reservation whose Stripe session expired unpaid.
            session = event["data"]["object"]
            metadata = session.get("metadata", {})
            user_id = metadata.get("user_id")
            checkout_token = metadata.get("checkout_token")
            if user_id and checkout_token:
                _release_monthly_checkout(user_id, checkout_token)

        # invoice.paid is the single source of truth for renewal grants. Stripe may ALSO emit
        # invoice.payment_succeeded for the same invoice with a DIFFERENT event id — handling
        # both would bypass event-id dedup and double-grant, so payment_succeeded is ignored.
        elif event_type == "invoice.paid":
            _handle_invoice_paid(event["data"]["object"], event_id)

        elif event_type == "invoice.payment_succeeded":
            logging.info(
                f"Stripe event {event_id} ignored; invoice.paid owns renewal credit grants.")

        elif event_type == "invoice.payment_failed":
            _handle_payment_failed(event["data"]["object"], event_id)

        elif event_type == "customer.subscription.deleted":
            _handle_subscription_ended(event["data"]["object"], event_id)

        elif event_type == "customer.subscription.updated":
            _handle_subscription_updated(event["data"]["object"], event_id)

    except RetryableStripeWebhookError as e:
        # A 2xx tells Stripe the event is permanently handled; a 500 makes Stripe replay the
        # still-unclaimed event after registration/reconciliation creates its target (dev's
        # paid-but-not-granted retry).
        logging.error(f"Retryable Stripe webhook failure: event={event_id}: {e}")
        return func.HttpResponse("Entitlement not applied; retry", status_code=500)

    return func.HttpResponse("OK", status_code=200)


def _fulfill_paid_checkout(session: dict, event_id: str):
    """Grant a Checkout purchase only after Stripe confirms that it is paid."""
    payment_status = session.get("payment_status")
    if payment_status != "paid":
        logging.info(
            f"Stripe Checkout not fulfilled: session={session.get('id')} "
            f"payment_status={payment_status} event={event_id}"
        )
        return

    session_id = session.get("id")
    if not session_id:
        logging.error(
            f"Paid Stripe Checkout has no session id; refusing fulfillment: event={event_id}"
        )
        return

    claim_id = f"checkout_session:{session_id}"
    payment_type = session.get("metadata", {}).get("payment_type")
    if payment_type == "one_time":
        _handle_onetime_payment(session, claim_id)
    elif payment_type == "monthly":
        _handle_monthly_checkout(session, claim_id)
    elif payment_type == "topup":
        _handle_topup(session, claim_id)
    elif payment_type == "org_seats":
        _handle_org_payment(session, claim_id)


def _claim_event(cur, event_id: str) -> bool:
    """Idempotency guard for Stripe mutations. Checkout grants use a session-scoped key so
    completed and async-payment events cannot both grant; other handlers use the event id.
    The claim commits in the same transaction as the mutation, and therefore rolls back with
    it when Stripe needs to retry. The migration 010 primary key is the concurrency backstop."""
    cur.execute(
        "INSERT INTO processed_stripe_events (event_id) "
        "SELECT ? WHERE NOT EXISTS (SELECT 1 FROM processed_stripe_events WHERE event_id = ?)",
        event_id, event_id,
    )
    return cur.rowcount == 1


def _handle_onetime_payment(session: dict, event_id: str):
    user_id = session.get("metadata", {}).get("user_id")
    plan    = session.get("metadata", {}).get("plan")
    if not user_id or plan not in ONE_TIME_PLANS:
        logging.error(f"one_time payment missing metadata: {session}")
        return

    credits = ONE_TIME_PLANS[plan]["credits"]
    conn = new_connection()
    try:
        cur = conn.cursor()
        # Idempotency: a retried delivery of this event must not add the credits again.
        if not _claim_event(cur, event_id):
            conn.rollback()
            logging.info(f"Stripe event {event_id} already processed; skipping one_time grant.")
            return
        cur.execute(
            """UPDATE users SET
                subscription_plan = ?,
                subscription_type = 'one_time',
                plan_name         = ?,
                credits_remaining = one_time_credits_remaining + ?,
                one_time_credits_remaining = one_time_credits_remaining + ?,
                monthly_credits_remaining = 0,
                credits_monthly_limit = NULL,
                subscription_renewed_at = NULL,
                subscription_cancel_at = NULL,
                payment_failed_at = NULL,
                one_time_plan     = ?,
                one_time_plan_name = ?
            WHERE user_id = ?
              AND NOT (subscription_type = 'monthly' AND stripe_subscription_id IS NOT NULL)""",
            plan, plan_key_for(plan, "one_time"), credits, credits,
            plan, plan_key_for(plan, "one_time"), user_id,
        )
        applied = cur.rowcount
        if applied == 0:
            # The user row doesn't exist yet (registration hadn't completed when they paid).
            # Roll back so the event is NOT recorded as processed — a later replay can still
            # apply the grant once the row exists. Log LOUDLY for manual reconciliation.
            conn.rollback()
            logging.error(
                f"PAYMENT NOT APPLIED (one_time): user_id={user_id} plan={plan} "
                f"credits={credits} matched 0 user rows — user not registered. "
                f"MANUAL RECONCILIATION REQUIRED (grant {credits} credits once the row exists)."
            )
            raise RetryableStripeWebhookError(
                f"one_time grant matched no user: user_id={user_id} plan={plan}")
        _clear_pending_purchase(cur, user_id)  # they completed a purchase; clear any queued one
        credit_ledger.record(cur, user_id, credits, credit_ledger.REASON_PURCHASE_ONE_TIME)
        conn.commit()  # claim + grant + ledger row commit atomically
        logging.info(f"One-time purchase: user={user_id} plan={plan} +{credits} credits")
        _write_event(user_id, "payment.one_time", target=event_id,
                     detail={"plan": plan, "credits": credits})
    finally:
        conn.close()


def _handle_org_payment(session: dict, event_id: str):
    organization_id = session.get("metadata", {}).get("organization_id")
    amount_total = session.get("amount_total")
    currency = session.get("currency", "usd")
    payment_intent_id = session.get("payment_intent", "")

    if not organization_id or amount_total is None:
        logging.error(f"org_seats payment missing metadata: {session}")
        return

    conn = new_connection()
    try:
        cur = conn.cursor()
        if not _claim_event(cur, event_id):
            conn.rollback()
            logging.info(f"Stripe event {event_id} already processed; skipping org payment.")
            return
        cur.execute(
            """INSERT INTO organization_payments
                (payment_id, organization_id, stripe_payment_intent_id, stripe_event_id,
                 amount_cents, currency, status)
               VALUES (?, ?, ?, ?, ?, ?, 'succeeded')""",
            str(uuid.uuid4()), organization_id, payment_intent_id, event_id,
            amount_total, currency,
        )

        # THE ACTUAL UNLOCK: read who the admin is and what they're entitled to
        # BEFORE flipping status, so both the status change and the credit grant
        # land together in one transaction — no window where one succeeded and the
        # other didn't.
        cur.execute(
            "SELECT admin_user_id, credits_per_seat FROM organizations "
            "WHERE organization_id = ? AND status = 'pending_payment'",
            organization_id,
        )
        org_row = cur.fetchone()
        if not org_row:
            # Either the org doesn't exist, or status isn't 'pending_payment' anymore
            # (already paid — a duplicate webhook that slipped past _claim_event some
            # other way, or a genuine data problem). Don't grant credits twice.
            conn.rollback()
            logging.error(
                f"PAYMENT NOT APPLIED (org_seats): organization_id={organization_id} "
                f"amount={amount_total} — org not found or not in 'pending_payment' state. "
                f"MANUAL RECONCILIATION REQUIRED."
            )
            return
        admin_user_id, credits_per_seat = org_row

        cur.execute(
            "UPDATE organizations SET status = 'active' WHERE organization_id = ?",
            organization_id,
        )

        # Raise the admin's own membership from the 0/0 it was created with up to
        # the real grant. This is the row that already existed from create_organization
        # — locked at 0 credits — not a new insert.
        cur.execute(
            """UPDATE organization_members
               SET credits_granted = ?, credits_remaining = ?
               WHERE organization_id = ? AND user_id = ?""",
            credits_per_seat, credits_per_seat, organization_id, admin_user_id,
        )
        if cur.rowcount == 0:
            # The admin's own membership row is supposed to always exist (created
            # alongside the org). If it's missing, something upstream is broken —
            # surface it loudly rather than silently activating an org with an
            # admin who still has no usable credits.
            conn.rollback()
            logging.error(
                f"PAYMENT NOT APPLIED (org_seats): organization_id={organization_id} "
                f"admin={admin_user_id} — admin membership row missing, credits not granted. "
                f"MANUAL RECONCILIATION REQUIRED."
            )
            return

        conn.commit()
        logging.info(
            f"Org seat payment recorded and unlocked: org={organization_id} "
            f"amount={amount_total} admin_credits={credits_per_seat}"
        )
    finally:
        conn.close()

        
def _handle_monthly_checkout(session: dict, event_id: str):
    user_id  = session.get("metadata", {}).get("user_id")
    plan     = session.get("metadata", {}).get("plan")
    customer = session.get("customer")
    sub_id   = session.get("subscription")
    checkout_token = session.get("metadata", {}).get("checkout_token")

    if not all([user_id, plan, customer, sub_id]) or plan not in MONTHLY_PLANS:
        logging.error(f"monthly checkout missing fields: {session}")
        return

    credits = MONTHLY_PLANS[plan]["credits"]
    conn = new_connection()
    try:
        cur = conn.cursor()
        # Idempotency: a retried delivery must not re-activate / re-grant credits.
        if not _claim_event(cur, event_id):
            conn.rollback()
            logging.info(f"Stripe event {event_id} already processed; skipping monthly activation.")
            return
        cur.execute(
            """UPDATE users SET
                subscription_plan       = ?,
                subscription_type       = 'monthly',
                plan_name               = ?,
                stripe_customer_id      = ?,
                stripe_subscription_id  = ?,
                credits_remaining       = ? +
                    CASE WHEN subscription_type = 'monthly'
                         THEN one_time_credits_remaining ELSE credits_remaining END,
                monthly_credits_remaining = ?,
                one_time_credits_remaining =
                    CASE WHEN subscription_type = 'monthly'
                         THEN one_time_credits_remaining ELSE credits_remaining END,
                one_time_plan =
                    CASE WHEN subscription_type = 'monthly'
                         THEN one_time_plan
                         ELSE ISNULL(one_time_plan, ISNULL(subscription_plan, plan_name)) END,
                one_time_plan_name =
                    CASE WHEN subscription_type = 'monthly'
                         THEN one_time_plan_name
                         ELSE ISNULL(one_time_plan_name, plan_name) END,
                credits_monthly_limit   = ?,
                subscription_renewed_at = GETUTCDATE(),
                retention_expires_at    = NULL,
                stripe_checkout_token   = NULL,
                stripe_checkout_expires_at = NULL
            WHERE user_id = ?
              AND (stripe_subscription_id IS NULL OR stripe_subscription_id = ?)
              AND (stripe_checkout_token IS NULL OR stripe_checkout_token = ?)""",
            plan, plan_key_for(plan, "monthly"), customer, sub_id,
            credits, credits, credits,
            user_id, sub_id, checkout_token,
        )
        applied = cur.rowcount
        if applied == 0:
            # Same silent-loss guard as the one-time path: a subscription paid for a user
            # row that doesn't exist would no-op. Roll back (don't record the event) so a
            # replay can still activate once the row exists; surface for reconciliation.
            conn.rollback()
            logging.error(
                f"PAYMENT NOT APPLIED (monthly): user_id={user_id} plan={plan} "
                f"credits={credits} sub={sub_id} matched 0 user rows — user not registered. "
                f"MANUAL RECONCILIATION REQUIRED."
            )
            raise RetryableStripeWebhookError(
                f"monthly activation matched no user: user_id={user_id} plan={plan}")
        _clear_pending_purchase(cur, user_id)  # they completed a purchase; clear any queued one
        credit_ledger.record(cur, user_id, credits, credit_ledger.REASON_PURCHASE_MONTHLY)
        conn.commit()  # claim + activation + ledger row commit atomically
        logging.info(f"Monthly subscription activated: user={user_id} plan={plan} credits={credits}")
        _write_event(user_id, "payment.monthly", target=event_id,
                     detail={"plan": plan, "credits": credits})
    finally:
        conn.close()


def _handle_topup(session: dict, event_id: str):
    """Grant a persistent one-time add-on to an ACTIVE monthly account.

    Monthly credits expire at the end of the billing cycle. Add-on credits are stored in the
    separate one-time bucket, are consumed only after monthly credits, and any unused amount
    becomes the active balance when the subscription ends.
    """
    user_id = session.get("metadata", {}).get("user_id")
    pack    = session.get("metadata", {}).get("plan")
    if not user_id or pack not in ONE_TIME_PLANS:
        logging.error(f"topup missing/invalid metadata: {session}")
        return
    images = ONE_TIME_PLANS[pack]["images"]
    conn = new_connection()
    try:
        cur = conn.cursor()
        if not _claim_event(cur, event_id):
            conn.rollback()
            logging.info(f"Stripe event {event_id} already processed; skipping topup.")
            return
        cur.execute(
            "SELECT plan_name FROM users WHERE user_id = ? AND subscription_type = 'monthly'",
            user_id)
        prow = cur.fetchone()
        if not prow:
            conn.rollback()
            logging.error(
                f"PAYMENT NOT APPLIED (topup): user_id={user_id} is not an active monthly "
                f"account. MANUAL RECONCILIATION REQUIRED (grant {images} images once fixed).")
            raise RetryableStripeWebhookError(
                f"topup matched no active monthly user: user_id={user_id} pack={pack}")
        monthly_plan = get_plan(prow[0])
        add_on_credits = images * monthly_plan.credits_per_image
        cur.execute(
            """UPDATE users SET
                credits_remaining = credits_remaining + ?,
                one_time_credits_remaining = one_time_credits_remaining + ?,
                one_time_plan = ?,
                one_time_plan_name = ?
            WHERE user_id = ? AND subscription_type = 'monthly'""",
            add_on_credits, add_on_credits, pack, prow[0], user_id,
        )
        credit_ledger.record(cur, user_id, add_on_credits, credit_ledger.REASON_TOPUP)
        conn.commit()  # claim + grant + ledger row commit atomically
        logging.info(
            f"Persistent add-on: user={user_id} pack={pack} "
            f"+{add_on_credits} add-on credits ({images} images)"
        )
    finally:
        conn.close()


def _invoice_subscription_id(invoice: dict):
    """The subscription id on an invoice, across Stripe API versions.

    THE BUG THIS FIXES — SILENT RENEWAL FAILURE
    -------------------------------------------
    Stripe removed the top-level invoice.subscription field. As of the version this account
    now sends events in (2026-05-27.dahlia), the top-level invoice.subscription is None, and
    the id lives at invoice.parent.subscription_details.subscription. Both invoice handlers
    read the old field, hit `if not sub_id: return`, and no-op — so EVERY monthly renewal's
    invoice.paid (billing_reason=subscription_cycle) silently failed to reset credits, and
    every invoice.payment_failed silently failed to flag dunning. Only the FIRST month
    worked, because activation goes through checkout.session.completed, a different handler.

    Checks the legacy top-level field first, then the new nested location, then the line
    item — so it works whatever version an event arrives in.
    """
    sub = invoice.get("subscription")
    if sub:
        return sub
    parent = invoice.get("parent") or {}
    sub = (parent.get("subscription_details") or {}).get("subscription")
    if sub:
        return sub
    for line in (invoice.get("lines", {}) or {}).get("data", []):
        lp = (line.get("parent") or {}).get("subscription_item_details") or {}
        if lp.get("subscription"):
            return lp["subscription"]
    return None


def _handle_invoice_paid(invoice: dict, event_id: str):
    sub_id = _invoice_subscription_id(invoice)
    if not sub_id:
        return
    conn = new_connection()
    try:
        cur = conn.cursor()
        # Idempotency: a retried invoice.paid must not reset (and thus re-grant) credits
        # twice — that would hand back credits the subscriber had already spent this cycle.
        if not _claim_event(cur, event_id):
            conn.rollback()
            logging.info(f"Stripe event {event_id} already processed; skipping credit reset.")
            return
        if invoice.get("billing_reason") == "subscription_update":
            cur.execute(
                "UPDATE users SET payment_failed_at = NULL "
                "WHERE stripe_subscription_id = ?",
                sub_id,
            )
            conn.commit()
            logging.info(
                f"Prorated upgrade invoice paid for subscription={sub_id}; "
                "credit delta is handled by customer.subscription.updated."
            )
            return
        cur.execute(
            """UPDATE users SET
                -- Separate-balance renewal: reset the monthly allowance to the limit (monthly
                -- credits do not roll over) and recompute the combined balance; one-time and
                -- top-up credits in one_time_credits_remaining are preserved (non-expiring).
                credits_remaining         = credits_monthly_limit + one_time_credits_remaining,
                monthly_credits_remaining = credits_monthly_limit,
                subscription_renewed_at = GETUTCDATE(),
                payment_failed_at       = NULL
            WHERE stripe_subscription_id = ?""",
            sub_id,
        )
        if cur.rowcount == 0:
            # Checkout/user registration and the first invoice can race. Roll back the
            # processed-event claim and force a retry instead of losing a paid renewal.
            conn.rollback()
            logging.error(
                f"PAYMENT NOT APPLIED (invoice): subscription={sub_id} matched 0 user rows. "
                f"Returning non-2xx for Stripe retry; event_id={event_id}."
            )
            raise RetryableStripeWebhookError(
                f"invoice grant matched no subscription: subscription={sub_id}")
        # Ledger the renewal grant: the monthly allowance granted this cycle (monthly credits
        # reset to the limit rather than rolling over, so the grant is the full limit).
        cur.execute(
            "SELECT user_id, credits_monthly_limit FROM users WHERE stripe_subscription_id = ?",
            sub_id)
        urow = cur.fetchone()  # stripe_subscription_id is unique per user → at most one row
        if urow:
            credit_ledger.record(cur, urow[0], int(urow[1] or 0), credit_ledger.REASON_RENEWAL)
        conn.commit()  # claim + reset + ledger row commit atomically
        logging.info(f"Credits reset for subscription={sub_id}")
    finally:
        conn.close()


def _handle_payment_failed(invoice: dict, event_id: str):
    """A monthly renewal charge FAILED. Flag the account (payment_failed_at) so the UI can
    prompt the user to update their card. Preserve the FIRST failed-at timestamp so Stripe's
    later retries do not restart our grace period. The flag is cleared automatically by the
    next successful invoice.paid (recovery)."""
    sub_id = _invoice_subscription_id(invoice)   # see _invoice_subscription_id: the id moved
    if not sub_id:
        return
    conn = new_connection()
    try:
        cur = conn.cursor()
        if not _claim_event(cur, event_id):
            conn.rollback()
            logging.info(f"Stripe event {event_id} already processed; skipping payment-failed flag.")
            return
        cur.execute(
            "UPDATE users SET payment_failed_at = COALESCE(payment_failed_at, GETUTCDATE()) "
            "WHERE stripe_subscription_id = ?",
            sub_id,
        )
        conn.commit()
        logging.warning(f"Monthly payment FAILED for subscription={sub_id}; flagged for dunning.")
    finally:
        conn.close()


@app.timer_trigger(schedule="0 0 * * * *", arg_name="timer", run_on_startup=False)
def failed_payment_grace_cleanup(timer: func.TimerRequest) -> None:
    """Hourly: remove only monthly credits after the failed-renewal grace period.

    The subscription remains linked while Stripe is retrying. If a later invoice.paid
    arrives, its normal renewal handler clears payment_failed_at and restores the monthly
    allowance. One-time/add-on credits are never removed here.
    """
    conn = new_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE users SET
                monthly_credits_remaining = 0,
                credits_remaining = one_time_credits_remaining
            WHERE subscription_type = 'monthly'
              AND payment_failed_at IS NOT NULL
              AND payment_failed_at <= DATEADD(DAY, -?, GETUTCDATE())
              AND monthly_credits_remaining > 0""",
            FAILED_PAYMENT_GRACE_DAYS,
        )
        expired = cur.rowcount
        conn.commit()
        if expired:
            logging.warning(
                f"Removed monthly credits for {expired} account(s) after "
                f"{FAILED_PAYMENT_GRACE_DAYS}-day failed-payment grace period."
            )
    finally:
        conn.close()


def _handle_subscription_updated(sub: dict, event_id: str):
    sub_id = sub.get("id")
    status = sub.get("status")
    if status in ("canceled", "unpaid"):
        _handle_subscription_ended(sub, event_id)
        return
    if not sub_id or status not in ("active", "trialing", "past_due"):
        return

    items = sub.get("items", {}).get("data", [])
    price_id = items[0].get("price", {}).get("id") if items else None
    try:
        plan = monthly_plan_for_price_id(price_id)
    except Exception as e:
        logging.error(f"Cannot map Stripe price for subscription={sub_id}: {e}")
        return
    if plan not in MONTHLY_PLANS:
        logging.error(f"Unknown monthly Stripe price={price_id} for subscription={sub_id}")
        return

    cancel_at = None
    period_end = subscription_period_end(sub)
    if (sub.get("cancel_at_period_end") or sub.get("cancel_at")) and period_end:
        cancel_at = datetime.fromtimestamp(
            period_end, tz=timezone.utc,
        ).replace(tzinfo=None)

    conn = new_connection()
    try:
        cur = conn.cursor()
        if not _claim_event(cur, event_id):
            conn.rollback()
            logging.info(f"Stripe event {event_id} already processed; skipping plan sync.")
            return
        credits = MONTHLY_PLANS[plan]["credits"]
        cur.execute(
            "SELECT credits_monthly_limit, monthly_credits_remaining, "
            "one_time_credits_remaining FROM users WHERE stripe_subscription_id = ?",
            sub_id,
        )
        balance_row = cur.fetchone()
        if not balance_row:
            conn.rollback()
            logging.error(f"Subscription update matched no user: subscription={sub_id}")
            return
        old_limit = int(balance_row[0] or 0)
        monthly_remaining = int(balance_row[1] or 0)
        add_on_remaining = int(balance_row[2] or 0)
        if credits > old_limit:
            monthly_remaining += credits - old_limit
        elif credits < old_limit:
            monthly_remaining = min(monthly_remaining, credits)
        total_remaining = monthly_remaining + add_on_remaining
        cur.execute(
            """UPDATE users SET
                subscription_plan      = ?,
                subscription_type      = 'monthly',
                plan_name              = ?,
                credits_remaining      = ?,
                monthly_credits_remaining = ?,
                credits_monthly_limit  = ?,
                subscription_cancel_at = ?,
                retention_expires_at   = NULL
            WHERE stripe_subscription_id = ?""",
            plan, plan_key_for(plan, "monthly"), total_remaining,
            monthly_remaining, credits, cancel_at, sub_id,
        )
        if cur.rowcount == 0:
            conn.rollback()
            logging.error(f"Subscription update matched no user: subscription={sub_id}")
            return
        conn.commit()
        logging.info(f"Subscription plan synced: subscription={sub_id} plan={plan}")
    finally:
        conn.close()


def _handle_subscription_ended(sub: dict, event_id: str):
    sub_id = sub.get("id")
    status = sub.get("status")
    # Only hard-downgrade on TERMINAL states. 'past_due' is deliberately EXCLUDED: it is a
    # transient dunning state where Stripe is still retrying the card (for days). Downgrading
    # on past_due nulls stripe_subscription_id, so the later successful invoice.paid can no
    # longer find the user (it matches on stripe_subscription_id) and a paying customer whose
    # card merely blipped is orphaned to 'free' forever. Let past_due ride; it resolves to
    # 'active' (recovered) or 'canceled'/'unpaid' (truly over), both of which we handle.
    if status in ("canceled", "unpaid"):
        conn = new_connection()
        try:
            cur = conn.cursor()
            # Idempotency: a retried delivery must not re-run the downgrade / reset the
            # retention clock. (customer.subscription.updated can also fire repeatedly.)
            if not _claim_event(cur, event_id):
                conn.rollback()
                logging.info(f"Stripe event {event_id} already processed; skipping downgrade.")
                return
            # Subscription is truly over → the user keeps their data for RETENTION_DAYS
            # more, then the hourly cleanup deletes the blobs.
            cur.execute(
                """UPDATE users SET
                    -- Separate-balance downgrade: when the subscription ends, keep any remaining
                    -- one-time credits (revert to a one_time account) rather than wiping them;
                    -- fall back to free only when there are none.
                    subscription_plan      =
                        CASE WHEN one_time_credits_remaining > 0
                             THEN ISNULL(one_time_plan, ISNULL(one_time_plan_name, 'free'))
                             ELSE 'free' END,
                    subscription_type      =
                        CASE WHEN one_time_credits_remaining > 0
                             THEN 'one_time' ELSE NULL END,
                    plan_name              =
                        CASE WHEN one_time_credits_remaining > 0
                             THEN ISNULL(one_time_plan_name, 'trial') ELSE 'trial' END,
                    stripe_subscription_id = NULL,
                    credits_remaining      = one_time_credits_remaining,
                    monthly_credits_remaining = 0,
                    credits_monthly_limit  = NULL,
                    subscription_cancel_at = NULL,
                    payment_failed_at      = NULL,
                    retention_expires_at   = DATEADD(DAY, ?, GETUTCDATE())
                WHERE stripe_subscription_id = ?""",
                RETENTION_DAYS, sub_id,
            )
            conn.commit()
            logging.info(f"Subscription {sub_id} ended → free; retention starts (+{RETENTION_DAYS}d)")
        finally:
            conn.close()


# ── Data retention cleanup ────────────────────────────────
def _delete_blobs(container_name: str, prefix: str) -> int:
    """Delete every blob under `prefix` in `container_name`. Best-effort; returns count.

    CASE-SENSITIVITY — the bug this guards against: Azure blob prefixes are CASE-SENSITIVE.
    Ids come out of SQL as UPPERCASE GUIDs ("908A8F2A-..."), but the pipeline WRITES paths
    with the lowercase guid (inputs/908a8f2a-.../, lora-weights/identity/908a8f2a-.../).
    Matching only the SQL casing therefore matched NOTHING: retention_cleanup logged a
    successful run every hour while every uploaded photo and trained adapter stayed in
    storage forever — a privacy problem (data never actually purged) and an unbounded
    storage bill. Proven empirically: the uppercase prefix deleted 0 blobs, the lowercase
    prefix deleted 17 for the same user. Try both casings so it works whichever the writer used.
    """
    n = 0
    variants = [prefix] if prefix == prefix.lower() else [prefix, prefix.lower()]
    try:
        container = get_blob_client().get_container_client(container_name)
        for p in variants:
            for b in container.list_blobs(name_starts_with=p):
                container.delete_blob(b.name)
                n += 1
    except Exception as e:
        logging.warning(f"retention: blob cleanup failed for {container_name}/{prefix}: {e}")
    return n


# ── Super-Admin API (Priority 1: role-gated, read-only platform views) ────────
# Every endpoint is gated by require_admin (internal-tenant token + "Admin" app role). These are
# PLATFORM-WIDE reads (all users/jobs), NOT the caller's own data — the gap Jayasri's report calls
# out. Reads only for now; mutations (credit adjust, suspend, refund) come next and each writes an
# audit event. Fails 401 (bad/missing token or admin API not configured) or 403 (valid but not admin).
def _require_admin_or_response(req: func.HttpRequest):
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        return require_admin(token), None
    except NotAdminError as e:
        logging.info(f"admin gate: forbidden — {e}")
        return None, func.HttpResponse(
            json.dumps({"error": "forbidden", "message": "Admin role required."}),
            mimetype="application/json", status_code=403)
    except Exception as e:
        logging.info(f"admin gate: unauthorized — {e}")
        return None, func.HttpResponse("Unauthorized", status_code=401)


def _admin_page(req: func.HttpRequest):
    """Clamp pagination: limit 1..200 (default 50), offset >= 0."""
    try:
        limit = max(1, min(200, int(req.params.get("limit", "50"))))
    except (TypeError, ValueError):
        limit = 50
    try:
        offset = max(0, int(req.params.get("offset", "0")))
    except (TypeError, ValueError):
        offset = 0
    return limit, offset


@app.route(route="superadmin/dashboard/summary", methods=["GET"])
def admin_dashboard_summary(req: func.HttpRequest) -> func.HttpResponse:
    _admin, err = _require_admin_or_response(req)
    if err:
        return err
    cur = get_db().cursor()

    def scalar(sql):
        cur.execute(sql)
        r = cur.fetchone()
        return r[0] if r and r[0] is not None else 0

    avg_secs = scalar(
        "SELECT AVG(CAST(DATEDIFF(SECOND, dispatched_at, completed_at) AS FLOAT)) FROM jobs "
        "WHERE status='completed' AND completed_at IS NOT NULL AND dispatched_at IS NOT NULL "
        "AND completed_at >= DATEADD(DAY,-1,GETUTCDATE())")
    return func.HttpResponse(json.dumps({
        "users": {
            "total": scalar("SELECT COUNT(*) FROM users"),
            "new_30d": scalar("SELECT COUNT(*) FROM users WHERE created_at >= DATEADD(DAY,-30,GETUTCDATE())"),
            "active_30d": scalar("SELECT COUNT(DISTINCT user_id) FROM jobs WHERE created_at >= DATEADD(DAY,-30,GETUTCDATE())"),
            "paying": scalar("SELECT COUNT(DISTINCT user_id) FROM credit_transactions WHERE transaction_type LIKE 'purchase%'"),
            "suspended": scalar("SELECT COUNT(*) FROM users WHERE suspended_at IS NOT NULL"),
            "active_subscriptions": scalar(
                "SELECT COUNT(*) FROM users WHERE subscription_type = 'monthly' AND subscription_cancel_at IS NULL"),
        },
        "jobs": {
            "total": scalar("SELECT COUNT(*) FROM jobs"),
            "completed_all_time": scalar("SELECT COUNT(*) FROM jobs WHERE status='completed'"),
            "today": scalar("SELECT COUNT(*) FROM jobs WHERE created_at >= CAST(GETUTCDATE() AS DATE)"),
            "completed_today": scalar("SELECT COUNT(*) FROM jobs WHERE status='completed' AND created_at >= CAST(GETUTCDATE() AS DATE)"),
            "failed_today": scalar("SELECT COUNT(*) FROM jobs WHERE status='failed' AND created_at >= CAST(GETUTCDATE() AS DATE)"),
            "queue_depth": scalar("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','dispatching','processing','waiting_lora')"),
            "avg_processing_seconds": round(float(avg_secs), 1) if avg_secs else 0,
            "total_images_generated": scalar("SELECT SUM(credits_consumed) FROM jobs WHERE status='completed'"),
        },
        "billing": {
            "credits_purchased_30d": scalar(
                "SELECT SUM(amount) FROM credit_transactions WHERE transaction_type LIKE 'purchase%' "
                "AND created_at >= DATEADD(DAY,-30,GETUTCDATE())"),
            "credits_used_all_time": scalar("SELECT SUM(credits_consumed) FROM jobs"),
            "note": "currency revenue needs the Stripe/plan-price join (not wired yet). "
                    "total_images_generated is credits_consumed on completed jobs (1 credit = 1 image).",
        },
        "organizations": {"total": scalar("SELECT COUNT(*) FROM organizations")},
        "support": {"open": 0, "note": "no support-ticket table yet"},
    }), mimetype="application/json", status_code=200)


@app.route(route="superadmin/users", methods=["GET"])
def admin_list_users(req: func.HttpRequest) -> func.HttpResponse:
    _admin, err = _require_admin_or_response(req)
    if err:
        return err
    limit, offset = _admin_page(req)
    q = (req.params.get("q") or "").strip()
    cur = get_db().cursor()
    where, args = "", []
    if q:
        where = "WHERE (email LIKE ? OR full_name LIKE ? OR CAST(user_id AS varchar(36)) LIKE ?)"
        args = [f"%{q}%"] * 3
    cur.execute(f"SELECT COUNT(*) FROM users {where}", *args)
    total = cur.fetchone()[0]
    cur.execute(
        f"SELECT user_id, email, full_name, plan_name, subscription_type, lora_status, "
        f"credits_remaining, one_time_credits_remaining, monthly_credits_remaining, created_at "
        f"FROM users {where} ORDER BY created_at DESC OFFSET ? ROWS FETCH NEXT ? ROWS ONLY",
        *(args + [offset, limit]))
    users = [{
        "user_id": str(r[0]), "email": r[1], "full_name": r[2], "plan_name": r[3],
        "subscription_type": r[4], "lora_status": r[5],
        "credits": int((r[7] or 0) + (r[8] or 0)) if (r[7] is not None or r[8] is not None) else int(r[6] or 0),
        "one_time_credits": int(r[7] or 0), "monthly_credits": int(r[8] or 0),
        "created_at": _utc_iso(r[9]),
    } for r in cur.fetchall()]
    return func.HttpResponse(
        json.dumps({"users": users, "total": total, "limit": limit, "offset": offset}),
        mimetype="application/json", status_code=200)


@app.route(route="superadmin/users/{user_id}", methods=["GET"])
def admin_user_detail(req: func.HttpRequest) -> func.HttpResponse:
    _admin, err = _require_admin_or_response(req)
    if err:
        return err
    user_id = req.route_params.get("user_id")
    cur = get_db().cursor()
    cur.execute(
        "SELECT user_id, email, full_name, plan_name, subscription_type, subscription_plan, "
        "lora_status, retrain_count, credits_remaining, one_time_credits_remaining, "
        "monthly_credits_remaining, created_at, subscription_start, subscription_end, "
        "stripe_customer_id, terms_accepted_at, suspended_at FROM users WHERE user_id = ?", user_id)
    r = cur.fetchone()
    if not r:
        return func.HttpResponse("User not found", status_code=404)
    user = {
        "user_id": str(r[0]), "email": r[1], "full_name": r[2], "plan_name": r[3],
        "subscription_type": r[4], "subscription_plan": r[5], "lora_status": r[6],
        "retrain_count": int(r[7] or 0), "credits_remaining": int(r[8] or 0),
        "one_time_credits_remaining": int(r[9] or 0), "monthly_credits_remaining": int(r[10] or 0),
        "created_at": _utc_iso(r[11]), "subscription_start": _utc_iso(r[12]),
        "subscription_end": _utc_iso(r[13]), "stripe_customer_id": r[14],
        "terms_accepted_at": _utc_iso(r[15]),
        "suspended": r[16] is not None, "suspended_at": _utc_iso(r[16]),
        "account_status": "suspended" if r[16] is not None else "active",
    }
    # internal support notes (most recent first)
    cur.execute("SELECT admin_email, note, created_at FROM admin_user_notes "
                "WHERE user_id = ? ORDER BY created_at DESC", user_id)
    user["notes"] = [{"admin_email": n[0], "note": n[1], "created_at": _utc_iso(n[2])}
                     for n in cur.fetchall()]
    cur.execute("SELECT TOP 20 job_id, status, category, credits_consumed, created_at, completed_at "
                "FROM jobs WHERE user_id = ? ORDER BY created_at DESC", user_id)
    user["recent_jobs"] = [{
        "job_id": str(j[0]), "status": j[1], "category": j[2],
        "credits_consumed": int(j[3] or 0), "created_at": _utc_iso(j[4]), "completed_at": _utc_iso(j[5]),
    } for j in cur.fetchall()]
    cur.execute("SELECT TOP 30 amount, transaction_type, job_id, created_at FROM credit_transactions "
                "WHERE user_id = ? ORDER BY created_at DESC", user_id)
    user["credit_ledger"] = [{
        "amount": int(t[0]), "type": t[1], "job_id": str(t[2]) if t[2] else None, "created_at": _utc_iso(t[3]),
    } for t in cur.fetchall()]
    return func.HttpResponse(json.dumps(user), mimetype="application/json", status_code=200)


@app.route(route="superadmin/jobs", methods=["GET"])
def admin_list_jobs(req: func.HttpRequest) -> func.HttpResponse:
    _admin, err = _require_admin_or_response(req)
    if err:
        return err
    limit, offset = _admin_page(req)
    status_f = (req.params.get("status") or "").strip()
    user_f = (req.params.get("user_id") or "").strip()
    cur = get_db().cursor()
    clauses, args = [], []
    if status_f:
        clauses.append("j.status = ?"); args.append(status_f)
    if user_f:
        clauses.append("j.user_id = ?"); args.append(user_f)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    cur.execute(f"SELECT COUNT(*) FROM jobs j {where}", *args)
    total = cur.fetchone()[0]
    cur.execute(
        f"SELECT j.job_id, j.user_id, u.email, j.status, j.category, j.source_type, "
        f"j.credits_consumed, j.created_at, j.completed_at, j.external_execution_id "
        f"FROM jobs j LEFT JOIN users u ON u.user_id = j.user_id {where} "
        f"ORDER BY j.created_at DESC OFFSET ? ROWS FETCH NEXT ? ROWS ONLY",
        *(args + [offset, limit]))
    jobs = [{
        "job_id": str(r[0]), "user_id": str(r[1]), "email": r[2], "status": r[3],
        "category": r[4], "source_type": r[5], "credits_consumed": int(r[6] or 0),
        "created_at": _utc_iso(r[7]), "completed_at": _utc_iso(r[8]), "execution_id": r[9],
    } for r in cur.fetchall()]
    return func.HttpResponse(
        json.dumps({"jobs": jobs, "total": total, "limit": limit, "offset": offset}),
        mimetype="application/json", status_code=200)


@app.route(route="superadmin/jobs/{job_id}", methods=["GET"])
def admin_job_detail(req: func.HttpRequest) -> func.HttpResponse:
    _admin, err = _require_admin_or_response(req)
    if err:
        return err
    job_id = _route_job_id(req)
    if job_id is None:
        return func.HttpResponse("Not found", status_code=404)
    cur = get_db().cursor()
    cur.execute(
        "SELECT j.job_id, j.user_id, u.email, j.status, j.category, j.source_type, j.job_type, "
        "j.credits_consumed, j.created_at, j.dispatched_at, j.completed_at, j.external_execution_id, "
        "j.output_blob_path, j.job_params FROM jobs j LEFT JOIN users u ON u.user_id = j.user_id "
        "WHERE j.job_id = ?", job_id)
    r = cur.fetchone()
    if not r:
        return func.HttpResponse("Not found", status_code=404)
    try:
        params = json.loads(r[13]) if r[13] else {}
    except (TypeError, ValueError):
        params = {}
    try:
        images = json.loads(r[12]) if r[12] else []
        image_count = len(images) if isinstance(images, list) else 0
    except (TypeError, ValueError):
        image_count = 0
    proc_secs = None
    if r[9] and r[10]:
        try:
            proc_secs = int((r[10] - r[9]).total_seconds())
        except Exception:
            proc_secs = None
    return func.HttpResponse(json.dumps({
        "job_id": str(r[0]), "user_id": str(r[1]), "email": r[2], "status": r[3],
        "category": r[4], "source_type": r[5], "job_type": r[6], "credits_consumed": int(r[7] or 0),
        "created_at": _utc_iso(r[8]), "dispatched_at": _utc_iso(r[9]), "completed_at": _utc_iso(r[10]),
        "execution_id": r[11], "image_count": image_count, "processing_seconds": proc_secs,
        "params": params,
    }), mimetype="application/json", status_code=200)


# ── Admin helpers: JSON body + immutable audit write ──────────────────────────
def _admin_json_body(req: func.HttpRequest):
    """(body_dict, None) or (None, 400 response)."""
    try:
        body = req.get_json()
        return (body if isinstance(body, dict) else {}), None
    except Exception:
        return None, func.HttpResponse(
            json.dumps({"error": "invalid JSON body"}), mimetype="application/json", status_code=400)


def _write_audit(cur, admin, action, target_type=None, target_id=None,
                 previous=None, new=None, reason=None, result="success"):
    """Append one admin-audit row using the CALLER'S cursor, so it commits atomically with the
    mutation in a transaction. Best-effort: an audit failure is logged, never fatal to the action."""
    try:
        cur.execute(
            "INSERT INTO admin_audit_log (actor_id, actor_email, action, target_type, target_id, "
            "previous_value, new_value, reason, result) VALUES (?,?,?,?,?,?,?,?,?)",
            (admin or {}).get("oid"), (admin or {}).get("email"), action, target_type,
            str(target_id) if target_id is not None else None,
            json.dumps(previous) if previous is not None else None,
            json.dumps(new) if new is not None else None, reason, result)
    except Exception as e:
        logging.warning(f"audit write failed action={action}: {e}")


@app.route(route="superadmin/me", methods=["GET"])
def admin_me(req: func.HttpRequest) -> func.HttpResponse:
    admin, err = _require_admin_or_response(req)
    if err:
        return err
    return func.HttpResponse(json.dumps({
        "oid": admin["oid"], "email": admin["email"], "name": admin["name"], "roles": admin["roles"],
    }), mimetype="application/json", status_code=200)


@app.route(route="superadmin/audit-logs", methods=["GET"])
def admin_audit_logs(req: func.HttpRequest) -> func.HttpResponse:
    admin, err = _require_admin_or_response(req)
    if err:
        return err
    limit, offset = _admin_page(req)
    clauses, args = [], []
    for col, key in (("target_type", "target_type"), ("target_id", "target_id")):
        v = (req.params.get(key) or "").strip()
        if v:
            clauses.append(f"{col} = ?"); args.append(v)
    act = (req.params.get("action") or "").strip()
    if act:
        clauses.append("action LIKE ?"); args.append(f"%{act}%")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    cur = get_db().cursor()
    cur.execute(f"SELECT COUNT(*) FROM admin_audit_log {where}", *args)
    total = cur.fetchone()[0]
    cur.execute(
        f"SELECT event_id, actor_email, action, target_type, target_id, previous_value, new_value, "
        f"reason, result, created_at FROM admin_audit_log {where} "
        f"ORDER BY created_at DESC OFFSET ? ROWS FETCH NEXT ? ROWS ONLY", *(args + [offset, limit]))
    events = [{
        "event_id": str(r[0]), "actor_email": r[1], "action": r[2], "target_type": r[3],
        "target_id": r[4], "previous_value": r[5], "new_value": r[6], "reason": r[7],
        "result": r[8], "created_at": _utc_iso(r[9]),
    } for r in cur.fetchall()]
    return func.HttpResponse(
        json.dumps({"events": events, "total": total, "limit": limit, "offset": offset}),
        mimetype="application/json", status_code=200)


@app.route(route="superadmin/users/{user_id}/suspend", methods=["POST"])
def admin_suspend_user(req: func.HttpRequest) -> func.HttpResponse:
    admin, err = _require_admin_or_response(req)
    if err:
        return err
    user_id = req.route_params.get("user_id")
    body, berr = _admin_json_body(req)
    if berr:
        return berr
    reason = (body.get("reason") or "").strip()
    if not reason:
        return func.HttpResponse(json.dumps({"error": "reason required"}),
                                 mimetype="application/json", status_code=400)
    conn = new_connection()
    try:
        conn.autocommit = False
        cur = conn.cursor()
        cur.execute("SELECT suspended_at FROM users WHERE user_id = ?", user_id)
        row = cur.fetchone()
        if not row:
            conn.rollback()
            return func.HttpResponse("User not found", status_code=404)
        cur.execute("UPDATE users SET suspended_at = SYSUTCDATETIME() "
                    "WHERE user_id = ? AND suspended_at IS NULL", user_id)
        _write_audit(cur, admin, "user.suspended", "user", user_id,
                     previous={"suspended": row[0] is not None}, new={"suspended": True}, reason=reason)
        conn.commit()
    finally:
        conn.close()
    return func.HttpResponse(json.dumps({"user_id": str(user_id), "suspended": True}),
                             mimetype="application/json", status_code=200)


@app.route(route="superadmin/users/{user_id}/reactivate", methods=["POST"])
def admin_reactivate_user(req: func.HttpRequest) -> func.HttpResponse:
    admin, err = _require_admin_or_response(req)
    if err:
        return err
    user_id = req.route_params.get("user_id")
    body, berr = _admin_json_body(req)
    if berr:
        return berr
    reason = (body.get("reason") or "").strip() or "reactivated"
    conn = new_connection()
    try:
        conn.autocommit = False
        cur = conn.cursor()
        cur.execute("SELECT suspended_at FROM users WHERE user_id = ?", user_id)
        row = cur.fetchone()
        if not row:
            conn.rollback()
            return func.HttpResponse("User not found", status_code=404)
        cur.execute("UPDATE users SET suspended_at = NULL WHERE user_id = ?", user_id)
        _write_audit(cur, admin, "user.reactivated", "user", user_id,
                     previous={"suspended": row[0] is not None}, new={"suspended": False}, reason=reason)
        conn.commit()
    finally:
        conn.close()
    return func.HttpResponse(json.dumps({"user_id": str(user_id), "suspended": False}),
                             mimetype="application/json", status_code=200)


@app.route(route="superadmin/users/{user_id}/notes", methods=["GET", "POST"])
def admin_user_notes(req: func.HttpRequest) -> func.HttpResponse:
    admin, err = _require_admin_or_response(req)
    if err:
        return err
    user_id = req.route_params.get("user_id")
    cur = get_db().cursor()
    if req.method == "POST":
        body, berr = _admin_json_body(req)
        if berr:
            return berr
        note = (body.get("note") or "").strip()
        if not note:
            return func.HttpResponse(json.dumps({"error": "note required"}),
                                     mimetype="application/json", status_code=400)
        cur.execute("INSERT INTO admin_user_notes (user_id, admin_id, admin_email, note) VALUES (?,?,?,?)",
                    user_id, admin["oid"], admin["email"], note[:4000])
        _write_audit(cur, admin, "user.note_added", "user", user_id, new={"note": note[:200]})
        return func.HttpResponse(json.dumps({"status": "added"}),
                                 mimetype="application/json", status_code=201)
    cur.execute("SELECT admin_email, note, created_at FROM admin_user_notes "
                "WHERE user_id = ? ORDER BY created_at DESC", user_id)
    notes = [{"admin_email": r[0], "note": r[1], "created_at": _utc_iso(r[2])} for r in cur.fetchall()]
    return func.HttpResponse(json.dumps({"notes": notes}), mimetype="application/json", status_code=200)


@app.route(route="superadmin/users/{user_id}/credits/adjust", methods=["POST"])
def admin_adjust_credits(req: func.HttpRequest) -> func.HttpResponse:
    admin, err = _require_admin_or_response(req)
    if err:
        return err
    user_id = req.route_params.get("user_id")
    body, berr = _admin_json_body(req)
    if berr:
        return berr
    reason = (body.get("reason") or "").strip()
    if not reason:
        return func.HttpResponse(json.dumps({"error": "reason required"}),
                                 mimetype="application/json", status_code=400)
    try:
        amount = int(body.get("amount"))
    except (TypeError, ValueError):
        return func.HttpResponse(json.dumps({"error": "amount must be an integer"}),
                                 mimetype="application/json", status_code=400)
    if amount == 0:
        return func.HttpResponse(json.dumps({"error": "amount must be non-zero"}),
                                 mimetype="application/json", status_code=400)
    conn = new_connection()
    try:
        conn.autocommit = False
        cur = conn.cursor()
        cur.execute("SELECT credits_remaining, one_time_credits_remaining, monthly_credits_remaining "
                    "FROM users WHERE user_id = ?", user_id)
        row = cur.fetchone()
        if not row:
            conn.rollback()
            return func.HttpResponse("User not found", status_code=404)
        before = {"legacy": int(row[0] or 0), "one_time": int(row[1] or 0), "monthly": int(row[2] or 0)}
        # Adjust the one_time bucket (+ legacy mirror). A deduction never drives the bucket below 0.
        new_one_time = before["one_time"] + amount if amount > 0 else max(0, before["one_time"] + amount)
        applied = new_one_time - before["one_time"]
        cur.execute("UPDATE users SET one_time_credits_remaining = ?, "
                    "credits_remaining = credits_remaining + ? WHERE user_id = ?",
                    new_one_time, applied, user_id)
        cur.execute("INSERT INTO credit_transactions (user_id, amount, transaction_type) VALUES (?,?,?)",
                    user_id, applied, "admin_adjust")
        after = {**before, "one_time": new_one_time, "legacy": before["legacy"] + applied}
        _write_audit(cur, admin, "user.credits.adjusted", "user", user_id,
                     previous=before, new=after, reason=reason)
        conn.commit()
    finally:
        conn.close()
    return func.HttpResponse(json.dumps({
        "user_id": str(user_id), "requested": amount, "applied": applied,
        "one_time_credits_remaining": new_one_time,
    }), mimetype="application/json", status_code=200)


@app.route(route="superadmin/credits", methods=["GET"])
def admin_credits_ledger(req: func.HttpRequest) -> func.HttpResponse:
    admin, err = _require_admin_or_response(req)
    if err:
        return err
    limit, offset = _admin_page(req)
    clauses, args = [], []
    uf = (req.params.get("user_id") or "").strip()
    tf = (req.params.get("type") or "").strip()
    if uf:
        clauses.append("t.user_id = ?"); args.append(uf)
    if tf:
        clauses.append("t.transaction_type = ?"); args.append(tf)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    cur = get_db().cursor()
    cur.execute(f"SELECT COUNT(*) FROM credit_transactions t {where}", *args)
    total = cur.fetchone()[0]
    cur.execute(
        f"SELECT t.transaction_id, t.user_id, u.email, t.amount, t.transaction_type, t.job_id, t.created_at "
        f"FROM credit_transactions t LEFT JOIN users u ON u.user_id = t.user_id {where} "
        f"ORDER BY t.created_at DESC OFFSET ? ROWS FETCH NEXT ? ROWS ONLY", *(args + [offset, limit]))
    entries = [{
        "transaction_id": str(r[0]), "user_id": str(r[1]), "email": r[2], "amount": int(r[3] or 0),
        "type": r[4], "job_id": str(r[5]) if r[5] else None, "created_at": _utc_iso(r[6]),
    } for r in cur.fetchall()]
    return func.HttpResponse(
        json.dumps({"entries": entries, "total": total, "limit": limit, "offset": offset}),
        mimetype="application/json", status_code=200)


@app.route(route="superadmin/jobs/{job_id}/cancel", methods=["POST"])
def admin_cancel_job(req: func.HttpRequest) -> func.HttpResponse:
    admin, err = _require_admin_or_response(req)
    if err:
        return err
    job_id = _route_job_id(req)
    if job_id is None:
        return func.HttpResponse("Not found", status_code=404)
    cur = get_db().cursor()
    cur.execute("SELECT status, external_execution_id FROM jobs WHERE job_id = ?", job_id)
    row = cur.fetchone()
    if not row:
        return func.HttpResponse("Not found", status_code=404)
    status, exec_id = (row[0] or "").strip(), row[1]
    if status in ("completed", "failed"):
        return func.HttpResponse(json.dumps({"job_id": str(job_id), "status": status, "cancelled": False}),
                                 mimetype="application/json", status_code=200)
    if exec_id:
        try:
            from shared.queue_trigger import stop_execution
            stop_execution(exec_id)
        except Exception as e:
            logging.warning(f"admin_cancel_job stop failed job={job_id}: {e}")
    _mark_failed(job_id)  # fail + refund, guarded once
    _write_audit(get_db().cursor(), admin, "job.cancelled", "job", str(job_id),
                 previous={"status": status}, new={"status": "failed"})
    return func.HttpResponse(json.dumps({"job_id": str(job_id), "status": "failed", "cancelled": True}),
                             mimetype="application/json", status_code=200)


@app.route(route="superadmin/jobs/{job_id}/retry", methods=["POST"])
def admin_retry_job(req: func.HttpRequest) -> func.HttpResponse:
    admin, err = _require_admin_or_response(req)
    if err:
        return err
    job_id = _route_job_id(req)
    if job_id is None:
        return func.HttpResponse("Not found", status_code=404)
    cur = get_db().cursor()
    cur.execute("SELECT status, user_id, job_params FROM jobs WHERE job_id = ?", job_id)
    r = cur.fetchone()
    if not r:
        return func.HttpResponse("Not found", status_code=404)
    status = (r[0] or "").strip()
    if status != "failed":
        return func.HttpResponse(
            json.dumps({"error": f"can only retry a failed job (status={status})"}),
            mimetype="application/json", status_code=409)
    cur.execute("UPDATE jobs SET status='queued', external_execution_id=NULL, completed_at=NULL "
                "WHERE job_id = ?", job_id)
    try:
        from shared.queue_client import INFERENCE_QUEUE, enqueue_job
        enqueue_job(INFERENCE_QUEUE, {"job_id": str(job_id), "user_id": str(r[1]), "job_params": r[2]})
    except Exception as e:
        logging.warning(f"admin_retry_job enqueue failed job={job_id}: {e}")
    _write_audit(get_db().cursor(), admin, "job.retried", "job", str(job_id),
                 previous={"status": status}, new={"status": "queued"})
    return func.HttpResponse(json.dumps({"job_id": str(job_id), "status": "queued"}),
                             mimetype="application/json", status_code=200)


@app.route(route="superadmin/jobs/{job_id}/restore-credit", methods=["POST"])
def admin_restore_credit(req: func.HttpRequest) -> func.HttpResponse:
    admin, err = _require_admin_or_response(req)
    if err:
        return err
    job_id = _route_job_id(req)
    if job_id is None:
        return func.HttpResponse("Not found", status_code=404)
    body, berr = _admin_json_body(req)
    if berr:
        return berr
    reason = (body.get("reason") or "").strip()
    if not reason:
        return func.HttpResponse(json.dumps({"error": "reason required"}),
                                 mimetype="application/json", status_code=400)
    conn = new_connection()
    try:
        conn.autocommit = False
        cur = conn.cursor()
        cur.execute("SELECT user_id, job_params FROM jobs WHERE job_id = ?", job_id)
        r = cur.fetchone()
        if not r:
            conn.rollback()
            return func.HttpResponse("Not found", status_code=404)
        try:
            cost = max(1, int(json.loads(r[1]).get("credit_cost", 1)))
        except Exception:
            cost = 1
        cur.execute("UPDATE users SET one_time_credits_remaining = one_time_credits_remaining + ?, "
                    "credits_remaining = credits_remaining + ? WHERE user_id = ?", cost, cost, r[0])
        cur.execute("INSERT INTO credit_transactions (user_id, amount, transaction_type, job_id) "
                    "VALUES (?,?,?,?)", r[0], cost, "admin_restore", job_id)
        _write_audit(cur, admin, "job.credits_restored", "job", str(job_id),
                     new={"restored": cost}, reason=reason)
        conn.commit()
    finally:
        conn.close()
    return func.HttpResponse(json.dumps({"job_id": str(job_id), "restored": cost}),
                             mimetype="application/json", status_code=200)


@app.route(route="superadmin/payments", methods=["GET"])
def admin_payments(req: func.HttpRequest) -> func.HttpResponse:
    admin, err = _require_admin_or_response(req)
    if err:
        return err
    limit, offset = _admin_page(req)
    cur = get_db().cursor()
    cur.execute("SELECT COUNT(*) FROM credit_transactions WHERE transaction_type LIKE 'purchase%'")
    total = cur.fetchone()[0]
    cur.execute(
        "SELECT t.transaction_id, t.user_id, u.email, u.stripe_customer_id, t.amount, "
        "t.transaction_type, t.created_at FROM credit_transactions t "
        "LEFT JOIN users u ON u.user_id = t.user_id WHERE t.transaction_type LIKE 'purchase%' "
        "ORDER BY t.created_at DESC OFFSET ? ROWS FETCH NEXT ? ROWS ONLY", offset, limit)
    payments = [{
        "transaction_id": str(r[0]), "user_id": str(r[1]), "email": r[2], "stripe_customer_id": r[3],
        "credits_granted": int(r[4] or 0), "type": r[5], "created_at": _utc_iso(r[6]),
    } for r in cur.fetchall()]
    return func.HttpResponse(json.dumps({
        "payments": payments, "total": total, "limit": limit, "offset": offset,
        "note": "credit-grant records from the ledger; currency amounts + Stripe refunds need the "
                "Stripe API join (not wired yet — use /jobs/{id}/restore-credit or credits/adjust to "
                "return credits).",
    }), mimetype="application/json", status_code=200)


@app.route(route="superadmin/subscriptions", methods=["GET"])
def admin_subscriptions(req: func.HttpRequest) -> func.HttpResponse:
    admin, err = _require_admin_or_response(req)
    if err:
        return err
    limit, offset = _admin_page(req)
    cur = get_db().cursor()
    cur.execute("SELECT COUNT(*) FROM users WHERE subscription_type IS NOT NULL")
    total = cur.fetchone()[0]
    cur.execute(
        "SELECT user_id, email, subscription_type, subscription_plan, monthly_credits_remaining, "
        "subscription_start, subscription_end, subscription_cancel_at, payment_failed_at "
        "FROM users WHERE subscription_type IS NOT NULL "
        "ORDER BY subscription_start DESC OFFSET ? ROWS FETCH NEXT ? ROWS ONLY", offset, limit)
    subs = [{
        "user_id": str(r[0]), "email": r[1], "subscription_type": r[2], "subscription_plan": r[3],
        "monthly_credits_remaining": int(r[4] or 0), "start": _utc_iso(r[5]), "end": _utc_iso(r[6]),
        "cancel_at": _utc_iso(r[7]), "payment_failed": r[8] is not None,
    } for r in cur.fetchall()]
    return func.HttpResponse(
        json.dumps({"subscriptions": subs, "total": total, "limit": limit, "offset": offset}),
        mimetype="application/json", status_code=200)


@app.route(route="superadmin/system-health", methods=["GET"])
def admin_system_health(req: func.HttpRequest) -> func.HttpResponse:
    admin, err = _require_admin_or_response(req)
    if err:
        return err
    health = {}
    try:
        get_db().cursor().execute("SELECT 1")
        health["sql"] = "ok"
    except Exception as e:
        health["sql"] = f"error: {str(e)[:100]}"
    try:
        get_blob_client().get_container_client("outputs").get_container_properties()
        health["blob"] = "ok"
    except Exception as e:
        health["blob"] = f"error: {str(e)[:100]}"
    try:
        from shared.queue_trigger import count_active_job_executions
        health["gpu_active_executions"] = count_active_job_executions()
    except Exception as e:
        health["gpu_active_executions"] = f"error: {str(e)[:100]}"
    try:
        cur = get_db().cursor()
        cur.execute("SELECT COUNT(*) FROM jobs WHERE status IN "
                    "('queued','dispatching','processing','waiting_lora')")
        health["queue_depth"] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM jobs WHERE status='failed' "
                    "AND created_at >= DATEADD(DAY,-1,GETUTCDATE())")
        health["failed_jobs_24h"] = cur.fetchone()[0]
    except Exception as e:
        health["queue_depth"] = f"error: {str(e)[:100]}"
    return func.HttpResponse(json.dumps(health), mimetype="application/json", status_code=200)


@app.timer_trigger(schedule="0 0 * * * *", arg_name="timer", run_on_startup=False)
def retention_cleanup(timer: func.TimerRequest) -> None:
    """Hourly: for users past their retention window, delete BLOBS (photos, LoRA,
    generated results) but KEEP the DB rows (users/jobs/lora_trainings) for analytics.
    lora_status is reset and the user's jobs are marked expired so History can show
    'expired' instead of loading a deleted image. The window is set per plan elsewhere:
    one-time on each generation, monthly on subscription end (see RETENTION_DAYS)."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id FROM users "
        "WHERE retention_expires_at IS NOT NULL AND retention_expires_at < GETUTCDATE() "
        "AND ISNULL(monthly_credits_remaining, 0) <= 0 "
        "AND ISNULL(one_time_credits_remaining, 0) <= 0"
    )
    due = [str(r[0]) for r in cur.fetchall()]
    if not due:
        return
    logging.info(f"retention_cleanup: {len(due)} user(s) due")

    for user_id in due:
        eligibility = new_connection()
        try:
            eligibility.autocommit = False
            eligibility_cur = eligibility.cursor()
            eligibility_cur.execute(
                "SELECT user_id FROM users WITH (UPDLOCK, HOLDLOCK) "
                "WHERE user_id = ? "
                "AND retention_expires_at IS NOT NULL "
                "AND retention_expires_at < GETUTCDATE() "
                "AND ISNULL(monthly_credits_remaining, 0) <= 0 "
                "AND ISNULL(one_time_credits_remaining, 0) <= 0 "
                "AND NOT EXISTS (SELECT 1 FROM jobs WHERE user_id = ? "
                "AND status NOT IN ('completed', 'failed'))",
                user_id, user_id,
            )
            eligible = eligibility_cur.fetchone() is not None
            if not eligible:
                eligibility.rollback()
                logging.info(
                    f"retention_cleanup: skipping user={user_id}; "
                    "credits, deadline, or active jobs changed")
                continue

            eligibility_cur.execute(
                "SELECT job_id FROM jobs WHERE user_id = ? "
                "AND status IN ('completed', 'failed')",
                user_id,
            )
            job_ids = [str(r[0]) for r in eligibility_cur.fetchall()]

            # Hold the locked eligibility transaction through deletion so a concurrent
            # purchase cannot add paid credits between the final check and adapter removal.
            n_photos = _delete_blobs("inputs", f"{user_id}/")
            n_lora = _delete_blobs("lora-weights", f"identity/{user_id}/")
            n_results = 0
            for jid in job_ids:
                n_results += _delete_blobs("outputs", f"results/{jid}/")
                _delete_blobs("outputs", f"debug/{jid}.txt")

            eligibility_cur.execute(
                "UPDATE jobs SET expired = 1 WHERE user_id = ? "
                "AND status IN ('completed', 'failed')",
                user_id,
            )
            eligibility_cur.execute(
                "UPDATE users SET lora_status = 'none', retention_expires_at = NULL "
                "WHERE user_id = ? "
                "AND retention_expires_at IS NOT NULL "
                "AND retention_expires_at < GETUTCDATE() "
                "AND ISNULL(monthly_credits_remaining, 0) <= 0 "
                "AND ISNULL(one_time_credits_remaining, 0) <= 0 "
                "AND NOT EXISTS (SELECT 1 FROM jobs WHERE user_id = ? "
                "AND status NOT IN ('completed', 'failed'))",
                user_id, user_id,
            )
            eligibility.commit()
        finally:
            eligibility.close()
        logging.info(
            f"retention_cleanup: user={user_id} deleted photos={n_photos} lora={n_lora} "
            f"results={n_results} jobs_expired={len(job_ids)}")
        _write_event(user_id, "retention.delete",
                     detail={"photos": n_photos, "lora": n_lora, "results": n_results,
                             "jobs_expired": len(job_ids)})


# ── Monthly credit renewal ───────────────────────────────
# NOTE: there is deliberately NO time-based refill timer. Renewal is driven SOLELY by
# Stripe's `invoice.paid` webhook (see _handle_invoice_paid), which fires only when a
# payment actually succeeds. The old timer here reset credits for any monthly row older
# than ~1 month WITHOUT checking payment, so a subscriber whose card was failing (or who
# was mid-dunning) got a free monthly refill. If a payment-confirmed fallback is ever
# needed (e.g. to cover a missed webhook), it must reconcile against Stripe — query the
# subscription's latest invoice status — not refill on elapsed time alone.

# ══════════════════════════════════════════════════════════════════════════
# Teams — invitations & members (migration 016)
#
# Owns the two tables from 016: invitations, organization_members.
# Sireesha's 015 owns organizations / organization_payments / jobs.organization_id.
#
# editing the
# file on Features_team — keeping each person's handlers in one contiguous block
# at a known place keeps merge conflicts to the seam instead of scattered through
# 2,500 lines.
# ══════════════════════════════════════════════════════════════════════════

# How long an invite link stays usable. Long enough that an employee can act on an
# email they read on Monday and click on Friday; short enough that a leaked link in
# an old inbox stops working.
INVITE_EXPIRY_DAYS = int(os.environ.get("INVITE_EXPIRY_DAYS", "14"))


def _require_org_admin(cursor, user_id, org_id):
    """Returns (org_row, None) if user_id is the admin of org_id, else (None, error_response).

    Every Teams endpoint below resolves authority through this: the org is looked up
    by BOTH id and admin_user_id, so a caller cannot act on an org they don't own by
    guessing an organization_id. 404 (not 403) on someone else's org — a wrong-owner
    request should not confirm the org exists.
    """
    cursor.execute(
        "SELECT organization_id, seats_purchased, credits_per_seat, status, name "
        "FROM organizations WHERE organization_id = ? AND admin_user_id = ?",
        org_id, user_id,
    )
    row = cursor.fetchone()
    if not row:
        return None, func.HttpResponse("Organization not found", status_code=404)
    if row[3] != "active":
        return None, func.HttpResponse(
            json.dumps({"error": "ORG_NOT_ACTIVE", "status": row[3]}),
            mimetype="application/json", status_code=403)
    return row, None


# ── Send invitations ──────────────────────────────────────
@app.route(route="orgs/{org_id}/invitations", methods=["POST"])
def create_invitations(req: func.HttpRequest) -> func.HttpResponse:
    """Admin invites employees by email. Body: {"emails": ["a@x.com", "b@x.com"]}

    Seat accounting counts members + still-pending invites together. Counting only
    members would let an admin with 5 seats send 50 invites and oversell the plan on
    a first-come race; a pending invite is a reserved seat until it expires.
    """
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        user_id = get_user_id(token)
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)

    org_id = req.route_params.get("org_id")
    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON", status_code=400)

    emails = body.get("emails") or []
    if isinstance(emails, str):          # tolerate a single address
        emails = [emails]
    # Normalize before de-duping so "A@x.com" and "a@x.com " don't both get a seat.
    emails = [e.strip().lower() for e in emails if isinstance(e, str) and e.strip()]
    emails = list(dict.fromkeys(emails))  # de-dupe, preserve order
    if not emails:
        return func.HttpResponse(
            json.dumps({"error": "NO_EMAILS"}),
            mimetype="application/json", status_code=400)

    conn = new_connection()
    try:
        conn.autocommit = False
        cur = conn.cursor()

        org, err = _require_org_admin(cur, user_id, org_id)
        if err:
            return err
        seats_purchased, credits_per_seat = org[1], org[2]

        # Seats already taken: active members + live pending invites.
        cur.execute(
            "SELECT COUNT(*) FROM organization_members "
            "WHERE organization_id = ? AND status = 'active'", org_id)
        active_members = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM invitations WHERE organization_id = ? "
            "AND status = 'pending' AND expires_at > SYSUTCDATETIME()", org_id)
        pending_invites = cur.fetchone()[0]
        seats_left = seats_purchased - active_members - pending_invites

        if seats_left <= 0:
            return func.HttpResponse(
                json.dumps({"error": "NO_SEATS_AVAILABLE",
                            "seats_purchased": seats_purchased,
                            "active_members": active_members,
                            "pending_invites": pending_invites}),
                mimetype="application/json", status_code=409)

        # Skip anyone already on the plan or already holding a live invite, rather than
        # spending a seat on a duplicate. Reported back so the admin sees why.
        cur.execute(
            "SELECT LOWER(i.email) FROM invitations i WHERE i.organization_id = ? "
            "AND i.status = 'pending' AND i.expires_at > SYSUTCDATETIME()", org_id)
        already_invited = {r[0] for r in cur.fetchall()}

        created, skipped = [], []
        expires_at = datetime.now(timezone.utc) + timedelta(days=INVITE_EXPIRY_DAYS)

        for email in emails:
            if email in already_invited:
                skipped.append({"email": email, "reason": "ALREADY_INVITED"})
                continue
            if len(created) >= seats_left:
                skipped.append({"email": email, "reason": "NO_SEATS_AVAILABLE"})
                continue
            # token_urlsafe is a CSPRNG — this value IS the credential in the emailed
            # link, so it must not come from uuid4/random. 32 bytes -> ~43 chars, well
            # inside the VARCHAR(128) column.
            invite_token = secrets.token_urlsafe(32)
            cur.execute("""
                INSERT INTO invitations
                    (organization_id, email, token, status, invited_by_user_id, expires_at)
                OUTPUT INSERTED.invitation_id
                VALUES (?, ?, ?, 'pending', ?, ?)
            """, org_id, email, invite_token, user_id, expires_at)
            invitation_id = cur.fetchone()[0]
            created.append({"invitation_id": str(invitation_id),
                            "email": email,
                            "token": invite_token})

        org_name = org[4]
        conn.commit()
    finally:
        conn.close()

    # AFTER the commit, deliberately. Sending inside the transaction would hold a DB
    # connection open across a network call to ACS, and a rollback after a successful
    # send would leave someone holding a live link to an invite that no longer exists.
    for inv in created:
        inv["email_sent"] = send_invite_email(
            inv["email"], org_name, inv["token"], credits_per_seat
        )

    sent = sum(1 for i in created if i["email_sent"])
    logging.info(f"invitations created: org={org_id} count={len(created)} "
                 f"emailed={sent} skipped={len(skipped)}")

    return func.HttpResponse(
        json.dumps({"created": created, "skipped": skipped,
                    "credits_per_seat": credits_per_seat,
                    "expires_at": _utc_iso(expires_at)}),
        mimetype="application/json", status_code=201)


# ── Accept an invitation ──────────────────────────────────
@app.route(route="invitations/{token}/accept", methods=["POST"])
def accept_invitation(req: func.HttpRequest) -> func.HttpResponse:
    """Employee joins the org after logging in.

    By the time this is called the caller already has a users row: the frontend's
    AuthContext calls POST /users/register after every login, and that endpoint is
    idempotent. So this only has to create the MEMBERSHIP, not the account.

    Everything runs in one transaction: re-check the invite, re-count seats, insert
    the member, mark the invite accepted. The seat count is re-checked HERE and not
    just at invite time, because two employees can accept the last seat at the same
    moment.
    """
    auth = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        user_id = get_user_id(auth)
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)

    invite_token = req.route_params.get("token")
    if not invite_token:
        return func.HttpResponse("Missing token", status_code=400)

    conn = new_connection()
    try:
        conn.autocommit = False
        cur = conn.cursor()

        # UPDLOCK/HOLDLOCK: hold the invite row for the life of the transaction so a
        # double-click can't have two requests both read 'pending' and both insert.
        cur.execute("""
            SELECT invitation_id, organization_id, status, expires_at
            FROM invitations WITH (UPDLOCK, HOLDLOCK)
            WHERE token = ?
        """, invite_token)
        inv = cur.fetchone()
        if not inv:
            return func.HttpResponse(
                json.dumps({"error": "INVALID_INVITE"}),
                mimetype="application/json", status_code=404)

        invitation_id, org_id, inv_status, expires_at = inv

        if inv_status != "pending":
            # 409, not 404: the link is real, it has just been used or revoked.
            return func.HttpResponse(
                json.dumps({"error": "INVITE_NOT_PENDING", "status": inv_status}),
                mimetype="application/json", status_code=409)

        # expires_at comes back naive from SQL Server; compare against naive UTC.
        if expires_at <= datetime.now(timezone.utc).replace(tzinfo=None):
            cur.execute(
                "UPDATE invitations SET status = 'expired' WHERE invitation_id = ?",
                invitation_id)
            conn.commit()
            return func.HttpResponse(
                json.dumps({"error": "INVITE_EXPIRED"}),
                mimetype="application/json", status_code=410)

        # A user belongs to at most one org (UQ_member_user). Check explicitly so the
        # caller gets a clear reason instead of a 500 from the unique violation, and
        # so re-accepting your own invite is a friendly no-op rather than an error.
        cur.execute(
            "SELECT organization_id FROM organization_members WHERE user_id = ?",
            user_id)
        existing = cur.fetchone()
        if existing:
            if str(existing[0]).lower() == str(org_id).lower():
                conn.commit()
                return func.HttpResponse(
                    json.dumps({"message": "Already a member",
                                "organization_id": str(org_id)}),
                    mimetype="application/json", status_code=200)
            return func.HttpResponse(
                json.dumps({"error": "ALREADY_IN_ANOTHER_ORG"}),
                mimetype="application/json", status_code=409)

        cur.execute(
            "SELECT seats_purchased, credits_per_seat, status "
            "FROM organizations WHERE organization_id = ?", org_id)
        org = cur.fetchone()
        if not org:
            return func.HttpResponse("Organization not found", status_code=404)
        seats_purchased, credits_per_seat, org_status = org
        if org_status != "active":
            return func.HttpResponse(
                json.dumps({"error": "ORG_NOT_ACTIVE", "status": org_status}),
                mimetype="application/json", status_code=403)

        cur.execute(
            "SELECT COUNT(*) FROM organization_members "
            "WHERE organization_id = ? AND status = 'active'", org_id)
        if cur.fetchone()[0] >= seats_purchased:
            return func.HttpResponse(
                json.dumps({"error": "NO_SEATS_AVAILABLE"}),
                mimetype="application/json", status_code=409)

        # credits_granted is a SNAPSHOT of credits_per_seat at join time. If the admin
        # later changes the per-seat number, people who already joined keep what they
        # were given.
        cur.execute("""
            INSERT INTO organization_members
                (organization_id, user_id, invitation_id,
                 credits_granted, credits_remaining, status)
            OUTPUT INSERTED.membership_id
            VALUES (?, ?, ?, ?, ?, 'active')
        """, org_id, user_id, invitation_id, credits_per_seat, credits_per_seat)
        membership_id = cur.fetchone()[0]

        cur.execute("""
            UPDATE invitations
            SET status = 'accepted', accepted_user_id = ?, accepted_at = SYSUTCDATETIME()
            WHERE invitation_id = ?
        """, user_id, invitation_id)

        conn.commit()
    finally:
        conn.close()

    logging.info(f"invitation accepted: org={org_id} user={user_id} "
                 f"membership={membership_id}")

    return func.HttpResponse(
        json.dumps({"membership_id": str(membership_id),
                    "organization_id": str(org_id),
                    "credits_granted": credits_per_seat}),
        mimetype="application/json", status_code=201)


# ── List org members ──────────────────────────────────────
@app.route(route="orgs/{org_id}/members", methods=["GET"])
def list_org_members(req: func.HttpRequest) -> func.HttpResponse:
    """Admin's roster view: who has joined, credits used, plus outstanding invites."""
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        user_id = get_user_id(token)
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)

    org_id = req.route_params.get("org_id")
    conn = get_db()
    cur = conn.cursor()

    org, err = _require_org_admin(cur, user_id, org_id)
    if err:
        return err
    seats_purchased = org[1]

    # LEFT JOIN users: the membership row is the source of truth for who is on the
    # plan. An INNER JOIN would silently hide a member whose users row is missing.
    cur.execute("""
        SELECT m.membership_id, m.user_id, u.email, u.full_name,
               m.credits_granted, m.credits_remaining, m.status, m.joined_at,
               m.invitation_id
        FROM organization_members m
        LEFT JOIN users u ON u.user_id = m.user_id
        WHERE m.organization_id = ?
        ORDER BY m.joined_at ASC
    """, org_id)
    members = [{
        "membership_id": str(r[0]),
        "user_id": str(r[1]),
        "email": r[2],
        "full_name": r[3],
        "credits_granted": r[4],
        "credits_remaining": r[5],
        "status": r[6],
        "joined_at": _utc_iso(r[7]),
        # NULL invitation_id = the admin, who created the org rather than accepting a link.
        "is_admin": r[8] is None,
    } for r in cur.fetchall()]

    cur.execute("""
        SELECT invitation_id, email, status, expires_at, created_at
        FROM invitations
        WHERE organization_id = ? AND status = 'pending'
              AND expires_at > SYSUTCDATETIME()
        ORDER BY created_at ASC
    """, org_id)
    pending = [{
        "invitation_id": str(r[0]),
        "email": r[1],
        "status": r[2],
        "expires_at": _utc_iso(r[3]),
        "created_at": _utc_iso(r[4]),
    } for r in cur.fetchall()]
    # Tokens are deliberately NOT returned here — this is a listing view, and a token
    # is a credential. They are returned once, at creation.

    active_count = sum(1 for m in members if m["status"] == "active")
    return func.HttpResponse(
        json.dumps({
            "organization_id": str(org_id),
            "seats_purchased": seats_purchased,
            "seats_used": active_count,
            "seats_available": max(0, seats_purchased - active_count - len(pending)),
            "members": members,
            "pending_invitations": pending,
        }),
        mimetype="application/json", status_code=200)

# ══════════════════════════════════════════════════════════════════════════
# Teams — member's own org context
#
# Append to the END of function_app.py, right after the Teams block you already
# added (create_invitations / accept_invitation / list_org_members).
#
# Needs at the top of function_app.py (already there from the org-credits work):
#   from shared.org_credits import get_active_membership
# ══════════════════════════════════════════════════════════════════════════
 
 
# ── What org am I in? ─────────────────────────────────────
@app.route(route="me/organization", methods=["GET"])
def get_my_organization(req: func.HttpRequest) -> func.HttpResponse:
    """The employee's own view: which org, how many credits left, admin or not.
 
    WHY THIS EXISTS SEPARATELY FROM /orgs/{id}/members:
    that endpoint is the ADMIN's roster and requires knowing the org id. An invited
    employee knows neither — they just logged in. This resolves everything from the
    token alone, so the member dashboard can load with no prior state.
 
    Returns 200 with organization: null for an ordinary individual user rather than
    404, so the frontend can call this unconditionally after login and branch on the
    result instead of treating a normal user as an error case.
    """
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        user_id = get_user_id(token)
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)
 
    conn = get_db()
    cur = conn.cursor()
 
    # Dashboard discovery includes a newly-created pending-payment workspace so its
    # admin can reach checkout. Generation still uses get_active_membership(), which
    # remains restricted to active organizations and therefore cannot spend org credits
    # before payment succeeds.
    cur.execute("""
        SELECT m.organization_id, m.credits_remaining, m.membership_id,
               o.name, o.admin_user_id, o.credits_per_seat,
               m.credits_granted, m.joined_at, o.status, o.seats_purchased
        FROM organization_members m
        JOIN organizations o ON o.organization_id = m.organization_id
        WHERE m.user_id = ? AND m.status = 'active'
          AND o.status IN ('pending_payment', 'active')
    """, user_id)
    row = cur.fetchone()
    if not row:
        return func.HttpResponse(
            json.dumps({"organization": None}),
            mimetype="application/json", status_code=200)

    (org_id, credits_remaining, membership_id, org_name, admin_user_id,
     credits_per_seat, credits_granted, joined_at, org_status, seats_purchased) = row
 
    # lora_status drives the dashboard's next action: an employee who hasn't uploaded
    # photos yet needs "upload photos", not "generate". Same field the individual
    # onboarding flow keys off, so the member dashboard can reuse that logic.
    cur.execute("SELECT lora_status FROM users WHERE user_id = ?", user_id)
    urow = cur.fetchone()
    lora_status = (urow[0] or "none").strip() if urow else "none"
 
    return func.HttpResponse(
        json.dumps({
            "organization": {
                "organization_id": str(org_id),
                "name": org_name,
                "is_admin": str(admin_user_id).lower() == str(user_id).lower(),
                "status": org_status,
                "seats_purchased": seats_purchased,
            },
            "membership": {
                "membership_id": str(membership_id),
                "credits_granted": credits_granted,
                "credits_remaining": credits_remaining,
                "credits_used": max(0, (credits_granted or 0) - (credits_remaining or 0)),
                "joined_at": _utc_iso(joined_at),
            },
            "lora_status": lora_status,
        }),
        mimetype="application/json", status_code=200)
