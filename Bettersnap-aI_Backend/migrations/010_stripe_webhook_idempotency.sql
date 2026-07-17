-- 010_stripe_webhook_idempotency.sql — dedup table so a Stripe webhook delivered more than
-- once (Stripe retries on any non-2xx AND re-sends on its own schedule) can never apply the
-- same grant twice. Without this, _handle_onetime_payment's
-- `credits_remaining = credits_remaining + ?` double-credits on a retry, and the SET-based
-- monthly/invoice handlers re-reset already-spent credits. Run once against bettersnap-db.
-- Nullable/additive new table — safe, online, no backfill. Idempotent.
--
-- Each mutating handler claims event_id in the SAME transaction as its grant (see
-- function_app._claim_event), so if the grant rolls back the claim rolls back too and a
-- genuine Stripe retry can still reprocess. Referenced by: stripe_webhook,
-- _handle_onetime_payment, _handle_monthly_checkout, _handle_invoice_paid,
-- _handle_subscription_ended.

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE object_id = OBJECT_ID('dbo.processed_stripe_events'))
    CREATE TABLE dbo.processed_stripe_events (
        event_id      NVARCHAR(255) NOT NULL PRIMARY KEY,   -- Stripe evt_... id
        processed_at  DATETIME2     NOT NULL DEFAULT SYSUTCDATETIME()
    );
GO

-- ── Rollback ──────────────────────────────────────────────────────────────
-- IF EXISTS (SELECT 1 FROM sys.tables WHERE object_id = OBJECT_ID('dbo.processed_stripe_events'))
--     DROP TABLE dbo.processed_stripe_events;
-- (New standalone table; dropping it only loses the dedup history — roll back only
--  alongside the code that reads it.)
