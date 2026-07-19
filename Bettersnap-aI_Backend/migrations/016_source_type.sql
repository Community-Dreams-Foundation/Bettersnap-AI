-- 016: tag each job and training with the PRODUCT it belongs to.
--
-- source_type: 'monthly' | 'one_time' | 'topup'
--   monthly   — a generation session (or its training) under an active monthly subscription
--   one_time  — a fused one-time pack (train + generate), ephemeral
--   topup     — credits added to an active monthly plan (reserved for the top-up flow; a
--               top-up's actual generation is a monthly session, so jobs are 'monthly'/'one_time')
--
-- This is the FOUNDATION for finding #6's redesign: the purchase gate (one active product at a
-- time), the per-product RETENTION rules (one-time = delete LoRA + images 3 days after last
-- generation; monthly = keep until the plan ends), and the LoRA lifecycle (monthly persistent
-- vs one-time ephemeral) all need to know which product a job/training belongs to.
--
-- Nullable + add-only (online, no backfill). Existing rows stay NULL; the code treats a NULL
-- source_type as legacy and defaults its behaviour to the safe/one-time path until backfilled.

IF COL_LENGTH('dbo.jobs', 'source_type') IS NULL
    ALTER TABLE dbo.jobs ADD source_type NVARCHAR(20) NULL;
GO

IF COL_LENGTH('dbo.lora_trainings', 'source_type') IS NULL
    ALTER TABLE dbo.lora_trainings ADD source_type NVARCHAR(20) NULL;
GO

-- ── Verify ────────────────────────────────────────────────────────────────
--   SELECT COL_LENGTH('dbo.jobs','source_type'), COL_LENGTH('dbo.lora_trainings','source_type');
-- ── Rollback ──────────────────────────────────────────────────────────────
--   ALTER TABLE dbo.jobs DROP COLUMN source_type;
--   ALTER TABLE dbo.lora_trainings DROP COLUMN source_type;
