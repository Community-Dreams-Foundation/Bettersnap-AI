-- 015: organizations — the Teams workspace root table.
--
-- WHY: the Teams layer needs an owner for every bulk order, branding asset, and
-- generated image. Today nothing in the schema represents a company. This table is
-- that root; every later Teams table hangs off organization_id.
--
-- SCOPE: Phase 1 is single-admin, so the owner lives here as a column rather than in
-- a separate organization_users table. Phase 2 (multi-user + RBAC) adds that table and
-- backfills one row per org from owner_user_id — a copy-across, not a rewrite.
--
-- ISOLATION: individual-user tables are untouched. Nothing here references dbo.users
-- with a FK; owner_user_id holds the same Entra 'oid' value users.user_id does, and is
-- validated in application code (matching how lora_trainings does it in 004).
--
-- Idempotent + GO-separated (see 003/004/014).

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE object_id = OBJECT_ID('dbo.organizations'))
    CREATE TABLE dbo.organizations (
        organization_id   UNIQUEIDENTIFIER NOT NULL PRIMARY KEY DEFAULT NEWID(),
        owner_user_id     UNIQUEIDENTIFIER NOT NULL,   -- Entra oid; the single admin (Phase 1)
        name              NVARCHAR(200)    NOT NULL,
        contact_email     NVARCHAR(320)    NOT NULL,   -- 320 = max RFC-5321 address length
        contact_phone     NVARCHAR(40)     NULL,
        logo_url          NVARCHAR(500)    NULL,       -- blob path; served via signed URL
        banner_url        NVARCHAR(500)    NULL,
        status            NVARCHAR(20)     NOT NULL DEFAULT 'active',  -- active|suspended|deleted
        created_at        DATETIME2        NOT NULL DEFAULT SYSUTCDATETIME(),
        updated_at        DATETIME2        NOT NULL DEFAULT SYSUTCDATETIME()
    );
GO

-- Guard the status vocabulary at the DB level so a typo in application code can't park a
-- row in a state no query filters on.
IF NOT EXISTS (SELECT 1 FROM sys.check_constraints
               WHERE name = 'CK_organizations_status')
    ALTER TABLE dbo.organizations ADD CONSTRAINT CK_organizations_status
        CHECK (status IN ('active', 'suspended', 'deleted'));
GO

-- Every request resolves the caller's org by owner_user_id, so this index is on the hot
-- path of essentially every Teams endpoint.
--
-- NOTE: this is a NON-unique index, which permits one person to own several orgs. If
-- product confirms one org per admin, swap it for a filtered UNIQUE index so the database
-- enforces it rather than the application:
--   CREATE UNIQUE INDEX UX_organizations_owner ON dbo.organizations (owner_user_id)
--       WHERE status <> 'deleted';
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_organizations_owner' AND object_id = OBJECT_ID('dbo.organizations'))
    CREATE INDEX IX_organizations_owner ON dbo.organizations (owner_user_id);
GO

-- ── Verify ────────────────────────────────────────────────────────────────
--   SELECT COUNT(*) FROM dbo.organizations;                      -- table exists, 0 rows
--   SELECT name, status FROM dbo.organizations ORDER BY created_at DESC;
-- ── Rollback ──────────────────────────────────────────────────────────────
--   DROP TABLE dbo.organizations;   -- only while no Teams tables reference it
