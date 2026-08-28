-- 028_admin_user_status_and_notes.sql — user account status (suspend) + internal support notes.
IF COL_LENGTH('dbo.users','suspended_at') IS NULL
    ALTER TABLE dbo.users ADD suspended_at DATETIME2 NULL;  -- NULL = active
GO
IF OBJECT_ID('dbo.admin_user_notes','U') IS NULL
    CREATE TABLE dbo.admin_user_notes (
        note_id     UNIQUEIDENTIFIER NOT NULL CONSTRAINT DF_note_id DEFAULT NEWID()
                      CONSTRAINT PK_admin_user_notes PRIMARY KEY,
        user_id     UNIQUEIDENTIFIER NOT NULL,
        admin_id    NVARCHAR(100) NULL,
        admin_email NVARCHAR(256) NULL,
        note        NVARCHAR(MAX) NOT NULL,
        created_at  DATETIME2 NOT NULL CONSTRAINT DF_note_created DEFAULT SYSUTCDATETIME());
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_notes_user' AND object_id=OBJECT_ID('dbo.admin_user_notes'))
    CREATE INDEX IX_notes_user ON dbo.admin_user_notes(user_id, created_at DESC);
GO
