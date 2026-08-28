-- 027_admin_audit_log.sql — immutable audit trail for super-admin mutations.
-- Every admin action that changes state (credit adjust, suspend, refund, job retry, ...) appends
-- one row here via _write_audit(). Idempotent.
IF OBJECT_ID('dbo.admin_audit_log','U') IS NULL
    CREATE TABLE dbo.admin_audit_log (
        event_id       UNIQUEIDENTIFIER NOT NULL CONSTRAINT DF_audit_id DEFAULT NEWID()
                         CONSTRAINT PK_admin_audit_log PRIMARY KEY,
        actor_id       NVARCHAR(100) NULL,   -- admin Entra oid
        actor_email    NVARCHAR(256) NULL,
        action         NVARCHAR(100) NOT NULL,
        target_type    NVARCHAR(50) NULL,
        target_id      NVARCHAR(100) NULL,
        previous_value NVARCHAR(MAX) NULL,
        new_value      NVARCHAR(MAX) NULL,
        reason         NVARCHAR(500) NULL,
        result         NVARCHAR(50) NULL,
        created_at     DATETIME2 NOT NULL CONSTRAINT DF_audit_created DEFAULT SYSUTCDATETIME());
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_audit_created' AND object_id=OBJECT_ID('dbo.admin_audit_log'))
    CREATE INDEX IX_audit_created ON dbo.admin_audit_log(created_at DESC);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_audit_target' AND object_id=OBJECT_ID('dbo.admin_audit_log'))
    CREATE INDEX IX_audit_target ON dbo.admin_audit_log(target_type, target_id);
GO
