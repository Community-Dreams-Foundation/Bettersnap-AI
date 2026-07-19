-- 011_dunning.sql — dunning support. Adds payment_failed_at so a FAILED monthly renewal can
-- be surfaced to the user ("update your card") instead of failing silently. Set by the
-- invoice.payment_failed webhook (_handle_payment_failed); cleared on the next successful
-- invoice.paid (recovery). Nullable/additive — safe, online, idempotent. Run once.
--
-- Referenced by: stripe_webhook (_handle_payment_failed, _handle_invoice_paid), subscription_status.

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id=OBJECT_ID('dbo.users') AND name='payment_failed_at')
    ALTER TABLE dbo.users ADD payment_failed_at DATETIME2 NULL;   -- NULL = no failed renewal outstanding
GO

-- ── Rollback ──────────────────────────────────────────────────────────────
-- IF EXISTS (SELECT 1 FROM sys.columns WHERE object_id=OBJECT_ID('dbo.users') AND name='payment_failed_at')
--     ALTER TABLE dbo.users DROP COLUMN payment_failed_at;
