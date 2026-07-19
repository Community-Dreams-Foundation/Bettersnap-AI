-- 014: transactional outbox — make each DB state change and its queue send ATOMIC.
--
-- THE BUG (finding #4): submit_job / start_training / training-completion each COMMIT a
-- state change (job row + credit charge; or lora_status + retrain charge; or the parked
-- waiting_lora -> queued flip) and only AFTERWARD send the queue message, in a separate
-- call. If Queue Storage is briefly unavailable (or the process dies) after the commit,
-- the row is a charged orphan with no message — and the reapers only touch
-- 'processing'/'dispatching', so an ordinary 'queued' row (or a stuck 'training' user) is
-- never recovered.
--
-- THE FIX: write an outbox row IN THE SAME TRANSACTION as the state change (either both
-- land or neither). A retrying dispatcher (the outbox_dispatcher timer) then sends every
-- undelivered row and marks it delivered. Delivery is at-least-once — which Azure Storage
-- Queues already are — so the idempotent consumers (GPU dispatch guards on
-- external_execution_id + status) need no change.
--
-- Idempotent + GO-separated (see 003/004).

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE object_id = OBJECT_ID('dbo.outbox'))
    CREATE TABLE dbo.outbox (
        outbox_id          BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
        queue_name         NVARCHAR(100)  NOT NULL,   -- 'inference-jobs' | 'lora-training-jobs'
        payload            NVARCHAR(MAX)  NOT NULL,   -- the JSON message body to send
        visibility_timeout INT            NULL,       -- optional delayed-send seconds
        created_at         DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME(),
        delivered_at       DATETIME2      NULL,       -- set once the message is on the queue
        attempts           INT            NOT NULL DEFAULT 0,
        last_error         NVARCHAR(400)  NULL
    );
GO

-- The dispatcher scans ONLY undelivered rows, oldest first. A filtered index keeps that
-- scan tiny even as delivered rows accumulate (delivered rows are never scanned).
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_outbox_pending' AND object_id = OBJECT_ID('dbo.outbox'))
    CREATE INDEX IX_outbox_pending ON dbo.outbox (outbox_id) WHERE delivered_at IS NULL;
GO

-- ── Verify ────────────────────────────────────────────────────────────────
--   SELECT COUNT(*) FROM dbo.outbox WHERE delivered_at IS NULL;   -- pending backlog
-- ── Rollback ──────────────────────────────────────────────────────────────
--   DROP TABLE dbo.outbox;   -- only after the outbox code paths are removed/reverted
