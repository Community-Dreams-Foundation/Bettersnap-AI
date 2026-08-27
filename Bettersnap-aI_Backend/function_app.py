import azure.functions as func
import os
import hmac
import json
import logging
import uuid
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
# Reaper: auto-fail jobs stuck in 'processing' or 'dispatching' past these thresholds.
# 'processing' threshold must be >> inference wall-time. Measured: 172s startup +
# ~7.9s/image, so even a 65-image Expert job is ~12 min.
REAPER_STUCK_MINUTES       = int(os.environ.get("REAPER_STUCK_MINUTES", "45"))
REAPER_DISPATCHING_MINUTES = int(os.environ.get("REAPER_DISPATCHING_MINUTES", "15"))

# ── Identity-LoRA training ────────────────────────────────────────────────
# DreamBooth needs enough angles/expressions to generalize, but the run is a fixed
# 1400 steps regardless of count — so more photos cost quality-nothing and time-nothing.
MIN_TRAINING_PHOTOS = int(os.environ.get("MIN_TRAINING_PHOTOS", "8"))
MAX_TRAINING_PHOTOS = int(os.environ.get("MAX_TRAINING_PHOTOS", "12"))
# Measured training wall-time is ~51 min (17.6 class-image gen + 28.1 train + startup).
# The watcher fails a run that blows past this, releasing any jobs parked behind it.
TRAINING_STUCK_MINUTES = int(os.environ.get("TRAINING_STUCK_MINUTES", "90"))
# Where cropped training images live, under the user's own prefix in `inputs`.
CROP_SUBDIR = "input/crop_upperbody"
# Data retention: N days after the clock starts, the user's BLOBS (photos, LoRA,
# results) are deleted; DB rows are KEPT for analytics. One-time plans start the clock
# on each generation (last-gen + N); monthly plans on subscription end (period-end + N).
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "3"))


def _gpu_dispatch_enabled() -> bool:
    # Emergency kill switch (read per-call so it takes effect without a redeploy).
    # A budget action group or an operator flips GPU_DISPATCH_ENABLED=false to
    # halt ALL A100 spend. Budgets only alert; this actually stops dispatch.
    return os.environ.get("GPU_DISPATCH_ENABLED", "true").lower() == "true"
from azure.storage.blob import generate_blob_sas, BlobSasPermissions
from shared.auth import validate_token, get_user_id
from shared.db import get_db, new_connection
from shared.job_reservation import reserve_job_slot
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
    cancel_subscription, reactivate_subscription, verify_webhook,
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
        return func.HttpResponse(
            json.dumps({"message": "User already exists"}),
            mimetype="application/json",
            status_code=200
        )

    logging.info(
        f"First registration for oid={user_id}; token claim keys="
        f"{sorted(payload.keys())} (email_present={'email' in payload}, "
        f"name_present={'name' in payload})"
    )
    # plan_name is set EXPLICITLY rather than left to the column default: the default
    # was 'basic' (30 images = 30 credits) while the grant is 20, so every new user was
    # created 10 credits short of ever running a single job. New users start on the free
    # trial plan, which REGISTRATION_CREDITS is sized to cover (see shared/plans.py).
    cursor.execute("""
        INSERT INTO users (user_id, email, full_name, credits_remaining, plan_name)
        VALUES (?, ?, ?, ?, ?)
    """, user_id, email, name, REGISTRATION_CREDITS, DEFAULT_PLAN_KEY)
    conn.commit()

    return func.HttpResponse(
        json.dumps({"message": "User registered", "credits": 20}),
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
        cur.execute(
            """INSERT INTO organizations
                (organization_id, name, admin_user_id, seats_purchased, credits_per_seat, status)
               VALUES (?, ?, ?, ?, ?, 'active')""",
            organization_id, name, admin_user_id, seats_purchased, ORG_CREDITS_PER_SEAT,
        )
        cur.execute(
            """INSERT INTO organization_members
                (membership_id, organization_id, user_id, invitation_id,
                 credits_granted, credits_remaining, status)
               VALUES (?, ?, ?, NULL, ?, ?, 'active')""",
            str(uuid.uuid4()), organization_id, admin_user_id,
            ORG_CREDITS_PER_SEAT, ORG_CREDITS_PER_SEAT,
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
            "status": "active",
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
    if str(admin_user_id).lower() != str(user_id).lower():
        return func.HttpResponse("Forbidden", status_code=403)
    if status != "active":
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
    if str(admin_user_id).lower() != str(user_id).lower():
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
    cursor.execute("SELECT credits_remaining FROM users WHERE user_id = ?", user_id)
    row = cursor.fetchone()
    if not row:
        return func.HttpResponse("User not found", status_code=404)

    return func.HttpResponse(
        json.dumps({"credits_remaining": row[0]}),
        mimetype="application/json",
        status_code=200
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
        updates.append("email = ?")
        params.append(body.get("email"))

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
    cursor.execute(f"UPDATE users SET {', '.join(updates)} WHERE user_id = ?", params)
    conn.commit()

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
        json.dumps({"terms_accepted_at": str(row[0]) if row[0] else None}),
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

    try:
        body = req.get_json()
        accepted_at = body.get("accepted_at")
    except Exception:
        accepted_at = None

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET terms_accepted_at = ? WHERE user_id = ?",
        accepted_at, user_id,
    )
    conn.commit()

    return func.HttpResponse(
        json.dumps({"terms_accepted_at": accepted_at}),
        mimetype="application/json",
        status_code=200,
    )

# ── Upload Photo ──────────────────────────────────────────
@app.route(route="upload", methods=["POST"])
def upload_photo(req: func.HttpRequest) -> func.HttpResponse:
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        user_id = get_user_id(token)
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)

    file = req.files.get("photo")
    if not file:
        return func.HttpResponse("No photo provided", status_code=400)

    ext = file.filename.split(".")[-1].lower()
    if ext not in ["jpg", "jpeg", "png"]:
        return func.HttpResponse("Invalid file type", status_code=400)

    blob_name = f"{user_id}/input/{file.filename}"
    url = upload_blob("inputs", blob_name, file.read())

    # Canonical convention: input_blob_path is "<container>/<blob>" so the
    # inference container resolves it without assuming a container name.
    # Clients must submit this exact value as input_blob_path to /jobs/submit.
    input_blob_path = f"inputs/{blob_name}"

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

    from shared.crops import crop_head_and_shoulders, NoFaceError, MultipleFacesError

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
        "SELECT lora_status, credits_remaining, retrain_count FROM users WHERE user_id = ?",
        user_id,
    )
    row = cur.fetchone()
    if not row:
        return func.HttpResponse("User not found", status_code=404)
    lora_status = (row[0] or "none").strip()
    credits, retrain_count = int(row[1] or 0), int(row[2] or 0)

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
        except ValueError as e:
            rejected.append({"photo": os.path.basename(blob_name), "index": i + 1,
                             "code": "NOT_AN_IMAGE", "reason": str(e)})

    if rejected:
        names = ", ".join(str(r["index"]) for r in rejected)
        return func.HttpResponse(
            json.dumps({
                "error": f"Photo {names} doesn't show your face clearly, please replace it."
                         if len(rejected) == 1 else
                         f"Photos {names} don't show your face clearly, please replace them.",
                "rejected": rejected,
            }),
            mimetype="application/json", status_code=400)

    # ── Upload crops + build FILES_JSON programmatically ──────────────────────
    # Paths are RELATIVE to the `inputs` container (no container prefix) because the
    # trainer joins them against INPUT_CONTAINER=inputs. Captions are omitted entirely:
    # this is DreamBooth, identity keys off --instance_prompt, and the trainer reads and
    # discards the caption field. Hand-typing them was pure waste.
    files = []
    for i, data in enumerate(crops):
        rel = f"{user_id}/{CROP_SUBDIR}/img{i}.jpg"
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
    conn2 = new_connection()
    try:
        conn2.autocommit = False
        cur2 = conn2.cursor()
        cur2.execute("""
            INSERT INTO lora_trainings (user_id, status, photo_count, class_word, files_json)
            OUTPUT INSERTED.training_id
            VALUES (?, 'queued', ?, ?, ?)
        """, user_id, len(files), cword, json.dumps(files))
        training_id = cur2.fetchone()[0]
        if is_retrain:
            cur2.execute(
                "UPDATE users SET lora_status = 'training', retrain_count = retrain_count + 1, "
                "credits_remaining = credits_remaining - ? WHERE user_id = ?",
                retrain_cost, user_id,
            )
        else:
            cur2.execute(
                "UPDATE users SET lora_status = 'training' WHERE user_id = ?", user_id)
        # Transactional outbox (finding #4): the training enqueue commits WITH the row +
        # lora_status + retrain charge, so a queue outage after commit can't strand the user
        # in 'training' with no message. Fast-path the send below; outbox_dispatcher backstops.
        train_msg = {"training_id": str(training_id), "user_id": str(user_id)}
        outbox_tid = outbox_add(cur2, TRAINING_QUEUE, train_msg)
        conn2.commit()
    finally:
        conn2.close()

    outbox_try_send_now(outbox_tid, TRAINING_QUEUE, train_msg)
    logging.info(
        f"training queued: training_id={training_id} user={user_id} "
        f"photos={len(files)} class_word={cword}"
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
            "created_at": t[4].isoformat() if t[4] else None,
            "completed_at": t[5].isoformat() if t[5] else None,
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

    body = req.get_json()
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
    input_blob_path = input_blob_path or ""

    # ── Resolve the user's plan (image_count + selection limits live in shared/plans) ──
    conn0 = get_db()
    cur0 = conn0.cursor()
    cur0.execute("SELECT plan_name, lora_status, credits_remaining FROM users WHERE user_id = ?", user_id)
    prow = cur0.fetchone()
    if not prow:
        return func.HttpResponse("User not found", status_code=404)
    plan = get_plan(prow[0])
    lora_status = (prow[1] or "none").strip()
    # A Teams seat spends the org pool, so the image-count budget below must be
    # computed against THAT balance, not the personal one. reserve_job_slot resolves
    # membership again under its lock — this read is only for sizing the request.
    credits_remaining, org_id = effective_credits(cur0, user_id, int(prow[2] or 0))

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
        # Every ref must exist in the catalog (drops typos / stale ids).
        bad = ([a for a in attire_ids if not catalog.valid_attire_ref(a)] +
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
        requested = int(body.get("image_count") or plan.min_session_images)
        max_by_credits = credits_remaining // plan.credits_per_image
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
    parked = lora_status == "training"
    result = reserve_job_slot(
        user_id, input_blob_path, job_params, PER_USER_DAILY_CAP, GLOBAL_DAILY_CAP,
        credit_cost=cost,
        initial_status="waiting_lora" if parked else "queued",
    )
    if not result.ok:
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
    outbox_try_send_now(
        result.outbox_id, INFERENCE_QUEUE,
        {"job_id": str(job_id), "user_id": str(user_id), "job_params": job_params},
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

    job_id = req.route_params.get("job_id")
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

    job_id = req.route_params.get("job_id")
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
@app.route(route="jobs/{job_id}", methods=["DELETE"])
def delete_job(req: func.HttpRequest) -> func.HttpResponse:
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        user_id = get_user_id(token)
    except Exception:
        return func.HttpResponse("Unauthorized", status_code=401)

    job_id = req.route_params.get("job_id")
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
            "created_at": str(r[5])
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
    cursor.execute("SELECT attire_id, name, category, blob_path FROM attires WHERE is_active = 1")
    rows = cursor.fetchall()

    attires = [
        {"id": r[0], "name": r[1], "category": r[2], "blob_path": r[3]}
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
    cursor.execute("SELECT background_id, name, category, blob_path FROM backgrounds WHERE is_active = 1")
    rows = cursor.fetchall()

    backgrounds = [
        {"id": r[0], "name": r[1], "category": r[2], "blob_path": r[3]}
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
        "AND created_at < DATEADD(MINUTE, ?, GETUTCDATE())",
        -minutes,
    )
    jobs = [
        {"job_id": str(r[0]), "user_id": r[1], "status": r[2],
         "external_execution_id": r[3], "created_at": str(r[4])}
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
    from shared.queue_trigger import trigger_container_job, count_active_job_executions
    from shared.queue_client import enqueue_job
    from shared.gpu_lease import (
        acquire_dispatch_lease, release_dispatch_lease,
        mark_dispatched, recent_dispatch_pending, DispatchConfigError,
    )
    # The host already base64-decodes the transport (messageEncoding=base64,
    # the extension-bundle default), so get_body() returns the raw JSON.
    payload = json.loads(msg.get_body().decode("utf-8"))
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
            if exec_id or status in ("dispatching", "processing", "completed", "failed"):
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
                "UPDATE jobs SET status = 'dispatching' WHERE job_id = ? AND status = 'queued'",
                job_id,
            )
            claimed = cur.rowcount == 1
            conn.commit()
            if not claimed:
                logging.info(f"job_id={job_id} claim lost (concurrent); skipping")
                return
        finally:
            conn.close()

        # 6) Start the GPU job. If the start fails, revert the claim so the job
        #    can be retried (no A100 was started -> no double-spend), then
        #    re-raise so the queue retries the message.
        try:
            execution_id = trigger_container_job(job_id, user_id)
        except Exception:
            logging.exception(f"start failed for job_id={job_id}; reverting claim to 'queued'")
            _revert_claim(job_id)
            raise

        mark_dispatched(owner)
        _record_execution_id(job_id, execution_id)
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
            cur.execute("SELECT job_params, user_id FROM jobs WHERE job_id = ?", job_id)
            r = cur.fetchone()
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
            else:
                cur.execute(
                    "UPDATE users SET credits_remaining = credits_remaining + ? "
                    "WHERE user_id = (SELECT user_id FROM jobs WHERE job_id = ?)",
                    refund, job_id,
                )
        conn.commit()
        logging.info(
            f"job_id={job_id} -> failed (transitioned={transitioned}, "
            f"credits_refunded={refund if transitioned else 0})"
        )
    finally:
        conn.close()


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
        mark_dispatched, recent_dispatch_pending, DispatchConfigError,
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
        finally:
            conn.close()

        try:
            # user_id is passed ONCE and drives both the input photo paths and the
            # adapter output path. trigger_training_job re-verifies every file lives
            # under this user's prefix before it will start.
            execution_id = trigger_training_job(user_id, json.loads(files_json), class_word)
        except Exception as e:
            logging.exception(f"training start failed for training_id={training_id}")
            # No A100 was started. Revert to 'queued' so the queue retry can pick it up
            # again — unless the payload itself is bad (ValueError from the prefix guard),
            # which will never succeed and must fail loudly instead of looping.
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
            raise

        mark_dispatched(owner)
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
            outbox_try_send_now(outbox_id, INFERENCE_QUEUE, msg)
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

    logging.error(
        f"POISON: job_id={job_id} exceeded retry limit (dequeue_count={msg.dequeue_count}); "
        f"marking failed"
    )
    if job_id:
        # Guarded fail + one-time credit refund (same helper as every other
        # failure path) so a poisoned message doesn't silently cost the user.
        _mark_failed(job_id)


# ── Timer-trigger reaper ──────────────────────────────────────────────────────
# Runs every 10 minutes. Finds jobs stuck in 'processing' past the inference
# wall-time ceiling (REAPER_STUCK_MINUTES, default 45 min — well above the
# SDXL 4-variation worst-case ≈ 20 min). Also reaps 'dispatching' rows the
# dispatcher crashed mid-claim (REAPER_DISPATCHING_MINUTES, default 15 min).
# Both paths call _mark_failed: guarded transition + one-time credit refund.
# An OOM SIGKILL (exit 137) leaves the row in 'processing' because the process
# is killed before it can write — this reaper is the ONLY thing that clears it.
@app.timer_trigger(schedule="0 */10 * * * *", arg_name="timer", run_on_startup=False)
def reaper(timer: func.TimerRequest):
    conn = new_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT job_id FROM jobs WHERE status = 'processing' "
            "AND created_at < DATEADD(MINUTE, ?, GETUTCDATE())",
            -REAPER_STUCK_MINUTES,
        )
        stuck = [str(r[0]) for r in cur.fetchall()]

        cur.execute(
            "SELECT job_id FROM jobs WHERE status = 'dispatching' "
            "AND created_at < DATEADD(MINUTE, ?, GETUTCDATE())",
            -REAPER_DISPATCHING_MINUTES,
        )
        stuck += [str(r[0]) for r in cur.fetchall()]
    finally:
        conn.close()

    for job_id in stuck:
        logging.warning(f"REAPER: failing stuck job_id={job_id}")
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
        "subscription_cancel_at FROM users WHERE user_id = ?",
        user_id,
    )
    row = cursor.fetchone()
    if not row:
        return func.HttpResponse("User not found", status_code=404)

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
            "credits_remaining": row[2],
            "credits_monthly_limit": row[3],
            "subscription_renewed_at": str(row[4]) if row[4] else None,
            "next_renewal": next_renewal,
            # Dunning: non-null means the latest monthly renewal charge FAILED and Stripe is
            # retrying — the UI should prompt "update your card". Cleared on the next success.
            "payment_failed": bool(row[5]),
            "payment_failed_at": str(row[5]) if row[5] else None,
            # Cancellation: non-null means a period-end cancel is scheduled — the UI shows
            # "cancels on {cancel_at}" and offers Reactivate (POST /subscriptions/reactivate).
            "cancel_pending": bool(row[6]),
            "cancel_at": str(row[6]) if row[6] else None,
        }),
        mimetype="application/json",
        status_code=200,
    )


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
        try:
            session = create_monthly_checkout(user_id, email, plan, success_url, cancel_url)
        except Exception as e:
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
        "SELECT stripe_subscription_id, subscription_type FROM users WHERE user_id = ?",
        user_id,
    )
    row = cursor.fetchone()
    if not row:
        return func.HttpResponse("User not found", status_code=404)

    stripe_sub_id, sub_type = row[0], row[1]
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
    ts = result.get("current_period_end")
    if ts:
        cancel_at = datetime.fromtimestamp(int(ts), tz=timezone.utc).replace(tzinfo=None)
        cursor.execute(
            "UPDATE users SET subscription_cancel_at = ? WHERE user_id = ?",
            cancel_at, user_id,
        )

    return func.HttpResponse(
        json.dumps({
            "message": "Subscription will cancel at end of billing period",
            "cancel_at": str(cancel_at) if cancel_at else None,
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

    if event_type == "checkout.session.completed":
        session = event["data"]["object"]
        payment_type = session.get("metadata", {}).get("payment_type")
        if payment_type == "one_time":
            _handle_onetime_payment(session, event_id)
        elif payment_type == "monthly":
            _handle_monthly_checkout(session, event_id)
        elif payment_type == "org_seats":
            _handle_org_payment(session, event_id)

    elif event_type in ("invoice.paid", "invoice.payment_succeeded"):
        _handle_invoice_paid(event["data"]["object"], event_id)

    elif event_type == "invoice.payment_failed":
        _handle_payment_failed(event["data"]["object"], event_id)

    elif event_type in ("customer.subscription.deleted", "customer.subscription.updated"):
        _handle_subscription_ended(event["data"]["object"], event_id)

    return func.HttpResponse("OK", status_code=200)


def _claim_event(cur, event_id: str) -> bool:
    """Idempotency guard for Stripe webhooks. Records event_id and returns True if THIS call
    claimed it (proceed with the grant), False if it was already processed (skip). Runs on
    the CALLER's cursor so the claim commits in the SAME transaction as the grant — if the
    grant rolls back, the claim rolls back too and a genuine Stripe retry can reprocess. The
    event_id PRIMARY KEY (migration 010) is the concurrency backstop."""
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
                credits_remaining = credits_remaining + ?
            WHERE user_id = ?""",
            plan, plan_key_for(plan, "one_time"), credits, user_id,
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
            return
        conn.commit()  # claim + grant commit atomically
        logging.info(f"One-time purchase: user={user_id} plan={plan} +{credits} credits")
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
        cur.execute(
            "UPDATE organizations SET status = 'active' WHERE organization_id = ?",
            organization_id,
        )
        applied = cur.rowcount
        if applied == 0:
            conn.rollback()
            logging.error(
                f"PAYMENT NOT APPLIED (org_seats): organization_id={organization_id} "
                f"amount={amount_total} matched 0 organization rows. "
                f"MANUAL RECONCILIATION REQUIRED."
            )
            return
        conn.commit()
        logging.info(f"Org seat payment recorded: org={organization_id} amount={amount_total}")
    finally:
        conn.close()

        
def _handle_monthly_checkout(session: dict, event_id: str):
    user_id  = session.get("metadata", {}).get("user_id")
    plan     = session.get("metadata", {}).get("plan")
    customer = session.get("customer")
    sub_id   = session.get("subscription")

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
                credits_remaining       = ?,
                credits_monthly_limit   = ?,
                subscription_renewed_at = GETUTCDATE(),
                retention_expires_at    = NULL
            WHERE user_id = ?""",
            plan, plan_key_for(plan, "monthly"), customer, sub_id, credits, credits, user_id,
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
            return
        conn.commit()  # claim + activation commit atomically
        logging.info(f"Monthly subscription activated: user={user_id} plan={plan} credits={credits}")
    finally:
        conn.close()


def _handle_invoice_paid(invoice: dict, event_id: str):
    sub_id = invoice.get("subscription")
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
        cur.execute(
            """UPDATE users SET
                credits_remaining       = credits_monthly_limit,
                subscription_renewed_at = GETUTCDATE(),
                payment_failed_at       = NULL
            WHERE stripe_subscription_id = ?""",
            sub_id,
        )
        conn.commit()  # claim + reset commit atomically
        logging.info(f"Credits reset for subscription={sub_id}")
    finally:
        conn.close()


def _handle_payment_failed(invoice: dict, event_id: str):
    """A monthly renewal charge FAILED. Flag the account (payment_failed_at) so the UI can
    prompt the user to update their card — but do NOT cut access here: Stripe keeps retrying
    (dunning) for days, and the terminal canceled/unpaid is handled by
    _handle_subscription_ended. The flag is cleared automatically by the next successful
    invoice.paid (recovery)."""
    sub_id = invoice.get("subscription")
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
            "UPDATE users SET payment_failed_at = GETUTCDATE() WHERE stripe_subscription_id = ?",
            sub_id,
        )
        conn.commit()
        logging.warning(f"Monthly payment FAILED for subscription={sub_id}; flagged for dunning.")
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
                    subscription_plan      = 'free',
                    subscription_type      = NULL,
                    plan_name              = 'trial',
                    stripe_subscription_id = NULL,
                    credits_monthly_limit  = 20,
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
    """Delete every blob under `prefix` in `container_name`. Best-effort; returns count."""
    n = 0
    try:
        container = get_blob_client().get_container_client(container_name)
        for b in container.list_blobs(name_starts_with=prefix):
            container.delete_blob(b.name)
            n += 1
    except Exception as e:
        logging.warning(f"retention: blob cleanup failed for {container_name}/{prefix}: {e}")
    return n


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
        "WHERE retention_expires_at IS NOT NULL AND retention_expires_at < GETUTCDATE()"
    )
    due = [str(r[0]) for r in cur.fetchall()]
    if not due:
        return
    logging.info(f"retention_cleanup: {len(due)} user(s) due")

    for user_id in due:
        # Photos (raw + crops) and the trained adapter (+ backups) live under the user's
        # own prefix; results are per-job in the 'outputs' container, so look those up.
        n_photos = _delete_blobs("inputs", f"{user_id}/")
        n_lora = _delete_blobs("lora-weights", f"identity/{user_id}/")
        cur.execute("SELECT job_id FROM jobs WHERE user_id = ?", user_id)
        job_ids = [str(r[0]) for r in cur.fetchall()]
        n_results = 0
        for jid in job_ids:
            n_results += _delete_blobs("outputs", f"results/{jid}/")
            _delete_blobs("outputs", f"debug/{jid}.txt")

        # Keep the rows: reset the user, mark jobs expired, clear the window — one txn so a
        # crash can't leave the window set (which would re-delete every hour) or half-done.
        conn2 = new_connection()
        try:
            conn2.autocommit = False
            cur2 = conn2.cursor()
            cur2.execute("UPDATE jobs SET expired = 1 WHERE user_id = ?", user_id)
            cur2.execute(
                "UPDATE users SET lora_status = 'none', retention_expires_at = NULL "
                "WHERE user_id = ?", user_id)
            conn2.commit()
        finally:
            conn2.close()
        logging.info(
            f"retention_cleanup: user={user_id} deleted photos={n_photos} lora={n_lora} "
            f"results={n_results} jobs_expired={len(job_ids)}")


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
                    "expires_at": expires_at.isoformat()}),
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
        "joined_at": r[7].isoformat() if r[7] else None,
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
        "expires_at": r[3].isoformat() if r[3] else None,
        "created_at": r[4].isoformat() if r[4] else None,
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
 
    # Same helper the generation path uses to decide which credit pool to charge, so
    # the number shown here is by construction the number that will actually be spent.
    membership = get_active_membership(cur, user_id)
    if not membership:
        return func.HttpResponse(
            json.dumps({"organization": None}),
            mimetype="application/json", status_code=200)
 
    org_id, credits_remaining, membership_id = membership
 
    cur.execute("""
        SELECT o.name, o.admin_user_id, o.credits_per_seat,
               m.credits_granted, m.joined_at
        FROM organizations o
        JOIN organization_members m ON m.organization_id = o.organization_id
        WHERE o.organization_id = ? AND m.membership_id = ?
    """, org_id, membership_id)
    row = cur.fetchone()
    if not row:
        # get_active_membership just returned this row, so a miss here means the org
        # was deleted mid-request. Treat as no org rather than 500.
        return func.HttpResponse(
            json.dumps({"organization": None}),
            mimetype="application/json", status_code=200)
 
    org_name, admin_user_id, credits_per_seat, credits_granted, joined_at = row
 
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
            },
            "membership": {
                "membership_id": str(membership_id),
                "credits_granted": credits_granted,
                "credits_remaining": credits_remaining,
                "credits_used": max(0, (credits_granted or 0) - (credits_remaining or 0)),
                "joined_at": joined_at.isoformat() if joined_at else None,
            },
            "lora_status": lora_status,
        }),
        mimetype="application/json", status_code=200)