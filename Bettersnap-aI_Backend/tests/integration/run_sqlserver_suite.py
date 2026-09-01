#!/usr/bin/env python
"""Isolated SQL Server integration suite.

WHAT THIS PROVES THAT THE OFFLINE SUITE CANNOT
The offline fakes honour WHERE clauses, rowcount and commit/rollback visibility, but they have
NO LOCK MANAGER, NO constraint engine and NO DDL. Everything below needs a real engine:
UPDLOCK/HOLDLOCK actually serializing connections, a trusted FK actually refusing an orphan, a
filtered unique index actually rejecting a duplicate, THROW 50034/50035 actually firing, and
migrations 000-036 actually applying and replaying.

EVERY CASE CALLS THE REAL PRODUCTION FUNCTION. No SQL is copied out of shared/ into this file
to make a test pass; where a statement appears here it is seeding, drift injection, or
assertion -- never a re-implementation of the behaviour under test.

DESTRUCTIVE SCHEMA MANIPULATION IS PERMITTED, IN EXACTLY ONE PLACE.
Case 6 deliberately DROPs and re-creates a constraint WITH NOCHECK, and creates a wrong-shaped
index, because that is the only way to prove migration 034's guards actually fire. That is
safe ONLY because guardrails.py has already refused anything that is not the disposable local
`bettersnap_test` database on port 11433, and because case 6 restores the correct shapes in a
`finally`. No other case may touch schema, and an offline test enforces that scoping.

SAFETY: see tests/integration/guardrails.py. localhost only, port 11433 only, database
bettersnap_test only, any Azure hostname aborts, no credential is ever printed.

    python tests/integration/run_sqlserver_suite.py --port 11433
    python tests/integration/run_sqlserver_suite.py --list        # no DB needed
    python tests/integration/run_sqlserver_suite.py --self-check  # no DB needed

Exit code is nonzero on any failed or blocked case.
"""
import argparse
import os
import sys
import threading
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.dirname(os.path.dirname(HERE))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from tests.integration import guardrails                       # noqa: E402
from tests.integration.harness import (                          # noqa: E402
    Harness, REACHABLE, DRIFT, same_guid,
)

# The EXACT migration set this plan covers. Pinned, not counted: if a 037 appears, the scope of
# this suite changed and that needs a reviewed plan update, not a silently wider run.
CANONICAL_MIGRATIONS = (
    "000_baseline.sql", "001_gpu_dispatch_lease.sql", "002_jobs_dispatch_idempotency.sql",
    "003_user_plans.sql", "004_lora_trainings.sql", "005_trial_plan_default.sql",
    "006_retrain.sql", "007_retention.sql", "008_terms_accepted.sql",
    "009_stripe_columns.sql", "010_stripe_webhook_idempotency.sql", "011_dunning.sql",
    "012_reserved.sql", "013_cancel_pending.sql", "014_outbox.sql", "015_dispatched_at.sql",
    "016_source_type.sql", "017_pending_purchases.sql",
    "018_monthly_checkout_reservation.sql", "019_separate_credit_balances.sql",
    "020_reconcile_credit_total.sql", "021_clear_stale_monthly_balances.sql",
    "022_teams_organizations.sql", "023_teams_invitations_members.sql",
    "024_credit_ledger.sql", "025_checkout_and_credit_split.sql",
    "026_retrain_credit_buckets.sql", "027_catalog_tables.sql", "028_biometric_consent.sql",
    "029_audit_log.sql", "030_admin_audit_log.sql", "031_admin_user_status_and_notes.sql",
    "032_fix_org_status_constraint.sql", "033_provisioning_retry.sql",
    "034_fused_job_link.sql", "035_organization_branding.sql",
    "036_teams_pricing_snapshot.sql", "037_unify_credit_buckets.sql",
    # Covering indexes for the bounded dashboard read model and the team roster. Both are
    # already applied in production; registering them here is what keeps this list honest
    # about what the schema actually contains.
    "038_dashboard_history_index.sql", "039_team_dashboard_roster_index.sql",
)

# SQL Server native error numbers raised by migration 034's shape guards.
THROW_FK_WRONG_SHAPE = 50034
THROW_INDEX_WRONG_SHAPE = 50035

# The normalised filter migration 034's index must carry.
EXPECTED_INDEX_FILTER = "fused_job_id IS NOT NULL"

# Join bound for the contended cases. Comfortably above LOCK_TIMEOUT_MS (30s) so a genuine
# lock wait resolves rather than tripping the thread bound -- but still finite.
RACE_TIMEOUT_S = 120

CASES = []


def case(number, title, exercises, reachability=REACHABLE, requires=(), foundational=False):
    """Register a case.

    `exercises`   names the REAL runtime function under test, so the plan-to-code mapping
                  lives in the source rather than in a document that can drift.
    `requires`    case numbers that must have PASSED first. A case whose prerequisites did not
                  pass is BLOCKED, not run -- cascading failures hide the real one.
    `foundational` a failure aborts the whole suite immediately: nothing downstream is
                  meaningful if the migrations did not apply.
    """
    def wrap(fn):
        fn.number = number
        fn.title = title
        fn.exercises = exercises
        fn.reachability = reachability
        fn.requires = tuple(requires)
        fn.foundational = foundational
        CASES.append(fn)
        return fn
    return wrap


class Failed(AssertionError):
    pass


def expect(condition, message):
    if not condition:
        raise Failed(message)


def expect_eq(actual, wanted, message):
    if actual != wanted:
        raise Failed("%s (got %r, wanted %r)" % (message, actual, wanted))


# ── shared helpers ───────────────────────────────────────────────────────────
def _batch_error_number(conn, batch):
    """The NATIVE SQL Server error number for one batch, from ERROR_NUMBER() itself.

    Not a substring match against a driver message: the batch is wrapped in T-SQL TRY/CATCH
    and SQL Server reports its own error number, so a coincidental '50034' in some other text
    cannot pass this. Returns 0 when the batch succeeds.
    """
    cur = conn.cursor()
    wrapped = ("SET NOCOUNT ON;\nDECLARE @caught INT = 0;\nBEGIN TRY\n"
               + batch + "\nEND TRY\nBEGIN CATCH\nSET @caught = ERROR_NUMBER();\nEND CATCH;\n"
               "SELECT @caught;")
    cur.execute(wrapped)
    row = cur.fetchone()
    number = int(row[0]) if row and row[0] is not None else 0
    while cur.nextset():
        pass
    return number


def _migration_sql(name):
    import run_migrations
    for path in run_migrations._migration_files():
        if os.path.basename(path) == name:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
    raise Failed("migration %s not found" % name)


def _batches(sql):
    import run_migrations
    return [b for b in run_migrations.split_batches(sql) if b.strip()]


def _replay(conn, sql):
    """Apply every batch, committing. Raises whatever SQL Server raises."""
    cur = conn.cursor()
    for batch in _batches(sql):
        cur.execute(batch)
    conn.commit()


def _replay_capturing_error(conn, sql):
    """Replay, returning the FIRST native error number any batch produced (0 if none)."""
    for batch in _batches(sql):
        number = _batch_error_number(conn, batch)
        if number:
            conn.rollback()
            return number
    conn.commit()
    return 0


def _race(fns, timeout=30):
    """Run N callables on real threads released by a shared barrier, so the overlap is proven
    rather than assumed. Bounded: the suite fails rather than hangs."""
    start = threading.Barrier(len(fns), timeout=timeout)
    results = [None] * len(fns)

    def runner(index, fn):
        try:
            start.wait()
            results[index] = ("ok", fn())
        except Exception as exc:                            # noqa: BLE001
            results[index] = ("error", exc)

    threads = [threading.Thread(target=runner, args=(i, fn), daemon=True)
               for i, fn in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout)
    alive = [i for i, t in enumerate(threads) if t.is_alive()]
    expect(not alive,
           "thread(s) %s did not finish within %ds — possible deadlock" % (alive, timeout))
    return results


# ── 1-2: migrations ──────────────────────────────────────────────────────────
@case(1, "migrations 000-034 apply to an empty database (exact canonical set)",
      "scripts/run_migrations.apply_migration + verify_runtime_schema",
      foundational=True)
def case_migrations(h):
    import run_migrations
    on_disk = tuple(sorted(os.path.basename(p) for p in run_migrations._migration_files()))
    expect_eq(on_disk, CANONICAL_MIGRATIONS,
              "the migration set on disk is not the canonical 000-034 set this plan covers. "
              "A new migration means the scope of this suite changed, which needs a reviewed "
              "plan update rather than a silently wider run")

    conn = h.connect()
    h.reset_schema(conn)
    applied = h.apply_all_migrations(conn)
    expect_eq(tuple(applied), CANONICAL_MIGRATIONS, "applied set")
    expect_eq(int(h.scalar(conn, "SELECT COUNT(*) FROM dbo.schema_migrations")),
              len(CANONICAL_MIGRATIONS), "schema_migrations row count")
    expect_eq(h.verify_runtime_schema(conn), [],
              "verify_runtime_schema must report no missing columns")


@case(2, "re-running every migration is a no-op (replay / idempotency)",
      "scripts/run_migrations.split_batches", requires=(1,))
def case_replay(h):
    conn = h.connect()
    before = (h.scalar(conn, "SELECT COUNT(*) FROM sys.columns"),
              h.scalar(conn, "SELECT COUNT(*) FROM sys.indexes"),
              h.scalar(conn, "SELECT COUNT(*) FROM sys.foreign_keys"))
    for name in CANONICAL_MIGRATIONS:
        _replay(conn, _migration_sql(name))
    after = (h.scalar(conn, "SELECT COUNT(*) FROM sys.columns"),
             h.scalar(conn, "SELECT COUNT(*) FROM sys.indexes"),
             h.scalar(conn, "SELECT COUNT(*) FROM sys.foreign_keys"))
    expect_eq(after, before, "replay changed the schema (columns, indexes, foreign keys)")


# ── 3-5: constraint shapes ───────────────────────────────────────────────────
@case(3, "FK_lora_trainings_fused_job exists, is enabled and is TRUSTED",
      "migrations/034_fused_job_link.sql", requires=(1,), foundational=True)
def case_fused_fk(h):
    conn = h.connect()
    exists, disabled, untrusted = h.fk(conn, "FK_lora_trainings_fused_job")
    expect(exists, "FK_lora_trainings_fused_job is missing")
    expect(disabled is False, "FK_lora_trainings_fused_job is disabled")
    expect(untrusted is False,
           "FK_lora_trainings_fused_job is NOT TRUSTED; 034 creates it WITH CHECK precisely "
           "so it can be relied on")
    user_id = h.seed_user(conn)
    training_id = h.seed_training(conn, user_id)
    cur = conn.cursor()
    try:
        cur.execute("UPDATE lora_trainings SET fused_job_id = ? WHERE training_id = ?",
                    h.new_id(), training_id)
        conn.commit()
        raise Failed("the FK allowed a fused_job_id pointing at a nonexistent job")
    except Failed:
        raise
    except Exception:
        conn.rollback()


@case(4, "FK_credit_tx_user exists, is enabled and is TRUSTED",
      "migrations/000_baseline.sql — the premise of the paid-orphan reasoning",
      requires=(1,), foundational=True)
def case_credit_tx_fk(h):
    conn = h.connect()
    exists, disabled, untrusted = h.fk(conn, "FK_credit_tx_user")
    expect(exists, "FK_credit_tx_user is missing")
    expect(disabled is False, "FK_credit_tx_user is disabled")
    expect(untrusted is False,
           "FK_credit_tx_user is NOT TRUSTED. The ORPHAN_USER paid path asserts a "
           "retrain_charge row cannot exist without its user; an untrusted FK breaks that")
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO credit_transactions (user_id, amount, transaction_type) "
            "VALUES (?, -1, 'job_reserve')", h.new_id())
        conn.commit()
        raise Failed("a ledger row was accepted for a nonexistent user")
    except Failed:
        raise
    except Exception:
        conn.rollback()


@case(5, "the filtered unique fused-job index rejects a second binding",
      "migrations/034_fused_job_link.sql", requires=(1,), foundational=True)
def case_fused_index(h):
    conn = h.connect()
    meta = h.index(conn, "dbo.lora_trainings", "UX_lora_trainings_fused_job")
    expect(meta is not None, "UX_lora_trainings_fused_job is missing")
    expect(meta["is_unique"], "UX_lora_trainings_fused_job is not UNIQUE")
    expect(meta["has_filter"], "UX_lora_trainings_fused_job is not filtered")
    expect(not meta["is_disabled"], "UX_lora_trainings_fused_job is disabled")
    user_id = h.seed_user(conn)
    job_id = h.seed_job(conn, user_id, status="waiting_lora")
    t1 = h.seed_training(conn, user_id)
    t2 = h.seed_training(conn, user_id)
    cur = conn.cursor()
    cur.execute("UPDATE lora_trainings SET fused_job_id = ? WHERE training_id = ?", job_id, t1)
    conn.commit()
    try:
        cur.execute("UPDATE lora_trainings SET fused_job_id = ? WHERE training_id = ?",
                    job_id, t2)
        conn.commit()
        raise Failed("two trainings were allowed to bind the SAME fused job")
    except Failed:
        raise
    except Exception:
        conn.rollback()
    expect(int(h.scalar(conn, "SELECT COUNT(*) FROM lora_trainings "
                              "WHERE fused_job_id IS NULL")) >= 1,
           "NULL fused_job_id must not be constrained")


# ── 6: the 034 guards actually fire ──────────────────────────────────────────
@case(6, "034 re-trusts a correct NOCHECK FK, and THROWs 50034 / 50035 on wrong shapes",
      "migrations/034_fused_job_link.sql guards", reachability=DRIFT,
      requires=(1, 3, 5))
def case_034_guards(h):
    """DRIFT FIXTURE. This case DROPs and re-creates constraints deliberately — it is the only
    way to prove the guards fire, and it is safe only because guardrails.py has already refused
    anything but the disposable bettersnap_test database. Correct shapes are restored in
    `finally`, even when an assertion fails."""
    conn = h.connect()
    sql034 = _migration_sql("034_fused_job_link.sql")
    cur = conn.cursor()
    try:
        # (a) a CORRECT FK that is merely untrusted must be RE-TRUSTED, not rejected.
        cur.execute("ALTER TABLE dbo.lora_trainings "
                    "DROP CONSTRAINT FK_lora_trainings_fused_job")
        cur.execute("ALTER TABLE dbo.lora_trainings WITH NOCHECK ADD CONSTRAINT "
                    "FK_lora_trainings_fused_job FOREIGN KEY (fused_job_id) "
                    "REFERENCES dbo.jobs (job_id)")
        conn.commit()
        _, _, untrusted = h.fk(conn, "FK_lora_trainings_fused_job")
        expect(untrusted is True, "the NOCHECK recreation should have left it untrusted")
        number = _replay_capturing_error(conn, sql034)
        expect_eq(number, 0,
                  "a CORRECT but untrusted FK must be re-validated by 034, not thrown on")
        _, _, untrusted = h.fk(conn, "FK_lora_trainings_fused_job")
        expect(untrusted is False, "034 must leave a correct FK TRUSTED after re-running")

        # (b) a SAME-NAMED FK of the WRONG SHAPE must THROW 50034.
        cur.execute("ALTER TABLE dbo.lora_trainings "
                    "DROP CONSTRAINT FK_lora_trainings_fused_job")
        cur.execute("ALTER TABLE dbo.lora_trainings WITH CHECK ADD CONSTRAINT "
                    "FK_lora_trainings_fused_job FOREIGN KEY (user_id) "
                    "REFERENCES dbo.users (user_id)")
        conn.commit()
        number = _replay_capturing_error(conn, sql034)
        expect_eq(number, THROW_FK_WRONG_SHAPE,
                  "034 must raise native error %d for a same-named FK mapping the wrong "
                  "columns" % THROW_FK_WRONG_SHAPE)
        cur.execute("ALTER TABLE dbo.lora_trainings "
                    "DROP CONSTRAINT FK_lora_trainings_fused_job")
        conn.commit()
        _replay(conn, sql034)
        _, _, untrusted = h.fk(conn, "FK_lora_trainings_fused_job")
        expect(untrusted is False, "034 must rebuild the correct trusted FK")

        # (c) a SAME-NAMED index of the WRONG SHAPE must THROW 50035.
        cur.execute("DROP INDEX UX_lora_trainings_fused_job ON dbo.lora_trainings")
        cur.execute("CREATE INDEX UX_lora_trainings_fused_job "
                    "ON dbo.lora_trainings (fused_job_id)")   # not unique, not filtered
        conn.commit()
        number = _replay_capturing_error(conn, sql034)
        expect_eq(number, THROW_INDEX_WRONG_SHAPE,
                  "034 must raise native error %d for a wrong-shaped index"
                  % THROW_INDEX_WRONG_SHAPE)
    finally:
        # If the body already failed, Python sets __context__ on whatever this raises, so
        # traceback.format_exc() prints BOTH: the original assertion AND the restoration
        # failure. Neither is hidden, and the restore is never swallowed.
        _restore_034_shapes(h, conn, sql034)


class RestorationFailed(AssertionError):
    """Case 6 could not put the schema back. NEVER swallowed: a silently broken FK or index
    would make every later case meaningless, and would look like a code defect rather than a
    fixture that failed to clean up."""


def _restore_034_shapes(h, conn, sql034):
    """Put the schema back and PROVE it. Runs in `finally`, and RAISES on any problem.

    The individual DROPs are best-effort by design -- whichever object survived the drift is
    unknown, so "it was not there" is a legitimate outcome for either statement. What is NOT
    best-effort is the replay and the shape verification that follow: if 034 cannot rebuild
    the correct FK and index, that must fail loudly here rather than corrupt every case after
    it. The verification below is what makes ignoring the drops safe.
    """
    try:
        conn.rollback()
    except Exception:
        pass
    cur = conn.cursor()
    for stmt in ("ALTER TABLE dbo.lora_trainings DROP CONSTRAINT FK_lora_trainings_fused_job",
                 "DROP INDEX UX_lora_trainings_fused_job ON dbo.lora_trainings"):
        try:
            cur.execute(stmt)
            conn.commit()
        except Exception:
            # The object may legitimately not exist at this point; _verify_034_shape is the
            # authority on whether the END STATE is correct.
            conn.rollback()
    try:
        _replay(conn, sql034)
    except Exception as exc:
        raise RestorationFailed(
            "case 6 could not replay migration 034 to restore the schema: %s"
            % guardrails.redact(exc))
    _verify_034_shape(h, conn)


def _verify_034_shape(h, conn):
    """Assert the EXACT shape migration 034 is supposed to produce. Raises RestorationFailed.

    Checks everything 034's own guards check, so "restored" can never mean "an object with
    the right name exists".
    """
    shape = h.fused_link_shape(conn)
    fk, ix = shape["fk"], shape["index"]

    if fk is None:
        raise RestorationFailed("FK_lora_trainings_fused_job was not restored")
    problems = []
    if fk["col_pairs"] != 1:
        problems.append("expected exactly 1 column pair, got %d" % fk["col_pairs"])
    if fk["parent_col"] != "fused_job_id":
        problems.append("parent column is %r, expected 'fused_job_id'" % fk["parent_col"])
    if fk["ref_table"] != "jobs":
        problems.append("references %r, expected 'jobs'" % fk["ref_table"])
    if fk["ref_col"] != "job_id":
        problems.append("references column %r, expected 'job_id'" % fk["ref_col"])
    if fk["is_disabled"]:
        problems.append("the FK is DISABLED")
    if fk["is_not_trusted"]:
        problems.append("the FK is NOT TRUSTED")
    if fk["delete_action"] != "NO_ACTION":
        problems.append("delete action is %r, expected NO_ACTION" % fk["delete_action"])
    if fk["update_action"] != "NO_ACTION":
        problems.append("update action is %r, expected NO_ACTION" % fk["update_action"])
    if problems:
        raise RestorationFailed(
            "FK_lora_trainings_fused_job was restored with the WRONG shape: %s"
            % "; ".join(problems))

    if ix is None:
        raise RestorationFailed("UX_lora_trainings_fused_job was not restored")
    problems = []
    if not ix["is_unique"]:
        problems.append("the index is not UNIQUE")
    if not ix["has_filter"]:
        problems.append("the index is not filtered")
    if ix["is_disabled"]:
        problems.append("the index is DISABLED")
    if ix["key_cols"] != 1:
        problems.append("expected exactly 1 key column, got %d" % ix["key_cols"])
    if ix["key_col"] != "fused_job_id":
        problems.append("key column is %r, expected 'fused_job_id'" % ix["key_col"])
    if ix["included_cols"] != 0:
        problems.append("expected 0 included columns, got %d" % ix["included_cols"])
    if ix["filter"] != EXPECTED_INDEX_FILTER:
        problems.append("filter normalises to %r, expected %r"
                        % (ix["filter"], EXPECTED_INDEX_FILTER))
    if problems:
        raise RestorationFailed(
            "UX_lora_trainings_fused_job was restored with the WRONG shape: %s"
            % "; ".join(problems))


# ── 7-8: real concurrency ────────────────────────────────────────────────────
@case(7, "two concurrent retry claims produce exactly one retry",
      "shared.provisioning_retry.retry_job", requires=(1,))
def case_concurrent_retry(h):
    from shared import provisioning_retry as pr
    from shared.outbox import outbox_add
    setup = h.connect()
    user_id = h.seed_user(setup, credits=0)
    exec_id = h.label("exec")
    job_id = h.seed_job(setup, user_id, status="processing", execution_id=exec_id)
    setup.commit()

    conns = [h.connect(), h.connect()]

    def attempt(conn):
        def run():
            result = pr.retry_job(conn.cursor(), job_id, exec_id, outbox_add=outbox_add,
                                  queue_name="inference-jobs")
            conn.commit()
            return result["plan"]
        return run

    results = _race([attempt(c) for c in conns])
    errors = [v for status, v in results if status == "error"]
    expect(not errors, "a racing retry raised: %s" % guardrails.redact(errors))
    plans = sorted(v for _s, v in results)
    expect_eq(plans, sorted([pr.PLAN_ALREADY_HANDLED, pr.PLAN_RETRY]),
              "exactly one connection may win the retry")

    check = h.connect()
    expect_eq(int(h.scalar(check, "SELECT provisioning_attempts FROM jobs WHERE job_id = ?",
                           job_id)), 1, "provisioning_attempts must be exactly 1")
    expect_eq(len(h.outbox_rows(check)), 1, "exactly one retry outbox row")
    expect_eq(h.scalar(check, "SELECT status FROM jobs WHERE job_id = ?", job_id), "queued",
              "the job must end queued")


@case(8, "two concurrent fused allocations: one allocates, one serializes and reuses",
      "shared.provisioning_retry.allocate_fused_job", requires=(1, 5))
def case_concurrent_fused(h):
    """The UPDLOCK/HOLDLOCK on the training row is what makes this deterministic: the second
    connection BLOCKS until the first commits, then sees the persisted link and reuses it.
    Asserting the OUTCOMES (not just the final state) is what proves the lock did its job."""
    from shared import provisioning_retry as pr
    setup = h.connect()
    user_id = h.seed_user(setup)
    job_a = h.seed_job(setup, user_id, status="waiting_lora")
    job_b = h.seed_job(setup, user_id, status="waiting_lora")
    training_id = h.seed_training(setup, user_id)
    setup.commit()

    conns = [h.connect(), h.connect()]

    def attempt(conn):
        def run():
            try:
                jid, why = pr.allocate_fused_job(conn.cursor(), training_id, user_id)
                conn.commit()
                return (why, jid)
            except pr.FusedLinkConflict as exc:
                conn.rollback()
                return ("conflict", str(exc))
        return run

    results = _race([attempt(c) for c in conns])
    errors = [v for status, v in results if status == "error"]
    expect(not errors,
           "a racing allocation raised (a lock timeout here means HOLDLOCK did not "
           "serialize as designed): %s" % guardrails.redact(errors))

    outcomes = [v for _s, v in results]
    whys = sorted(why for why, _ in outcomes)
    expect_eq(whys, ["allocated", "reused existing link"],
              "expected exactly ONE initial allocation and ONE serialized reuse; anything "
              "else means the UPDLOCK/HOLDLOCK did not serialize the two connections")
    returned = {jid for _why, jid in outcomes}
    expect_eq(len(returned), 1,
              "both connections must return the SAME job; got %r" % (returned,))
    bound_job = returned.pop()

    check = h.connect()
    persisted = h.scalar(check, "SELECT fused_job_id FROM lora_trainings "
                                "WHERE training_id = ?", training_id)
    expect(same_guid(persisted, bound_job),
           "the persisted fused_job_id must be the job both callers returned")
    # bound_job came back from SQL (UPPERCASE); job_a/job_b were minted by new_id()
    # (lowercase). Exact string comparison here failed a real run, and the `other` selection
    # below silently picked the WRONG job rather than failing -- hence parsing, not casing.
    expect(same_guid(bound_job, job_a) or same_guid(bound_job, job_b),
           "the bound job is not one of the parked jobs")
    other = job_b if same_guid(bound_job, job_a) else job_a
    expect_eq(h.scalar(check, "SELECT status FROM jobs WHERE job_id = ?", str(bound_job)),
              "processing", "the bound job must be claimed")
    expect_eq(h.scalar(check, "SELECT status FROM jobs WHERE job_id = ?", other),
              "waiting_lora", "the OTHER parked job must be untouched")


# ── 9-10: atomicity and exactly-once ─────────────────────────────────────────
@case(9, "a rollback between the state update and commit leaves NOTHING",
      "shared.provisioning_retry.retry_job + shared.outbox.outbox_add", requires=(1,))
def case_rollback_atomicity(h):
    from shared import provisioning_retry as pr
    from shared.outbox import outbox_add
    setup = h.connect()
    user_id = h.seed_user(setup)
    exec_id = h.label("exec-rb")
    job_id = h.seed_job(setup, user_id, status="processing", execution_id=exec_id)
    setup.commit()
    before_outbox = len(h.outbox_rows(setup))

    conn = h.connect()
    result = pr.retry_job(conn.cursor(), job_id, exec_id, outbox_add=outbox_add,
                          queue_name="inference-jobs")
    expect_eq(result["plan"], pr.PLAN_RETRY, "the retry should have been staged")
    conn.rollback()

    check = h.connect()
    expect_eq(h.scalar(check, "SELECT status FROM jobs WHERE job_id = ?", job_id),
              "processing", "the state change survived a rollback")
    expect_eq(int(h.scalar(check, "SELECT provisioning_attempts FROM jobs WHERE job_id = ?",
                           job_id)), 0, "the attempt counter survived a rollback")
    expect_eq(len(h.outbox_rows(check)), before_outbox,
              "an outbox row survived a rollback — state and message must be atomic")


@case(10, "five CONCURRENT connections serialize and refund EXACTLY once",
       "shared.provisioning_retry.terminalize_and_refund", requires=(1,))
def case_exactly_once_refund(h):
    """PROVES SERIALIZATION, not merely that one finished first.

    A lock timeout is NOT an acceptable outcome here. A contender that timed out never reached
    the guarded UPDATE, so a suite that tolerated timeouts could report "exactly once" while
    four connections had simply given up -- which proves nothing about the guard. Every
    contender must therefore run to completion THROUGH the row lock and report a real verdict:
    one transitions, four find the row already terminal and do nothing.

    Any SQL lock timeout (1222), deadlock (1205), query timeout, or thread that fails to
    finish fails this case. Both bounds stay finite (LOCK_TIMEOUT_MS, RACE_TIMEOUT_S) so it
    cannot hang.
    """
    from shared import provisioning_retry as pr
    from shared import credit_ledger
    setup = h.connect()
    user_id = h.seed_user(setup, credits=0, monthly=0, one_time=0)
    job_id = h.seed_job(setup, user_id, status="processing", credit_cost=40,
                        monthly_cost=25, one_time_cost=15)
    setup.commit()

    conns = [h.connect() for _ in range(5)]

    def attempt(conn):
        def run():
            # NO try/except: a lock timeout, deadlock or any other error must surface as a
            # thread error and fail the case.
            transitioned, amount, state = pr.terminalize_and_refund(
                conn.cursor(), job_id, credit_ledger=credit_ledger)
            conn.commit()
            return (bool(transitioned), amount, state)
        return run

    results = _race([attempt(c) for c in conns], timeout=RACE_TIMEOUT_S)
    errors = [v for status, v in results if status == "error"]
    expect(not errors,
           "every contender must serialize through the guarded row and finish. A lock "
           "timeout (1222) or deadlock (1205) here means a contender never reached the "
           "guard, so 'exactly once' would be unproven: %s" % guardrails.redact(errors))

    outcomes = [v for _s, v in results]
    winners = [o for o in outcomes if o[0]]
    losers = [o for o in outcomes if not o[0]]
    expect_eq(len(winners), 1, "exactly ONE connection may transition the job")
    expect_eq(len(losers), 4, "the other four must have run and found it already terminal")
    expect_eq(winners[0][2], pr.REFUND_DONE, "the winner must have refunded")
    for state in (o[2] for o in losers):
        expect_eq(state, pr.REFUND_NONE,
                  "every loser must report REFUND_NONE -- proof it executed the guarded "
                  "UPDATE and matched zero rows, rather than timing out before it")

    check = h.connect()
    expect_eq(h.balances(check, user_id), (40, 25, 15),
              "aggregate AND both spendable buckets must be restored exactly once")
    refunds = h.ledger(check, job_id=job_id, kind=credit_ledger.REASON_JOB_REFUND)
    expect_eq(len(refunds), 1, "exactly one job_refund ledger row")
    expect_eq(int(refunds[0][2]), 40, "the ledger amount must equal the charge")
    expect_eq(h.scalar(check, "SELECT status FROM jobs WHERE job_id = ?", job_id), "failed",
              "the job must end failed")


# ── 11: corrupt history ──────────────────────────────────────────────────────
@case(11, "a corrupt execution history fails closed and is left untouched",
       "shared.provisioning_retry.retry_job / parse_history", reachability=DRIFT,
       requires=(1,))
def case_corrupt_history(h):
    """DRIFT FIXTURE (data only, no schema change): provisioning_execution_ids is
    NVARCHAR(MAX) with no JSON constraint, so a corrupt value is representable. Production
    writes only valid JSON; this models the corruption the fail-closed path exists for."""
    from shared import provisioning_retry as pr
    from shared.outbox import outbox_add
    setup = h.connect()
    user_id = h.seed_user(setup)
    exec_id = h.label("exec-corrupt")
    job_id = h.seed_job(setup, user_id, status="processing", execution_id=exec_id)
    cur = setup.cursor()
    cur.execute("UPDATE jobs SET provisioning_execution_ids = ? WHERE job_id = ?",
                "{not json", job_id)
    setup.commit()
    before_outbox = len(h.outbox_rows(setup))

    conn = h.connect()
    raised = False
    try:
        pr.retry_job(conn.cursor(), job_id, exec_id, outbox_add=outbox_add,
                     queue_name="inference-jobs")
    except pr.HistoryCorrupt:
        raised = True
    conn.rollback()
    expect(raised, "a corrupt history must raise HistoryCorrupt, not be reset")

    check = h.connect()
    expect_eq(h.scalar(check, "SELECT provisioning_execution_ids FROM jobs "
                              "WHERE job_id = ?", job_id), "{not json",
              "the corrupt history must be left EXACTLY as found")
    expect_eq(h.scalar(check, "SELECT status FROM jobs WHERE job_id = ?", job_id),
              "processing", "nothing may be transitioned on a corrupt history")
    expect_eq(len(h.outbox_rows(check)), before_outbox,
              "no outbox row on a corrupt history")


# ── 12: pending-refund compensation (REACHABLE via org membership) ───────────
@case(12, "an org member who left leaves the refund PENDING; rejoining settles it once",
       "shared.provisioning_retry.terminalize_and_refund + compensate_pending_refund",
       requires=(1,))
def case_pending_refund(h):
    """REACHABLE, and the case the original plan got wrong.

    `FK_jobs_user` and `FK_credit_tx_user` make "delete the users row" IMPOSSIBLE for any job
    that went through reserve_job_slot, so a PERSONAL job can never lose its refund target.
    organization_members has NO FK to users, and removing a member is an ordinary product
    operation, so an ORGANIZATION job genuinely can. Re-adding the member is equally ordinary.
    """
    from shared import provisioning_retry as pr
    from shared import credit_ledger
    setup = h.connect()
    user_id = h.seed_user(setup, credits=0)
    org_id = h.seed_org_membership(setup, user_id, credits=0)
    job_id = h.seed_job(setup, user_id, status="processing", credit_cost=40,
                        organization_id=org_id)
    setup.commit()
    expect_eq(h.remove_org_membership(setup, org_id, user_id), 1,
              "the membership row should have been removed")
    setup.commit()

    conn = h.connect()
    cur = conn.cursor()
    transitioned, _amount, state = pr.terminalize_and_refund(
        cur, job_id, credit_ledger=credit_ledger)
    expect(transitioned, "the job must still terminalize")
    expect_eq(state, pr.REFUND_PENDING, "the refund target is gone, so this must be PENDING")
    plan = pr.build_refund_plan(cur, job_id, credit_ledger=credit_ledger)
    expect(pr.mark_refund_pending(cur, job_id, plan), "the debt marker must be written")
    conn.commit()

    conn2 = h.connect()
    expect_eq(pr.compensate_pending_refund(conn2.cursor(), job_id,
                                           credit_ledger=credit_ledger),
              pr.REFUND_PENDING, "with no membership row the debt must stay pending")
    conn2.commit()
    expect_eq(len(h.ledger(conn2, job_id=job_id,
                           kind=credit_ledger.REASON_JOB_REFUND)), 0,
              "nothing may be ledgered while the target is missing")

    # The real shape (023): there is NO `role` column, and credits_granted is NOT NULL with
    # no DEFAULT. The rejoining member starts with an empty pool -- granted 0, remaining 0 --
    # so the 40 credits observed at the end can only have come from the compensation.
    # status and joined_at carry DEFAULTs and are left to the schema.
    re_add = h.connect()
    re_add.cursor().execute(
        "INSERT INTO organization_members (organization_id, user_id, "
        "credits_granted, credits_remaining) VALUES (?, ?, 0, 0)", org_id, user_id)
    re_add.commit()

    for _ in range(3):
        conn3 = h.connect()
        pr.compensate_pending_refund(conn3.cursor(), job_id, credit_ledger=credit_ledger)
        conn3.commit()

    check = h.connect()
    expect_eq(int(h.scalar(check, "SELECT credits_remaining FROM organization_members "
                                  "WHERE organization_id = ? AND user_id = ?",
                           org_id, user_id)), 40,
              "the org pool must be credited exactly once")
    expect_eq(len(h.ledger(check, job_id=job_id,
                           kind=credit_ledger.REASON_JOB_REFUND)), 1,
              "exactly one job_refund ledger row across three compensation passes")
    expect(pr.read_refund_pending(check.cursor(), job_id) is None,
           "the marker must be cleared after settlement")


# ── 13-14 ────────────────────────────────────────────────────────────────────
@case(13, "the schema accepts a NEGATIVE retrain charge (why accounting_invalid exists)",
       "migrations/026_retrain_credit_buckets.sql — no CHECK constraint", requires=(1,))
def case_negative_charge(h):
    conn = h.connect()
    user_id = h.seed_user(conn)
    training_id = h.seed_training(conn, user_id)
    cur = conn.cursor()
    cur.execute("UPDATE lora_trainings SET monthly_credit_cost = -20 WHERE training_id = ?",
                training_id)
    conn.commit()
    stored = h.scalar(conn, "SELECT monthly_credit_cost FROM lora_trainings "
                            "WHERE training_id = ?", training_id)
    expect_eq(int(stored), -20,
              "026 declares these INT NOT NULL with NO CHECK — a negative must be storable, "
              "which is the entire premise for the accounting_invalid path")
    from shared import training_orphan
    expect(not training_orphan.is_valid_charge(int(stored)),
           "the runtime validator must reject what the schema permits")


@case(14, "ORPHAN_USER:% and TRAINING_ACCOUNTING_INVALID:% return disjoint sets",
       "shared.training_orphan.build_orphan_marker / build_accounting_invalid_marker",
       requires=(1,))
def case_disjoint_markers(h):
    from shared import training_orphan
    conn = h.connect()
    user_id = h.seed_user(conn)
    t_orphan = h.seed_training(conn, user_id, status="failed")
    t_acct = h.seed_training(conn, user_id, status="failed")
    orphan_marker, _ = training_orphan.build_orphan_marker(
        t_orphan, user_id, monthly_owed=20, one_time_owed=15,
        original_error="trainer exited 137")
    acct_marker, _ = training_orphan.build_accounting_invalid_marker(
        t_acct, user_id, monthly_owed=-20, one_time_owed=15)
    cur = conn.cursor()
    cur.execute("UPDATE lora_trainings SET error = ? WHERE training_id = ?",
                orphan_marker, t_orphan)
    cur.execute("UPDATE lora_trainings SET error = ? WHERE training_id = ?",
                acct_marker, t_acct)
    conn.commit()

    cur.execute("SELECT training_id FROM lora_trainings WHERE error LIKE 'ORPHAN_USER:%'")
    orphans = {str(r[0]).lower() for r in cur.fetchall()}
    cur.execute("SELECT training_id FROM lora_trainings "
                "WHERE error LIKE 'TRAINING_ACCOUNTING_INVALID:%'")
    accts = {str(r[0]).lower() for r in cur.fetchall()}
    expect(t_orphan.lower() in orphans, "the orphan marker was not matched by its predicate")
    expect(t_acct.lower() in accts, "the accounting marker was not matched by its predicate")
    expect_eq(orphans & accts, set(),
              "the two predicates must return DISJOINT sets — the operator story depends on "
              "being able to tell the conditions apart")
    stored = h.scalar(conn, "SELECT error FROM lora_trainings WHERE training_id = ?",
                      t_orphan)
    expect(training_orphan.parse_marker(stored) is not None,
           "the marker must parse after a round trip through NVARCHAR(1000)")
    expect_eq(training_orphan.original_error_from(stored), "trainer exited 137",
              "the preserved original error must survive")


# ── runner ───────────────────────────────────────────────────────────────────
def _ordered():
    return sorted(CASES, key=lambda c: c.number)


def self_check():
    """Offline: the guardrails refuse everything they should, and the registry is coherent."""
    failures = []
    for label, fn in (
            ("azure host", lambda: guardrails.check_host("x.database.windows.net")),
            ("remote host", lambda: guardrails.check_host("10.0.0.5")),
            ("prod port", lambda: guardrails.check_port(1433)),
            ("other port", lambda: guardrails.check_port(5432)),
            ("prod database", lambda: guardrails.check_database("bettersnap")),
            ("master database", lambda: guardrails.check_database("master"))):
        try:
            fn()
            failures.append("guardrail did NOT refuse: %s" % label)
        except guardrails.UnsafeTarget:
            pass
    try:
        guardrails.check_target("127.0.0.1", 11433, "bettersnap_test")
    except guardrails.UnsafeTarget as exc:
        failures.append("guardrail wrongly refused the legitimate target: %s" % exc)

    numbers = [c.number for c in CASES]
    if len(set(numbers)) != len(numbers):
        failures.append("duplicate case numbers: %s" % numbers)
    known = set(numbers)
    for c in CASES:
        if not c.exercises:
            failures.append("case %d names no runtime function" % c.number)
        for dep in c.requires:
            if dep not in known:
                failures.append("case %d requires unknown case %d" % (c.number, dep))
            if dep >= c.number:
                failures.append("case %d requires %d, which does not run earlier"
                                % (c.number, dep))
    if len(CANONICAL_MIGRATIONS) != len(set(CANONICAL_MIGRATIONS)):
        failures.append("CANONICAL_MIGRATIONS contains duplicates")

    for line in failures:
        print("SELF-CHECK FAIL: %s" % line)
    if not failures:
        print("self-check OK: %d cases, %d canonical migrations, guardrails refuse every "
              "unsafe target" % (len(CASES), len(CANONICAL_MIGRATIONS)))
    return 0 if not failures else 1


def list_cases():
    for c in _ordered():
        flags = []
        if c.foundational:
            flags.append("foundational")
        if c.requires:
            flags.append("needs %s" % ",".join(str(n) for n in c.requires))
        print("%2d  [%-13s] %-64s %s\n    -> %s"
              % (c.number, c.reachability, c.title,
                 "(%s)" % "; ".join(flags) if flags else "", c.exercises))
    return 0


def _resolve_selection(only):
    """`--only` must establish its prerequisites or refuse clearly."""
    selected = _ordered()
    if not only:
        return selected, None
    wanted = set(only)
    unknown = wanted - {c.number for c in CASES}
    if unknown:
        return None, "unknown case number(s): %s" % ", ".join(str(n) for n in sorted(unknown))
    missing = {}
    for c in _ordered():
        if c.number in wanted:
            gaps = [d for d in c.requires if d not in wanted]
            if gaps:
                missing[c.number] = gaps
    if missing:
        lines = ["--only refuses to run: prerequisites are not included."]
        for number, gaps in sorted(missing.items()):
            lines.append("  case %d requires %s"
                         % (number, ", ".join(str(g) for g in gaps)))
        lines.append("Re-run including them, e.g. --only %s"
                     % " --only ".join(str(n) for n in sorted(
                         wanted | {g for gs in missing.values() for g in gs})))
        return None, "\n".join(lines)
    return [c for c in selected if c.number in wanted], None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=guardrails.REQUIRED_PORT)
    parser.add_argument("--database", default=guardrails.REQUIRED_DATABASE)
    parser.add_argument("--list", action="store_true", help="list cases and exit (no DB)")
    parser.add_argument("--self-check", action="store_true",
                        help="verify the guardrails offline and exit (no DB)")
    parser.add_argument("--only", type=int, action="append",
                        help="run only these case numbers (prerequisites must be included)")
    args = parser.parse_args(argv)

    if args.list:
        return list_cases()
    if args.self_check:
        return self_check()

    selected, refusal = _resolve_selection(args.only)
    if refusal:
        print(refusal)
        return 2

    try:
        guardrails.check_target(args.host, args.port, args.database)
    except guardrails.UnsafeTarget as exc:
        print("REFUSED: %s" % exc)
        return 2

    h = Harness(args.host, args.port, args.database)
    print("target %s  run_id=%s" % (
        guardrails.safe_summary(h.host, h.port, h.database), h.run_id))

    passed, failed, blocked = set(), [], []
    aborted = None
    try:
        for c in selected:
            gaps = [d for d in c.requires if d not in passed]
            if gaps:
                blocked.append(c.number)
                print("BLOCK %2d  %s (prerequisite %s did not pass)"
                      % (c.number, c.title, ", ".join(str(g) for g in gaps)))
                continue
            try:
                c(h)
                passed.add(c.number)
                print("PASS  %2d  %s" % (c.number, c.title))
            except Exception:                               # noqa: BLE001
                failed.append(c.number)
                print("FAIL  %2d  %s" % (c.number, c.title))
                # Redacted: driver errors can echo the connection string back at you.
                print(guardrails.redact(traceback.format_exc()))
                if c.foundational:
                    aborted = c.number
                    print("ABORT: case %d is foundational; nothing downstream is meaningful "
                          "without it." % c.number)
                    break
    finally:
        h.close_all()

    ran = len(passed) + len(failed)
    print("\n%d/%d ran passed" % (len(passed), ran if ran else 0))
    if failed:
        print("failed:  %s" % ", ".join(str(n) for n in failed))
    if blocked:
        print("blocked: %s" % ", ".join(str(n) for n in blocked))
    if aborted is not None:
        print("aborted after foundational case %d" % aborted)
    return 1 if (failed or blocked) else 0


if __name__ == "__main__":
    sys.exit(main())
