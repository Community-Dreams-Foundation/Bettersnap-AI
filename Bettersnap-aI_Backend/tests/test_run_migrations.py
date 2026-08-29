"""Tests for monotonic schema migration ordering."""
import importlib.util
import pathlib
import re
import sys
import types
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
fake_db = types.ModuleType("shared.db")
fake_db.new_connection = lambda: None
sys.modules.setdefault("shared.db", fake_db)

spec = importlib.util.spec_from_file_location(
    "run_migrations_under_test", ROOT / "scripts" / "run_migrations.py"
)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class MigrationOrderTests(unittest.TestCase):
    def test_repository_sequence_is_contiguous(self):
        names = [pathlib.Path(path).name for path in runner._migration_files()]
        runner.validate_migration_order(names, applied=set())
        self.assertIn("012_reserved.sql", names)

    def test_migrations_do_not_embed_account_guids(self):
        guid = re.compile(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
            re.IGNORECASE,
        )
        offenders = []
        for path in (ROOT / "migrations").glob("*.sql"):
            if guid.search(path.read_text(encoding="utf-8")):
                offenders.append(path.name)
        self.assertEqual(offenders, [], f"account GUIDs embedded in migrations: {offenders}")

    def test_org_status_repair_replaces_legacy_constraint(self):
        migration = ROOT / "migrations" / "032_fix_org_status_constraint.sql"
        sql = migration.read_text(encoding="utf-8").lower()

        self.assertIn("drop constraint ck_org_status", sql)
        self.assertIn("add constraint ck_org_status", sql)
        self.assertIn("'pending_payment'", sql)
        self.assertIn("'active'", sql)
        self.assertIn("'suspended'", sql)
        self.assertIn("'closed'", sql)

    def test_late_backfill_is_rejected(self):
        names = ["011_dunning.sql", "012_late.sql", "013_cancel.sql"]
        applied = {"011_dunning.sql", "013_cancel.sql"}
        with self.assertRaisesRegex(RuntimeError, "refusing out-of-order"):
            runner.validate_migration_order(names, applied)

    def test_gap_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "missing: \\[12\\]"):
            runner.validate_migration_order(
                ["011_dunning.sql", "013_cancel.sql"], applied=set()
            )

    def test_duplicate_version_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "duplicate migration version"):
            runner.validate_migration_order(
                ["012_first.sql", "012_second.sql"], applied=set()
            )

    def test_explicit_baseline_can_record_backfill_marker(self):
        runner.validate_migration_order(
            ["011_dunning.sql", "012_reserved.sql", "013_cancel.sql"],
            applied={"011_dunning.sql", "013_cancel.sql"},
            allow_backfill=True,
        )


class AtomicMigrationTests(unittest.TestCase):
    class Conn:
        def __init__(self):
            self.commits = 0
            self.rollbacks = 0

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

    class Cursor:
        def __init__(self, fail_on=None):
            self.fail_on = fail_on
            self.executed = []

        def execute(self, sql, *params):
            normalized = sql.strip()
            self.executed.append((normalized, params))
            if normalized == self.fail_on:
                raise RuntimeError("batch failed")

    def test_all_batches_and_tracking_row_commit_together(self):
        conn = self.Conn()
        cur = self.Cursor()
        runner.apply_migration(conn, cur, "018_test.sql", "SELECT 1;\nGO\nSELECT 2;")
        self.assertEqual(conn.commits, 1)
        self.assertEqual(conn.rollbacks, 0)
        self.assertEqual(cur.executed[-1][1], ("018_test.sql",))

    def test_mid_file_failure_rolls_back_without_tracking_insert(self):
        conn = self.Conn()
        cur = self.Cursor(fail_on="SELECT 2;")
        with self.assertRaisesRegex(RuntimeError, "batch failed"):
            runner.apply_migration(
                conn, cur, "018_test.sql", "SELECT 1;\nGO\nSELECT 2;\nGO\nSELECT 3;"
            )
        self.assertEqual(conn.commits, 0)
        self.assertEqual(conn.rollbacks, 1)
        self.assertFalse(any("schema_migrations" in sql for sql, _ in cur.executed))


if __name__ == "__main__":
    unittest.main(verbosity=2)
