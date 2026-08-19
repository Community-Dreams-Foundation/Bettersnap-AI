-- 022: Teams layer, part 1 — organizations, organization_payments, and the org link on jobs.
--
-- RECOVERED from the original single-file Teams migration (Features_team 015_teams_schema.sql,
-- commit 8ba1e55), which was dropped when Features_team merged into development (its 015 number
-- collided with development's 015_dispatched_at). Part 2 — invitations + organization_members —
-- survives as 023_teams_invitations_members.sql, whose FKs reference dbo.organizations, so this
-- file (which creates it) MUST keep a lower number. These two files are DISJOINT: 021 creates
-- organizations/organization_payments/jobs.organization_id; 022 creates invitations/members.
--
-- MODEL: an admin buys a one-time plan covering N employees, emails each an invite link, and
-- each invitee (the admin included) logs in and gets their own credits. Employees are ordinary
-- users rows, so the existing per-user identity/LoRA and generation paths work unchanged.
--
-- ISOLATION: individual-user data is untouched. The only change to an existing table is a
-- NULLABLE jobs.organization_id — NULL means "ordinary individual job", exactly as today.
--
-- Idempotent and safe to re-run. GO separators are REQUIRED (see 003's note): the ALTER on
-- jobs and the index that references the new column must compile in separate batches.
--
-- Text types follow the existing files: VARCHAR for machine-ish values (status, Stripe ids,
-- tokens) and NVARCHAR for anything a human types — company names can contain non-ASCII.

-- ── 1. organizations ──────────────────────────────────────────────────────
-- One row per company. Phase 1 is single-admin, so the admin lives here as a column
-- rather than being derived from organization_members.
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE object_id = OBJECT_ID('dbo.organizations'))
    CREATE TABLE dbo.organizations (
        organization_id   UNIQUEIDENTIFIER NOT NULL PRIMARY KEY DEFAULT NEWID(),
        name              NVARCHAR(255)    NOT NULL,
        admin_user_id     UNIQUEIDENTIFIER NOT NULL,  -- Entra oid; matches users.user_id
        seats_purchased   INT              NOT NULL,
        credits_per_seat  INT              NOT NULL CONSTRAINT DF_org_credits_per_seat DEFAULT 10,
        status            VARCHAR(16)      NOT NULL CONSTRAINT DF_org_status DEFAULT 'active',
        created_at        DATETIME2        NOT NULL CONSTRAINT DF_org_created_at DEFAULT SYSUTCDATETIME(),
        updated_at        DATETIME2        NOT NULL CONSTRAINT DF_org_updated_at DEFAULT SYSUTCDATETIME()
    );
GO

-- Every Teams request resolves the caller's org, so this is on the hot path.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_organizations_admin'
               AND object_id = OBJECT_ID('dbo.organizations'))
    CREATE INDEX IX_organizations_admin ON dbo.organizations (admin_user_id);
GO

-- ── 2. organization_payments ──────────────────────────────────────────────
-- One row per Stripe payment. stripe_event_id is UNIQUE so a replayed webhook
-- (Stripe retries) can only ever insert once — the duplicate insert fails and the
-- handler treats it as an already-processed no-op.
--
-- OPEN: migration 010 already added dbo.processed_stripe_events for exactly this
-- purpose on the individual-user side. Two mechanisms now guard the same problem.
-- Confirm with whoever owns the Stripe workstream which one Teams should use before
-- the webhook handler is written; this table is harmless either way.
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE object_id = OBJECT_ID('dbo.organization_payments'))
    CREATE TABLE dbo.organization_payments (
        payment_id               UNIQUEIDENTIFIER NOT NULL PRIMARY KEY DEFAULT NEWID(),
        organization_id          UNIQUEIDENTIFIER NOT NULL,
        stripe_payment_intent_id VARCHAR(255)     NOT NULL,
        stripe_event_id          VARCHAR(255)     NOT NULL,
        amount_cents             INT              NOT NULL,
        currency                 VARCHAR(8)       NOT NULL CONSTRAINT DF_orgpay_currency DEFAULT 'usd',
        status                   VARCHAR(16)      NOT NULL,
        created_at               DATETIME2        NOT NULL CONSTRAINT DF_orgpay_created_at DEFAULT SYSUTCDATETIME(),
        CONSTRAINT FK_orgpay_org FOREIGN KEY (organization_id)
            REFERENCES dbo.organizations (organization_id),
        CONSTRAINT UQ_orgpay_stripe_event UNIQUE (stripe_event_id)
    );
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_orgpay_org'
               AND object_id = OBJECT_ID('dbo.organization_payments'))
    CREATE INDEX IX_orgpay_org ON dbo.organization_payments (organization_id);
GO

-- ── 3. jobs.organization_id (existing table) ──────────────────────────────
-- NULL = ordinary individual job, unchanged. Non-NULL = this job belongs to a Teams
-- seat and spends from organization_members.credits_remaining (created in 022).
-- Nullable add-only column: backfills as NULL, online, no data migration.
IF COL_LENGTH('dbo.jobs', 'organization_id') IS NULL
    ALTER TABLE dbo.jobs ADD organization_id UNIQUEIDENTIFIER NULL;
GO

-- Separate batch: the column must exist at compile time (see 003's GO note).
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_jobs_org'
               AND object_id = OBJECT_ID('dbo.jobs'))
    CREATE INDEX IX_jobs_org ON dbo.jobs (organization_id) WHERE organization_id IS NOT NULL;
GO

-- ── Status vocabularies ───────────────────────────────────────────────────
-- Guarded at the DB level so a typo in application code can't park a row in a state
-- no query filters on. (invitations/organization_members CHECKs live with their tables in 022.)
IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_org_status')
    ALTER TABLE dbo.organizations ADD CONSTRAINT CK_org_status
        CHECK (status IN ('active', 'suspended', 'closed'));
GO

IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_orgpay_status')
    ALTER TABLE dbo.organization_payments ADD CONSTRAINT CK_orgpay_status
        CHECK (status IN ('succeeded', 'failed'));
GO

-- ── Notes for the API layer (not enforced here) ───────────────────────────
-- 1. admin_user_id (and the member/inviter user ids in 022) hold users.user_id (Entra oid)
--    values but carry NO FK, matching lora_trainings (004). Validate in application code.
-- 2. Seat cap: the invite-accept handler (022) must count active organization_members against
--    organizations.seats_purchased inside one transaction.
--
-- ── Verify ────────────────────────────────────────────────────────────────
--   SELECT COUNT(*) FROM dbo.organizations;          -- exists, 0 rows
--   SELECT COUNT(*) FROM dbo.organization_payments;  -- exists, 0 rows
--   SELECT COUNT(*) FROM dbo.jobs WHERE organization_id IS NOT NULL;  -- 0
--
-- ── Rollback ──────────────────────────────────────────────────────────────
--   DROP INDEX IX_jobs_org ON dbo.jobs;
--   ALTER TABLE dbo.jobs DROP COLUMN organization_id;
--   DROP TABLE dbo.organization_payments;   -- FK to organizations, drop first
--   DROP TABLE dbo.organizations;
