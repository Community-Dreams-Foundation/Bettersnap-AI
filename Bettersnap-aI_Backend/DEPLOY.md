# Deploy plan — cost-control / guarded-dispatch release

Schema changes ship in this release, so deploy is **migration-ordered**. Do NOT
deploy the code before the migrations are applied and verified — the new dispatch
path requires the `GpuDispatchLease` table and the `jobs.external_execution_id`
column, and fails loud (DISPATCH_CONFIG_ERROR) without them.

## Order
1. **Back up the database.** Azure SQL: take a manual/copy-only backup or note the
   point-in-time-restore window before any DDL.
2. **Apply migration 001** (`migrations/001_gpu_dispatch_lease.sql`) — idempotent.
3. **Verify the lease row exists:**
   ```sql
   SELECT * FROM dbo.GpuDispatchLease;     -- expect exactly 1 row: 'gpu-dispatch'
   ```
   If 0 rows, dispatch will DISPATCH_CONFIG_ERROR every job — re-run 001.
4. **Apply migration 002** (`migrations/002_jobs_dispatch_idempotency.sql`) —
   idempotent, add-only nullable column (online, no backfill).
5. **Verify the column exists:**
   ```sql
   SELECT COL_LENGTH('dbo.jobs','external_execution_id');  -- expect non-NULL
   ```
6. **Set app settings** on the Function app (override defaults as needed):
   `MAX_ACTIVE_GPU_JOBS`, `PER_USER_DAILY_CAP`, `GLOBAL_DAILY_CAP`,
   `GPU_DISPATCH_ENABLED=true`, `ADMIN_API_KEY` (long random; Key Vault ref).
7. **Run the test gates** (see COST_CONTROLS.md → Validation):
   - `python -m unittest tests.test_dispatch_logic` (no Azure needed)
   - `TEST_SQL_CONN=… python -m unittest tests.test_concurrency_integration`
     against a **disposable test DB** — must show 10/user→5, 50 global→25,
     20 lease→1, missing-row→fail-closed.
8. **Deploy the code.**
9. **Smoke test:** submit one real job → confirm it dispatches once, completes,
   and `GpuDispatchLease.last_dispatch_at` updated; submit past the per-user cap →
   expect 429.

## Rollback
Code and schema are coupled — roll back together, code first:
1. **Redeploy the previous code** (drops the dependency on the new schema).
2. Schema can usually be **left in place** (the lease table + nullable column are
   harmless to old code). Only if you must fully revert:
   - `external_execution_id` is dispatch **audit history** — prefer to keep it.
     Drop only if certain: `ALTER TABLE dbo.jobs DROP COLUMN external_execution_id;`
   - `DROP TABLE dbo.GpuDispatchLease;` (only after the new code is gone).
3. Restore from the step-1 backup only if a migration corrupted data (it
   shouldn't — both are additive/idempotent).

## Emergency stop
Flip the kill switch any time (no redeploy): set `GPU_DISPATCH_ENABLED=false` —
see `scripts/disable_dispatch.sh`. In-flight jobs pause and resume on re-enable.
