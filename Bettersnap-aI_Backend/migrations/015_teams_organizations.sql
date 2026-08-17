IF OBJECT_ID('dbo.organizations', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.organizations (
        organization_id     UNIQUEIDENTIFIER NOT NULL PRIMARY KEY DEFAULT NEWID(),
        name                 VARCHAR(255)     NOT NULL,
        admin_user_id        UNIQUEIDENTIFIER NOT NULL,   -- FK -> dbo.users(user_id)
        seats_purchased       INT              NOT NULL,
        credits_per_seat      INT              NOT NULL DEFAULT 10,  -- images per employee
        -- active | suspended | closed
        status                VARCHAR(16)      NOT NULL DEFAULT 'active',
        created_at            DATETIME2        NOT NULL DEFAULT GETUTCDATE(),

        CONSTRAINT FK_organizations_admin_user
            FOREIGN KEY (admin_user_id) REFERENCES dbo.users(user_id)
    );
END;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_organizations_admin_user')
    CREATE INDEX IX_organizations_admin_user ON dbo.organizations (admin_user_id);
GO

-- Rollback
-- DROP TABLE IF EXISTS dbo.organizations;


IF OBJECT_ID('dbo.organization_payments', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.organization_payments (
        payment_id                UNIQUEIDENTIFIER NOT NULL PRIMARY KEY DEFAULT NEWID(),
        organization_id           UNIQUEIDENTIFIER NOT NULL,
        stripe_payment_intent_id  VARCHAR(255)     NOT NULL,
        stripe_event_id           VARCHAR(255)     NOT NULL,  -- webhook idempotency key
        amount_cents               INT              NOT NULL,
        currency                   VARCHAR(8)       NOT NULL DEFAULT 'usd',
        -- succeeded | failed
        status                     VARCHAR(16)      NOT NULL,
        created_at                 DATETIME2        NOT NULL DEFAULT GETUTCDATE(),

        CONSTRAINT FK_org_payments_organization
            FOREIGN KEY (organization_id) REFERENCES dbo.organizations(organization_id)
    );
END;
GO

-- One successful payment per Stripe event, globally — a retried webhook delivery
-- is a no-op, not a duplicate seat grant. Same guarantee as migration 010 gives
-- individual-user payments.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UX_org_payments_stripe_event')
    CREATE UNIQUE INDEX UX_org_payments_stripe_event
        ON dbo.organization_payments (stripe_event_id);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_org_payments_organization')
    CREATE INDEX IX_org_payments_organization ON dbo.organization_payments (organization_id);
GO

-- Rollback
-- DROP TABLE IF EXISTS dbo.organization_payments;


-- NULL = an individual-user job (unchanged, untouched). Non-NULL = this job was
-- submitted against an organization's credits (admin or employee — both have an
-- organization_members row).
IF COL_LENGTH('dbo.jobs', 'organization_id') IS NULL
    ALTER TABLE dbo.jobs ADD organization_id UNIQUEIDENTIFIER NULL;
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.foreign_keys WHERE name = 'FK_jobs_organization'
)
    ALTER TABLE dbo.jobs
        ADD CONSTRAINT FK_jobs_organization
        FOREIGN KEY (organization_id) REFERENCES dbo.organizations(organization_id);
GO

-- The admin dashboard's core query: "every job for my org, newest first".
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_jobs_organization')
    CREATE INDEX IX_jobs_organization ON dbo.jobs (organization_id)
        WHERE organization_id IS NOT NULL;
GO

-- Rollback
-- ALTER TABLE dbo.jobs DROP CONSTRAINT FK_jobs_organization;
-- ALTER TABLE dbo.jobs DROP COLUMN organization_id;