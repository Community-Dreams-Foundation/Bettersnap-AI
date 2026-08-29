#!/usr/bin/env python3
"""Versioned migration runner.

Applies every migrations/NNN_*.sql that has not been applied yet, in filename order, and
records it in dbo.schema_migrations so it can never run twice. Idempotent and safe to run
on every deploy from CI/CD — that is what stops the schema from drifting away from the
code (no more hand-run sqlcmd, no more "007 written but not applied").

Each file is split on `GO` batch separators (pyodbc executes ONE batch per call, so a file
with GO must be split first).

Usage:
  python scripts/run_migrations.py            # apply all pending migrations
  python scripts/run_migrations.py --dry-run       # list what WOULD be applied (read-only)
  python scripts/run_migrations.py --verify-schema # fail-fast: are the runtime columns present?
  python scripts/run_migrations.py --baseline # record all current files as applied WITHOUT
                                              # running them — use once when adopting this
                                              # runner on a DB that was already hand-migrated.

The DB connection comes from shared.db (Key Vault), so the runner needs Key Vault access
(a managed identity / service principal with the secret-get permission in CI).
"""
import os
import re
import sys
import glob

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                       # Bettersnap-aI_Backend/
MIGRATIONS_DIR = os.path.join(ROOT, "migrations")

sys.path.insert(0, ROOT)
from shared.db import new_connection  # noqa: E402

_TRACKING_DDL = """
IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE object_id = OBJECT_ID('dbo.schema_migrations'))
    CREATE TABLE dbo.schema_migrations (
        filename    NVARCHAR(255) NOT NULL PRIMARY KEY,
        applied_at  DATETIME2     NOT NULL DEFAULT SYSUTCDATETIME()
    );
"""


def split_batches(sql: str):
    """Split a .sql file on lines that are exactly GO (the sqlcmd batch separator)."""
    parts = re.split(r"(?im)^\s*GO\s*$", sql)
    return [p.strip() for p in parts if p.strip()]


def _migration_files():
    return sorted(
        glob.glob(os.path.join(MIGRATIONS_DIR, "*.sql")),
        key=lambda p: os.path.basename(p),
    )


_MIGRATION_NAME = re.compile(r"^(\d{3})_.+\.sql$", re.IGNORECASE)


def _version(filename: str) -> int:
    match = _MIGRATION_NAME.match(os.path.basename(filename))
    if not match:
        raise RuntimeError(
            f"invalid migration filename {filename!r}; expected NNN_description.sql"
        )
    return int(match.group(1))


def validate_migration_order(names, applied, allow_backfill=False):
    """Reject ambiguous numbering and migrations added behind deployed history."""
    versions = [_version(name) for name in names]
    if len(versions) != len(set(versions)):
        raise RuntimeError("duplicate migration version found")
    if versions:
        expected = list(range(versions[0], versions[-1] + 1))
        if versions != expected:
            missing = sorted(set(expected) - set(versions))
            raise RuntimeError(f"migration version gap; missing: {missing}")

    applied_versions = [_version(name) for name in applied]
    if applied_versions and not allow_backfill:
        highest_applied = max(applied_versions)
        backfills = [
            name for name in names
            if name not in applied and _version(name) <= highest_applied
        ]
        if backfills:
            raise RuntimeError(
                "refusing out-of-order migration(s) older than the highest applied "
                f"version {highest_applied:03d}: {', '.join(backfills)}; "
                "repair schema_migrations explicitly before deploying"
            )


def apply_migration(conn, cur, name: str, sql: str):
    """Apply every batch and record the filename in one SQL transaction."""
    try:
        for batch in split_batches(sql):
            cur.execute(batch)
        cur.execute("INSERT INTO dbo.schema_migrations (filename) VALUES (?)", name)
        conn.commit()
    except Exception:
        # SQL Server supports transactional DDL. Rolling back here restores every
        # earlier GO batch and leaves no tracking row, so retry starts cleanly.
        conn.rollback()
        raise


# Columns the RUNTIME retry/reconcile code reads unconditionally. If any of these is
# missing, the deployed Functions app does not merely lose the retry feature — the training
# watcher's inflight SELECT raises on EVERY tick, so no training ever completes and no parked
# generation is ever released. That is a total outage, and it must be impossible to reach by
# shipping code ahead of its schema.
#
# REQUIRED ORDER:  migrations 033/034  ->  verify_retry_schema  ->  Functions deployment.
#
# The check runs even with --skip-apply (the deploy script's -SkipMigrations path), because
# "I applied migrations out of band" is exactly the claim that needs verifying.
REQUIRED_RUNTIME_COLUMNS = {
    "dbo.jobs": ("provisioning_attempts", "provisioning_execution_ids",
                 "first_terminal_observed_at"),
    "dbo.lora_trainings": ("provisioning_attempts", "provisioning_execution_ids",
                           "first_terminal_observed_at", "fused_job_id"),
}


def verify_runtime_schema(cur):
    """Return the list of missing 'table.column' names the runtime requires. Empty == safe."""
    missing = []
    for table, columns in REQUIRED_RUNTIME_COLUMNS.items():
        for column in columns:
            cur.execute("SELECT COL_LENGTH(?, ?)", table, column)
            row = cur.fetchone()
            if row is None or row[0] is None:
                missing.append("%s.%s" % (table, column))
    return missing


def main():
    dry = "--dry-run" in sys.argv
    baseline = "--baseline" in sys.argv
    verify_only = "--verify-schema" in sys.argv

    conn = new_connection()
    # One explicit transaction per migration file. GO only separates driver batches;
    # it must not become a commit boundary.
    conn.autocommit = False
    cur = conn.cursor()

    # Applied set (the tracking table may not exist yet on a first run).
    applied = set()
    cur.execute("SELECT 1 FROM sys.tables WHERE object_id = OBJECT_ID('dbo.schema_migrations')")
    table_exists = cur.fetchone() is not None
    if table_exists:
        cur.execute("SELECT filename FROM dbo.schema_migrations")
        applied = {r[0] for r in cur.fetchall()}

    if verify_only:
        # Fail-fast gate. No apply, no tracking-table write: purely "is the deployed schema
        # able to run the code we are about to ship?"
        missing = verify_runtime_schema(cur)
        conn.close()
        if missing:
            print("SCHEMA GATE FAILED - the runtime requires columns that do not exist:")
            for name in missing:
                print("  missing: %s" % name)
            print("Apply migrations 033_provisioning_retry.sql and 034_fused_job_link.sql "
                  "BEFORE deploying this code. Required order: migrations -> verify -> deploy.")
            sys.exit(2)
        print("schema gate OK - every runtime-required column is present.")
        return

    files = _migration_files()
    names = [os.path.basename(f) for f in files]
    # --baseline is the explicit repair/adoption operation, so it may record a
    # reserved/backfilled marker. Normal and dry runs must remain monotonic.
    validate_migration_order(names, applied, allow_backfill=baseline)
    pending = [f for f in files if os.path.basename(f) not in applied]

    if dry:
        print(f"[dry-run] tracking table exists: {table_exists}; "
              f"{len(applied)} applied, {len(pending)} pending:")
        for f in pending:
            print(f"  would apply: {os.path.basename(f)}")
        conn.close()
        return

    # Ensure the tracking table exists for real runs.
    cur.execute(_TRACKING_DDL)
    conn.commit()

    if baseline:
        for name in names:
            if name not in applied:
                cur.execute(
                    "INSERT INTO dbo.schema_migrations (filename) VALUES (?)", name)
        conn.commit()
        print(f"[baseline] recorded {len(names)} migration file(s) as applied "
              "WITHOUT running them.")
        conn.close()
        return

    print(f"{len(applied)} already applied, {len(pending)} pending.")
    for f in pending:
        name = os.path.basename(f)
        print(f"  applying {name} ...")
        sql = open(f, encoding="utf-8").read()
        apply_migration(conn, cur, name, sql)
    print("migrations up to date.")
    missing = verify_runtime_schema(cur)
    conn.close()
    if missing:
        # Applying every pending file should make this impossible; if it did not, the schema
        # is not what the code expects and shipping would be an outage.
        print("SCHEMA GATE FAILED after applying migrations - still missing:")
        for name in missing:
            print("  missing: %s" % name)
        sys.exit(2)
    print("schema gate OK - every runtime-required column is present.")


if __name__ == "__main__":
    main()
