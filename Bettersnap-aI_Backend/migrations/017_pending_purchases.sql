-- 017: queued plan purchases (finding #6, the "queue" step).
--
-- While one product is active, a new PLAN purchase is not started immediately — it is QUEUED
-- here, and the user completes checkout when their current product ends and they are idle again
-- (PAY-AT-ACTIVATION: we do NOT charge at queue time; there is no Stripe schedule to manage).
-- One PENDING row per user; a new queue supersedes the prior one. Credit TOP-UPS are never
-- queued — they apply immediately to an active monthly plan (see topup_credits).

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE object_id = OBJECT_ID('dbo.pending_purchases'))
    CREATE TABLE dbo.pending_purchases (
        pending_id    BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
        user_id       UNIQUEIDENTIFIER NOT NULL,
        purchase_type NVARCHAR(20)  NOT NULL,   -- 'monthly' | 'one_time'
        -- NOTE: named plan_key, NOT "plan" — `plan` is a RESERVED KEYWORD in SQL Server and
        -- an unquoted column of that name fails with "Incorrect syntax near the keyword 'plan'".
        plan_key      NVARCHAR(40)  NOT NULL,   -- basic | pro | expert
        status        NVARCHAR(20)  NOT NULL DEFAULT 'pending',  -- pending | done | canceled
        created_at    DATETIME2     NOT NULL DEFAULT SYSUTCDATETIME()
    );
GO

-- At most ONE pending row per user (a new queue supersedes the old — also enforced in code).
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'UX_pending_one_per_user' AND object_id = OBJECT_ID('dbo.pending_purchases'))
    CREATE UNIQUE INDEX UX_pending_one_per_user ON dbo.pending_purchases (user_id)
        WHERE status = 'pending';
GO

-- ── Verify ────────────────────────────────────────────────────────────────
--   SELECT user_id, purchase_type, plan_key, status FROM dbo.pending_purchases WHERE status='pending';
-- ── Rollback ──────────────────────────────────────────────────────────────
--   DROP TABLE dbo.pending_purchases;
