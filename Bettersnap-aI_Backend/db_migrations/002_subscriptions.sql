-- Migration 002: subscription columns + terms acceptance
-- Safe to re-run: each block checks existence before altering.

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('users') AND name = 'terms_accepted_at'
)
    ALTER TABLE users ADD terms_accepted_at DATETIME NULL;

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('users') AND name = 'subscription_plan'
)
    ALTER TABLE users ADD subscription_plan VARCHAR(20) NOT NULL DEFAULT 'free';

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('users') AND name = 'subscription_type'
)
    ALTER TABLE users ADD subscription_type VARCHAR(10) NULL; -- 'one_time' or 'monthly'

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('users') AND name = 'stripe_customer_id'
)
    ALTER TABLE users ADD stripe_customer_id VARCHAR(255) NULL;

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('users') AND name = 'stripe_subscription_id'
)
    ALTER TABLE users ADD stripe_subscription_id VARCHAR(255) NULL;

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('users') AND name = 'subscription_renewed_at'
)
    ALTER TABLE users ADD subscription_renewed_at DATETIME NULL;

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('users') AND name = 'credits_monthly_limit'
)
    ALTER TABLE users ADD credits_monthly_limit INT NOT NULL DEFAULT 20;
