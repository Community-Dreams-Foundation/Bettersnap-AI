-- 007_retention.sql — plan-based data retention.
--
-- Deletes a user's BLOBS (photos, LoRA, generated results) after a retention window,
-- while KEEPING the DB rows (users/jobs/lora_trainings) for usage + revenue reporting.
--
--   One-time plans : window = last successful generation + 3 days.
--   Monthly plans  : kept while the subscription is active; on cancel, + 3 days.
--
-- users.retention_expires_at  — when this user's blobs become eligible for deletion.
--                               NULL = not scheduled (no generation yet, or active monthly).
-- jobs.expired                — set when a cleanup run deletes this job's result blobs,
--                               so History/Dashboard can render "expired" instead of a 404.
--
-- Idempotent: safe to re-run (guards on existence).

-- Filtered index below requires QUOTED_IDENTIFIER ON (sqlcmd defaults it OFF).
SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('dbo.users') AND name = 'retention_expires_at'
)
    ALTER TABLE dbo.users ADD retention_expires_at DATETIME2 NULL;
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('dbo.jobs') AND name = 'expired'
)
    ALTER TABLE dbo.jobs
        ADD expired BIT NOT NULL CONSTRAINT DF_jobs_expired DEFAULT 0;
GO

-- The hourly cleanup timer scans for due users by this column; index the lookup.
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes WHERE name = 'IX_users_retention_expires_at'
)
    CREATE INDEX IX_users_retention_expires_at
        ON dbo.users (retention_expires_at)
        WHERE retention_expires_at IS NOT NULL;
GO
