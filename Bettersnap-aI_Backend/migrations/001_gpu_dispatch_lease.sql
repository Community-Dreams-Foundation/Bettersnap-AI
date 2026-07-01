-- 001: GPU dispatch lease — a single-row global lock that serializes starting
-- A100 Container Apps jobs across all scaled-out Function instances (see
-- shared/gpu_lease.py). Idempotent and safe to re-run.
IF OBJECT_ID('dbo.GpuDispatchLease', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.GpuDispatchLease (
        lease_name       VARCHAR(64)  NOT NULL PRIMARY KEY,
        owner_id         VARCHAR(128) NULL,
        expires_at       DATETIME2    NULL,
        last_dispatch_at DATETIME2    NULL
    );
END;

-- Seed the singleton row independently, so a re-run repairs a missing row
-- (a missing row makes acquire_dispatch_lease() raise DISPATCH_CONFIG_ERROR).
IF NOT EXISTS (SELECT 1 FROM dbo.GpuDispatchLease WHERE lease_name = 'gpu-dispatch')
    INSERT INTO dbo.GpuDispatchLease (lease_name) VALUES ('gpu-dispatch');

-- ── Rollback ──────────────────────────────────────────────────────────────
-- DROP TABLE IF EXISTS dbo.GpuDispatchLease;
-- (Removing the lease disables guarded dispatch — only roll back together with
--  reverting the code that depends on it.)
