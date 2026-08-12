# Deploy plan — migration-ordered release

Schema changes ship with the code, so deploy is **migration-ordered**: the database
is brought up to date FIRST, then the code. Never deploy code ahead of its schema —
handlers 500 on a missing table/column. Examples the runtime REQUIRES today: every
mutating Stripe webhook calls `_claim_event()` against `processed_stripe_events`
(migration `010`); training/terms/retention/subscriptions each need their own
tables/columns from `004`–`013`.

## One-command deploy

`deploy.ps1` (repo root) runs the stages in the correct order — **migrations first**,
then the GPU image, then the Functions app:

```powershell
./deploy.ps1 -FunctionApp "bettersnap-functions" -ImageTag v43
#   -SkipMigrations   only if the migrations are already applied (not recommended)
#   -ImageTag         ALWAYS pass the current tag; the script's default is stale, and
#                     it must stay in lock-step with image: in job.yaml
```

Stage 1 runs `scripts/run_migrations.py` (below); the deploy ABORTS if it fails
("schema not up to date, refusing to deploy code"). Stages 2–3 build/push the GPU
image and point the `bettersnapai-if` job at it; Stage 4 publishes the Functions app.
The image build and the publish are each behind a y/N confirm.

## Migrations — the versioned runner (applies ALL, tracks history)

This replaced the old hand-run `sqlcmd` list (which only applied `001`/`002`/`003`
and silently skipped everything after). Run standalone, or let `deploy.ps1` Stage 1
do it:

```bash
cd Bettersnap-aI_Backend
python scripts/run_migrations.py --dry-run   # list pending (read-only)
python scripts/run_migrations.py             # apply every pending file, in order
```

- Applies **every** `migrations/NNN_*.sql` in deterministic (filename) order.
- Applies all `GO` batches in one transaction per file; the schema changes and tracking
  insert commit together, or all roll back on failure.
- Records each applied file in `dbo.schema_migrations`; only NEW files ever run.
- Idempotent — a ~1s no-op when nothing is pending, so it is safe on every deploy.
- Fails loud (non-zero exit) if it cannot reach the DB or a migration errors.
- Auth: reads the DB password from Key Vault via `DefaultAzureCredential` (the same
  `az login`). Needs `python` + `pyodbc` + ODBC Driver 18.
- **Adopting the runner on an already-hand-migrated DB:** run once with `--baseline`
  to record the current files as applied WITHOUT re-running them. (Prod was baselined
  2026-07-18 — 12 files. Do NOT baseline again; new migrations apply normally.)

**Current migrations** are contiguous. `012_reserved.sql` is an explicit no-op marker
for the previously unused number; never insert a new migration below the highest version
already deployed. The runner rejects gaps, duplicate versions, and late backfills.

For a database that already has `013` or later recorded but predates the `012` marker,
record the no-op marker once before running the upgraded runner:

```sql
INSERT INTO dbo.schema_migrations (filename)
SELECT '012_reserved.sql'
WHERE NOT EXISTS (
    SELECT 1 FROM dbo.schema_migrations WHERE filename = '012_reserved.sql'
);
```
gpu-dispatch lease, jobs idempotency, user plans, lora trainings, trial default,
retrain, retention, terms accepted, stripe columns, stripe-webhook idempotency,
dunning, cancel-pending. **All are required by the running code** — a fresh or
partially provisioned environment must have every one applied.

## Before the code goes live
1. **Back up the database** (Azure SQL copy-only, or note the point-in-time-restore
   window) before any DDL.
2. **App settings** on the Function app (override defaults as needed):
   `MAX_ACTIVE_GPU_JOBS`, `PER_USER_DAILY_CAP`, `GLOBAL_DAILY_CAP`,
   `GPU_DISPATCH_ENABLED=true`, `ADMIN_API_KEY` (long random; Key Vault ref), and the
   Stripe settings (`STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, price IDs). See
   `.env.example` for the full list.
   Configure the Stripe webhook at `/api/webhooks/stripe` for exactly these events:
   `checkout.session.completed`, `checkout.session.expired`, `invoice.paid`,
   `invoice.payment_failed`, `customer.subscription.updated`, and
   `customer.subscription.deleted`. Do not enable `invoice.payment_succeeded` for renewal
   grants; `invoice.paid` is the single successful-invoice event handled by the backend.
3. **Test gates** (see COST_CONTROLS.md → Validation):
   - `python -m unittest tests.test_dispatch_logic tests.test_prompt_planning` (no Azure needed)
   - `TEST_SQL_CONN=… python -m unittest tests.test_concurrency_integration` against a
     **disposable test DB** — must show 10/user→5, 50 global→25, 20 lease→1,
     missing-row→fail-closed.
4. **Smoke test after deploy:** submit one real job → it dispatches once, completes,
   and `GpuDispatchLease.last_dispatch_at` updates; submit past the per-user cap → 429.

## Rollback
Code and schema are coupled — roll back together, **code first**:
1. Redeploy the previous code (drops the dependency on the new schema).
2. Schema can usually be **left in place** (columns/tables are additive + harmless to
   old code). Only fully revert a specific migration if certain — and delete its row
   from `dbo.schema_migrations` so the runner will re-apply it later if needed.
3. Restore from the step-1 backup only if a migration corrupted data (it shouldn't —
   all migrations are additive/idempotent).

## Emergency stop
Flip the kill switch any time (no redeploy): set `GPU_DISPATCH_ENABLED=false` — see
`scripts/disable_dispatch.sh`. In-flight jobs pause and resume on re-enable.
