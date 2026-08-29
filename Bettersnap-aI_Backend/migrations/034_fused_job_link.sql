-- 034: Persist the EXACT generation job a fused train_infer run is bound to.
--
-- THE DEFECTS THIS FIXES (independent of retry)
-- When a training completes, the dispatcher currently finds the generation job to fuse with:
--
--     SELECT TOP 1 job_id FROM jobs WHERE user_id = ? AND status = 'waiting_lora'
--     ORDER BY created_at
--
-- That query IS ordered by created_at. Three defects remain:
--   1. no job_id tie-break: two rows sharing a created_at value order arbitrarily, so which of
--      that pair gets fused is undefined;
--   2. no persisted linkage: the chosen job lives only in a local variable, so nothing
--      downstream can prove which job this training was bound to;
--   3. re-selection per attempt: a later dispatch or a retry runs the query again and may pick
--      a DIFFERENT job than the first attempt claimed.
--
-- This migration adds the real link, so the binding is decided ONCE and deterministically
-- (ORDER BY created_at, job_id) and every later step (reclaim, pre-container retry, un-claim)
-- uses that exact job instead of re-selecting.
--
-- LIFECYCLE
--   allocation : first dispatch locks the training row; if fused_job_id IS NULL it selects one
--                eligible job deterministically (ORDER BY created_at, job_id) and writes the
--                link in the SAME transaction as the waiting_lora -> processing claim.
--   reclaim    : later dispatches read fused_job_id and reclaim ONLY that job. Never re-select.
--   un-claim   : ACA start failure / pre-container retry moves that exact job
--                processing -> waiting_lora and RETAINS fused_job_id, so the next attempt
--                reclaims the same job rather than silently substituting another.
--   cleared    : never on the success path. fused_job_id is retained after completion as
--                permanent audit linkage answering "which generation did this adapter feed?".
--                It is cleared only if the linked job is hard-deleted, which the FK enforces by
--                blocking that delete while the link exists.
--   NULL       : not a fused run, or a historical row predating this migration. NULL is NOT a
--                licence to fall back to the user_id/status query — fused retry treats a NULL
--                link as ineligible and fails/refunds safely.
--
-- Restart-safe: column, FK and index are each guarded, so re-running is a no-op.

------------------------------------------------------------------------------
-- The link column
------------------------------------------------------------------------------
IF COL_LENGTH('dbo.lora_trainings', 'fused_job_id') IS NULL
    ALTER TABLE dbo.lora_trainings ADD fused_job_id UNIQUEIDENTIFIER NULL;
GO

------------------------------------------------------------------------------
-- Trusted FK. WITH CHECK (not WITH NOCHECK) so SQL Server validates existing rows and marks the
-- constraint trusted — every existing row is NULL, so validation is free. A trusted FK is what
-- lets the optimiser rely on it and, more importantly here, guarantees fused_job_id can never
-- point at a job row that does not exist.
------------------------------------------------------------------------------
-- The existence check is scoped by parent_object_id: sys.foreign_keys.name is unique per
-- schema, not globally, and an unscoped name match could see a same-named constraint on a
-- different table and wrongly skip creating this one.
IF OBJECT_ID('dbo.lora_trainings', 'U') IS NOT NULL
   AND OBJECT_ID('dbo.jobs', 'U') IS NOT NULL
   AND NOT EXISTS (
        SELECT 1 FROM sys.foreign_keys
        WHERE name = 'FK_lora_trainings_fused_job'
          AND parent_object_id = OBJECT_ID('dbo.lora_trainings'))
    ALTER TABLE dbo.lora_trainings WITH CHECK
        ADD CONSTRAINT FK_lora_trainings_fused_job
        FOREIGN KEY (fused_job_id) REFERENCES dbo.jobs (job_id);
GO

-- A constraint of that name may ALREADY exist from a partial or hand-run apply. Two cases:
--   * right shape but untrusted (created WITH NOCHECK, or disabled) -> re-validate it. Until
--     it is trusted, SQL Server does not guarantee existing rows satisfy it, so fused_job_id
--     could already point at a non-existent job.
--   * wrong shape (not referencing dbo.jobs.job_id, or not on fused_job_id) -> FAIL LOUDLY.
--     Silently accepting it would leave the link unenforced while appearing correct.
IF EXISTS (SELECT 1 FROM sys.foreign_keys
           WHERE name = 'FK_lora_trainings_fused_job'
             AND parent_object_id = OBJECT_ID('dbo.lora_trainings'))
BEGIN
    -- Exactly one column pair. A COMPOSITE key of the right columns plus extras would satisfy a
    -- naive EXISTS check while enforcing something different.
    DECLARE @fk_col_count INT = (
        SELECT COUNT(*)
        FROM sys.foreign_keys fk
        JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
        WHERE fk.name = 'FK_lora_trainings_fused_job'
          AND fk.parent_object_id = OBJECT_ID('dbo.lora_trainings'));

    -- Precise mapping AND referential actions. ON DELETE CASCADE would let deleting a job
    -- silently destroy the training's linkage; ON DELETE SET NULL would silently unlink it.
    -- Both must be NO_ACTION so the FK blocks the delete instead.
    DECLARE @fk_ok INT = (
        SELECT COUNT(*)
        FROM sys.foreign_keys fk
        JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
        WHERE fk.name = 'FK_lora_trainings_fused_job'
          AND fk.parent_object_id = OBJECT_ID('dbo.lora_trainings')
          AND fk.referenced_object_id = OBJECT_ID('dbo.jobs')
          AND COL_NAME(fkc.parent_object_id, fkc.parent_column_id) = 'fused_job_id'
          AND COL_NAME(fkc.referenced_object_id, fkc.referenced_column_id) = 'job_id'
          AND fk.delete_referential_action_desc = 'NO_ACTION'
          AND fk.update_referential_action_desc = 'NO_ACTION');

    IF @fk_col_count <> 1 OR @fk_ok <> 1
        THROW 50034, 'FK_lora_trainings_fused_job exists with an unexpected shape. Expected exactly one column pair dbo.lora_trainings(fused_job_id) -> dbo.jobs(job_id) with NO_ACTION on delete and update. Inspect and drop it before re-running migration 034.', 1;

    -- Idempotent: re-validating an already-trusted constraint is a no-op. Required because a
    -- constraint created WITH NOCHECK (or disabled) does not guarantee existing rows satisfy it.
    ALTER TABLE dbo.lora_trainings WITH CHECK CHECK CONSTRAINT FK_lora_trainings_fused_job;
END;
GO

------------------------------------------------------------------------------
-- Filtered unique index: one generation job can be fused to at most ONE training. The WHERE
-- clause is required — a plain UNIQUE index would treat multiple NULLs as duplicates in SQL
-- Server and reject every non-fused training row after the first.
------------------------------------------------------------------------------
-- Validate a PRE-EXISTING index of this name before accepting it. Name + object_id alone is not
-- enough: a non-unique, unfiltered, disabled, wrong-key, composite-key or INCLUDE-bearing index
-- would all pass that check while enforcing something other than "one job fuses to one training".
IF EXISTS (SELECT 1 FROM sys.indexes
           WHERE name = 'UX_lora_trainings_fused_job'
             AND object_id = OBJECT_ID('dbo.lora_trainings'))
BEGIN
    DECLARE @ix_ok INT = (
        SELECT COUNT(*)
        FROM sys.indexes i
        WHERE i.name = 'UX_lora_trainings_fused_job'
          AND i.object_id = OBJECT_ID('dbo.lora_trainings')
          AND i.is_unique = 1
          AND i.is_disabled = 0
          AND i.has_filter = 1
          -- filter_definition renders as '([fused_job_id] IS NOT NULL)'; strip brackets and
          -- whitespace so an equivalent filter written differently still matches.
          AND REPLACE(REPLACE(REPLACE(REPLACE(i.filter_definition, '[', ''), ']', ''), '(', ''), ')', '')
              = 'fused_job_id IS NOT NULL'
          -- exactly one KEY column, and it is fused_job_id
          AND (SELECT COUNT(*) FROM sys.index_columns ic
               WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id
                 AND ic.is_included_column = 0) = 1
          AND EXISTS (SELECT 1 FROM sys.index_columns ic
                      WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id
                        AND ic.is_included_column = 0
                        AND COL_NAME(ic.object_id, ic.column_id) = 'fused_job_id')
          -- no INCLUDE columns
          AND (SELECT COUNT(*) FROM sys.index_columns ic
               WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id
                 AND ic.is_included_column = 1) = 0);

    IF @ix_ok <> 1
        THROW 50035, 'UX_lora_trainings_fused_job exists with an unexpected shape. Expected a UNIQUE, enabled, single-key index on dbo.lora_trainings(fused_job_id) filtered WHERE fused_job_id IS NOT NULL, with no included columns. Inspect and drop it before re-running migration 034.', 1;
END
ELSE IF OBJECT_ID('dbo.lora_trainings', 'U') IS NOT NULL
    CREATE UNIQUE INDEX UX_lora_trainings_fused_job
        ON dbo.lora_trainings (fused_job_id)
        WHERE fused_job_id IS NOT NULL;
GO
