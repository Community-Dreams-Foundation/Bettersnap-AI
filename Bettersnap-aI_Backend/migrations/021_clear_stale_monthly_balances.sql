-- 021: one-time accounts must never expose or spend a stale monthly quota.
--
-- Reconcile historical rows created before one-time purchases reset monthly-only fields.

UPDATE dbo.users
SET credits_remaining = ISNULL(one_time_credits_remaining, 0),
    monthly_credits_remaining = 0,
    credits_monthly_limit = NULL,
    subscription_renewed_at = NULL,
    subscription_cancel_at = NULL,
    payment_failed_at = NULL
WHERE ISNULL(subscription_type, '') <> 'monthly';
GO
