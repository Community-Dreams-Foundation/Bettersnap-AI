-- 024_credit_ledger.sql — formalize dbo.credit_transactions as the append-only credit ledger.
--
-- The table already existed (created in 000_baseline) but was DORMANT — never written to
-- (0 rows). shared/credit_ledger.py now appends one signed row to it on every credit change
-- (grant / spend / refund), in the same transaction as the balance UPDATE. This migration
-- makes that role explicit in the schema history and — the substantive part — adds the two
-- indexes the ledger is actually read by. The base table only has a PRIMARY KEY on
-- transaction_id, so both audit queries below would table-scan a table that only grows.
--
-- Idempotent: the table guard is a no-op on the existing prod DB; each index is created only
-- if absent. Safe to run repeatedly and on a fresh build.

-- Defensive: ensure the ledger table exists even if 000_baseline was somehow skipped.
IF OBJECT_ID('dbo.credit_transactions', 'U') IS NULL
    CREATE TABLE dbo.credit_transactions (
        transaction_id   UNIQUEIDENTIFIER NOT NULL CONSTRAINT DF_credit_tx_id DEFAULT NEWID(),
        user_id          UNIQUEIDENTIFIER NOT NULL,
        amount           INT NULL,
        transaction_type NVARCHAR(50) NULL,
        job_id           UNIQUEIDENTIFIER NULL,
        created_at       DATETIME2 NULL CONSTRAINT DF_credit_tx_created DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_credit_transactions PRIMARY KEY (transaction_id)
    );
GO

-- Per-user audit trail: "every credit event for this user, newest first" (dispute handling,
-- reconstructing a balance). Without this, a per-user ledger read scans the whole table.
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_credit_tx_user_created' AND object_id = OBJECT_ID('dbo.credit_transactions'))
    CREATE INDEX IX_credit_tx_user_created
        ON dbo.credit_transactions (user_id, created_at);
GO

-- Per-job trail: reconcile the spend + any refund for one job_id (support / QA).
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_credit_tx_job' AND object_id = OBJECT_ID('dbo.credit_transactions'))
    CREATE INDEX IX_credit_tx_job
        ON dbo.credit_transactions (job_id);
GO
