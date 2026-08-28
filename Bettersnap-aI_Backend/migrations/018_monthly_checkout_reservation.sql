-- 018_monthly_checkout_reservation.sql — prevents concurrent monthly Stripe Checkout
-- Sessions for one account. The API atomically claims these columns before contacting
-- Stripe and clears them after activation or provider failure. Expired reservations
-- recover automatically if a process crashes.

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id=OBJECT_ID('dbo.users') AND name='stripe_checkout_token')
    ALTER TABLE dbo.users ADD stripe_checkout_token NVARCHAR(64) NULL;
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id=OBJECT_ID('dbo.users') AND name='stripe_checkout_expires_at')
    ALTER TABLE dbo.users ADD stripe_checkout_expires_at DATETIME2 NULL;
GO

-- Rollback:
-- ALTER TABLE dbo.users DROP COLUMN stripe_checkout_expires_at;
-- ALTER TABLE dbo.users DROP COLUMN stripe_checkout_token;
