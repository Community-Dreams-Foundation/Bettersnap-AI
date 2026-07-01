"""Global GPU dispatch lease.

A single-row SQL lease that serializes "check active jobs -> start an A100 job"
across ALL scaled-out Function instances, closing the race where two instances
both see 0 active jobs and both start a GPU job. SQL is the one shared source of
truth, so a TTL row-lease here is a true global lock; host.json batchSize only
limits a single instance.

Requires the GpuDispatchLease table (see migrations/001_gpu_dispatch_lease.sql).
"""
import os
import uuid
from .db import new_connection

LEASE_NAME = "gpu-dispatch"
LEASE_TTL_SECONDS = int(os.environ.get("GPU_LEASE_TTL_SECONDS", "180"))
# A just-started job may not appear in the Container Apps executions API for a
# few seconds (eventual consistency). During this window treat it as active so a
# second job can't slip in.
DISPATCH_GRACE_SECONDS = int(os.environ.get("GPU_DISPATCH_GRACE_SECONDS", "60"))


class DispatchConfigError(Exception):
    """The lease table or its singleton row is missing — a deploy/config error.
    Callers must NOT start a job and must NOT defer forever; fail loudly instead
    (DISPATCH_CONFIG_ERROR) so the broken deploy is visible."""


def acquire_dispatch_lease():
    """Atomically acquire the lease. The single UPDATE is the critical section:
    only one caller can flip a free/expired lease to owned. TTL auto-releases a
    crashed holder so the queue can never deadlock permanently.

    Returns:
        owner token -> acquired.
        None        -> lease currently HELD by someone else (caller should defer).
    Raises:
        DispatchConfigError -> lease row/table MISSING (deploy error: fail loud,
                               never start, never infinite-defer).
    """
    owner = uuid.uuid4().hex
    conn = new_connection()
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                """
                UPDATE GpuDispatchLease
                SET owner_id = ?, expires_at = DATEADD(SECOND, ?, GETUTCDATE())
                WHERE lease_name = ?
                  AND (expires_at IS NULL OR expires_at < GETUTCDATE())
                """,
                owner, LEASE_TTL_SECONDS, LEASE_NAME,
            )
        except Exception as e:  # table missing / DB error during acquire
            conn.rollback()
            raise DispatchConfigError(f"lease acquire failed: {e}") from e

        if cur.rowcount == 1:
            conn.commit()
            return owner

        # 0 rows updated: either the lease is HELD (row exists, unexpired) or the
        # singleton row is MISSING. Distinguish so a missing row fails loud.
        conn.commit()
        cur.execute("SELECT COUNT(*) FROM GpuDispatchLease WHERE lease_name = ?", LEASE_NAME)
        if cur.fetchone()[0] != 1:
            raise DispatchConfigError(
                f"GpuDispatchLease row '{LEASE_NAME}' missing — run migration 001"
            )
        return None  # genuinely held
    finally:
        conn.close()


def release_dispatch_lease(owner: str):
    conn = new_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE GpuDispatchLease SET owner_id = NULL, expires_at = NULL "
            "WHERE lease_name = ? AND owner_id = ?",
            LEASE_NAME, owner,
        )
        conn.commit()
    finally:
        conn.close()


def mark_dispatched(owner: str):
    """Stamp the moment an A100 job was started so recent_dispatch_pending()
    counts it as active until the executions API catches up."""
    conn = new_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE GpuDispatchLease SET last_dispatch_at = GETUTCDATE() "
            "WHERE lease_name = ? AND owner_id = ?",
            LEASE_NAME, owner,
        )
        conn.commit()
    finally:
        conn.close()


def recent_dispatch_pending() -> bool:
    """True if a job was dispatched within the grace window (executions API may
    not list it yet)."""
    conn = new_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM GpuDispatchLease WHERE lease_name = ? "
            "AND last_dispatch_at IS NOT NULL "
            "AND last_dispatch_at > DATEADD(SECOND, ?, GETUTCDATE())",
            LEASE_NAME, -DISPATCH_GRACE_SECONDS,
        )
        return cur.fetchone() is not None
    finally:
        conn.close()
