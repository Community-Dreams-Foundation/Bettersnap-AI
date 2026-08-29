# Bounded pre-container provisioning retry

What shipped, why, and what it deliberately does not do.

Supersedes the design recorded in `PRE_CONTAINER_RETRY.md`, which describes an earlier
implementation that is **not** in this tree — see [Superseded work](#superseded-work-b2ba04e).

## The failure class

Confirmed by read-only forensics on the 2026-08-24/25 A100 failures:

```
SuccessfulCreate -> AssigningReplica -> BackoffLimitExceeded (+1-2s) -> PodDeletion
```

with **no** `PullingImage`, `GpuDriverInfo`, `ContainerStarted`, application logs, or exit code.
Azure fails to back the GPU replica **before the container starts**.

Call this an **ACA pre-container replica-provisioning failure. The exact platform cause is
unconfirmed** — scheduler, admission, profile and capacity are all still open, and separating
them needs Microsoft platform logs with an execution correlation id. Do not call it capacity
exhaustion; nothing in the evidence establishes that.

The distinction that matters operationally: a job that never started a container did no work and
consumed no GPU time, so retrying it is free and refunding it is owed. A job whose container
started and exited non-zero is an application failure and must **not** be retried.

## What the retry does

`shared/provisioning_retry.py` is a pure state machine. It takes a cursor, decides, and returns —
it never commits, never rolls back, and never talks to Azure. Callers own the transaction, which
is what makes the outbox dispatch atomic with the state transition.

- **Budget:** `MAX_PROVISIONING_EXECUTIONS` (default **3**) *total* ACA executions per job or
  training, counting the original. `provisioning_attempts` is never reset — the budget is
  per-job, not per-attempt, so a job cannot loop by being re-observed.
- **History:** `provisioning_execution_ids` records every execution the machine has handled, so a
  spent execution is never re-adopted, and a corrupt history fails closed rather than guessing.
- **Refund:** exactly-once, enforced by the state transition itself —
  `status NOT IN ('failed','completed')` plus `rowcount == 1`. Every downstream write is
  conditional on that rowcount, so a duplicate call moves nothing.
- **Refund evidence:** amounts come from recorded reserve/charge evidence. A missing or
  unparseable amount is never guessed; the run is marked `accounting_invalid` and left for a
  human. A missing refund target (an org member who left) becomes a durable pending debt that a
  later compensation pass settles once — and only once.
- **Fused train+infer:** the training row links to its generation job through
  `lora_trainings.fused_job_id`, serialized by `UPDLOCK/HOLDLOCK` and backed by a filtered unique
  index, so two concurrent allocators cannot bind one training twice.

### GUID comparisons

`user_id` is `UNIQUEIDENTIFIER`. SQL Server renders it **uppercase** through pyodbc; callers
carry it **lowercase** (Entra oids, `uuid.uuid4()`). `WHERE user_id = ?` is case-INSENSITIVE
because SQL Server compares uniqueidentifier as a binary type — but Python `str(a) == str(b)` is
not. That asymmetry is a real defect class; it was caught only by running against a real engine.

`same_user_id()` parses both sides with `uuid.UUID()` rather than lowercasing, so malformed and
non-UUID values fail closed instead of matching each other. **ACA execution names are not GUIDs
and must keep comparing exactly** — normalising them would merge two distinct executions.

## Schema

| Migration | Adds |
|---|---|
| `033_provisioning_retry.sql` | `provisioning_attempts`, `provisioning_execution_ids`, `first_terminal_observed_at` on both `jobs` and `lora_trainings` |
| `034_fused_job_link.sql` | `lora_trainings.fused_job_id`, trusted `FK_lora_trainings_fused_job`, filtered unique `UX_lora_trainings_fused_job`, and `THROW 50034 / 50035` guards that refuse to proceed against a wrong-shaped FK or index |

Both are append-only. Canonical migrations `000`–`032` are untouched. **There is no migration
035**, and none is needed: exactly-once refunding is enforced by the application guard, and that
was measured on a real engine (five concurrent connections refund once; three repeated
compensation passes credit once).

### Deploy order is load-bearing

```
migrations 033/034  ->  run_migrations.py --verify-schema  ->  Functions publish
```

If the runtime ships ahead of its schema, the training watcher's inflight `SELECT` raises on
**every** tick: no training completes and no parked generation is released. That is a total
outage, not a degraded feature. `deploy.ps1` now runs the schema gate even under
`-SkipMigrations`, because "I applied them out of band" is exactly the claim that needs checking.

**As of this commit, 033 and 034 have not been applied to production or to any other persistent
environment.** They have only ever run against a disposable local container.

## Tests

**Offline** (no database, no Azure, no GPU) — `python -m pytest tests`:
`test_provisioning_retry.py`, `test_provisioning_wiring.py`, `test_retry_audit_fixes.py`,
`test_refund_plan.py`, `test_refund_evidence.py`, `test_retrain_refund.py`,
`test_retrain_allocation.py`, `test_fused_exhaustion_composed.py`, `test_execution_evidence.py`,
`test_training_orphan.py`, `test_training_orphan_amounts.py`, `test_guid_normalization.py`,
`test_migrations_033_034.py`, `test_integration_harness_offline.py`.

**Disposable SQL Server** — `tests/integration/run_sqlserver_suite.py`, 14 cases against a
throwaway container (loopback-bound port 11433, database `bettersnap_test`, guardrails refuse
every other target). It proves what fakes cannot: `UPDLOCK/HOLDLOCK` actually serializing
connections, a trusted FK actually refusing an orphan, a filtered unique index actually rejecting
a duplicate, `THROW 50034/50035` actually firing, and 000–034 actually applying and replaying.
See `docs/sqlserver_integration_plan.md`.

The offline fakes model SQL Server's case-insensitive uniqueidentifier comparison
(`sql_guid_eq`). They did not, originally, which is why the GUID defect reached a real engine
before anyone saw it.

## Superseded work (`b2ba04e`)

Local branch `development` carries `b2ba04e` *"feat(reliability): pre-container provisioning
retry, token SQL auth, refs preflight"* — an **earlier, independent implementation of this same
feature**. It is not an ancestor of this branch and none of its files are in this tree.

**Do not cherry-pick or merge it.** Three reasons:

1. **It duplicates retry behaviour.** It carries its own 253-line `shared/provisioning_retry.py`.
   The implementation here supersedes it and is the one that has been audited and proven against
   a real database; taking both would leave two state machines competing for the same rows.
2. **Its migration collides.** It adds `migrations/030_provisioning_retry.sql`, but `030` is
   already taken by the canonical, committed `030_admin_audit_log.sql`. The migration runner
   validates monotonic order and would refuse it; applying it would corrupt the version line.
3. **Its SQL-auth and container work belongs to a different track.** `db_connect.py` (token SQL
   auth), `refs_preflight.py`, the `Dockerfile.v52thin` / `main.py` / `runtime/engines` changes
   and the GPU-count work are West US 3 / container-image concerns. They are unrelated to the
   retry state machine and must be evaluated on their own, not smuggled in behind it.

If anything in `b2ba04e` is still wanted, recover the *intent* into this structure and test it
here — the way `ea53384`'s Teams GUID regression tests were recovered into
`tests/test_org_teams.py` without cherry-picking the commit.

`PRE_CONTAINER_RETRY.md` documents `b2ba04e`, not this implementation: it names migration `030`,
`refs_preflight.py`, `tests/test_pre_container_retry.py` and
`tests/test_generation_refs_preflight.py`, none of which exist in this tree. It is kept out of
this commit for that reason. Its forensics section is still accurate and is reproduced above.

## Known limitations

- Definitive attribution of the platform failure still requires an Azure support ticket with an
  execution correlation id. Reserved A100 capacity is in West US 3 / North Europe, not East US.
- Retry is inert (fails safe) if the Functions app's managed identity lacks **Log Analytics
  Reader** on the workspace, because execution events cannot then be read.
- The fused re-dispatch path has not been validated against a live GPU container.
