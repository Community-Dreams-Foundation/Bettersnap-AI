-- 000_baseline.sql — the base schema that every later migration ALTERs but that
-- NOTHING previously created in version control.
--
-- WHY THIS EXISTS
-- Migrations 002+ do `ALTER TABLE dbo.users` / `ALTER TABLE dbo.jobs`, and other files
-- reference `lora_models`, `subscriptions`, `credit_transactions`, `attires`, etc. — but
-- no migration ever CREATED those tables. Their DDL lived only in a test fixture, so
-- `run_migrations.py` broke at 002 on a fresh database and no new/DR environment could be
-- provisioned from git. This file closes that gap.
--
-- It captures the ORIGINAL (pre-migration-002) shape of each base table: `users` and
-- `jobs` here contain ONLY the columns that predate the incremental migrations, so 002-017
-- add their columns on top exactly as they did historically — no conflict. Tables that no
-- migration alters (plans, attires, backgrounds, lora_models, subscriptions,
-- credit_transactions, job_attires, job_backgrounds) are created with their full shape.
--
-- Every CREATE is guarded by `IF OBJECT_ID(...) IS NULL`, so on the existing production DB
-- (where these tables already exist) this whole file is a safe no-op and simply records
-- itself in schema_migrations. On a fresh DB it builds the base, then 001-018 apply on top.
--
-- Sorts before 001 by filename, so the runner applies it first.

-- ── users (original columns only; plan_name/stripe_*/etc. are added by 003-018) ──────────
IF OBJECT_ID('dbo.users', 'U') IS NULL
    CREATE TABLE dbo.users (
        user_id            UNIQUEIDENTIFIER NOT NULL CONSTRAINT DF_users_user_id DEFAULT NEWID(),
        email              NVARCHAR(255) NULL,
        full_name          NVARCHAR(255) NULL,
        auth_provider      NVARCHAR(50)  NULL,
        subscription_tier  NVARCHAR(50)  NULL,
        subscription_start DATETIME2 NULL,
        subscription_end   DATETIME2 NULL,
        grace_period_end   DATETIME2 NULL,
        credits_remaining  INT NULL CONSTRAINT DF_users_credits DEFAULT 0,
        created_at         DATETIME2 NULL CONSTRAINT DF_users_created DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_users PRIMARY KEY (user_id)
    );
GO

-- ── plans ────────────────────────────────────────────────────────────────────────────────
IF OBJECT_ID('dbo.plans', 'U') IS NULL
    CREATE TABLE dbo.plans (
        plan_id         INT IDENTITY(1,1) NOT NULL,
        plan_name       NVARCHAR(100) NULL,
        max_attires     INT NULL,
        max_backgrounds INT NULL,
        photos_per_job  INT NULL,
        price           DECIMAL(10,2) NULL,
        created_at      DATETIME2 NULL CONSTRAINT DF_plans_created DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_plans PRIMARY KEY (plan_id)
    );
GO

-- ── attires ──────────────────────────────────────────────────────────────────────────────
IF OBJECT_ID('dbo.attires', 'U') IS NULL
    CREATE TABLE dbo.attires (
        attire_id  INT IDENTITY(1,1) NOT NULL,
        name       NVARCHAR(100) NULL,
        category   NVARCHAR(50)  NULL,
        blob_path  NVARCHAR(500) NULL,
        is_active  BIT NULL CONSTRAINT DF_attires_active DEFAULT 1,
        created_at DATETIME2 NULL CONSTRAINT DF_attires_created DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_attires PRIMARY KEY (attire_id)
    );
GO

-- ── backgrounds ──────────────────────────────────────────────────────────────────────────
IF OBJECT_ID('dbo.backgrounds', 'U') IS NULL
    CREATE TABLE dbo.backgrounds (
        background_id INT IDENTITY(1,1) NOT NULL,
        name          NVARCHAR(100) NULL,
        category      NVARCHAR(50)  NULL,
        blob_path     NVARCHAR(500) NULL,
        is_active     BIT NULL CONSTRAINT DF_backgrounds_active DEFAULT 1,
        created_at    DATETIME2 NULL CONSTRAINT DF_backgrounds_created DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_backgrounds PRIMARY KEY (background_id)
    );
GO

-- ── jobs (original columns only; external_execution_id/expired/dispatched_at/source_type
--    are added by 002/007/015/016) ────────────────────────────────────────────────────────
IF OBJECT_ID('dbo.jobs', 'U') IS NULL
    CREATE TABLE dbo.jobs (
        job_id           UNIQUEIDENTIFIER NOT NULL CONSTRAINT DF_jobs_job_id DEFAULT NEWID(),
        user_id          UNIQUEIDENTIFIER NOT NULL,
        plan_id          INT NULL,
        status           NVARCHAR(50) NULL,
        job_type         NVARCHAR(50) NULL,
        category         NVARCHAR(50) NULL,
        input_blob_path  NVARCHAR(500) NULL,
        output_blob_path NVARCHAR(MAX) NULL,
        credits_consumed INT NULL CONSTRAINT DF_jobs_credits DEFAULT 0,
        created_at       DATETIME2 NULL CONSTRAINT DF_jobs_created DEFAULT SYSUTCDATETIME(),
        completed_at     DATETIME2 NULL,
        expires_at       DATETIME2 NULL,
        job_params       NVARCHAR(MAX) NULL,
        CONSTRAINT PK_jobs PRIMARY KEY (job_id),
        CONSTRAINT FK_jobs_user  FOREIGN KEY (user_id) REFERENCES dbo.users (user_id),
        CONSTRAINT FK_jobs_plan  FOREIGN KEY (plan_id) REFERENCES dbo.plans (plan_id)
    );
GO

-- ── lora_models ──────────────────────────────────────────────────────────────────────────
IF OBJECT_ID('dbo.lora_models', 'U') IS NULL
    CREATE TABLE dbo.lora_models (
        lora_id    UNIQUEIDENTIFIER NOT NULL CONSTRAINT DF_lora_models_id DEFAULT NEWID(),
        user_id    UNIQUEIDENTIFIER NOT NULL,
        lora_type  NVARCHAR(50)  NULL,
        blob_path  NVARCHAR(500) NULL,
        created_at DATETIME2 NULL CONSTRAINT DF_lora_models_created DEFAULT SYSUTCDATETIME(),
        expires_at DATETIME2 NULL,
        CONSTRAINT PK_lora_models PRIMARY KEY (lora_id),
        CONSTRAINT FK_lora_models_user FOREIGN KEY (user_id) REFERENCES dbo.users (user_id)
    );
GO

-- ── subscriptions ────────────────────────────────────────────────────────────────────────
IF OBJECT_ID('dbo.subscriptions', 'U') IS NULL
    CREATE TABLE dbo.subscriptions (
        subscription_id   UNIQUEIDENTIFIER NOT NULL CONSTRAINT DF_subs_id DEFAULT NEWID(),
        user_id           UNIQUEIDENTIFIER NOT NULL,
        plan_id           INT NOT NULL,
        payment_reference NVARCHAR(255) NULL,
        start_date        DATETIME2 NULL,
        end_date          DATETIME2 NULL,
        status            NVARCHAR(50) NULL,
        created_at        DATETIME2 NULL CONSTRAINT DF_subs_created DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_subscriptions PRIMARY KEY (subscription_id),
        CONSTRAINT FK_subs_user FOREIGN KEY (user_id) REFERENCES dbo.users (user_id),
        CONSTRAINT FK_subs_plan FOREIGN KEY (plan_id) REFERENCES dbo.plans (plan_id)
    );
GO

-- ── credit_transactions ──────────────────────────────────────────────────────────────────
IF OBJECT_ID('dbo.credit_transactions', 'U') IS NULL
    CREATE TABLE dbo.credit_transactions (
        transaction_id   UNIQUEIDENTIFIER NOT NULL CONSTRAINT DF_credit_tx_id DEFAULT NEWID(),
        user_id          UNIQUEIDENTIFIER NOT NULL,
        amount           INT NULL,
        transaction_type NVARCHAR(50) NULL,
        job_id           UNIQUEIDENTIFIER NULL,
        created_at       DATETIME2 NULL CONSTRAINT DF_credit_tx_created DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_credit_transactions PRIMARY KEY (transaction_id),
        CONSTRAINT FK_credit_tx_user FOREIGN KEY (user_id) REFERENCES dbo.users (user_id),
        CONSTRAINT FK_credit_tx_job  FOREIGN KEY (job_id)  REFERENCES dbo.jobs (job_id)
    );
GO

-- ── job_attires ──────────────────────────────────────────────────────────────────────────
IF OBJECT_ID('dbo.job_attires', 'U') IS NULL
    CREATE TABLE dbo.job_attires (
        id        INT IDENTITY(1,1) NOT NULL,
        job_id    UNIQUEIDENTIFIER NOT NULL,
        attire_id INT NOT NULL,
        CONSTRAINT PK_job_attires PRIMARY KEY (id),
        CONSTRAINT FK_job_attires_job    FOREIGN KEY (job_id)    REFERENCES dbo.jobs (job_id),
        CONSTRAINT FK_job_attires_attire FOREIGN KEY (attire_id) REFERENCES dbo.attires (attire_id)
    );
GO

-- ── job_backgrounds ──────────────────────────────────────────────────────────────────────
IF OBJECT_ID('dbo.job_backgrounds', 'U') IS NULL
    CREATE TABLE dbo.job_backgrounds (
        id            INT IDENTITY(1,1) NOT NULL,
        job_id        UNIQUEIDENTIFIER NOT NULL,
        background_id INT NOT NULL,
        CONSTRAINT PK_job_backgrounds PRIMARY KEY (id),
        CONSTRAINT FK_job_backgrounds_job FOREIGN KEY (job_id) REFERENCES dbo.jobs (job_id),
        CONSTRAINT FK_job_backgrounds_bg  FOREIGN KEY (background_id) REFERENCES dbo.backgrounds (background_id)
    );
GO
