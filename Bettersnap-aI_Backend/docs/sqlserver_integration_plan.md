# Isolated SQL Server integration test — PLAN ONLY (not executed)

**Status: prepared, NOT run.** No container has been started, nothing has been installed, and
no production connection string exists anywhere in this plan. Awaiting authorization.

## Why this exists

Every retry/refund test in this branch runs against an in-memory fake. Those fakes now honour
WHERE clauses, rowcount and commit/rollback visibility, but they have **no lock manager**.
`tests/test_fused_exhaustion_composed.py::SequentialInterleaving` documents the gap explicitly:
two uncommitted transactions both "win", because nothing serializes them. The following can
only be proven against a real engine:

| claim | why a fake cannot prove it |
|---|---|
| `UPDLOCK, HOLDLOCK` serializes two reconcilers | no lock manager, no lock waits |
| the filtered unique index blocks double-binding | no index enforcement |
| the trusted FK blocks orphan links | no constraint engine |
| a rollback undoes a partial refund | modelled, but not by the engine that will run it |
| migrations 000–034 apply cleanly and are replayable | no DDL engine |
| `THROW 50034 / 50035` fire on a wrong-shaped object | no `sys.*` catalog |

## Runtime and image

Use the **existing container runtime only** (Docker Desktop, already present — the repo's
`validate-locally-before-cloud-build` practice depends on it). No host packages are installed.

| item | value |
|---|---|
| image | `mcr.microsoft.com/mssql/server` pinned **by digest** — see "Pinning" below |
| pull size | ~1.5 GB compressed, **~2.9 GB on disk** |
| data volume | ~500 MB for an empty DB + 35 migrations; allow **4 GB total** |
| container RAM | 2 GB minimum (SQL Server refuses to start below it) |
| host port | **11433** → container 1433 (deliberately NOT 1433, so it cannot collide with, or be mistaken for, a local/prod instance) |
| lifetime | ephemeral; destroyed with its volume at the end |

`mssql-tools18` is **inside** the image (`/opt/mssql-tools18/bin/sqlcmd`), so nothing is
installed on the host. Python connects with the `pyodbc` already used by the repo.

## Credentials

The SA password is generated per-run into the shell environment, never written to a file,
never committed, never logged:

```bash
export MSSQL_SA_PASSWORD="$(python -c 'import secrets,string;a=string.ascii_letters+string.digits;print("Aa1!"+"".join(secrets.choice(a) for _ in range(20)))')"
```

It is passed to the container via `-e` and to the test via the same variable. This is a
throwaway local instance with no customer data; **no Key Vault secret, no production
connection string and no Azure credential is used or referenced.**

## Pinning the image

`2022-latest` is a MUTABLE tag: the bytes behind it change without notice, so a suite that
passed last week could fail today for reasons unrelated to this branch. Resolve the digest
ONCE, record it here, and use the digest thereafter.

Step 1 — resolve (this is the only step that contacts the registry):

```bash
docker buildx imagetools inspect mcr.microsoft.com/mssql/server:2022-latest --format "{{.Manifest.Digest}}"
```

Step 2 — record the result in this document, replacing the placeholder:

    MSSQL_IMAGE=mcr.microsoft.com/mssql/server@sha256:<PASTE_DIGEST_HERE>

**Not yet resolved.** No registry call has been made, so this document contains a placeholder
rather than a digest. Resolving it is the first authorized action of the next phase, and the
digest belongs in this file so a later run is reproducible.

## Exact commands

Container name is explicit and unique to this harness, so the cleanup target cannot be
ambiguous:

```bash
export MSSQL_CONTAINER=bettersnap-mssql-test-11433
```

```bash
export MSSQL_IMAGE=mcr.microsoft.com/mssql/server@sha256:PASTE_DIGEST_HERE
```

```bash
export MSSQL_SA_PASSWORD="$(python -c 'import secrets,string;a=string.ascii_letters+string.digits;print("Aa1!"+"".join(secrets.choice(a) for _ in range(20)))')"
```

```bash
docker run -d --name "$MSSQL_CONTAINER" -p 127.0.0.1:11433:1433 -m 2g -e ACCEPT_EULA=Y -e MSSQL_SA_PASSWORD="$MSSQL_SA_PASSWORD" -e MSSQL_PID=Developer "$MSSQL_IMAGE"
```

Note `-p 127.0.0.1:11433:1433`: the port is bound to the loopback interface only, so the
throwaway instance is not reachable from the network even briefly.

On **Git Bash / MSYS**, `MSYS_NO_PATHCONV=1` is REQUIRED: without it the shell rewrites the
container-side path `/opt/mssql-tools18/bin/sqlcmd` into `C:/Program Files/Git/opt/...` and
docker reports `no such file or directory` for a binary that is present. Observed on the first
real run. (`-C` trusts the container's self-signed certificate; it is a throwaway instance.)

```bash
MSYS_NO_PATHCONV=1 docker exec "$MSSQL_CONTAINER" /opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -P "$MSSQL_SA_PASSWORD" -C -Q "CREATE DATABASE bettersnap_test;"
```

PowerShell needs no such workaround:

```powershell
docker exec $env:MSSQL_CONTAINER /opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -P $env:MSSQL_SA_PASSWORD -C -Q "CREATE DATABASE bettersnap_test;"
```

```bash
python Bettersnap-aI_Backend/tests/integration/run_sqlserver_suite.py --port 11433
```

Cleanup — validate the target before removing it, so a mistyped or shared name cannot be
destroyed:

```bash
docker inspect --format "{{.Name}} {{.Config.Image}}" "$MSSQL_CONTAINER"
```

```bash
docker rm -f "$MSSQL_CONTAINER"
```

No volume is created, so removing the container removes the data. `docker image rm
"$MSSQL_IMAGE"` reclaims the ~2.9 GB if the image is not wanted.

## The harness

`tests/integration/run_sqlserver_suite.py` now EXISTS (it did not when this plan was first
written — the command above was conceptual). Two modes need no database and no container:

```bash
python Bettersnap-aI_Backend/tests/integration/run_sqlserver_suite.py --list
```

```bash
python Bettersnap-aI_Backend/tests/integration/run_sqlserver_suite.py --self-check
```

Safety is enforced in code, not by convention (`tests/integration/guardrails.py`): localhost
only, port 11433 only (1433 refused outright), database `bettersnap_test` only, any Azure SQL
hostname or production database name aborts with exit 2 before a connection is attempted, and
no credential is ever printed. `tests/test_integration_harness_offline.py` tests every one of
those refusals in the ordinary offline suite.

## Coverage

Generated from the harness's own case registry — `--list` prints exactly this, so the document
cannot drift from the code. **Foundational** cases abort the suite on failure; a case whose
prerequisites did not pass is BLOCKED rather than run, so one broken schema cannot produce a
dozen misleading failures.

| # | reachability | case | prerequisites | exercises (REAL runtime function) |
|---|---|---|---|---|
| 1 | reachable | migrations 000-034 apply to an empty database (exact canonical set) | — · **foundational** | `scripts/run_migrations.apply_migration + verify_runtime_schema` |
| 2 | reachable | re-running every migration is a no-op (replay / idempotency) | 1 | `scripts/run_migrations.split_batches` |
| 3 | reachable | FK_lora_trainings_fused_job exists, is enabled and is TRUSTED | 1 · **foundational** | `migrations/034_fused_job_link.sql` |
| 4 | reachable | FK_credit_tx_user exists, is enabled and is TRUSTED | 1 · **foundational** | `migrations/000_baseline.sql — the premise of the paid-orphan reasoning` |
| 5 | reachable | the filtered unique fused-job index rejects a second binding | 1 · **foundational** | `migrations/034_fused_job_link.sql` |
| 6 | schema-drift | 034 re-trusts a correct NOCHECK FK, and THROWs 50034 / 50035 on wrong shapes | 1, 3, 5 | `migrations/034_fused_job_link.sql guards` |
| 7 | reachable | two concurrent retry claims produce exactly one retry | 1 | `shared.provisioning_retry.retry_job` |
| 8 | reachable | two concurrent fused allocations: one allocates, one serializes and reuses | 1, 5 | `shared.provisioning_retry.allocate_fused_job` |
| 9 | reachable | a rollback between the state update and commit leaves NOTHING | 1 | `shared.provisioning_retry.retry_job + shared.outbox.outbox_add` |
| 10 | reachable | five CONCURRENT connections terminalizing one job refund EXACTLY once | 1 | `shared.provisioning_retry.terminalize_and_refund` |
| 11 | schema-drift | a corrupt execution history fails closed and is left untouched | 1 | `shared.provisioning_retry.retry_job / parse_history` |
| 12 | reachable | an org member who left leaves the refund PENDING; rejoining settles it once | 1 | `shared.provisioning_retry.terminalize_and_refund + compensate_pending_refund` |
| 13 | reachable | the schema accepts a NEGATIVE retrain charge (why accounting_invalid exists) | 1 | `migrations/026_retrain_credit_buckets.sql — no CHECK constraint` |
| 14 | reachable | ORPHAN_USER:% and TRAINING_ACCOUNTING_INVALID:% return disjoint sets | 1 | `shared.training_orphan.build_orphan_marker / build_accounting_invalid_marker` |

## Destructive DDL — honestly scoped

Case 6 DROPs and re-creates a constraint `WITH NOCHECK`, creates a wrong-shaped index, and
creates a same-named FK mapping the WRONG columns. That is the only way to prove migration
034's guards actually fire, and it is permitted **only** because:

* `guardrails.py` has already refused anything that is not the disposable local
  `bettersnap_test` database on port 11433;
* case 6 restores the correct shapes in a `finally`, even when an assertion fails;
* an offline test asserts destructive DDL appears **nowhere else** in the suite, and never in
  the seeding helpers.

An earlier version of this document claimed no fixture ever drops or NOCHECKs a constraint.
That was wrong — the test backing it only scanned `harness.py`. The claim and the test have
both been corrected.

## Native error numbers, not string matching

Case 6 wraps each migration batch in T-SQL `BEGIN TRY … BEGIN CATCH SET @caught =
ERROR_NUMBER()`, so **SQL Server reports its own error number**. A coincidental `50034`
appearing in some driver message cannot satisfy the assertion. Three separate outcomes are
proven:

| drift injected | expected |
|---|---|
| correct FK recreated `WITH NOCHECK` | error `0` — 034 **re-validates** it and leaves it trusted |
| same-named FK mapping `user_id → users.user_id` | native **50034** |
| same-named index that is neither unique nor filtered | native **50035** |

Cases 7 and 8 use **real threads on separate connections with a `threading.Barrier`**, so the
overlap is proven rather than assumed; both threads are joined with a timeout and the suite
fails rather than hangs if either does not finish. Case 10 uses five sequential connections,
which is what "exactly once" actually requires. Every connection sets `SET LOCK_TIMEOUT 5000`
and a 20s query timeout.

### Reachability audit (corrects the original plan)

The FKs decide which states may be seeded, and two of them invalidate the original wording:

| FK | exists? | consequence |
|---|---|---|
| `FK_jobs_user` (jobs → users) | **yes** | a user with any job **cannot** be deleted |
| `FK_credit_tx_user` (ledger → users) | **yes** | a user with any ledger row **cannot** be deleted |
| `organization_members.user_id` | **no FK** | a membership row **can** vanish while the user remains |
| `lora_trainings.user_id` | **no FK** | a training **can** outlive its user |

So "delete the users row, then refund the job" is **impossible**, not merely unlikely — a
personal job can never lose its refund target. The genuinely reachable missing-target is an
**organization** one, and removing/re-adding a member is an ordinary product operation. Cases
8 and 12 were rewritten accordingly.

Only two cases model states production cannot reach, and both say so in their own name and in
`--list` output: **case 6** (a hand-altered constraint, which is the drift the 034 guards
exist to catch) and **case 11** (a corrupt `provisioning_execution_ids`, which is what the
fail-closed path exists for). Neither ever disables, drops or bypasses a constraint to
manufacture its scenario — asserted by an offline test.

## Not covered, deliberately

Azure Container Apps, Log Analytics, blob storage and the queue are all out of scope: this
proves the **database** contract only. The ACA behaviour remains covered by the offline
evidence/classifier tests.

## Authorization checklist

- [ ] Docker Desktop running
- [ ] ~4 GB free disk
- [ ] Port 11433 free
- [ ] Explicit "go" to start the container

Nothing above has been executed.
