"""Offline tests for the SQL Server harness — no container, no database, no network.

The harness runs destructive DDL, so its refusals and its scoping are the parts that most need
testing, and they are the parts that can be tested without a server. Everything here is pure.

This runs in the ordinary backend suite.
"""
import glob
import os
import sys
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from tests.integration import guardrails                        # noqa: E402
from tests.integration import run_sqlserver_suite as suite      # noqa: E402

SUITE_PATH = os.path.join(BACKEND_DIR, "tests", "integration", "run_sqlserver_suite.py")
HARNESS_PATH = os.path.join(BACKEND_DIR, "tests", "integration", "harness.py")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ── guardrails ───────────────────────────────────────────────────────────────
class HostRefusal(unittest.TestCase):
    def test_azure_sql_hostnames_are_refused(self):
        for host in ("bettersnap.database.windows.net", "x.database.azure.com",
                     "y.database.chinacloudapi.cn", "z.database.usgovcloudapi.net",
                     "w.sql.azuresynapse.net", "my-azure-box"):
            with self.subTest(host=host):
                with self.assertRaises(guardrails.UnsafeTarget):
                    guardrails.check_host(host)

    def test_remote_hosts_are_refused(self):
        for host in ("10.0.0.5", "db.internal", "example.com", "", None):
            with self.subTest(host=host):
                with self.assertRaises(guardrails.UnsafeTarget):
                    guardrails.check_host(host)

    def test_local_hosts_are_allowed(self):
        for host in ("localhost", "127.0.0.1", "::1", "LOCALHOST"):
            with self.subTest(host=host):
                self.assertIn(guardrails.check_host(host), guardrails.ALLOWED_HOSTS)


class PortRefusal(unittest.TestCase):
    def test_the_default_sql_port_is_refused_outright(self):
        """1433 is refused even locally: a tunnel or a locally-installed instance would
        otherwise be reachable by a copy-pasted command."""
        with self.assertRaises(guardrails.UnsafeTarget):
            guardrails.check_port(1433)

    def test_only_the_test_port_is_allowed(self):
        for port in (5432, 1434, 0, -1, "abc", None, 11434):
            with self.subTest(port=port):
                with self.assertRaises(guardrails.UnsafeTarget):
                    guardrails.check_port(port)
        self.assertEqual(guardrails.check_port(11433), 11433)
        self.assertEqual(guardrails.check_port("11433"), 11433)


class DatabaseRefusal(unittest.TestCase):
    def test_production_and_system_databases_are_refused(self):
        for name in ("bettersnap", "bettersnapdb", "bettersnap_prod", "bettersnap-prod",
                     "master", "msdb", "model", "tempdb", "", None, "BetterSnap"):
            with self.subTest(name=name):
                with self.assertRaises(guardrails.UnsafeTarget):
                    guardrails.check_database(name)

    def test_only_the_test_database_is_allowed(self):
        self.assertEqual(guardrails.check_database("bettersnap_test"), "bettersnap_test")


class CredentialHandling(unittest.TestCase):
    def test_the_password_must_come_from_the_environment(self):
        with self.assertRaises(guardrails.UnsafeTarget):
            guardrails.read_password(env={})

    def test_there_is_no_default_password_anywhere_in_the_harness(self):
        for path in (os.path.join(BACKEND_DIR, "tests", "integration", "guardrails.py"),
                     HARNESS_PATH, SUITE_PATH):
            text = _read(path)
            with self.subTest(file=os.path.basename(path)):
                self.assertNotIn("PWD=Password", text)
                self.assertNotIn("sa_password =", text.lower())

    def test_the_summary_never_contains_a_credential(self):
        summary = guardrails.safe_summary("127.0.0.1", 11433, "bettersnap_test")
        self.assertEqual(summary, "127.0.0.1:11433/bettersnap_test")
        self.assertNotIn("PWD", summary)
        self.assertNotIn("UID", summary)

    def test_redact_removes_the_password_and_credential_fields(self):
        secret = "Aa1!SuperSecretValue"
        cleaned = guardrails.redact(
            "login failed for DRIVER={x};UID=sa;PWD=%s;DATABASE=y" % secret, password=secret)
        self.assertNotIn(secret, cleaned)
        self.assertIn("PWD=***", cleaned)

    def test_redact_handles_a_missing_password(self):
        self.assertEqual(guardrails.redact(None), "")
        self.assertIn("PWD=***", guardrails.redact("PWD=whatever;", password=None))

    def test_the_connection_string_validates_before_building(self):
        with self.assertRaises(guardrails.UnsafeTarget):
            guardrails.connection_string("x.database.windows.net", 11433,
                                         "bettersnap_test", "pw")
        with self.assertRaises(guardrails.UnsafeTarget):
            guardrails.connection_string("127.0.0.1", 1433, "bettersnap_test", "pw")
        built = guardrails.connection_string("127.0.0.1", 11433, "bettersnap_test", "pw")
        self.assertIn("SERVER=127.0.0.1,11433", built)
        self.assertIn("DATABASE=bettersnap_test", built)


# ── registry, prerequisites, scope ───────────────────────────────────────────
class CaseRegistry(unittest.TestCase):
    def test_all_fourteen_cases_are_registered(self):
        self.assertEqual(sorted(c.number for c in suite.CASES), list(range(1, 15)))

    def test_case_numbers_are_unique(self):
        numbers = [c.number for c in suite.CASES]
        self.assertEqual(len(set(numbers)), len(numbers))

    def test_every_case_names_the_runtime_function_it_exercises(self):
        for c in suite.CASES:
            with self.subTest(case=c.number):
                self.assertTrue(c.exercises and len(c.exercises) > 5)
                self.assertTrue(c.title)

    def test_prerequisites_are_known_and_run_earlier(self):
        known = {c.number for c in suite.CASES}
        for c in suite.CASES:
            for dep in c.requires:
                with self.subTest(case=c.number, requires=dep):
                    self.assertIn(dep, known)
                    self.assertLess(dep, c.number,
                                    "a prerequisite must run before its dependant")

    def test_the_foundational_cases_are_the_ones_others_rely_on(self):
        self.assertEqual({c.number for c in suite.CASES if c.foundational}, {1, 3, 4, 5},
                         "migrations and the constraint shapes later cases rely on must "
                         "abort the suite rather than cascade")

    def test_drift_fixtures_are_labelled_explicitly(self):
        self.assertEqual({c.number for c in suite.CASES
                          if c.reachability == suite.DRIFT}, {6, 11})

    def test_self_check_passes_offline(self):
        self.assertEqual(suite.self_check(), 0)

    def test_list_mode_needs_no_database(self):
        self.assertEqual(suite.list_cases(), 0)

    def test_main_refuses_an_unsafe_target_without_connecting(self):
        self.assertEqual(suite.main(["--host", "x.database.windows.net"]), 2)
        self.assertEqual(suite.main(["--port", "1433"]), 2)
        self.assertEqual(suite.main(["--database", "bettersnap"]), 2)


class OnlySelection(unittest.TestCase):
    """`--only` must establish its prerequisites or refuse clearly — never run a case whose
    schema preconditions were never verified."""

    def test_only_refuses_when_a_prerequisite_is_missing(self):
        selected, refusal = suite._resolve_selection([6])
        self.assertIsNone(selected)
        self.assertIn("prerequisites are not included", refusal)
        self.assertIn("case 6 requires", refusal)

    def test_the_refusal_suggests_the_complete_command(self):
        _selected, refusal = suite._resolve_selection([10])
        self.assertIn("--only", refusal)
        self.assertIn("1", refusal)

    def test_only_accepts_a_complete_set(self):
        selected, refusal = suite._resolve_selection([1, 3, 5, 6])
        self.assertIsNone(refusal)
        self.assertEqual([c.number for c in selected], [1, 3, 5, 6])

    def test_only_rejects_an_unknown_case_number(self):
        selected, refusal = suite._resolve_selection([99])
        self.assertIsNone(selected)
        self.assertIn("unknown case number", refusal)

    def test_a_case_with_no_prerequisites_runs_alone(self):
        selected, refusal = suite._resolve_selection([1])
        self.assertIsNone(refusal)
        self.assertEqual([c.number for c in selected], [1])

    def test_main_returns_two_when_only_is_refused(self):
        self.assertEqual(suite.main(["--only", "6"]), 2)


class MigrationScope(unittest.TestCase):
    """The suite covers an EXACT migration set. A new 035 changes its scope and must be a
    reviewed decision, not a silently wider run."""

    def test_the_canonical_set_matches_the_repository_exactly(self):
        names = tuple(sorted(os.path.basename(p) for p in
                             glob.glob(os.path.join(BACKEND_DIR, "migrations", "*.sql"))))
        self.assertEqual(names, suite.CANONICAL_MIGRATIONS,
                         "migrations/ no longer matches the canonical set this suite covers; "
                         "update docs/sqlserver_integration_plan.md and CANONICAL_MIGRATIONS "
                         "together, deliberately")

    def test_it_is_exactly_000_through_035(self):
        # 035 adds the Teams pricing snapshot (contract teams_basic_v1). Updated
        # deliberately, together with CANONICAL_MIGRATIONS — this test exists so an
        # accidental migration cannot slip into the covered set unnoticed.
        self.assertEqual(len(suite.CANONICAL_MIGRATIONS), 36)
        self.assertTrue(suite.CANONICAL_MIGRATIONS[0].startswith("000_"))
        self.assertTrue(suite.CANONICAL_MIGRATIONS[-1].startswith("035_"))

    def test_versions_are_unique_and_ordered(self):
        prefixes = [n.split("_", 1)[0] for n in suite.CANONICAL_MIGRATIONS]
        self.assertEqual(len(set(prefixes)), len(prefixes))
        self.assertEqual(prefixes, sorted(prefixes))

    def test_case_one_asserts_the_exact_set_not_a_count(self):
        src = _read(SUITE_PATH)
        region = src[src.index("def case_migrations("):src.index("def case_replay(")]
        self.assertIn("expect_eq(on_disk, CANONICAL_MIGRATIONS", region)
        self.assertIn("expect_eq(tuple(applied), CANONICAL_MIGRATIONS", region)
        self.assertNotIn(">= 35", region)


# ── honest scoping of destructive DDL ────────────────────────────────────────
class DestructiveDDLIsScopedToCase6(unittest.TestCase):
    """HONEST SCOPING.

    An earlier version of this file asserted that no fixture ever drops or NOCHECKs a
    constraint — which was FALSE: case 6 does both, deliberately, because that is the only way
    to prove migration 034's guards fire. That test passed only because it scanned harness.py
    and never looked at the suite.

    Destructive schema manipulation is PERMITTED here, but only inside the disposable,
    guard-checked bettersnap_test database, and only inside case 6.
    """

    DESTRUCTIVE = ("DROP CONSTRAINT", "WITH NOCHECK", "DROP INDEX", "ADD CONSTRAINT")

    def setUp(self):
        self.src = _read(SUITE_PATH)

    def _case6(self):
        start = self.src.index("def case_034_guards(")
        return self.src[start:self.src.index("# ── 7-8: real concurrency", start)]

    def test_case_6_really_does_perform_destructive_ddl(self):
        """If this stops being true, the 034 guards are no longer being proven."""
        region = self._case6()
        for token in ("DROP CONSTRAINT", "WITH NOCHECK", "DROP INDEX"):
            with self.subTest(token=token):
                self.assertIn(token, region)

    def test_no_other_case_performs_destructive_ddl(self):
        start6 = self.src.index("def case_034_guards(")
        end6 = self.src.index("# ── 7-8: real concurrency", start6)
        before = self.src[self.src.index("# ── 1-2: migrations"):start6]
        after = self.src[end6:]
        for region, label in ((before, "cases 1-5"), (after, "cases 7-14 and the runner")):
            for token in self.DESTRUCTIVE:
                with self.subTest(region=label, token=token):
                    self.assertNotIn(token, region,
                                     "destructive DDL outside case 6: %s in %s"
                                     % (token, label))

    def test_case_6_restores_the_shapes_in_a_finally(self):
        region = self._case6()
        self.assertIn("finally:", region)
        self.assertIn("_restore_034_shapes(", region)

    def test_the_restore_helper_rebuilds_from_the_real_migration(self):
        start = self.src.index("def _restore_034_shapes(")
        region = self.src[start:self.src.index("# ── 7-8: real concurrency", start)]
        self.assertIn("_replay(conn, sql034)", region)

    def test_the_harness_itself_never_weakens_a_constraint(self):
        src = _read(HARNESS_PATH).upper()
        for token in ("NOCHECK CONSTRAINT", "DISABLE TRIGGER", "DROP CONSTRAINT FK_"):
            with self.subTest(token=token):
                self.assertNotIn(token, src,
                                 "seeding must never weaken a constraint to manufacture a "
                                 "scenario; only case 6 alters schema")

    def test_the_module_documents_that_this_is_permitted_only_when_guarded(self):
        self.assertIn("DESTRUCTIVE SCHEMA MANIPULATION IS PERMITTED", self.src)
        self.assertIn("guardrails.py has already refused", self.src)


class NativeErrorNumbers(unittest.TestCase):
    """Case 6 must read SQL Server's own ERROR_NUMBER(), not grep a driver string."""

    def setUp(self):
        self.src = _read(SUITE_PATH)

    def test_the_error_number_comes_from_sql_server(self):
        start = self.src.index("def _batch_error_number(")
        region = self.src[start:self.src.index("def _migration_sql(")]
        for token in ("BEGIN TRY", "BEGIN CATCH", "ERROR_NUMBER()"):
            with self.subTest(token=token):
                self.assertIn(token, region)

    def test_the_numbers_are_named_constants(self):
        self.assertEqual(suite.THROW_FK_WRONG_SHAPE, 50034)
        self.assertEqual(suite.THROW_INDEX_WRONG_SHAPE, 50035)

    def test_case_6_asserts_both_numbers_and_the_re_trust_path(self):
        start = self.src.index("def case_034_guards(")
        region = self.src[start:self.src.index("def _restore_034_shapes(")]
        self.assertIn("expect_eq(number, THROW_FK_WRONG_SHAPE", region)
        self.assertIn("expect_eq(number, THROW_INDEX_WRONG_SHAPE", region)
        self.assertIn("expect_eq(number, 0", region)
        self.assertNotIn("in str(exc)", region,
                         "the assertion must not be a substring match on an exception")


class ConcurrencyIsReal(unittest.TestCase):
    def setUp(self):
        self.src = _read(SUITE_PATH)

    def _region(self, name):
        start = self.src.index("def %s(" % name)
        nxt = self.src.find("\n@case(", start)
        return self.src[start:nxt if nxt != -1 else len(self.src)]

    def test_the_race_helper_uses_threads_a_barrier_and_a_timeout(self):
        region = self._region("_race")
        for token in ("threading.Barrier", "threading.Thread", "join(timeout)",
                      "possible deadlock"):
            with self.subTest(token=token):
                self.assertIn(token, region)

    def test_case_10_is_concurrent_not_sequential(self):
        region = self._region("case_exactly_once_refund")
        self.assertIn("_race(", region)
        self.assertIn("range(5)", region)
        self.assertIn("conn.commit()", region)

    def test_case_8_asserts_the_race_outcomes_not_just_final_state(self):
        region = self._region("case_concurrent_fused")
        self.assertIn('["allocated", "reused existing link"]', region)
        self.assertIn("expect_eq(len(returned), 1", region)
        self.assertIn("the OTHER parked job must be untouched", region)

    def test_every_racing_case_bounds_its_threads(self):
        for name in ("case_concurrent_retry", "case_concurrent_fused",
                     "case_exactly_once_refund"):
            with self.subTest(case=name):
                self.assertIn("_race(", self._region(name))


class LockTimeoutIsAlwaysAFailure(unittest.TestCase):
    """A contender that timed out never reached the guarded UPDATE.

    Tolerating a lock timeout would let case 10 report "exactly once" while four connections
    had simply given up — proving nothing about serialization. That tolerance is gone.
    """

    def setUp(self):
        self.src = _read(SUITE_PATH)

    def _case10(self):
        start = self.src.index("def case_exactly_once_refund(")
        nxt = self.src.find("\n@case(", start)
        return self.src[start:nxt if nxt != -1 else len(self.src)]

    def test_the_tolerance_helper_is_gone_entirely(self):
        self.assertNotIn("_is_lock_timeout", self.src,
                         "lock-timeout acceptance must not exist anywhere in the suite")
        self.assertFalse(hasattr(suite, "_is_lock_timeout"))

    def test_case_10_does_not_swallow_any_error(self):
        region = self._case10()
        self.assertNotIn("except Exception", region,
                         "a lock timeout, deadlock or any other error must surface as a "
                         "thread error and fail the case")
        self.assertNotIn('"lock_timeout"', region)

    def test_case_10_requires_four_real_no_op_losers(self):
        region = self._case10()
        self.assertIn("expect_eq(len(winners), 1", region)
        self.assertIn("expect_eq(len(losers), 4", region)
        self.assertIn("expect_eq(state, pr.REFUND_NONE", region,
                      "every loser must report REFUND_NONE, which is only possible if it "
                      "actually executed the guarded UPDATE")
        self.assertIn("expect_eq(winners[0][2], pr.REFUND_DONE", region)

    def test_case_10_asserts_no_thread_errors_at_all(self):
        region = self._case10()
        self.assertIn("expect(not errors", region)
        self.assertIn("1222", region)
        self.assertIn("1205", region)

    def test_case_10_asserts_the_economic_outcome(self):
        region = self._case10()
        self.assertIn("(40, 25, 15)", region)
        self.assertIn("expect_eq(len(refunds), 1", region)
        self.assertIn('"failed"', region)

    def test_the_bounds_are_finite_and_generous_enough_to_serialize(self):
        from tests.integration import harness
        self.assertEqual(harness.LOCK_TIMEOUT_MS, 30000)
        self.assertEqual(harness.QUERY_TIMEOUT_S, 60)
        self.assertEqual(suite.RACE_TIMEOUT_S, 120)
        self.assertGreater(suite.RACE_TIMEOUT_S * 1000, harness.LOCK_TIMEOUT_MS,
                           "the join bound must exceed the lock bound, or a genuine lock "
                           "wait would trip the thread timeout instead")
        self.assertLess(suite.RACE_TIMEOUT_S, 600, "the bound must stay finite")


class RestorationFailsLoudly(unittest.TestCase):
    """Case 6's restore must never silently return success."""

    def setUp(self):
        self.src = _read(SUITE_PATH)

    def _restore(self):
        start = self.src.index("def _restore_034_shapes(")
        return self.src[start:self.src.index("def _verify_034_shape(")]

    def _verify(self):
        start = self.src.index("def _verify_034_shape(")
        return self.src[start:self.src.index("# ── 7-8: real concurrency")]

    def test_restoration_has_its_own_failure_type(self):
        self.assertTrue(issubclass(suite.RestorationFailed, AssertionError))

    def test_the_replay_failure_is_raised_not_swallowed(self):
        region = self._restore()
        self.assertIn("raise RestorationFailed(", region)
        self.assertIn("_replay(conn, sql034)", region)

    def test_restoration_ends_with_a_shape_verification(self):
        region = self._restore()
        self.assertIn("_verify_034_shape(h, conn)", region)
        self.assertGreater(region.index("_verify_034_shape(h, conn)"),
                           region.index("_replay(conn, sql034)"),
                           "the shape must be verified AFTER the replay")

    def test_there_is_no_catch_all_that_returns_success(self):
        region = self._restore()
        # The only tolerated failures are the two best-effort DROPs, and each must roll back
        # rather than return.
        self.assertNotIn("except Exception:\n            return", region)
        self.assertNotIn("except Exception:\n        return", region)
        self.assertNotIn("pass\n    _verify", region)
        self.assertEqual(region.count("conn.rollback()"), 2,
                         "exactly two tolerated rollbacks: the initial one and the "
                         "best-effort DROP loop")

    def test_the_verification_checks_every_required_fk_property(self):
        region = self._verify()
        for token in ('col_pairs', 'parent_col', 'ref_table', 'ref_col',
                      'is_disabled', 'is_not_trusted', 'delete_action', 'update_action'):
            with self.subTest(property=token):
                self.assertIn(token, region)
        self.assertIn("'fused_job_id'", region)
        self.assertIn("'jobs'", region)
        self.assertIn("'job_id'", region)
        self.assertIn("NO_ACTION", region)

    def test_the_verification_checks_every_required_index_property(self):
        region = self._verify()
        for token in ("is_unique", "has_filter", "is_disabled", "key_cols",
                      "key_col", "included_cols", "EXPECTED_INDEX_FILTER"):
            with self.subTest(property=token):
                self.assertIn(token, region)
        self.assertEqual(suite.EXPECTED_INDEX_FILTER, "fused_job_id IS NOT NULL")

    def test_a_missing_object_is_a_failure_not_a_pass(self):
        region = self._verify()
        self.assertIn('raise RestorationFailed("FK_lora_trainings_fused_job was not restored"',
                      region)
        self.assertIn('raise RestorationFailed("UX_lora_trainings_fused_job was not restored"',
                      region)

    def test_case_6_calls_the_restore_in_a_finally_and_chains_failures(self):
        start = self.src.index("def case_034_guards(")
        region = self.src[start:self.src.index("class RestorationFailed")]
        self.assertIn("finally:", region)
        self.assertIn("_restore_034_shapes(h, conn, sql034)", region)
        self.assertIn("__context__", region,
                      "the code must document that both failures are reported, not one")

    def test_the_shape_query_lives_in_the_harness_not_the_case(self):
        self.assertIn("def fused_link_shape(", _read(HARNESS_PATH))

    def test_the_filter_normaliser_matches_the_migration_s_own_rule(self):
        from tests.integration.harness import normalize_filter
        self.assertEqual(normalize_filter("([fused_job_id] IS NOT NULL)"),
                         "fused_job_id IS NOT NULL")
        self.assertEqual(normalize_filter("(([fused_job_id] IS NOT NULL))"),
                         "fused_job_id IS NOT NULL")
        self.assertIsNone(normalize_filter(None))


class NoProductionReimplementation(unittest.TestCase):
    """The suite must call production functions, not paste their SQL."""

    def setUp(self):
        self.src = _read(SUITE_PATH)

    def test_it_calls_the_real_transaction_functions(self):
        for symbol in ("pr.retry_job(", "pr.allocate_fused_job(",
                       "pr.terminalize_and_refund(", "pr.compensate_pending_refund(",
                       "pr.build_refund_plan(", "training_orphan.build_orphan_marker(",
                       "h.apply_all_migrations("):
            with self.subTest(symbol=symbol):
                self.assertIn(symbol, self.src)

    def test_it_does_not_reimplement_the_behaviour_under_test(self):
        lowered = self.src.lower()
        for forbidden in (
                "update jobs set status = 'queued', external_execution_id = null",
                "update users set monthly_credits_remaining = monthly_credits_remaining +",
                "insert into outbox"):
            with self.subTest(sql=forbidden):
                self.assertNotIn(forbidden, lowered,
                                 "the suite must exercise production SQL, not restate it")


class HarnessSafety(unittest.TestCase):
    def test_the_harness_module_imports_without_a_database(self):
        from tests.integration import harness
        # Bounds are asserted in detail by LockTimeoutIsAlwaysAFailure; here we only need the
        # module to import and to carry finite bounds.
        self.assertGreater(harness.LOCK_TIMEOUT_MS, 0)
        self.assertGreater(harness.QUERY_TIMEOUT_S, 0)
        self.assertLess(harness.QUERY_TIMEOUT_S, 600)

    def test_constructing_a_harness_for_an_unsafe_target_raises(self):
        from tests.integration import harness
        with self.assertRaises(guardrails.UnsafeTarget):
            harness.Harness(host="x.database.windows.net")
        with self.assertRaises(guardrails.UnsafeTarget):
            harness.Harness(port=1433)

    def test_reachability_of_each_seeder_is_documented(self):
        src = _read(HARNESS_PATH)
        self.assertIn("REACHABLE: reserve_job_slot writes exactly this pair", src)
        self.assertIn("DRIFT FIXTURE", src)
        self.assertIn("FK_jobs_user", src)


class TrackingTableIsCreatedFromTheRunnersOwnDDL(unittest.TestCase):
    """Regression for the first real-run failure.

    `apply_migration` records every file with `INSERT INTO dbo.schema_migrations`.
    `run_migrations.main()` creates that table before its loop; the harness calls
    `apply_migration` directly and skipped it, so 000_baseline applied its DDL and then failed
    to record itself with `Invalid object name 'dbo.schema_migrations'` (SQL 208).
    """

    def setUp(self):
        self.src = _read(HARNESS_PATH)
        start = self.src.index("def apply_all_migrations(")
        self.region = self.src[start:self.src.index("def verify_runtime_schema(")]

    def test_the_runner_exposes_the_constant_the_harness_uses(self):
        import run_migrations
        self.assertTrue(hasattr(run_migrations, "_TRACKING_DDL"))
        self.assertIn("schema_migrations", run_migrations._TRACKING_DDL)

    def test_the_harness_references_the_runners_constant(self):
        self.assertIn("run_migrations._TRACKING_DDL", self.region)

    def test_the_ddl_is_not_duplicated_in_the_harness(self):
        upper = self.src.upper()
        self.assertNotIn("CREATE TABLE DBO.SCHEMA_MIGRATIONS", upper,
                         "the harness must USE the runner's DDL, never restate it")
        self.assertNotIn("APPLIED_AT", upper)

    def test_it_creates_and_commits_before_the_first_apply_migration(self):
        create_at = self.region.index("run_migrations._TRACKING_DDL")
        commit_at = self.region.index("conn.commit()", create_at)
        apply_at = self.region.index("run_migrations.apply_migration(")
        self.assertLess(create_at, commit_at, "the tracking DDL must be committed")
        self.assertLess(commit_at, apply_at,
                        "the tracking table must exist BEFORE the first apply_migration, or "
                        "the first file applies its DDL and then cannot record itself")

    def test_repeated_setup_stays_idempotent(self):
        import run_migrations
        ddl = run_migrations._TRACKING_DDL.upper()
        self.assertIn("IF NOT EXISTS", ddl,
                      "the DDL must be guarded so a second setup is a no-op")
        self.assertIn("OBJECT_ID('DBO.SCHEMA_MIGRATIONS')", ddl)

    def test_the_harness_still_applies_the_real_migrations_through_the_real_runner(self):
        self.assertIn("run_migrations.apply_migration(conn, cur, name, sql)", self.region)
        self.assertIn("run_migrations._migration_files()", self.region)



class HarnessInsertsMatchTheRealSchema(unittest.TestCase):
    """Regression for the case-12 failure: `Invalid column name 'owner_user_id'`.

    The organizations seeder invented a column name. Migration 022 calls it `admin_user_id`
    ("Entra oid; matches users.user_id"). Nothing offline caught it, so the mistake survived
    until a real engine rejected it eight cases into a 14-case run.

    SCANS BOTH `harness.py` AND `run_sqlserver_suite.py`. The first version of this check read
    the harness only. A DUPLICATE of the same bad insert lived in the suite file, so fixing the
    harness left case 12 failing on the identical `Invalid column name 'role'` -- the guard
    existed to prevent exactly that and did not cover the file it happened in. Any file that
    seeds rows must be inside the scan, or the guard is decorative.

    This reads EVERY `INSERT INTO` in both files, resolves the target table's real columns out
    of the migration files, and asserts two things per insert:
      1. every column named actually exists;
      2. every NOT NULL column WITHOUT a DEFAULT is supplied.
    Rule 2 is what stops the opposite drift — an insert that compiles but fails at runtime
    because a required column was omitted.

    It is schema-derived, not a hand-written list, so it stays true as migrations land.
    """

    SKIP_WORDS = ("constraint", "primary", "foreign", "unique", "check", "index", ")", "")

    @classmethod
    def setUpClass(cls):
        cls.columns = cls._schema()
        cls.inserts = cls._inserts()

    @classmethod
    def _strip(cls, sql):
        out = []
        for line in sql.splitlines():
            out.append(line.split("--", 1)[0])
        return chr(10).join(out)

    @classmethod
    def _schema(cls):
        """{table: {column: has_default}} built from CREATE TABLE and ALTER TABLE ADD."""
        tables = {}
        for path in sorted(glob.glob(os.path.join(BACKEND_DIR, "migrations", "*.sql"))):
            sql = cls._strip(_read(path))
            low = sql.lower()
            at = 0
            while True:
                at = low.find("create table", at)
                if at < 0:
                    break
                head = sql[at:sql.index("(", at)]
                table = head.split()[-1].split(".")[-1].strip("[]").lower()
                depth, end = 0, None
                for i in range(sql.index("(", at), len(sql)):
                    if sql[i] == "(":
                        depth += 1
                    elif sql[i] == ")":
                        depth -= 1
                        if depth == 0:
                            end = i
                            break
                body = sql[sql.index("(", at) + 1:end]
                cols = tables.setdefault(table, {})
                for line in body.splitlines():
                    line = line.strip().rstrip(",").strip()
                    first = line.split(" ")[0].strip("[]").lower() if line else ""
                    if first in cls.SKIP_WORDS or not first.replace("_", "").isalnum():
                        continue
                    nullable = "not null" not in line.lower()
                    cols[first] = nullable or "default" in line.lower()
                at = end or at + 1
            # add-only columns
            for chunk in low.split("alter table")[1:]:
                words = chunk.split()
                if len(words) < 3 or words[1] != "add":
                    continue
                table = words[0].split(".")[-1].strip("[]")
                col = words[2].strip("[]")
                if col in ("constraint",):
                    continue
                line = chunk[:chunk.find(";") if ";" in chunk else len(chunk)]
                tables.setdefault(table, {})[col] = (
                    "not null" not in line or "default" in line)
        return tables

    # Every file that seeds rows. Adding a seeding module without adding it here is the one
    # way this check can go blind again.
    SEEDING_FILES = (("harness.py", HARNESS_PATH), ("run_sqlserver_suite.py", SUITE_PATH))

    @classmethod
    def _inserts(cls):
        """[(origin, table, [columns])] for every INSERT INTO in every seeding file."""
        found = []
        for origin, path in cls.SEEDING_FILES:
            src = " ".join(_read(path).replace('"', " ").split())
            low, at = src.lower(), 0
            while True:
                at = low.find("insert into ", at)
                if at < 0:
                    break
                rest = src[at + len("insert into "):]
                table = rest.split()[0].split(".")[-1].strip(",`").lower()
                open_paren = rest.find("(")
                close = rest.find(")", open_paren)
                cols = [c.strip().strip("[]").lower()
                        for c in rest[open_paren + 1:close].split(",")]
                found.append((origin, table, cols))
                at += 12
        return found

    def test_the_schema_and_the_inserts_were_actually_parsed(self):
        self.assertIn("organizations", self.columns)
        self.assertIn("admin_user_id", self.columns["organizations"])
        self.assertNotIn("owner_user_id", self.columns["organizations"],
                         "the column the seeder used to name does not exist")
        self.assertGreaterEqual(len(self.inserts), 5,
                                "every seeder's insert must be visible to this check")
        self.assertIn("organizations", [t for _o, t, _c in self.inserts])

    def test_both_seeding_files_are_actually_scanned(self):
        """Regression for the coverage gap: the suite file seeds too."""
        origins = {o for o, _t, _c in self.inserts}
        self.assertEqual(origins, {"harness.py", "run_sqlserver_suite.py"},
                         "both seeding files must contribute inserts to this check")
        suite_tables = {t for o, t, _c in self.inserts if o == "run_sqlserver_suite.py"}
        self.assertIn("organization_members", suite_tables,
                      "the suite-local case-12 insert must be inside the scan")

    def test_every_inserted_column_exists_in_the_schema(self):
        for origin, table, cols in self.inserts:
            known = self.columns.get(table)
            self.assertIsNotNone(known,
                                 "%s inserts into unknown table %r" % (origin, table))
            for col in cols:
                with self.subTest(origin=origin, table=table, column=col):
                    self.assertIn(col, known, "%s: %s.%s does not exist in any migration"
                                  % (origin, table, col))

    def test_every_required_column_is_supplied(self):
        """NOT NULL and no DEFAULT means the insert MUST name it."""
        for origin, table, cols in self.inserts:
            required = [c for c, has_default in self.columns.get(table, {}).items()
                        if not has_default]
            for col in required:
                with self.subTest(origin=origin, table=table, column=col):
                    self.assertIn(col, cols,
                                  "%s: %s.%s is NOT NULL with no DEFAULT but is not supplied"
                                  % (origin, table, col))

    def test_the_columns_022_defaults_are_correctly_treated_as_optional(self):
        """Documents WHY the seeder supplies only four organizations columns."""
        org = self.columns["organizations"]
        for optional in ("credits_per_seat", "status", "created_at", "updated_at",
                         "organization_id"):
            self.assertTrue(org[optional], "%s carries a DEFAULT in 022" % optional)
        for required in ("name", "admin_user_id", "seats_purchased"):
            self.assertFalse(org[required], "%s has no DEFAULT" % required)



class GuidComparisonsInCaseBodies(unittest.TestCase):
    """Regression for the case-8 failure.

    `bound_job` comes back from SQL as an UPPERCASE uniqueidentifier; `job_a`/`job_b` are
    minted by `Harness.new_id()` as `str(uuid.uuid4())`, lowercase. The case compared them with
    `==`, so `expect(str(bound_job) in (job_a, job_b))` failed on a real engine -- and the very
    next line, `other = job_b if str(bound_job) == job_a else job_a`, could NEVER be true, so
    it would have silently asserted "the OTHER parked job is untouched" against the WRONG job.
    A quiet wrong answer, not a loud failure. Hence parsing rather than lowercasing.
    """

    def setUp(self):
        self.suite = _read(SUITE_PATH)
        start = self.suite.index("def case_concurrent_fused(")
        self.body = self.suite[start:self.suite.index("def case_rollback_atomicity(")]

    def test_the_helper_exists_and_parses_rather_than_lowercases(self):
        from tests.integration.harness import same_guid
        self.assertTrue(same_guid("448C5F40-56F7-4311-BFBC-B8D1215835A8",
                                  "448c5f40-56f7-4311-bfbc-b8d1215835a8"))
        self.assertFalse(same_guid("448c5f40-56f7-4311-bfbc-b8d1215835a8",
                                   "11111111-2222-3333-4444-555555555555"))
        for bad in (None, "", "job-a", 7, []):
            with self.subTest(value=bad):
                self.assertFalse(same_guid(bad, "448c5f40-56f7-4311-bfbc-b8d1215835a8"))
        self.assertFalse(same_guid("job-a", "job-a"),
                         "non-UUID values must fail closed, even against themselves")

    def test_both_corrected_assertions_use_it(self):
        self.assertIn("same_guid(bound_job, job_a) or same_guid(bound_job, job_b)", self.body)
        self.assertIn("other = job_b if same_guid(bound_job, job_a) else job_a", self.body)
        self.assertIn("same_guid(persisted, bound_job)", self.body)

    def test_no_exact_id_comparison_survives_in_the_case(self):
        self.assertNotIn("str(bound_job) == job_a", self.body)
        self.assertNotIn("str(bound_job) in (job_a", self.body)
        self.assertNotIn(".lower()", self.body,
                         "lowercasing would accept two identical NON-uuids as equal")

    def test_the_outcome_assertions_that_were_never_reached_are_still_present(self):
        """The case failed BEFORE these ran, so they were unproven. They must still be there."""
        self.assertIn('"processing", "the bound job must be claimed"', self.body)
        self.assertIn('"waiting_lora", "the OTHER parked job must be untouched"', self.body)

    def test_production_helper_is_not_reused_for_job_ids(self):
        """same_user_id is documented user-id-only so it can never reach ACA execution names;
        the test-side comparison keeps its own helper."""
        self.assertNotIn("same_user_id", self.suite)


class Case12InsertMatchesTheRealMemberSchema(unittest.TestCase):
    """Regression for the case-12 failure: `Invalid column name 'role'` (SQL 207).

    The generic scan above already catches this shape, but these name the specific columns so
    the intent survives even if the parser is rewritten.
    """

    def setUp(self):
        suite = _read(SUITE_PATH)
        start = suite.index("def case_pending_refund(")
        self.body = suite[start:suite.index("def case_negative_charge(")]
        self.ddl = _read(os.path.join(BACKEND_DIR, "migrations",
                                      "023_teams_invitations_members.sql"))

    def test_023_really_has_no_role_column_and_requires_credits_granted(self):
        members = self.ddl[self.ddl.index("CREATE TABLE dbo.organization_members"):]
        members = members[:members.index(");")]
        self.assertNotIn("role", members, "023 defines no `role` column")
        self.assertIn("credits_granted   INT              NOT NULL", members)
        self.assertNotIn("DF_member_credits_granted", members,
                         "credits_granted carries no DEFAULT, so it must be supplied")

    def test_the_rejoin_insert_names_the_real_columns(self):
        self.assertIn("INSERT INTO organization_members (organization_id, user_id, ", self.body)
        self.assertIn("credits_granted, credits_remaining) VALUES (?, ?, 0, 0)", self.body)

    def test_the_rejoin_insert_no_longer_names_role(self):
        self.assertNotIn("'member'", self.body)
        self.assertNotIn(", role,", self.body)

    def test_the_rejoining_member_starts_empty_so_the_credit_can_only_be_the_refund(self):
        """granted 0 / remaining 0: the 40 asserted at the end cannot be seeded state."""
        self.assertIn("VALUES (?, ?, 0, 0)", self.body)
        self.assertIn('"the org pool must be credited exactly once"', self.body)

    def test_the_exactly_once_assertions_that_were_never_reached_are_still_present(self):
        self.assertIn("for _ in range(3):", self.body)
        self.assertIn('"exactly one job_refund ledger row across three compensation passes"',
                      self.body)
        self.assertIn('"the marker must be cleared after settlement"', self.body)



if __name__ == "__main__":
    unittest.main(verbosity=2)
