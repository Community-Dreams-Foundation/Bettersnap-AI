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
  python scripts/run_migrations.py --dry-run  # list what WOULD be applied (read-only)
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


def main():
    dry = "--dry-run" in sys.argv
    baseline = "--baseline" in sys.argv

    conn = new_connection()
    conn.autocommit = True   # DDL auto-commits; each migration is its own unit of work.
    cur = conn.cursor()

    # Applied set (the tracking table may not exist yet on a first run).
    applied = set()
    cur.execute("SELECT 1 FROM sys.tables WHERE object_id = OBJECT_ID('dbo.schema_migrations')")
    table_exists = cur.fetchone() is not None
    if table_exists:
        cur.execute("SELECT filename FROM dbo.schema_migrations")
        applied = {r[0] for r in cur.fetchall()}

    files = _migration_files()
    names = [os.path.basename(f) for f in files]
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

    if baseline:
        for name in names:
            if name not in applied:
                cur.execute(
                    "INSERT INTO dbo.schema_migrations (filename) VALUES (?)", name)
        print(f"[baseline] recorded {len(names)} migration file(s) as applied "
              "WITHOUT running them.")
        conn.close()
        return

    print(f"{len(applied)} already applied, {len(pending)} pending.")
    for f in pending:
        name = os.path.basename(f)
        print(f"  applying {name} ...")
        sql = open(f, encoding="utf-8").read()
        for batch in split_batches(sql):
            cur.execute(batch)
        cur.execute("INSERT INTO dbo.schema_migrations (filename) VALUES (?)", name)
    print("migrations up to date.")
    conn.close()


if __name__ == "__main__":
    main()
