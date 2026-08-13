-- 015: Teams layer — organizations, payments, invitations, members, and the
-- org link on jobs. Implements the agreed Teams design.
--
-- MODEL: an admin buys a one-time plan covering N employees, emails each an invite
-- link, and each invitee (the admin included) logs in and gets their own credits.
-- Employees are ordinary users rows, so the existing per-user identity/LoRA and
-- generation paths work unchanged.
--
-- ISOLATION: individual-user data is untouched. The only change to an existing table
-- is a NULLABLE jobs.organization_id — NULL means "ordinary individual job", exactly
-- as today. Nothing backfills, nothing rewrites.
--
-- Idempotent and safe to re-run. GO separators are REQUIRED (see 003's note): the
-- ALTER on jobs and the index that references the new column must compile in
-- separate batches.
--
-- Text types follow the existing files: VARCHAR for machine-ish values (status,
-- Stripe ids, tokens) and NVARCHAR for anything a human types — company names and
-- email addresses can contain non-ASCII, and VARCHAR would silently mangle them.

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

-- ── 3. invitations ────────────────────────────────────────────────────────
-- One row per invite link. The token IS the credential in the emailed link, so it
-- must be generated with a CSPRNG (secrets.token_urlsafe), never a guessable value.
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE object_id = OBJECT_ID('dbo.invitations'))
    CREATE TABLE dbo.invitations (
        invitation_id      UNIQUEIDENTIFIER NOT NULL PRIMARY KEY DEFAULT NEWID(),
        organization_id    UNIQUEIDENTIFIER NOT NULL,
        email              NVARCHAR(255)    NOT NULL,
        token              VARCHAR(128)     NOT NULL,
        status             VARCHAR(16)      NOT NULL CONSTRAINT DF_inv_status DEFAULT 'pending',
        invited_by_user_id UNIQUEIDENTIFIER NOT NULL,
        accepted_user_id   UNIQUEIDENTIFIER NULL,   -- set on accept
        expires_at         DATETIME2        NOT NULL,
        created_at         DATETIME2        NOT NULL CONSTRAINT DF_inv_created_at DEFAULT SYSUTCDATETIME(),
        accepted_at        DATETIME2        NULL,
        CONSTRAINT FK_inv_org FOREIGN KEY (organization_id)
            REFERENCES dbo.organizations (organization_id),
        CONSTRAINT UQ_inv_token UNIQUE (token)
    );
GO

-- Accept-flow lookup is by token (covered by UQ_inv_token). This index serves the
-- admin's "list invites for my org" view.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_inv_org_status'
               AND object_id = OBJECT_ID('dbo.invitations'))
    CREATE INDEX IX_inv_org_status ON dbo.invitations (organization_id, status);
GO

-- ── 4. organization_members ───────────────────────────────────────────────
-- One row per person on the plan, admin included. credits_remaining here is the
-- Teams credit pool and is SEPARATE from users.credits_remaining (the individual
-- pool). jobs.organization_id decides which pool a job spends from.
--
-- user_id is UNIQUE: a person can belong to at most one organization.
-- invitation_id is NULL for the admin, who created the org rather than accepting a link.
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE object_id = OBJECT_ID('dbo.organization_members'))
    CREATE TABLE dbo.organization_members (
        membership_id     UNIQUEIDENTIFIER NOT NULL PRIMARY KEY DEFAULT NEWID(),
        organization_id   UNIQUEIDENTIFIER NOT NULL,
        user_id           UNIQUEIDENTIFIER NOT NULL,
        invitation_id     UNIQUEIDENTIFIER NULL,
        credits_granted   INT              NOT NULL,
        credits_remaining INT              NOT NULL,
        status            VARCHAR(16)      NOT NULL CONSTRAINT DF_member_status DEFAULT 'active',
        joined_at         DATETIME2        NOT NULL CONSTRAINT DF_member_joined_at DEFAULT SYSUTCDATETIME(),
        CONSTRAINT FK_member_org FOREIGN KEY (organization_id)
            REFERENCES dbo.organizations (organization_id),
        CONSTRAINT FK_member_invitation FOREIGN KEY (invitation_id)
            REFERENCES dbo.invitations (invitation_id),
        CONSTRAINT UQ_member_user UNIQUE (user_id)
    );
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_member_org'
               AND object_id = OBJECT_ID('dbo.organization_members'))
    CREATE INDEX IX_member_org ON dbo.organization_members (organization_id, status);
GO

-- ── 5. jobs.organization_id (existing table) ──────────────────────────────
-- NULL = ordinary individual job, unchanged. Non-NULL = this job belongs to a Teams
-- seat and spends from organization_members.credits_remaining.
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
-- no query filters on.
IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_org_status')
    ALTER TABLE dbo.organizations ADD CONSTRAINT CK_org_status
        CHECK (status IN ('active', 'suspended', 'closed'));
GO

IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_orgpay_status')
    ALTER TABLE dbo.organization_payments ADD CONSTRAINT CK_orgpay_status
        CHECK (status IN ('succeeded', 'failed'));
GO

IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_inv_status')
    ALTER TABLE dbo.invitations ADD CONSTRAINT CK_inv_status
        CHECK (status IN ('pending', 'accepted', 'expired', 'revoked'));
GO

IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_member_status')
    ALTER TABLE dbo.organization_members ADD CONSTRAINT CK_member_status
        CHECK (status IN ('active', 'removed'));
GO

-- ── Notes for the API layer (not enforced here) ───────────────────────────
-- 1. Seat limit: nothing stops more accepted invites than organizations.seats_purchased.
--    The accept handler must count active members inside the same transaction and
--    reject when full.
-- 2. FKs to users: admin_user_id, invited_by_user_id, accepted_user_id and
--    organization_members.user_id all hold users.user_id values but carry no FK, matching
--    lora_trainings (004). Validate in application code.
-- 3. Training cost: a retrain costs credits today. Decide whether a Teams member's
--    initial training spends from credits_per_seat before the accept flow grants them.
--
-- ── Verify ────────────────────────────────────────────────────────────────
--   SELECT COUNT(*) FROM dbo.organizations;          -- exists, 0 rows
--   SELECT COUNT(*) FROM dbo.organization_payments;
--   SELECT COUNT(*) FROM dbo.invitations;
--   SELECT COUNT(*) FROM dbo.organization_members;
--   SELECT COUNT(*) FROM dbo.jobs WHERE organization_id IS NOT NULL;  -- 0
--
-- ── Rollback ──────────────────────────────────────────────────────────────
--   DROP INDEX IX_jobs_org ON dbo.jobs;
--   ALTER TABLE dbo.jobs DROP COLUMN organization_id;
--   DROP TABLE dbo.organization_members;   -- order matters (FKs)
--   DROP TABLE dbo.invitations;
--   DROP TABLE dbo.organization_payments;
--   DROP TABLE dbo.organizations;
