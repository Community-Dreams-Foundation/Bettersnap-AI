-- Dispatch idempotency: record the Container Apps execution started for a job so
-- a retried queue message (e.g. after a crash) can never start a SECOND A100 for
-- the same job_id. Run once against bettersnap-db.
--
-- The dispatch state machine uses the existing jobs.status column with values:
--   queued -> dispatching -> processing -> completed | failed
-- 'dispatching' is set by the backend the moment it claims a job (under the
-- global lease); the inference container then sets 'processing'.
IF COL_LENGTH('dbo.jobs', 'external_execution_id') IS NULL
    ALTER TABLE dbo.jobs ADD external_execution_id VARCHAR(128) NULL;

-- ── Rollback ──────────────────────────────────────────────────────────────
-- IF COL_LENGTH('dbo.jobs', 'external_execution_id') IS NOT NULL
--     ALTER TABLE dbo.jobs DROP COLUMN external_execution_id;
-- (Nullable add-only column — backfills as NULL, so this is a safe, online
--  change with no data migration. Roll back only with the code that reads it.)
