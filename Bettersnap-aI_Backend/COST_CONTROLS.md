# A100 Cost Controls

BetterSnap inference runs on a serverless **A100 80GB** Container Apps job. A
single stuck or duplicated job — or a flood of submissions — can burn GPU money
fast. Cost is defended at **four layers**; no single one is a hard global cap on
its own.

| Layer | Where | What it bounds |
|---|---|---|
| 1. Per-replica | `job.yaml` | retries, runtime, fan-out of one job execution |
| 2. Queue concurrency | `host.json` | how fast messages are processed **per function instance** |
| 3. Backend policy | `function_app.py` | active-job cap (global), daily caps, poison handling |
| 4. Azure Budget | Azure portal / CLI | hard dollar visibility + alerts (the real backstop) |

Values are intentionally **low** for an unproven product. Raise only after real
cost/runtime data.

## Layer 1 — `job.yaml`
- `replicaRetryLimit: 0` — never re-run a failed job on the A100. `main.py`
  already retries the DB connect and re-raises real inference errors, so a
  replica retry just burns GPU and fails again. User resubmits instead.
- `replicaTimeout: 1800` — max seconds a replica may run. **Do not lower** until
  logs prove p95/p99 of successful runs is safely under the new value; cutting it
  too low turns slow-but-valid jobs into false failures.
- `manualTriggerConfig.parallelism: 1`, `replicaCompletionCount: 1` — one job
  execution = exactly one replica; never fan out.

## Layer 2 — `host.json` queue settings
```json
"queues": { "batchSize": 1, "newBatchThreshold": 0, "maxDequeueCount": 3 }
```
- `batchSize: 1` + `newBatchThreshold: 0` → process one message at a time **per
  function instance**. ⚠️ This is **not** a global cap: Azure Functions can scale
  out and each instance gets its own batch. The hard global cap is Layer 3.
- `maxDequeueCount: 3` → after 3 failed attempts a message is auto-moved to
  `inference-jobs-poison` (see poison handler).

## Layer 3 — backend policy (`function_app.py`)
Tunable via app settings (defaults shown):

- **Global dispatch lease** (`shared/gpu_lease.py`) — closes the scale-out race.
  Before starting a job the trigger acquires a SQL **TTL row-lease**
  (`GpuDispatchLease`) via a single atomic `UPDATE`, so only one instance across
  the whole app can be in the "check active → start" critical section at a time.
  TTL (`GPU_LEASE_TTL_SECONDS=180`) auto-releases a crashed holder, so the queue
  can't deadlock. **Requires `migrations/001_gpu_dispatch_lease.sql` to be run.**
- `MAX_ACTIVE_GPU_JOBS=1` — the cap itself, counted from the **Container Apps
  job-executions API** (source of truth, not DB). A just-started job may lag the
  API, so `recent_dispatch_pending()` adds a grace bump
  (`GPU_DISPATCH_GRACE_SECONDS=60`) within the lease so a second job can't slip
  in during that window.
- **Back-pressure with backoff + hard stop.** Over-cap (or lease-busy) messages
  re-enqueue with **exponential backoff** (`GPU_BACKPRESSURE_BASE=30` × 2^defer,
  capped at `GPU_BACKPRESSURE_MAX=600`s) and complete normally (not counted
  toward `maxDequeueCount`). A `defer_count` rides in the payload; after
  `MAX_DISPATCH_DEFERS=20` the job is **failed (DISPATCH_TIMEOUT)** so a stuck cap
  or broken API can never churn the queue forever.
- `PER_USER_DAILY_CAP=5` / `GLOBAL_DAILY_CAP=25` — checked in `submit_job` before
  enqueue. **Atomic:** the credits check + cap checks + insert + credit decrement
  run in ONE transaction guarded by `sp_getapplock` (a SQL-server-wide exclusive
  lock), so concurrent submits can't both pass the same cap (TOCTOU). Returns 429
  (cap) / 402 (credits) / 503 (lock timeout).
- **Emergency kill switch** — `GPU_DISPATCH_ENABLED=false` (read per-call) halts
  ALL dispatch immediately; in-flight messages re-enqueue without incrementing
  `defer_count`, so a deliberate pause never times jobs out. Budgets only alert;
  this actually stops spend. Wire the 100% budget alert's action group to flip
  this.
- **Poison handler** (`handle_poison_job`) marks the job `failed` so bad messages
  stop re-entering the GPU queue and aren't silently lost.

> **Residual TODO:** failed dispatch-timeouts are stored as `status='failed'`
> (UI-compatible) with the reason only in logs — add a `failure_code` column with
> the schema-drift fix for first-class diagnosability.

### Dispatch correctness guarantees
- **Fail-closed:** `acquire_dispatch_lease()` returns `None` if the lease is held
  *or the singleton row is missing*; the trigger then defers and **never starts a
  job without the lease**. No lease row = no dispatch (not "start anyway").
- **Tiny critical section:** the lease is held only around
  *idempotency check → cap check → claim → start → record*. It is **not** held
  during inference (that runs in the separate container).
- **Idempotency:** before starting, the job is skipped if it already has an
  `external_execution_id` or status ∈ {dispatching, processing, completed,
  failed}. A retried message can't start a second A100. Start failures revert the
  claim (`dispatching → queued`) so the job can retry without double-spend.
- **Loss-safe requeue:** over-cap/paused messages re-enqueue a delayed copy
  *before* the original completes; if the enqueue fails, the trigger raises and
  the host retries the original — the job is never dropped.
- **Fail loud on bad deploy:** a *missing* lease row/table raises
  `DispatchConfigError` → the job is **failed (DISPATCH_CONFIG_ERROR)** and
  logged for alerting, **not** deferred forever. (A held lease is the only case
  that defers.)
- **Stuck-job repair:** if a dispatcher crashes around start, a job can be left
  in `dispatching`. Admin endpoints (guarded by `ADMIN_API_KEY`) provide a manual
  repair path — never automatic, to avoid double-start:
  - `GET /admin/stuck-dispatch?older_than_min=15` — list stuck jobs.
  - `POST /admin/jobs/{job_id}/requeue` — after confirming no live execution
    exists, reset that job to `queued` and re-enqueue.
  - **Lockdown:** `ADMIN_API_KEY` must be a long random value stored only in app
    settings / Key Vault (never in code or logs). Auth uses a constant-time
    compare and is checked **before** any DB access or data is returned. Keep the
    `/admin/*` routes off the public surface (Front Door / APIM allowlist) or
    rate-limit them; rotate the key periodically.

## Validation (deploy gates — do NOT deploy until green)
1. **Migrations applied & seeded** — run `migrations/001_*.sql` and
   `migrations/002_*.sql` against `bettersnap-db`, then prove:
   ```sql
   SELECT * FROM dbo.GpuDispatchLease;          -- must return exactly 1 row ('gpu-dispatch')
   SELECT COL_LENGTH('dbo.jobs','external_execution_id');  -- must be non-NULL
   ```
2. **Unit logic tests** (no Azure needed):
   ```
   cd Bettersnap-aI_Backend && python -m unittest tests.test_dispatch_logic
   ```
   Covers fail-closed, idempotency/duplicate-skip, over-cap defer + max-defer
   fail, loss-safe requeue, kill switch, start-failure revert, daily-cap codes.
3. **Concurrency integration tests** (need a disposable test SQL DB):
   ```
   export TEST_SQL_CONN="DRIVER={ODBC Driver 18 for SQL Server};SERVER=...;DATABASE=bettersnap_test;UID=...;PWD=...;Encrypt=yes"
   python -m unittest tests.test_concurrency_integration
   ```
   Proves: 10 concurrent submits/user → 5; 50 concurrent global → 25; 20 racing
   lease acquires → 1 winner; missing lease row → fails closed.

## Layer 4 — Azure Budget alert (MANDATORY before going public)
A budget on resource group **`bettersnap-ai-rg`** with alerts at **50% / 80% /
100%**. This is the only true dollar backstop — set it regardless of code.

### Portal
1. Cost Management + Billing → **Budgets** → **Add**.
2. Scope: resource group `bettersnap-ai-rg`.
3. Reset period **Monthly**, set the amount (decide $/month).
4. Add alert conditions at **50%, 80%, 100%** of budget.
5. Wire each to an **Action Group** that emails the team (and/or SMS/webhook).
6. (Recommended) Also create a small **daily** budget for early-stage spend
   visibility.

### CLI (fill in AMOUNT and EMAIL, dates as needed)
```bash
# Action group for the alert emails
az monitor action-group create \
  --resource-group bettersnap-ai-rg \
  --name bettersnap-cost-alerts \
  --short-name bsnapcost \
  --email-receiver name=team email=EMAIL

# Monthly budget with 50/80/100% alerts on the resource group
az consumption budget create-with-rg \
  --resource-group bettersnap-ai-rg \
  --budget-name bettersnap-monthly \
  --amount AMOUNT \
  --time-grain Monthly \
  --category Cost \
  --start-date 2026-07-01 --end-date 2027-07-01 \
  --notifications '{
    "p50": {"enabled": true, "operator": "GreaterThanOrEqualTo", "threshold": 50,  "contactEmails": ["EMAIL"]},
    "p80": {"enabled": true, "operator": "GreaterThanOrEqualTo", "threshold": 80,  "contactEmails": ["EMAIL"]},
    "p100":{"enabled": true, "operator": "GreaterThanOrEqualTo", "threshold": 100, "contactEmails": ["EMAIL"]}
  }'
```
> Note: budget alerts notify on spend; they do **not** auto-stop the GPU.

### Wiring the budget alert to the kill switch (the actual brake)
Budget alerts only fire an Action Group — they don't stop spend. To make 100%
(or a daily threshold) actually halt A100s, point the Action Group at automation
that flips the kill switch:

```bash
# The command the automation must run to stop all dispatch immediately:
az functionapp config appsettings set \
  --resource-group bettersnap-ai-rg \
  --name <function-app-name> \
  --settings GPU_DISPATCH_ENABLED=false
# (re-enable later with GPU_DISPATCH_ENABLED=true)
```
Options to trigger it from the budget Action Group:
- **Automation runbook** (Action Group → Automation) running the `az` command, or
- **Logic App** (Action Group → Logic App) calling the App Service management API
  to set the app setting.

After flipping, in-flight queue messages re-enqueue on a long pause
(`KILL_SWITCH_PAUSE_DELAY`) and resume cleanly when re-enabled — no job loss, no
churn.

> Once this is wired, the budget stops being "just visibility" and becomes a real
> spend brake.
