"""Transactional outbox — make a DB state change and its queue send atomic.

The dual-write problem (finding #4): submit_job / start_training / training-completion
each COMMIT a state change and then send a queue message in a SEPARATE step. If the queue
is briefly down (or the process dies) after the commit, the row is a charged orphan with
no message, and the reapers don't recover ordinary 'queued' rows.

Fix in three parts:
  1. outbox_add(cur, ...) writes an outbox row using the CALLER'S cursor, so it commits in
     the SAME transaction as the state change — either both land or neither.
  2. outbox_try_send_now(...) is the fast path: right after the caller commits it best-effort
     sends the message and marks the row delivered, so normal latency is unchanged.
  3. outbox_dispatch_pending() is the retrying dispatcher (the outbox_dispatcher timer): it
     sends anything the fast path could not (queue was down, or the process died) and marks
     it delivered.

Delivery is at-least-once — which Azure Storage Queues already are (a message can redeliver
on visibility-timeout/retry) — so the consumers are already idempotent (GPU dispatch guards
on external_execution_id + status). The outbox adds no new idempotency burden.
"""
import json
import logging

from .db import new_connection
from .queue_client import _send


def outbox_add(cur, queue_name: str, payload: dict, visibility_timeout: int = None) -> int:
    """INSERT an outbox row using the CALLER'S cursor, so it commits atomically with the
    caller's state change. Returns the new outbox_id (for the fast-path send)."""
    cur.execute(
        "INSERT INTO outbox (queue_name, payload, visibility_timeout) "
        "OUTPUT INSERTED.outbox_id VALUES (?, ?, ?)",
        queue_name, json.dumps(payload), visibility_timeout,
    )
    return int(cur.fetchone()[0])


def _mark_delivered(outbox_id: int):
    conn = new_connection()
    try:
        conn.autocommit = True
        conn.cursor().execute(
            "UPDATE outbox SET delivered_at = SYSUTCDATETIME() "
            "WHERE outbox_id = ? AND delivered_at IS NULL",
            outbox_id,
        )
    finally:
        conn.close()


def _record_failure(outbox_id: int, err: str):
    conn = new_connection()
    try:
        conn.autocommit = True
        conn.cursor().execute(
            "UPDATE outbox SET attempts = attempts + 1, last_error = ? WHERE outbox_id = ?",
            (err or "")[:400], outbox_id,
        )
    finally:
        conn.close()


def outbox_try_send_now(outbox_id: int, queue_name: str, payload: dict,
                        visibility_timeout: int = None) -> bool:
    """Fast path: best-effort send right after the caller commits. NEVER raises — the state
    change already committed, so on ANY failure we just leave the row undelivered and let
    outbox_dispatch_pending() retry it. Returns True if sent+marked delivered."""
    if outbox_id is None:
        return False
    try:
        _send(queue_name, payload, visibility_timeout)
    except Exception as e:
        logging.warning(
            f"outbox fast-path send failed for outbox_id={outbox_id}; "
            f"the dispatcher will retry: {e}"
        )
        _record_failure(outbox_id, str(e))
        return False
    _mark_delivered(outbox_id)
    return True


def outbox_dispatch_pending(max_batch: int = 200, max_attempts: int = 50):
    """Send every undelivered outbox row (oldest first) and mark it delivered. On a send
    failure, bump attempts + record the error and leave the row for the next tick (rows past
    max_attempts are skipped so a permanently-bad message can't spin forever — they surface
    via last_error). Returns (sent, seen). Called by the outbox_dispatcher timer."""
    conn = new_connection()
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            "SELECT TOP (?) outbox_id, queue_name, payload, visibility_timeout "
            "FROM outbox WHERE delivered_at IS NULL AND attempts < ? ORDER BY outbox_id",
            max_batch, max_attempts,
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    sent = 0
    for outbox_id, queue_name, payload_json, vis in rows:
        try:
            _send(queue_name, json.loads(payload_json), vis)
        except Exception as e:
            logging.warning(f"outbox dispatch send failed outbox_id={outbox_id}: {e}")
            _record_failure(int(outbox_id), str(e))
            continue
        _mark_delivered(int(outbox_id))
        sent += 1

    if rows:
        logging.info(f"outbox_dispatcher: delivered {sent}/{len(rows)} pending message(s)")
    return sent, len(rows)
