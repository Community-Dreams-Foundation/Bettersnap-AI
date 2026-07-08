import azure.functions as func
import os
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone

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
# 'processing' threshold must be >> inference wall-time (SDXL 4-var ≈ 20 min on A100).
REAPER_STUCK_MINUTES       = int(os.environ.get("REAPER_STUCK_MINUTES", "45"))
REAPER_DISPATCHING_MINUTES = int(os.environ.get("REAPER_DISPATCHING_MINUTES", "15"))


def _gpu_dispatch_enabled() -> bool:
    # Emergency kill switch (read per-call so it takes effect without a redeploy).
    # A budget action group or an operator flips GPU_DISPATCH_ENABLED=false to
    # halt ALL A100 spend. Budgets only alert; this actually stops dispatch.
    return os.environ.get("GPU_DISPATCH_ENABLED", "true").lower() == "true"
from azure.storage.blob import generate_blob_sas, BlobSasPermissions
from shared.auth import validate_token, get_user_id
from shared.db import get_db, new_connection
from shared.job_reservation import reserve_job_slot
from shared.queue_client import enqueue_job
from shared.blob import upload_blob, get_blob_client
from shared.keyvault import get_secret

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# ── Health Check ──────────────────────────────────────────
@app.route(route="health", methods=["GET"])
def health(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse("OK", status_code=200)

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
    email = payload.get("email", "")
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
    cursor.execute("""
        INSERT INTO users (user_id, email, full_name, credits_remaining)
        VALUES (?, ?, ?, 20)
    """, user_id, email, name)
    conn.commit()

    return func.HttpResponse(
        json.dumps({"message": "User registered", "credits": 20}),
        mimetype="application/json",
        status_code=201
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
        "SELECT user_id, email, full_name, credits_remaining FROM users WHERE user_id = ?",
        user_id,
    )
    row = cursor.fetchone()
    # Fresh-start: no row until the user has registered. register_user is the ONLY
    # path that creates a users row (and grants the initial credits).
    if not row:
        return func.HttpResponse("User not found", status_code=404)

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
    purpose = body.get("purpose")
    background = body.get("background")
    input_blob_path = body.get("input_blob_path")

    if not all([gender, age_range, hair_color, purpose, background, input_blob_path]):
        return func.HttpResponse("Missing required fields", status_code=400)

    job_params = json.dumps({
        "gender": gender,
        "age_range": age_range,
        "hair_color": hair_color,
        "purpose": purpose,
        "background": background,
        "input_blob_path": input_blob_path
    })

    # Atomic credits + daily-cap check + insert + decrement (serialized across
    # all instances via sp_getapplock). See shared/job_reservation.py. Daily caps
    # exist because credits alone don't bound spend: multi-account abuse,
    # duplicate-job bugs, or a test account can all flood the GPU.
    result = reserve_job_slot(
        user_id, input_blob_path, job_params, PER_USER_DAILY_CAP, GLOBAL_DAILY_CAP
    )
    if not result.ok:
        if result.reason == "credits":
            return func.HttpResponse("Insufficient credits", status_code=402)
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
    enqueue_job({"job_id": str(job_id), "user_id": str(user_id), "job_params": job_params})

    return func.HttpResponse(
        json.dumps({"job_id": str(job_id)}),
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

    jobs = [
        {
            "job_id": str(r[0]),
            "status": r[1],
            "job_type": r[2],
            "category": r[3],
            "output_blob_path": r[4],
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
        if transitioned:
            cur.execute(
                "UPDATE users SET credits_remaining = credits_remaining + 1 "
                "WHERE user_id = (SELECT user_id FROM jobs WHERE job_id = ?)",
                job_id,
            )
        conn.commit()
        logging.info(
            f"job_id={job_id} -> failed (transitioned={transitioned}, "
            f"credit_refunded={transitioned})"
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

    if stuck:
        logging.info(f"REAPER: reaped {len(stuck)} stuck jobs")
    else:
        logging.info("REAPER: no stuck jobs found")