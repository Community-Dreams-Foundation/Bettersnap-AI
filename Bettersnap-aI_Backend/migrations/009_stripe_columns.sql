-- 009_stripe_columns.sql — add the subscription/Stripe columns the Stripe code
-- (shared/stripe_client.py + the webhook handlers in function_app.py) reads and writes
-- but which were never created. Without these, any real Stripe call 500s on an invalid
-- column name. All nullable/additive — safe, online, no backfill. Idempotent.
--
-- Referenced by: _handle_onetime_payment, _handle_monthly_checkout, _handle_invoice_paid,
-- _handle_subscription_ended, subscriptions/status, subscriptions/cancel.

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id=OBJECT_ID('dbo.users') AND name='subscription_plan')
    ALTER TABLE dbo.users ADD subscription_plan NVARCHAR(50) NULL;   -- basic|pro|expert|free
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id=OBJECT_ID('dbo.users') AND name='subscription_type')
    ALTER TABLE dbo.users ADD subscription_type NVARCHAR(20) NULL;   -- one_time|monthly|NULL
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id=OBJECT_ID('dbo.users') AND name='stripe_customer_id')
    ALTER TABLE dbo.users ADD stripe_customer_id NVARCHAR(255) NULL; -- cus_...
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id=OBJECT_ID('dbo.users') AND name='stripe_subscription_id')
    ALTER TABLE dbo.users ADD stripe_subscription_id NVARCHAR(255) NULL; -- sub_...
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id=OBJECT_ID('dbo.users') AND name='credits_monthly_limit')
    ALTER TABLE dbo.users ADD credits_monthly_limit INT NULL;        -- monthly credit quota
GO
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id=OBJECT_ID('dbo.users') AND name='subscription_renewed_at')
    ALTER TABLE dbo.users ADD subscription_renewed_at DATETIME2 NULL;
GO
