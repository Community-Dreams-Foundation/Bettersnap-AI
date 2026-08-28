-- 016: Teams layer, part 2 — invitations and organization_members.
--
-- DEPENDS ON 015 (organizations, organization_payments). Both tables here carry an FK
-- to dbo.organizations, so 015 must run first. The runner applies files in filename
-- order, so this must keep a higher number than the file that creates organizations.
--
-- MODEL: an admin buys a plan covering N employees and emails each an invite link.
-- Each invitee logs in through the normal Microsoft flow, which already creates their
-- users row via POST /users/register — so accepting an invite only needs to insert the
-- membership row here, not create an account.
--
-- Idempotent and safe to re-run. GO separators required between batches (see 003).
--
-- Text types follow the existing files: VARCHAR for machine values (status, tokens),
-- NVARCHAR for anything a human types — an email address can contain non-ASCII, and
-- VARCHAR would silently mangle it.

-- ── invitations ───────────────────────────────────────────────────────────
-- One row per invite link. The token IS the credential carried in the emailed link,
-- so the API must generate it with a CSPRNG (secrets.token_urlsafe), never a
-- sequential or guessable value.
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE object_id = OBJECT_ID('dbo.invitations'))
    CREATE TABLE dbo.invitations (
        invitation_id      UNIQUEIDENTIFIER NOT NULL PRIMARY KEY DEFAULT NEWID(),
        organization_id    UNIQUEIDENTIFIER NOT NULL,
        email              NVARCHAR(255)    NOT NULL,
        token              VARCHAR(128)     NOT NULL,
        status             VARCHAR(16)      NOT NULL CONSTRAINT DF_inv_status DEFAULT 'pending',
        invited_by_user_id UNIQUEIDENTIFIER NOT NULL,   -- Entra oid of the admin
        accepted_user_id   UNIQUEIDENTIFIER NULL,       -- set on accept
        expires_at         DATETIME2        NOT NULL,
        created_at         DATETIME2        NOT NULL CONSTRAINT DF_inv_created_at DEFAULT SYSUTCDATETIME(),
        accepted_at        DATETIME2        NULL,
        CONSTRAINT FK_inv_org FOREIGN KEY (organization_id)
            REFERENCES dbo.organizations (organization_id),
        CONSTRAINT UQ_inv_token UNIQUE (token)
    );
GO

-- Accept-flow lookup is by token, already covered by UQ_inv_token.
-- This index serves the admin's "list invites for my org" view.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_inv_org_status'
               AND object_id = OBJECT_ID('dbo.invitations'))
    CREATE INDEX IX_inv_org_status ON dbo.invitations (organization_id, status);
GO

IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_inv_status')
    ALTER TABLE dbo.invitations ADD CONSTRAINT CK_inv_status
        CHECK (status IN ('pending', 'accepted', 'expired', 'revoked'));
GO

-- ── organization_members ──────────────────────────────────────────────────
-- One row per person on the plan, admin included.
--
-- credits_remaining here is the TEAMS pool and is separate from users.credits_remaining
-- (the individual pool). jobs.organization_id decides which pool a job spends from.
--
-- user_id is UNIQUE: a person belongs to at most one organization.
-- invitation_id is NULL for the admin, who created the org rather than accepting a link.
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE object_id = OBJECT_ID('dbo.organization_members'))
    CREATE TABLE dbo.organization_members (
        membership_id     UNIQUEIDENTIFIER NOT NULL PRIMARY KEY DEFAULT NEWID(),
        organization_id   UNIQUEIDENTIFIER NOT NULL,
        user_id           UNIQUEIDENTIFIER NOT NULL,   -- Entra oid; matches users.user_id
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

IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_member_status')
    ALTER TABLE dbo.organization_members ADD CONSTRAINT CK_member_status
        CHECK (status IN ('active', 'removed'));
GO

-- ── Notes for the API layer (not enforced here) ───────────────────────────
-- 1. Seat limit: nothing stops more accepted invites than organizations.seats_purchased.
--    The accept handler must count active members inside the same transaction and
--    reject when the org is full.
-- 2. Expiry: expires_at is stored but not enforced by the DB. The accept handler must
--    reject an expired or already-accepted invite.
-- 3. FKs to users: invited_by_user_id, accepted_user_id and organization_members.user_id
--    hold users.user_id values but carry no FK, matching lora_trainings (004).
--    Validate in application code.
-- 4. Training cost: a retrain costs credits today. Decide whether a member's initial
--    training spends from credits_granted before the accept flow grants them.
--
-- ── Verify ────────────────────────────────────────────────────────────────
--   SELECT COUNT(*) FROM dbo.invitations;           -- exists, 0 rows
--   SELECT COUNT(*) FROM dbo.organization_members;  -- exists, 0 rows
--
-- ── Rollback ──────────────────────────────────────────────────────────────
--   DROP TABLE dbo.organization_members;   -- order matters (FK to invitations)
--   DROP TABLE dbo.invitations;
