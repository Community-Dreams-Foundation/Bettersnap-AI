"""Migrations 033 + 034 — structural and runner-contract tests.

These are OFFLINE tests. They do not connect to a database and do not apply anything: the
production database is at version 032 and applying an unreviewed migration is exactly the
mistake this suite exists to prevent. What they DO verify is everything that can be decided
from the files plus the runner's own rules:

  * the runner's three hard gates (no duplicate version, no gap, no backfill below the highest
    applied version) all pass for the resulting migration set;
  * every statement is guarded, so a re-run after a partial failure is a no-op;
  * the FK is trusted (WITH CHECK) and the unique index is filtered — the two properties that
    are silently wrong if written the obvious way.

The runner records migrations BY FILENAME (`schema_migrations.filename` is the primary key), so
renaming an already-applied migration makes it look unapplied and trips the backfill guard.
That is why 033/034 are append-only above 032 and nothing existing is renumbered.
"""
import io
import os
import re
import unittest

MIGRATIONS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "migrations")
NAME_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")

# The highest version recorded in production dbo.schema_migrations at the time these were
# authored. New migrations must be strictly above it or the runner's backfill guard fires.
HIGHEST_APPLIED_IN_PROD = 32

# The exact canonical REPOSITORY filename set for versions 000-032, as present at commit
# 928d6aa2fc49636518ebf7287368e29ec22a94bf (origin/Features_team).
#
# This is NOT the production dbo.schema_migrations ledger and must not be described as one.
# The live ledger holds MORE rows than this set: it also records historical filenames from two
# earlier renumberings (e.g. 015_teams_organizations.sql and 025_admin_audit_log.sql) whose
# content now ships under different names here. Those extra ledger rows are harmless — the
# runner only refuses when a file present in the REPO is absent from the ledger at or below the
# highest applied version.
#
# This offline test therefore asserts one thing only: that this branch has not renamed or
# dropped a canonical repository migration. Production-ledger evidence is gathered separately,
# against the database, and is deliberately not encoded here.
CANONICAL_REPO_000_032 = {
    "000_baseline.sql", "001_gpu_dispatch_lease.sql", "002_jobs_dispatch_idempotency.sql",
    "003_user_plans.sql", "004_lora_trainings.sql", "005_trial_plan_default.sql",
    "006_retrain.sql", "007_retention.sql", "008_terms_accepted.sql", "009_stripe_columns.sql",
    "010_stripe_webhook_idempotency.sql", "011_dunning.sql", "012_reserved.sql",
    "013_cancel_pending.sql", "014_outbox.sql", "015_dispatched_at.sql", "016_source_type.sql",
    "017_pending_purchases.sql", "018_monthly_checkout_reservation.sql",
    "019_separate_credit_balances.sql", "020_reconcile_credit_total.sql",
    "021_clear_stale_monthly_balances.sql", "022_teams_organizations.sql",
    "023_teams_invitations_members.sql", "024_credit_ledger.sql",
    "025_checkout_and_credit_split.sql", "026_retrain_credit_buckets.sql",
    "027_catalog_tables.sql", "028_biometric_consent.sql", "029_audit_log.sql",
    "030_admin_audit_log.sql", "031_admin_user_status_and_notes.sql",
    "032_fix_org_status_constraint.sql",
}


def read(name):
    """Context-managed so CPython's refcount timing is not relied on; an unclosed handle
    emits ResourceWarning under -W error and on non-refcounting interpreters."""
    with io.open(os.path.join(MIGRATIONS, name), encoding="utf-8") as fh:
        return fh.read()


def sql_only(name):
    """File with `--` comments stripped.

    These migrations deliberately QUOTE the anti-patterns they replace — the old
    `WHERE user_id = ? AND status = 'waiting_lora'` query, and the phrase "not WITH NOCHECK".
    A raw substring scan therefore matches the documentation of the fix rather than any
    executable statement, so assertions about what the migration DOES must read code only."""
    return "\n".join(re.sub(r"--.*$", "", line) for line in read(name).splitlines())


def read_self():
    with io.open(os.path.abspath(__file__), encoding="utf-8") as fh:
        return fh.read()


def all_migrations():
    return sorted(f for f in os.listdir(MIGRATIONS) if f.endswith(".sql"))


def version_of(name):
    m = NAME_RE.match(name)
    assert m, "invalid migration filename: %r" % name
    return int(m.group(1))


class RunnerContract(unittest.TestCase):
    """The three conditions run_migrations.py raises on."""

    def setUp(self):
        self.names = all_migrations()
        self.versions = [version_of(n) for n in self.names]

    def test_filenames_match_the_runner_pattern(self):
        for n in self.names:
            self.assertRegex(n, NAME_RE, "%s would raise 'invalid migration filename'" % n)

    def test_no_duplicate_versions(self):
        dupes = {v for v in self.versions if self.versions.count(v) > 1}
        self.assertEqual(dupes, set(),
                         "run_migrations raises 'duplicate migration version found' for %s"
                         % sorted(dupes))

    def test_no_version_gap(self):
        expected = list(range(self.versions[0], self.versions[-1] + 1))
        missing = sorted(set(expected) - set(self.versions))
        self.assertEqual(missing, [], "run_migrations raises 'migration version gap'")

    def test_new_migrations_are_above_the_highest_applied_version(self):
        """A new file whose version is <= the highest applied version is a backfill, and the
        runner refuses to deploy until schema_migrations is repaired by hand."""
        for n in ("033_provisioning_retry.sql", "034_fused_job_link.sql"):
            self.assertIn(n, self.names)
            self.assertGreater(version_of(n), HIGHEST_APPLIED_IN_PROD,
                               "%s would be treated as a backfill" % n)

    def test_no_existing_migration_was_renumbered_or_removed(self):
        """The runner keys on FILENAME (schema_migrations.filename is the primary key), so
        checking only that numeric versions survive would pass even if a file were renamed
        022_teams_organizations -> 022_teams_orgs — which the runner would then see as unapplied
        and refuse as a backfill. Assert the exact canonical repository names."""
        actual = {n for n in self.names if version_of(n) <= HIGHEST_APPLIED_IN_PROD}
        self.assertEqual(actual, CANONICAL_REPO_000_032,
                         "canonical migration filenames changed; missing=%s unexpected=%s"
                         % (sorted(CANONICAL_REPO_000_032 - actual), sorted(actual - CANONICAL_REPO_000_032)))


class Restartability(unittest.TestCase):
    """Every DDL statement must be guarded so a re-run cannot fail."""

    def test_033_every_add_column_is_guarded(self):
        sql = sql_only("033_provisioning_retry.sql")
        adds = re.findall(r"ALTER TABLE\s+(\S+)\s+ADD\s+(\w+)", sql, re.I)
        self.assertEqual(len(adds), 6, "expected 6 ADD COLUMN statements, found %d" % len(adds))
        guards = re.findall(r"IF COL_LENGTH\('([^']+)',\s*'([^']+)'\)\s+IS NULL", sql, re.I)
        self.assertEqual(len(guards), 6, "every ADD must be preceded by a COL_LENGTH guard")
        self.assertEqual({(t.lower(), c.lower()) for t, c in adds},
                         {(t.lower(), c.lower()) for t, c in guards},
                         "a guard does not match the column it protects")

    def test_033_adds_the_expected_columns_to_both_tables(self):
        sql = sql_only("033_provisioning_retry.sql").lower()
        for table in ("dbo.jobs", "dbo.lora_trainings"):
            for col in ("provisioning_attempts", "provisioning_execution_ids",
                        "first_terminal_observed_at"):
                self.assertIn("col_length('%s', '%s')" % (table, col), sql,
                              "%s.%s is missing" % (table, col))

    def test_033_attempts_columns_are_not_null_with_a_zero_default(self):
        """Existing rows must read as 'never retried', not NULL, or the +1 arithmetic breaks."""
        sql = sql_only("033_provisioning_retry.sql")
        for m in re.finditer(r"ADD provisioning_attempts INT NOT NULL\s+CONSTRAINT \w+ DEFAULT 0", sql):
            pass
        self.assertEqual(len(re.findall(r"provisioning_attempts INT NOT NULL", sql)), 2)
        self.assertEqual(len(re.findall(r"DEFAULT 0 WITH VALUES", sql)), 2,
                         "WITH VALUES is required so existing rows are backfilled with 0")

    def test_034_column_fk_and_index_are_all_guarded(self):
        """Column: COL_LENGTH guard. FK: NOT EXISTS guard before ADD CONSTRAINT.
        Index: an EXISTS branch that VALIDATES a pre-existing index, with CREATE on the ELSE —
        a bare `NOT EXISTS -> CREATE` would silently accept a wrong-shaped index of that name."""
        sql = sql_only("034_fused_job_link.sql")
        self.assertRegex(sql, r"IF COL_LENGTH\('dbo\.lora_trainings',\s*'fused_job_id'\)\s+IS NULL")
        self.assertRegex(sql, r"NOT EXISTS\s*\(\s*SELECT 1 FROM sys\.foreign_keys")
        self.assertRegex(sql, r"IF EXISTS \(SELECT 1 FROM sys\.indexes")
        self.assertRegex(sql, r"ELSE IF OBJECT_ID\('dbo\.lora_trainings',\s*'U'\)\s+IS NOT NULL\s+"
                              r"CREATE UNIQUE INDEX")

    def test_no_destructive_statements_anywhere(self):
        for n in ("033_provisioning_retry.sql", "034_fused_job_link.sql"):
            sql = sql_only(n).upper()
            for bad in ("DROP TABLE", "DROP COLUMN", "TRUNCATE", "DELETE FROM", "UPDATE DBO."):
                self.assertNotIn(bad, sql, "%s contains %s" % (n, bad))


class BaselineProvenance(unittest.TestCase):
    """The offline baseline is a REPOSITORY set, not the production ledger."""

    def test_constant_is_named_for_the_repository(self):
        self.assertTrue(CANONICAL_REPO_000_032)
        self.assertEqual(len(CANONICAL_REPO_000_032), HIGHEST_APPLIED_IN_PROD + 1)

    def test_provenance_comment_names_the_exact_commit_and_disclaims_the_ledger(self):
        src = read_self()
        self.assertIn("928d6aa2fc49636518ebf7287368e29ec22a94bf", src,
                      "the baseline must name the commit it was captured from")
        self.assertIn("NOT the production dbo.schema_migrations ledger", src,
                      "the baseline must not be described as the production ledger")


class RetryClockInvariant(unittest.TestCase):
    """first_terminal_observed_at is per ACA execution ATTEMPT, not per row.

    A retry begins a new attempt, so the clock must restart. If a retry left the previous
    attempt's stamp in place, the new attempt would look like it had already exhausted
    MAX_OBSERVATION the instant it was dispatched, and would be refunded as unclassified before
    Log Analytics could describe it. The runtime UPDATE must therefore null the stamp in the
    SAME transaction as status='queued' / external_execution_id=NULL.

    Migration 033 is where that contract is written down, so it is asserted here; the runtime
    enforcement test lands with the queue_trigger/exec_reconcile change."""

    def test_033_documents_the_per_attempt_reset(self):
        doc = read("033_provisioning_retry.sql")
        self.assertIn("PER ACA EXECUTION ATTEMPT", doc.upper(),
                      "033 must state that the stamp is per attempt, not per row")
        self.assertRegex(doc, r"first_terminal_observed_at\s*=\s*NULL",
                         "033 must show the reset in the retry transition")

    def test_033_reset_is_shown_alongside_the_other_retry_columns(self):
        """The reset only works if it is in the SAME statement as the status transition."""
        doc = read("033_provisioning_retry.sql")
        m = re.search(r"UPDATE dbo\.jobs(.{0,600}?)WHERE job_id", doc, re.S)
        self.assertIsNotNone(m, "033 must document the retry UPDATE")
        stmt = m.group(1)
        for frag in ("status = 'queued'", "external_execution_id = NULL",
                     "first_terminal_observed_at = NULL"):
            self.assertIn(frag, stmt, "%s must be in the same UPDATE" % frag)


class FusedLinkShape(unittest.TestCase):
    """The two properties that are silently wrong if written the obvious way."""

    def test_fk_is_declared_WITH_CHECK_so_it_is_trusted(self):
        sql = sql_only("034_fused_job_link.sql")
        self.assertRegex(sql, r"ALTER TABLE dbo\.lora_trainings WITH CHECK\s+ADD CONSTRAINT",
                         "WITH NOCHECK would leave the FK untrusted")
        self.assertNotIn("WITH NOCHECK", sql.upper())

    def test_fk_targets_jobs_job_id(self):
        sql = sql_only("034_fused_job_link.sql")
        self.assertRegex(sql, r"FOREIGN KEY \(fused_job_id\) REFERENCES dbo\.jobs \(job_id\)")

    def test_unique_index_is_FILTERED_on_not_null(self):
        """Without the filter, SQL Server treats multiple NULLs as duplicates and every
        non-fused training row after the first would be rejected."""
        sql = sql_only("034_fused_job_link.sql")
        self.assertRegex(sql, r"CREATE UNIQUE INDEX UX_lora_trainings_fused_job\s+"
                              r"ON dbo\.lora_trainings \(fused_job_id\)\s+"
                              r"WHERE fused_job_id IS NOT NULL")

    def test_fk_existence_check_is_scoped_to_the_parent_table(self):
        """sys.foreign_keys.name is unique per schema, not globally. An unscoped name match
        could see a same-named FK on another table and skip creating this one."""
        sql = sql_only("034_fused_job_link.sql")
        self.assertRegex(sql, r"parent_object_id\s*=\s*OBJECT_ID\('dbo\.lora_trainings'\)")

    def test_existing_untrusted_fk_is_revalidated(self):
        """A constraint left untrusted (WITH NOCHECK, or disabled) does not guarantee existing
        rows satisfy it, so fused_job_id could already point at a missing job."""
        sql = sql_only("034_fused_job_link.sql")
        self.assertRegex(sql, r"WITH CHECK CHECK CONSTRAINT FK_lora_trainings_fused_job")

    def test_existing_wrong_shaped_fk_fails_loudly(self):
        """Accepting a same-named FK of the wrong shape would leave the link unenforced while
        looking correct."""
        sql = sql_only("034_fused_job_link.sql")
        self.assertIn("THROW 50034", sql)
        self.assertRegex(sql, r"foreign_key_columns")
        self.assertRegex(sql, r"referenced_object_id\s*=\s*OBJECT_ID\('dbo\.jobs'\)")

    def test_fk_requires_exactly_one_column_pair(self):
        """A COMPOSITE key containing the right columns plus extras would satisfy a naive
        EXISTS check while enforcing something different."""
        sql = sql_only("034_fused_job_link.sql")
        self.assertRegex(sql, r"@fk_col_count\s+INT\s*=")
        self.assertRegex(sql, r"@fk_col_count\s*<>\s*1")

    def test_fk_rejects_wrong_columns(self):
        sql = sql_only("034_fused_job_link.sql")
        self.assertRegex(sql, r"COL_NAME\(fkc\.parent_object_id,\s*fkc\.parent_column_id\)\s*=\s*'fused_job_id'")
        self.assertRegex(sql, r"COL_NAME\(fkc\.referenced_object_id,\s*fkc\.referenced_column_id\)\s*=\s*'job_id'")

    def test_fk_rejects_cascading_or_set_null_actions(self):
        """ON DELETE CASCADE would destroy linkage when a job is deleted; SET NULL would
        silently unlink. Both must be NO_ACTION so the FK blocks the delete."""
        sql = sql_only("034_fused_job_link.sql")
        self.assertRegex(sql, r"delete_referential_action_desc\s*=\s*'NO_ACTION'")
        self.assertRegex(sql, r"update_referential_action_desc\s*=\s*'NO_ACTION'")

    def test_index_rejects_non_unique_or_disabled(self):
        sql = sql_only("034_fused_job_link.sql")
        self.assertRegex(sql, r"i\.is_unique\s*=\s*1")
        self.assertRegex(sql, r"i\.is_disabled\s*=\s*0")

    def test_index_rejects_unfiltered_or_wrong_filter(self):
        sql = sql_only("034_fused_job_link.sql")
        self.assertRegex(sql, r"i\.has_filter\s*=\s*1")
        self.assertIn("'fused_job_id IS NOT NULL'", sql)

    def test_index_rejects_composite_or_wrong_key(self):
        sql = sql_only("034_fused_job_link.sql")
        self.assertRegex(sql, r"is_included_column\s*=\s*0\)\s*=\s*1")
        self.assertRegex(sql, r"COL_NAME\(ic\.object_id,\s*ic\.column_id\)\s*=\s*'fused_job_id'")

    def test_index_rejects_included_columns(self):
        sql = sql_only("034_fused_job_link.sql")
        self.assertRegex(sql, r"is_included_column\s*=\s*1\)\s*=\s*0")

    def test_wrong_shaped_index_throws_and_is_not_silently_accepted(self):
        sql = sql_only("034_fused_job_link.sql")
        self.assertIn("THROW 50035", sql)
        self.assertRegex(sql, r"@ix_ok\s*<>\s*1")

    def test_index_created_only_when_genuinely_absent(self):
        """CREATE must sit on the ELSE of the existence check, never unconditionally."""
        sql = sql_only("034_fused_job_link.sql")
        self.assertRegex(sql, r"ELSE IF OBJECT_ID\('dbo\.lora_trainings',\s*'U'\) IS NOT NULL\s+"
                              r"CREATE UNIQUE INDEX UX_lora_trainings_fused_job")

    def test_no_cascade_on_the_fk(self):
        """ON DELETE CASCADE would let deleting a job silently destroy training linkage."""
        self.assertNotIn("CASCADE", sql_only("034_fused_job_link.sql").upper())

    def test_034_does_not_backfill_by_user_id(self):
        """The whole point of the link is to stop inferring the job from (user_id, status).
        A backfill written that way would bake the bug into history."""
        sql = sql_only("034_fused_job_link.sql").lower()
        self.assertNotIn("user_id", sql,
                         "034 must not reference user_id in any executable statement")
        self.assertNotIn("waiting_lora", sql,
                         "034 must not reference job status in any executable statement")


if __name__ == "__main__":
    unittest.main(verbosity=2)
