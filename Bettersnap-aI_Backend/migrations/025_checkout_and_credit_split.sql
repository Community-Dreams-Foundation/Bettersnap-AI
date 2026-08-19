-- 025_checkout_and_credit_split.sql — capture the 6 `users` columns that exist in
-- production but were added out-of-band and appear in NO migration (schema drift the audit
-- found). Code references all of them (one-time vs monthly credit split, Stripe Checkout
-- token handshake), so a fresh/DR database provisioned purely from git was missing them.
--
-- Each ADD is guarded by COL_LENGTH IS NULL, so this is a safe no-op on the production DB
-- (columns already present) and simply back-fills them on a fresh build.

IF COL_LENGTH('dbo.users', 'stripe_checkout_token') IS NULL
    ALTER TABLE dbo.users ADD stripe_checkout_token NVARCHAR(64) NULL;
GO

IF COL_LENGTH('dbo.users', 'stripe_checkout_expires_at') IS NULL
    ALTER TABLE dbo.users ADD stripe_checkout_expires_at DATETIME2 NULL;
GO

IF COL_LENGTH('dbo.users', 'one_time_credits_remaining') IS NULL
    ALTER TABLE dbo.users ADD one_time_credits_remaining INT NOT NULL
        CONSTRAINT DF_users_one_time_credits DEFAULT 0;
GO

IF COL_LENGTH('dbo.users', 'monthly_credits_remaining') IS NULL
    ALTER TABLE dbo.users ADD monthly_credits_remaining INT NOT NULL
        CONSTRAINT DF_users_monthly_credits DEFAULT 0;
GO

IF COL_LENGTH('dbo.users', 'one_time_plan') IS NULL
    ALTER TABLE dbo.users ADD one_time_plan NVARCHAR(50) NULL;
GO

IF COL_LENGTH('dbo.users', 'one_time_plan_name') IS NULL
    ALTER TABLE dbo.users ADD one_time_plan_name VARCHAR(32) NULL;
GO
