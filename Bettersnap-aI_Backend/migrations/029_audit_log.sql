-- 029_audit_log.sql
-- SOC 2: comprehensive, append-only application audit trail covering authentication, uploads,
-- deletions, payments, and consent (complements admin_audit_log, which covers admin mutations).
-- Stores only references/ids and non-sensitive context in `detail` — NEVER PII or biometric data.
-- NOTE: this is append-only in the application layer; true WORM/immutability requires immutable
-- storage (Azure immutable blob / ledger table) — tracked as a separate infrastructure item.
IF OBJECT_ID('dbo.audit_log', 'U') IS NULL
    CREATE TABLE dbo.audit_log (
        event_id    INT IDENTITY(1,1) PRIMARY KEY,
        user_id     UNIQUEIDENTIFIER NULL,      -- acting user (NULL for system events)
        event_type  VARCHAR(48)   NOT NULL,     -- e.g. 'auth.register','photo.upload','payment.one_time'
        target      NVARCHAR(256) NULL,         -- id/ref acted on (job id, blob name, ...)
        detail      NVARCHAR(MAX) NULL,         -- JSON context (no PII / biometric content)
        ip          VARCHAR(64)   NULL,
        created_at  DATETIME2     NOT NULL
            CONSTRAINT DF_audit_log_created DEFAULT SYSUTCDATETIME()
    );

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_audit_log_user_time' AND object_id = OBJECT_ID('dbo.audit_log'))
    CREATE INDEX IX_audit_log_user_time ON dbo.audit_log (user_id, created_at DESC);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_audit_log_type_time' AND object_id = OBJECT_ID('dbo.audit_log'))
    CREATE INDEX IX_audit_log_type_time ON dbo.audit_log (event_type, created_at DESC);
