-- 037: Unify the legacy credit mirror with the authoritative per-type buckets.
--
-- WHY THIS EXISTS
-- dbo.users carries THREE credit columns: credits_remaining (the original single balance)
-- plus monthly_credits_remaining / one_time_credits_remaining (the per-type buckets that
-- superseded it). reserve_job_slot forks on jobs.source_type:
--
--   source_type = 'one_time'  ->  debits the bucket AND credits_remaining (the mirror)
--   source_type IS NULL       ->  debits credits_remaining ONLY (legacy path)
--
-- A user whose jobs alternated between those paths had credits_remaining debited twice
-- for work charged once, so the mirror drifted below the buckets and eventually went
-- negative (vvr0408: credits_remaining = -19 while one_time_credits_remaining = 40).
--
-- WHAT THE AUDIT FOUND, AND WHY THIS IS NOT A BLANKET RECOMPUTE
-- 24 of 52 users show credits_remaining <> monthly + one_time, but only ONE is corrupt.
-- The other 23 are LEGITIMATE legacy balances that live only in credits_remaining:
--   * 13 trial users holding their 4 free registration credits
--   * 3 monthly users holding 150 / 125 / 100 from grants that predate the buckets
-- Setting credits_remaining = monthly + one_time across the board would have ZEROED all
-- of them. So step 2 MOVES those balances into a bucket instead, and step 3 is
-- RAISE-ONLY: its WHERE clause matches only rows where the mirror sits BELOW the
-- buckets, which is precisely the double-debit signature. No user can lose credits here.
--
-- TRANSACTION OWNERSHIP
-- This file deliberately contains NO BEGIN/COMMIT/ROLLBACK. scripts/run_migrations.py
-- already wraps each migration in one transaction and records the tracking row inside
-- it. An inner BEGIN TRANSACTION would nest, and an inner ROLLBACK unwinds EVERY level
-- in SQL Server -- after which the runner, unaware, would still INSERT the tracking row
-- and COMMIT, recording 037 as applied when it had actually rolled itself back. On a
-- migration that moves money that failure mode is unacceptable, so verification THROWs
-- instead: the runner catches it, rolls back, and writes no tracking row.
--
-- Leaves org-funded members alone -- their credits live in organization_members.
--
-- AFTER THIS RUNS the invariant credits_remaining = monthly + one_time holds for every
-- non-org user, which is the precondition for making the mirror derived in code and
-- deleting the legacy debit branch in reserve_job_slot.

SET XACT_ABORT ON;
GO

-- 1. Reversible snapshot -------------------------------------------------------
-- Captured BEFORE any write, for every row this migration could touch. The rollback
-- script at the foot of this file restores from it verbatim.
IF OBJECT_ID('dbo.users_credit_backfill_037', 'U') IS NULL
BEGIN
    SELECT
        user_id,
        email,
        subscription_type,
        credits_remaining              AS old_credits_remaining,
        monthly_credits_remaining      AS old_monthly_credits_remaining,
        one_time_credits_remaining     AS old_one_time_credits_remaining,
        SYSUTCDATETIME()               AS captured_at
    INTO dbo.users_credit_backfill_037
    FROM dbo.users
    WHERE credits_remaining
          <> (ISNULL(monthly_credits_remaining, 0) + ISNULL(one_time_credits_remaining, 0));
END;
GO

-- 2. Move legacy balances into the one-time bucket -----------------------------
-- Only where BOTH buckets are empty and the legacy column holds real credits, so this
-- cannot double-count an account that already has bucket balances. This is what
-- preserves the 13 trial users' 4 credits and the 3 monthly users' 150/125/100.
UPDATE dbo.users
SET    one_time_credits_remaining = credits_remaining
WHERE  ISNULL(monthly_credits_remaining, 0) = 0
  AND  ISNULL(one_time_credits_remaining, 0) = 0
  AND  credits_remaining > 0;
GO

-- 3. Repair mirrors that drifted below the authoritative bucket total ----------
-- RAISE-ONLY. The WHERE clause restricts this to rows where the mirror is LOWER than
-- the buckets, which is exactly the double-debit signature (vvr0408: -19 -> 40). A row
-- whose mirror is HIGHER is a legacy balance and is left untouched by design.
UPDATE dbo.users
SET    credits_remaining = ISNULL(monthly_credits_remaining, 0)
                         + ISNULL(one_time_credits_remaining, 0)
WHERE  credits_remaining < (ISNULL(monthly_credits_remaining, 0)
                          + ISNULL(one_time_credits_remaining, 0));
GO

-- 4. Verification --------------------------------------------------------------
-- Both counts MUST be 0. THROW (not ROLLBACK) so the runner owns the unwind and no
-- tracking row is written -- see TRANSACTION OWNERSHIP above.
DECLARE @negatives int, @drifted int, @msg nvarchar(400);

SELECT @negatives = COUNT(*)
FROM   dbo.users
WHERE  credits_remaining < 0
   OR  ISNULL(monthly_credits_remaining, 0) < 0
   OR  ISNULL(one_time_credits_remaining, 0) < 0;

SELECT @drifted = COUNT(*)
FROM   dbo.users
WHERE  credits_remaining
       <> (ISNULL(monthly_credits_remaining, 0) + ISNULL(one_time_credits_remaining, 0));

IF @negatives <> 0 OR @drifted <> 0
BEGIN
    SET @msg = CONCAT('037 verification FAILED: negatives=', @negatives,
                      ' drifted=', @drifted, ' - migration rolled back, nothing recorded.');
    THROW 50037, @msg, 1;
END;
GO

-- ── Verify (read-only, after applying) ────────────────────────────────────────
--   SELECT COUNT(*) FROM dbo.users WHERE credits_remaining < 0;                  -- 0
--   SELECT COUNT(*) FROM dbo.users
--    WHERE credits_remaining
--          <> (ISNULL(monthly_credits_remaining,0) + ISNULL(one_time_credits_remaining,0)); -- 0
--   SELECT COUNT(*) FROM dbo.users_credit_backfill_037;                          -- rows touched
--
-- ── Rollback ──────────────────────────────────────────────────────────────────
--   BEGIN TRANSACTION;
--   UPDATE u SET u.credits_remaining          = b.old_credits_remaining,
--                u.monthly_credits_remaining  = b.old_monthly_credits_remaining,
--                u.one_time_credits_remaining = b.old_one_time_credits_remaining
--     FROM dbo.users u
--     JOIN dbo.users_credit_backfill_037 b ON b.user_id = u.user_id;
--   -- DELETE FROM dbo.schema_migrations WHERE filename = '037_unify_credit_buckets.sql';
--   COMMIT TRANSACTION;
