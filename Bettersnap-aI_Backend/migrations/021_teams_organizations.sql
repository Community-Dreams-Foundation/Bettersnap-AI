-- 021: Teams layer, part 1 — organizations and organization_payments.
--
-- Part 2 (invitations, organization_members) is 022 and carries FKs to
-- dbo.organizations (organization_id), so this file MUST create it first — the runner
-- applies files in filename order. (022's own header calls out this dependency.)
--
-- MODEL (matches 022's header): an admin creates an organization, buys a plan covering
-- N seats (organizations.seats_purchased), and invites employees. Each seat is still a
-- normal users row with its own login and identity LoRA; the organization is the billing
-- + membership wrapper. organizations holds the org-level Stripe subscription; per-seat
-- credit accounting lives on organization_members (022).
--
-- Idempotent and safe to re-run. GO separators required between batches (see 003).
-- Text types follow the existing files: VARCHAR for machine values (status, Stripe ids),
-- NVARCHAR for anything a human types (org name).

-- ── organizations ─────────────────────────────────────────────────────────
-- One row per organization. seats_purchased mirrors the Stripe subscription quantity and
-- is the seat cap 022's accept handler counts active members against (022 note #1).
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE object_id = OBJECT_ID('dbo.organizations'))
    CREATE TABLE dbo.organizations (
        organization_id        UNIQUEIDENTIFIER NOT NULL PRIMARY KEY DEFAULT NEWID(),
        name                   NVARCHAR(255)    NOT NULL,
        owner_user_id          UNIQUEIDENTIFIER NOT NULL,   -- Entra oid of the admin who created the org
        seats_purchased        INT              NOT NULL CONSTRAINT DF_org_seats DEFAULT 0,
        seat_plan_key          VARCHAR(32)      NULL,       -- shared/plans.py key for the per-seat plan
        status                 VARCHAR(16)      NOT NULL CONSTRAINT DF_org_status DEFAULT 'active',
        stripe_customer_id     VARCHAR(64)      NULL,
        stripe_subscription_id VARCHAR(64)      NULL,
        created_at             DATETIME2        NOT NULL CONSTRAINT DF_org_created_at DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_org_owner UNIQUE (owner_user_id)      -- one org per owner (v1); a member belongs to one org (022 UQ_member_user)
    );
GO

IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = 'CK_org_status')
    ALTER TABLE dbo.organizations ADD CONSTRAINT CK_org_status
        CHECK (status IN ('active', 'past_due', 'canceled'));
GO

-- Stripe webhooks resolve an org by its subscription id; index that lookup.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_org_stripe_sub'
               AND object_id = OBJECT_ID('dbo.organizations'))
    CREATE INDEX IX_org_stripe_sub ON dbo.organizations (stripe_subscription_id);
GO

-- ── organization_payments ─────────────────────────────────────────────────
-- Append-only record of org-level Stripe payment events (seat purchases, renewals) and
-- the idempotency guard for the org billing webhooks — parallels
-- 010_stripe_webhook_idempotency for individual accounts.
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE object_id = OBJECT_ID('dbo.organization_payments'))
    CREATE TABLE dbo.organization_payments (
        payment_id       UNIQUEIDENTIFIER NOT NULL PRIMARY KEY DEFAULT NEWID(),
        organization_id  UNIQUEIDENTIFIER NOT NULL,
        stripe_event_id  VARCHAR(128)     NULL,       -- Stripe event id, for idempotent webhook handling
        event_type       VARCHAR(64)      NULL,       -- e.g. invoice.paid, customer.subscription.updated
        amount_cents     INT              NULL,
        seats            INT              NULL,        -- seat quantity this event settled
        status           VARCHAR(16)      NULL,
        created_at       DATETIME2        NOT NULL CONSTRAINT DF_org_pay_created_at DEFAULT SYSUTCDATETIME(),
        CONSTRAINT FK_org_pay_org FOREIGN KEY (organization_id)
            REFERENCES dbo.organizations (organization_id)
    );
GO

-- At most one row per Stripe event id (filtered — the column is nullable, many NULLs allowed).
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UX_org_pay_event'
               AND object_id = OBJECT_ID('dbo.organization_payments'))
    CREATE UNIQUE INDEX UX_org_pay_event ON dbo.organization_payments (stripe_event_id)
        WHERE stripe_event_id IS NOT NULL;
GO

-- Org billing-history view.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_org_pay_org'
               AND object_id = OBJECT_ID('dbo.organization_payments'))
    CREATE INDEX IX_org_pay_org ON dbo.organization_payments (organization_id, created_at);
GO

-- ── Notes for the API layer (not enforced here) ───────────────────────────
-- 1. owner_user_id (and the member user ids in 022) hold users.user_id (Entra oid) values
--    but carry NO FK, matching 022 and lora_trainings (004). Validate in application code.
-- 2. Seat cap: the invite-accept handler must count active organization_members against
--    organizations.seats_purchased inside one transaction (see 022 note #1).
-- 3. TEAMS.md envisions a later redesign (a nullable users.org_id column, per-user seat
--    credits instead of an org pool, owner/admin/member roles). This file deliberately
--    matches the ALREADY-SHIPPED 022 migration's contract (organization_id PK,
--    seats_purchased) so its FKs resolve today; reconcile with TEAMS.md when the teams
--    API is actually built.
--
-- ── Verify ────────────────────────────────────────────────────────────────
--   SELECT COUNT(*) FROM dbo.organizations;          -- exists, 0 rows
--   SELECT COUNT(*) FROM dbo.organization_payments;  -- exists, 0 rows
--
-- ── Rollback ──────────────────────────────────────────────────────────────
--   DROP TABLE dbo.organization_payments;   -- FK to organizations, drop first
--   DROP TABLE dbo.organizations;
