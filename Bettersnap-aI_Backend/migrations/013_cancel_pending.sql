-- 013_cancel_pending.sql — record a SCHEDULED (period-end) cancellation so the UI can show
-- "cancels on {date}" while the subscription is still active, and so it can be reactivated.
-- Set by subscriptions/cancel (from Stripe's current_period_end), cleared by
-- subscriptions/reactivate and by the terminal downgrade. Nullable/additive, idempotent.
--
-- Referenced by: cancel_user_subscription, reactivate_user_subscription, subscription_status,
-- _handle_subscription_ended.

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id=OBJECT_ID('dbo.users') AND name='subscription_cancel_at')
    ALTER TABLE dbo.users ADD subscription_cancel_at DATETIME2 NULL;   -- NULL = not scheduled to cancel
GO

-- ── Rollback ──────────────────────────────────────────────────────────────
-- IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id=OBJECT_ID('dbo.users') AND name='subscription_cancel_at')
--     ALTER TABLE dbo.users DROP COLUMN subscription_cancel_at;
