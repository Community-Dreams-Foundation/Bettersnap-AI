-- 004: per-user identity-LoRA training runs.
--
-- One row per training attempt. Mirrors the `jobs` table's role for inference:
-- it is the dispatch-idempotency record (external_execution_id) AND the thing the
-- training watcher polls to decide when users.lora_status flips to ready/failed.
--
-- users.lora_status (added in 003, never populated until now) is the FAST path the
-- frontend and /jobs/submit read: none | training | ready | failed. This table is the
-- audit/dispatch detail behind it.
--
-- NOTE: GO batch separators are REQUIRED (see 003). Apply via sqlcmd or the portal
-- query editor — do NOT feed this to a runner that ignores GO.

IF OBJECT_ID('dbo.lora_trainings', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.lora_trainings (
        training_id           UNIQUEIDENTIFIER NOT NULL PRIMARY KEY DEFAULT NEWID(),
        user_id               UNIQUEIDENTIFIER NOT NULL,
        -- queued | dispatching | training | completed | failed
        status                VARCHAR(16)      NOT NULL,
        external_execution_id VARCHAR(128)     NULL,
        photo_count           INT              NULL,
        class_word            VARCHAR(16)      NULL,   -- woman | man | person
        files_json            NVARCHAR(MAX)    NULL,   -- exactly what was sent to the trainer
        error                 NVARCHAR(1000)   NULL,
        created_at            DATETIME2        NOT NULL DEFAULT GETUTCDATE(),
        completed_at          DATETIME2        NULL
    );
END;
GO

-- The watcher scans by status; /train checks "is this user already training?".
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_lora_trainings_status')
    CREATE INDEX IX_lora_trainings_status ON dbo.lora_trainings (status);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_lora_trainings_user_status')
    CREATE INDEX IX_lora_trainings_user_status ON dbo.lora_trainings (user_id, status);
GO

-- Every existing user predates the trainer, so none of them has an adapter.
-- 'none' (not NULL) so /jobs/submit's gate reads a real value for everyone.
UPDATE dbo.users SET lora_status = 'none'
WHERE lora_status IS NULL OR LTRIM(RTRIM(lora_status)) = '';
GO

-- ⚠ REQUIRED FOLLOW-UP — users who ALREADY have an adapter in blob storage.
-- The blanket 'none' above is correct for everyone who has never trained, but it would
-- lock an already-trained user out of /jobs/submit (the gate would tell them to train
-- first). SQL cannot see blob storage, so this backfill is manual and deliberate.
--
-- Check what exists:
--   az storage blob list --account-name bettersnapaistorage -c lora-weights \
--     --prefix identity/ --account-key <key> --query "[].name" -o tsv
--
-- Then mark each verified user through an operator-controlled repair, not this
-- production migration. Migration files must never embed test-account identities.

-- ── Rollback ──────────────────────────────────────────────────────────────
-- DROP TABLE IF EXISTS dbo.lora_trainings;
-- UPDATE dbo.users SET lora_status = NULL;
-- (Roll back only together with the code that reads lora_status, or /jobs/submit
--  will park every job in 'waiting_lora' forever.)
