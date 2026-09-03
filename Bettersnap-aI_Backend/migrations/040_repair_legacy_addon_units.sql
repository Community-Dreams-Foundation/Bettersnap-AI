-- 040: Repair subscriptions ended by the pre-conversion webhook.
--
-- The old handler changed subscription_type to one_time but copied a monthly_* plan_name
-- and left the add-on bucket in monthly credit units. submit_job resolves pricing from
-- plan_name, so those accounts continued paying 5 credits/image after cancellation.
-- Snapshot every repaired row and fail closed unless the two balance mirrors agree and can
-- be converted exactly. The migration runner owns the surrounding transaction.

SET XACT_ABORT ON;
GO

IF OBJECT_ID('dbo.users_addon_unit_repair_040', 'U') IS NULL
BEGIN
    SELECT
        user_id,
        credits_remaining AS old_credits_remaining,
        one_time_credits_remaining AS old_one_time_credits_remaining,
        subscription_plan AS old_subscription_plan,
        plan_name AS old_plan_name,
        one_time_plan AS old_one_time_plan,
        one_time_plan_name AS old_one_time_plan_name,
        SYSUTCDATETIME() AS captured_at
    INTO dbo.users_addon_unit_repair_040
    FROM dbo.users WITH (UPDLOCK, HOLDLOCK)
    WHERE subscription_type = 'one_time'
      AND plan_name IN ('monthly_basic', 'monthly_pro', 'monthly_expert')
      AND stripe_subscription_id IS NULL
      AND ISNULL(monthly_credits_remaining, 0) = 0;
END;
GO

IF EXISTS (
    SELECT 1
    FROM dbo.users_addon_unit_repair_040
    WHERE ISNULL(old_credits_remaining, 0) <> ISNULL(old_one_time_credits_remaining, 0)
       OR ISNULL(old_credits_remaining, 0) % 5 <> 0
       OR ISNULL(old_one_time_credits_remaining, 0) % 5 <> 0
)
BEGIN
    ;THROW 50040, '040 repair blocked: legacy add-on balances are inconsistent or not divisible by 5', 1;
END;
GO

UPDATE u
SET
    u.credits_remaining = ISNULL(s.old_credits_remaining, 0) / 5,
    u.one_time_credits_remaining = ISNULL(s.old_one_time_credits_remaining, 0) / 5,
    u.subscription_plan = p.plan_key,
    u.plan_name = p.plan_key,
    u.one_time_plan = p.plan_key,
    u.one_time_plan_name = p.plan_key
FROM dbo.users u
JOIN dbo.users_addon_unit_repair_040 s ON s.user_id = u.user_id
CROSS APPLY (
    SELECT CASE
        WHEN LOWER(ISNULL(s.old_one_time_plan, '')) IN ('basic', 'pro', 'expert')
            THEN LOWER(s.old_one_time_plan)
        WHEN LOWER(ISNULL(s.old_subscription_plan, '')) IN ('basic', 'pro', 'expert')
            THEN LOWER(s.old_subscription_plan)
        WHEN s.old_plan_name = 'monthly_pro' THEN 'pro'
        WHEN s.old_plan_name = 'monthly_expert' THEN 'expert'
        ELSE 'basic'
    END AS plan_key
) p;
GO

INSERT INTO dbo.credit_transactions (user_id, amount, transaction_type, job_id)
SELECT
    user_id,
    (ISNULL(old_one_time_credits_remaining, 0) / 5)
        - ISNULL(old_one_time_credits_remaining, 0),
    'plan_unit_conversion',
    NULL
FROM dbo.users_addon_unit_repair_040
WHERE ISNULL(old_one_time_credits_remaining, 0) <> 0;
GO

IF EXISTS (
    SELECT 1
    FROM dbo.users u
    JOIN dbo.users_addon_unit_repair_040 s ON s.user_id = u.user_id
    WHERE u.subscription_type <> 'one_time'
       OR u.plan_name NOT IN ('basic', 'pro', 'expert')
       OR u.credits_remaining <> u.one_time_credits_remaining
       OR ISNULL(u.monthly_credits_remaining, 0) <> 0
)
BEGIN
    ;THROW 50041, '040 verification failed: repaired add-on account is still inconsistent', 1;
END;
GO
