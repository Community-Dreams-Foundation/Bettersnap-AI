"""Append-only credit ledger.

WHY THIS EXISTS
`users.credits_remaining` is a single mutable integer. Every grant / spend / refund was an
in-place UPDATE with no history, so a billing dispute ("I was charged but got no credits",
"my refund never came") was unresolvable — there was nothing to reconstruct the balance from.

This records ONE immutable row per balance change, IN THE SAME TRANSACTION as the UPDATE, so
the ledger and the balance can never disagree: if the balance change rolls back, so does its
ledger row. It reuses the pre-existing (previously dormant, zero-row) dbo.credit_transactions
table — no new table needed:
    transaction_id (PK) | user_id | amount (SIGNED) | transaction_type | job_id | created_at

`amount` is signed: positive for grants/refunds, negative for spends. `transaction_type` is a
stable reason code (see REASONS). `job_id` links the row to a job when the change is job-scoped
(spend at reserve, refund on failure); it is NULL for account-level events (purchases, renewals,
retrain charges — a training is not a job).
"""

# Stable reason codes. Keep these strings stable — they're the audit vocabulary.
REASON_PURCHASE_ONE_TIME = "purchase_one_time"
REASON_PURCHASE_MONTHLY = "purchase_monthly"
REASON_TOPUP = "topup"
REASON_RENEWAL = "renewal"
REASON_JOB_RESERVE = "job_reserve"         # spend at submit (negative)
REASON_JOB_REFUND = "job_refund"           # refund on no-fault failure (positive)
REASON_JOB_REFUND_WAITING = "job_refund_waiting"  # refund of parked waiting_lora jobs
REASON_RETRAIN_CHARGE = "retrain_charge"   # spend for a retrain (negative)


def record(cur, user_id, amount, transaction_type, job_id=None):
    """Append one ledger row on the CALLER's cursor, so it commits in the caller's transaction.

    amount: SIGNED delta (+grant/+refund, -spend). Zero is skipped (no-op events don't clutter
    the ledger). transaction_type: one of the REASON_* codes. job_id: optional job linkage.
    """
    amount = int(amount)
    if amount == 0:
        return
    cur.execute(
        "INSERT INTO credit_transactions (user_id, amount, transaction_type, job_id) "
        "VALUES (?, ?, ?, ?)",
        user_id, amount, transaction_type, job_id,
    )
