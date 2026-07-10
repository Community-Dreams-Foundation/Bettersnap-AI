"""Atomic job reservation: credits + daily-cap check + insert + credit decrement
in ONE serialized transaction.

Pulled out of submit_job so the exact production path can be exercised by the
concurrency integration tests. sp_getapplock is a SQL-server-wide exclusive lock
held for the transaction, so concurrent submits across ALL scaled-out instances
serialize here — no two can both pass the same cap (TOCTOU-safe).
"""
from .db import new_connection


class ReserveResult:
    def __init__(self, ok: bool, job_id=None, reason: str = None):
        self.ok = ok
        self.job_id = job_id
        self.reason = reason   # one of: busy | credits | user_cap | global_cap


def reserve_job_slot(user_id, input_blob_path, job_params,
                     per_user_cap, global_cap, lock_timeout_ms=5000) -> ReserveResult:
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

        cur.execute("SELECT credits_remaining FROM users WHERE user_id = ?", user_id)
        row = cur.fetchone()
        if not row or row[0] < 20:
            conn.rollback()
            return ReserveResult(False, reason="credits")

        cur.execute(
            "SELECT COUNT(*) FROM jobs WHERE user_id = ? AND created_at >= CAST(GETUTCDATE() AS DATE)",
            user_id,
        )
        if cur.fetchone()[0] >= per_user_cap:
            conn.rollback()
            return ReserveResult(False, reason="user_cap")

        cur.execute(
            "SELECT COUNT(*) FROM jobs WHERE created_at >= CAST(GETUTCDATE() AS DATE)"
        )
        if cur.fetchone()[0] >= global_cap:
            conn.rollback()
            return ReserveResult(False, reason="global_cap")

        cur.execute("""
            INSERT INTO jobs (user_id, status, input_blob_path, job_params)
            OUTPUT INSERTED.job_id
            VALUES (?, 'queued', ?, ?)
        """, user_id, input_blob_path, job_params)
        job_id = cur.fetchone()[0]

        cur.execute(
            "UPDATE users SET credits_remaining = credits_remaining - 20 WHERE user_id = ?",
            user_id,
        )
        conn.commit()   # releases the app lock
        return ReserveResult(True, job_id=job_id)
    finally:
        conn.close()
