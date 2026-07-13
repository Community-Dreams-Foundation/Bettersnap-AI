-- 003: Per-user plan + LoRA status on the existing users table.
--   plan_name   — which plan the user is on (basic|pro|expert|monthly). Drives
--                 image_count + selection limits, resolved from shared/plans.py.
--                 Defaults to 'basic' so any pre-existing row has a valid plan.
--   lora_status — identity-LoRA lifecycle for the 8-12-image trainer (Milestone 2):
--                 none|training|ready|failed. Wired now, populated later.
-- credits_remaining already exists (int) and is now decremented PER IMAGE
-- (image_count * plan.credits_per_image) instead of once per job.
-- Idempotent and safe to re-run.
--
-- NOTE: GO batch separators are REQUIRED. SQL Server compiles a whole batch before
-- executing it, so the ADD COLUMN and any later statement referencing that column
-- must be in SEPARATE batches — otherwise deferred name resolution fails with
-- "Invalid column name 'plan_name'". Apply via sqlcmd or the portal query editor
-- (both honor GO); do NOT feed this file to a runner that ignores GO.

IF COL_LENGTH('dbo.users', 'plan_name') IS NULL
    ALTER TABLE dbo.users ADD plan_name VARCHAR(32) NOT NULL
        CONSTRAINT DF_users_plan_name DEFAULT 'basic';
GO

IF COL_LENGTH('dbo.users', 'lora_status') IS NULL
    ALTER TABLE dbo.users ADD lora_status VARCHAR(16) NULL;
GO

-- Defensive backfill (NOT NULL + DEFAULT already fills existing rows on ADD, but
-- this repairs any row left blank by a prior partial run). Separate batch so
-- plan_name is guaranteed to exist at compile time.
UPDATE dbo.users SET plan_name = 'basic'
WHERE plan_name IS NULL OR LTRIM(RTRIM(plan_name)) = '';
GO

-- ── Rollback ──────────────────────────────────────────────────────────────
-- ALTER TABLE dbo.users DROP CONSTRAINT DF_users_plan_name;
-- ALTER TABLE dbo.users DROP COLUMN plan_name;
-- ALTER TABLE dbo.users DROP COLUMN lora_status;
