-- 015: record when a job was DISPATCHED, so the reaper measures the processing deadline
-- from when the GPU run started, not from when the job was submitted.
--
-- THE BUG (finding #5, part A): the reaper failed jobs where
--   status = 'processing' AND created_at < now - REAPER_STUCK_MINUTES
-- created_at is SUBMIT time, so the queue + dispatch WAIT counted against the processing
-- deadline. With MAX_ACTIVE_GPU_JOBS = 1 jobs serialize, so a large (Pro/Expert, many-image)
-- job that sat in the queue could cross the deadline WHILE HEALTHY and be reaped
-- (failed + refunded). Its later completion then no-ops against the guarded UPDATE
-- (see main.py, finding #5 part B), so the user sees 'failed' + a refund for images that
-- actually generated.
--
-- THE FIX: stamp dispatched_at when the dispatcher flips queued -> dispatching, and have the
-- reaper (and the ops stuck-dispatch scan) measure from COALESCE(dispatched_at, created_at) --
-- which excludes the queue wait. Rows written before this migration have dispatched_at NULL
-- and fall back to created_at (harmless — they are old, already past any deadline).
--
-- Idempotent, add-only nullable column (online, no backfill).

IF COL_LENGTH('dbo.jobs', 'dispatched_at') IS NULL
    ALTER TABLE dbo.jobs ADD dispatched_at DATETIME2 NULL;
GO

-- ── Verify ────────────────────────────────────────────────────────────────
--   SELECT COL_LENGTH('dbo.jobs', 'dispatched_at');   -- expect non-NULL
-- ── Rollback ──────────────────────────────────────────────────────────────
--   ALTER TABLE dbo.jobs DROP COLUMN dispatched_at;   -- only with the code reverted
