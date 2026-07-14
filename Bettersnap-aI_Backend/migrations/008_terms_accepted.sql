-- 008_terms_accepted.sql — add the column the terms endpoints already reference.
--
-- users/terms-status (GET) and users/accept-terms (POST) both read/write
-- users.terms_accepted_at, but the column was never created — so an authenticated call
-- 500s (invalid column name), and terms acceptance was never actually recorded
-- server-side (the frontend only wrote localStorage). Compliance gap.
--
-- Idempotent.

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('dbo.users') AND name = 'terms_accepted_at'
)
    ALTER TABLE dbo.users ADD terms_accepted_at DATETIME2 NULL;
GO
