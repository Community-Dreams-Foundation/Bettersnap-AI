-- 019: separate expiring monthly credits from persistent one-time credits.
--
-- credits_remaining remains the compatibility field used by existing clients and always
-- mirrors the balance for the CURRENT product. Monthly activation parks the one-time balance;
-- subscription expiry clears the monthly balance and restores the parked one-time balance.

IF COL_LENGTH('dbo.users', 'one_time_credits_remaining') IS NULL
    ALTER TABLE dbo.users ADD one_time_credits_remaining INT NOT NULL
        CONSTRAINT DF_users_one_time_credits_remaining DEFAULT 0;
GO

IF COL_LENGTH('dbo.users', 'monthly_credits_remaining') IS NULL
    ALTER TABLE dbo.users ADD monthly_credits_remaining INT NOT NULL
        CONSTRAINT DF_users_monthly_credits_remaining DEFAULT 0;
GO

IF COL_LENGTH('dbo.users', 'one_time_plan') IS NULL
    ALTER TABLE dbo.users ADD one_time_plan NVARCHAR(50) NULL;
GO

IF COL_LENGTH('dbo.users', 'one_time_plan_name') IS NULL
    ALTER TABLE dbo.users ADD one_time_plan_name VARCHAR(32) NULL;
GO

-- Conservative backfill: the legacy schema only recorded one active balance, so historical
-- monthly rows cannot prove whether an older one-time pack existed before the subscription.
-- Preserve the currently visible balance in the bucket matching the current product.
UPDATE dbo.users
SET monthly_credits_remaining = ISNULL(credits_remaining, 0)
WHERE subscription_type = 'monthly'
  AND monthly_credits_remaining = 0
  AND one_time_credits_remaining = 0;
GO

UPDATE dbo.users
SET one_time_credits_remaining = ISNULL(credits_remaining, 0),
    one_time_plan = ISNULL(subscription_plan, plan_name),
    one_time_plan_name = plan_name
WHERE ISNULL(subscription_type, '') <> 'monthly'
  AND monthly_credits_remaining = 0
  AND one_time_credits_remaining = 0;
GO
