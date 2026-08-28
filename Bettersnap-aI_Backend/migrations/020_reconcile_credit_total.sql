-- 020: make credits_remaining the aggregate spendable balance.
--
-- Monthly and persistent add-on credits remain separate source-of-truth buckets. The legacy
-- field is retained for clients that only understand one balance and mirrors their sum.

UPDATE dbo.users
SET credits_remaining =
    ISNULL(monthly_credits_remaining, 0) + ISNULL(one_time_credits_remaining, 0)
WHERE subscription_type = 'monthly';
GO

UPDATE dbo.users
SET credits_remaining = ISNULL(one_time_credits_remaining, 0)
WHERE ISNULL(subscription_type, '') <> 'monthly';
GO
