-- 006: retrain accounting.
--
-- Policy: the FIRST retrain is free (covers "my photos were bad" / "it doesn't look like
-- me"), every retrain after that costs credits. Retraining is the most expensive button in
-- the product — ~32 min of A100 EACH — and because MAX_ACTIVE_GPU_JOBS=1 it also blocks the
-- queue for every other user for that whole time. Free + unlimited would be a
-- denial-of-service on our own GPU, so it has to be metered.
--
-- retrain_count counts SUCCESSFUL retrain STARTS (not the initial training).
--   0 -> next retrain is free
--  >0 -> next retrain costs RETRAIN_CREDITS (shared/plans.py)
--
-- NOTE: GO batch separators are REQUIRED (see 003/004/005).

IF COL_LENGTH('dbo.users', 'retrain_count') IS NULL
    ALTER TABLE dbo.users ADD retrain_count INT NOT NULL
        CONSTRAINT DF_users_retrain_count DEFAULT 0;
GO

-- Backfill: nobody has retrained through the product yet (the endpoint did not exist),
-- so everyone still has their one free retrain available.
UPDATE dbo.users SET retrain_count = 0 WHERE retrain_count IS NULL;
GO

-- ── Rollback ──────────────────────────────────────────────────────────────
-- ALTER TABLE dbo.users DROP CONSTRAINT DF_users_retrain_count;
-- ALTER TABLE dbo.users DROP COLUMN retrain_count;
