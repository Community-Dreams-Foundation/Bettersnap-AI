-- 026: persist the exact credit buckets charged for each retrain.
-- Required so a failed paid retrain can restore monthly and one-time/add-on credits
-- to the same buckets exactly once during the guarded terminal transition.

IF COL_LENGTH('dbo.lora_trainings', 'monthly_credit_cost') IS NULL
    ALTER TABLE dbo.lora_trainings ADD monthly_credit_cost INT NOT NULL
        CONSTRAINT DF_lora_trainings_monthly_credit_cost DEFAULT 0;
GO

IF COL_LENGTH('dbo.lora_trainings', 'one_time_credit_cost') IS NULL
    ALTER TABLE dbo.lora_trainings ADD one_time_credit_cost INT NOT NULL
        CONSTRAINT DF_lora_trainings_one_time_credit_cost DEFAULT 0;
GO
