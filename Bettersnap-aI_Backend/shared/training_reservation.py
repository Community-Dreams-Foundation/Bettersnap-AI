"""Serialized LoRA-training reservation and retrain charging."""
import json
from .db import new_connection
from .outbox import outbox_add
from .queue_client import TRAINING_QUEUE
from .plans import get_plan
from . import credit_ledger


class TrainingReserveResult:
    def __init__(self, ok, training_id=None, reason=None, outbox_id=None,
                 message=None, retrain=False, credits_charged=0):
        self.ok = ok
        self.training_id = training_id
        self.reason = reason
        self.outbox_id = outbox_id
        self.message = message
        self.retrain = retrain
        self.credits_charged = credits_charged


def reserve_training_slot(user_id, files, class_word, force, free_retrains,
                          retrain_credits, max_per_day, lock_timeout_ms=5000):
    """Recheck eligibility + cap, insert, status flip, charge, and outbox atomically."""
    conn = new_connection()
    try:
        conn.autocommit = False
        cur = conn.cursor()
        cur.execute(
            "DECLARE @r int; EXEC @r = sp_getapplock @Resource = ?, "
            "@LockMode = 'Exclusive', @LockOwner = 'Transaction', @LockTimeout = ?; SELECT @r",
            f"train-user:{user_id}", lock_timeout_ms,
        )
        if cur.fetchone()[0] < 0:
            conn.rollback()
            return TrainingReserveResult(False, reason="busy")

        cur.execute(
            "SELECT lora_status, credits_remaining, retrain_count, plan_name, "
            "one_time_credits_remaining, monthly_credits_remaining "
            "FROM users WITH (UPDLOCK, HOLDLOCK) WHERE user_id = ?",
            user_id,
        )
        row = cur.fetchone()
        if not row:
            conn.rollback()
            return TrainingReserveResult(False, reason="user_missing")

        status = (row[0] or "none").strip()
        legacy = int(row[1] or 0)
        retrain_count = int(row[2] or 0)
        one_time_bal = int(row[4] or 0)
        monthly_bal = int(row[5] or 0)
        # Spendable balance = the buckets (what generation debits), NOT the legacy column: a
        # one-time top-up lands in one_time_credits_remaining and leaves credits_remaining at 0,
        # so checking/charging legacy here blocked retrains for a fully-funded account. Fall back
        # to legacy only for pre-bucket users whose balance never moved into a bucket.
        bucket_total = one_time_bal + monthly_bal
        effective = bucket_total if bucket_total > 0 else legacy
        if status == "training":
            conn.rollback()
            return TrainingReserveResult(False, reason="already_training")
        if status == "ready" and not force:
            conn.rollback()
            return TrainingReserveResult(False, reason="force_required")

        is_retrain = status in ("ready", "failed") and force
        charge = retrain_credits if is_retrain and retrain_count >= free_retrains else 0
        if effective < charge:
            conn.rollback()
            return TrainingReserveResult(False, reason="credits")

        cur.execute(
            "SELECT COUNT(*) FROM lora_trainings "
            "WHERE user_id = ? AND created_at >= CAST(GETUTCDATE() AS DATE)",
            user_id,
        )
        if int(cur.fetchone()[0]) >= max_per_day:
            conn.rollback()
            return TrainingReserveResult(False, reason="daily_cap")

        files_json = json.dumps(files)
        source_type = get_plan(row[3]).plan_type
        cur.execute(
            "INSERT INTO lora_trainings "
            "(user_id, status, photo_count, class_word, files_json, source_type) "
            "OUTPUT INSERTED.training_id VALUES (?, 'queued', ?, ?, ?, ?)",
            user_id, len(files), class_word, files_json, source_type,
        )
        training_id = cur.fetchone()[0]
        if is_retrain:
            # Charge from the SAME buckets generation debits (mirror reserve_job_slot): monthly
            # first then one-time overflow for monthly plans, one-time for one-time plans; keep
            # the legacy mirror in sync. Pre-bucket users (no bucket balance) charge legacy only.
            if bucket_total > 0:
                monthly_debit = min(monthly_bal, charge) if source_type == "monthly" else 0
                one_time_debit = charge - monthly_debit
            else:
                monthly_debit = 0
                one_time_debit = 0
            cur.execute(
                "UPDATE users SET lora_status = 'training', "
                "retrain_count = retrain_count + 1, "
                "monthly_credits_remaining = monthly_credits_remaining - ?, "
                "one_time_credits_remaining = one_time_credits_remaining - ?, "
                "credits_remaining = credits_remaining - ? WHERE user_id = ?",
                monthly_debit, one_time_debit, charge, user_id,
            )
            # Ledger the retrain spend (record() no-ops when charge==0, i.e. a free retrain).
            credit_ledger.record(cur, user_id, -charge, credit_ledger.REASON_RETRAIN_CHARGE)
        else:
            cur.execute("UPDATE users SET lora_status = 'training' WHERE user_id = ?", user_id)

        message = {"training_id": str(training_id), "user_id": str(user_id)}
        outbox_id = outbox_add(cur, TRAINING_QUEUE, message)
        conn.commit()
        return TrainingReserveResult(
            True, training_id=training_id, outbox_id=outbox_id, message=message,
            retrain=is_retrain, credits_charged=charge,
        )
    finally:
        conn.close()
