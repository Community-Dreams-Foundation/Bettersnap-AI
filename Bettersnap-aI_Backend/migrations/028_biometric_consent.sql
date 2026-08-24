-- 028_biometric_consent.sql
-- BIPA / GDPR: explicit, informed biometric consent must be captured and stored SERVER-SIDE
-- before any facial data (uploaded photos, face crops, or the trained per-user LoRA) is
-- processed. This closes the gap where consent was a frontend checkbox only.
--
-- Design: an APPEND-ONLY event log. Each row is one consent event ('given' or 'revoked');
-- the user's CURRENT consent is the latest row for that user. The row history itself is the
-- audit trail the compliance report requires (given / revoked, with version, purpose, policy
-- version, timestamp, and revocation reason) — no mutable JSON blob to tamper with.
-- Idempotent: safe to re-run.
IF OBJECT_ID('dbo.biometric_consent', 'U') IS NULL
    CREATE TABLE dbo.biometric_consent (
        consent_id       INT IDENTITY(1,1) PRIMARY KEY,
        user_id          UNIQUEIDENTIFIER NOT NULL,
        event            VARCHAR(16)   NOT NULL,   -- 'given' | 'revoked'
        consent_version  NVARCHAR(32)  NOT NULL,   -- version of the consent language, e.g. 'v1.0'
        consent_purpose  NVARCHAR(256) NOT NULL,   -- e.g. 'Biometric processing for AI photo generation'
        policy_version   NVARCHAR(32)  NULL,       -- Privacy Policy version accepted (if provided)
        reason           NVARCHAR(512) NULL,       -- revocation reason (when event = 'revoked')
        created_at       DATETIME2     NOT NULL
            CONSTRAINT DF_biometric_consent_created DEFAULT SYSUTCDATETIME()
    );

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_biometric_consent_user' AND object_id = OBJECT_ID('dbo.biometric_consent'))
    CREATE INDEX IX_biometric_consent_user ON dbo.biometric_consent (user_id, created_at DESC);
