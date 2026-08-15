"""Atomic job reservation: credits + daily-cap check + insert + credit decrement
in ONE serialized transaction.

Pulled out of submit_job so the exact production path can be exercised by the
concurrency integration tests. sp_getapplock is a SQL-server-wide exclusive lock
held for the transaction, so concurrent submits across ALL scaled-out instances
serialize here — no two can both pass the same cap (TOCTOU-safe).
"""
import json

from .db import new_connection
from .outbox import outbox_add
from .queue_client import INFERENCE_QUEUE
from . import credit_ledger


class ReserveResult:
    def __init__(self, ok: bool, job_id=None, reason: str = None, outbox_id=None,
                 status: str = None):
        self.ok = ok
        self.job_id = job_id
        self.reason = reason   # one of: busy | credits | user_cap | global_cap
        # For a 'queued' job: the outbox row written ATOMICALLY with the job + credit
        # charge, so the caller can fast-path the send (and the dispatcher can retry it).
        # None for 'waiting_lora' rows, which are deliberately not enqueued.
        self.outbox_id = outbox_id
        # Final status chosen inside the reservation transaction. This can differ
        # from a status observed earlier by the HTTP handler if training completes.
        self.status = status


def reserve_job_slot(user_id, input_blob_path, job_params,
                     per_user_cap, global_cap, lock_timeout_ms=5000,
                     credit_cost=1, initial_status="queued",
                     source_type=None, resolve_lora_status=False,
                     image_count=None, credits_per_image=1) -> ReserveResult:
    """credit_cost = total credits this job consumes = image_count *
    plan.credits_per_image (resolved by the caller). One-time plans use
    credits_per_image=1 so credit_cost == number of images. The decrement and the
    'enough credits' check both use this amount so the counter tracks images, not jobs.

    initial_status: 'queued' (normal — caller enqueues immediately) or 'waiting_lora'
    (the user has no trained identity LoRA yet). A 'waiting_lora' row is deliberately
    NOT enqueued: the training watcher flips it to 'queued' and enqueues it the moment
    the adapter is ready. Credits are still reserved here, so a user cannot submit more
    work than they paid for while waiting. The reaper only touches 'processing' and
    'dispatching', so a parked row is never reaped out from under the trainer."""
    credit_cost = max(1, int(credit_cost))
    conn = new_connection()
    try:
        conn.autocommit = False
        cur = conn.cursor()

        # Serialize the whole critical section across instances.
        cur.execute(
            "DECLARE @r int; "
            "EXEC @r = sp_getapplock @Resource = 'submit-job', @LockMode = 'Exclusive', "
            "@LockOwner = 'Transaction', @LockTimeout = ?; SELECT @r",
            lock_timeout_ms,
        )
        if cur.fetchone()[0] < 0:
            conn.rollback()
            return ReserveResult(False, reason="busy")

        # Resolve LoRA readiness FIRST, under an update/hold lock (dev's training-completion
        # race fix), so a concurrent _finish_training can't orphan a charged waiting_lora row.
        # Kept as its OWN query so `row` below is always the credit-balance row.
        if resolve_lora_status:
            cur.execute(
                "SELECT lora_status FROM users WITH (UPDLOCK, HOLDLOCK) WHERE user_id = ?",
                user_id,
            )
            lrow = cur.fetchone()
            lora_status = ((lrow[0] if lrow else "none") or "none").strip()
            if lora_status == "ready":
                initial_status = "queued"
            elif lora_status == "training":
                initial_status = "waiting_lora"
            else:
                conn.rollback()
                return ReserveResult(False, reason="lora_not_ready")

        # Credit sufficiency against the CORRECT balance (features-stripe separate-balance model):
        # a monthly job spends monthly credits first and overflows to one-time; a one-time job
        # spends one-time; a legacy job (no source_type) spends the single credits_remaining.
        monthly_debit = 0
        one_time_debit = 0
        if source_type == "monthly":
            cur.execute(
                "SELECT monthly_credits_remaining, one_time_credits_remaining "
                "FROM users WHERE user_id = ?",
                user_id,
            )
            row = cur.fetchone()
            one_time_balance = 0
            if row:
                monthly_balance = int(row[0] or 0)
                one_time_balance = int(row[1] or 0)
                monthly_debit = min(monthly_balance, credit_cost)
                one_time_debit = credit_cost - monthly_debit
            if not row or one_time_debit > one_time_balance:
                conn.rollback()
                return ReserveResult(False, reason="credits")
        elif source_type == "one_time":
            cur.execute(
                "SELECT one_time_credits_remaining FROM users WHERE user_id = ?",
                user_id,
            )
            row = cur.fetchone()
            if row:
                one_time_debit = credit_cost
            if not row or int(row[0] or 0) < credit_cost:
                conn.rollback()
                return ReserveResult(False, reason="credits")
        else:
            cur.execute("SELECT credits_remaining FROM users WHERE user_id = ?", user_id)
            row = cur.fetchone()
            if not row or row[0] < credit_cost:
                conn.rollback()
                return ReserveResult(False, reason="credits")

        # A failed job has been refunded and a waiting_lora job has not reached the GPU,
        # so neither may consume daily capacity. Keep queued in the reservation count:
        # excluding it would let a burst of submissions all pass before dispatch starts.
        cap_statuses = "('queued', 'dispatching', 'processing', 'completed')"
        cur.execute(
            "SELECT COUNT(*) FROM jobs WHERE user_id = ? "
            f"AND status IN {cap_statuses} "
            "AND created_at >= CAST(GETUTCDATE() AS DATE)",
            user_id,
        )
        if cur.fetchone()[0] >= per_user_cap:
            conn.rollback()
            return ReserveResult(False, reason="user_cap")

        cur.execute(
            "SELECT COUNT(*) FROM jobs "
            f"WHERE status IN {cap_statuses} "
            "AND created_at >= CAST(GETUTCDATE() AS DATE)"
        )
        if cur.fetchone()[0] >= global_cap:
            conn.rollback()
            return ReserveResult(False, reason="global_cap")

        if source_type in ("monthly", "one_time"):
            try:
                params = json.loads(job_params) if job_params else {}
            except (TypeError, ValueError):
                params = {}
            params["monthly_credit_cost"] = monthly_debit
            params["one_time_credit_cost"] = one_time_debit
            job_params = json.dumps(params)

        cur.execute("""
            INSERT INTO jobs (user_id, status, input_blob_path, job_params, source_type)
            OUTPUT INSERTED.job_id
            VALUES (?, ?, ?, ?, ?)
        """, user_id, initial_status, input_blob_path, job_params, source_type)
        job_id = cur.fetchone()[0]

        if source_type == "monthly":
            cur.execute(
                "UPDATE users SET "
                "monthly_credits_remaining = monthly_credits_remaining - ?, "
                "one_time_credits_remaining = one_time_credits_remaining - ?, "
                "credits_remaining = credits_remaining - ? - ? "
                "WHERE user_id = ?",
                monthly_debit, one_time_debit, monthly_debit, one_time_debit, user_id,
            )
        elif source_type == "one_time":
            cur.execute(
                "UPDATE users SET "
                "one_time_credits_remaining = one_time_credits_remaining - ?, "
                "credits_remaining = credits_remaining - ? WHERE user_id = ?",
                one_time_debit, one_time_debit, user_id,
            )
        else:
            cur.execute(
                "UPDATE users SET credits_remaining = credits_remaining - ? WHERE user_id = ?",
                credit_cost, user_id,
            )

        # Audit ledger: one signed row for the spend, in THIS transaction alongside the debit
        # (append-only credit ledger). credit_cost is the total debited across whichever
        # balance(s) the branch above touched.
        credit_ledger.record(cur, user_id, -credit_cost, credit_ledger.REASON_JOB_RESERVE, job_id)

        # Transactional outbox: for a 'queued' job, write the queue message into the outbox
        # IN THIS SAME TRANSACTION as the row + credit charge, so the send can no longer be
        # lost after commit (finding #4). 'waiting_lora' rows are deliberately NOT enqueued
        # — the training watcher releases them — so they get no outbox row here.
        outbox_id = None
        if initial_status == "queued":
            outbox_id = outbox_add(
                cur, INFERENCE_QUEUE,
                {"job_id": str(job_id), "user_id": str(user_id), "job_params": job_params},
            )

        conn.commit()   # releases the app lock; job + credit charge + outbox row commit together
        return ReserveResult(
            True, job_id=job_id, outbox_id=outbox_id, status=initial_status)
    finally:
        conn.close()
