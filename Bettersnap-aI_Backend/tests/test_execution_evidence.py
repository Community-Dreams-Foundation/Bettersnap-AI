"""Evidence collection + classification — offline, no Azure, no DB, no network.

The invariant every test here defends: ABSENCE IS NOT EVIDENCE. A missing preflight blob, an
empty Log Analytics result, or a failed query must never be read as proof that the container
never started, because the one class that reading would produce — pre-container provisioning
failure — is the only retryable one. Get that wrong and genuine application failures get
re-dispatched forever at A100 prices.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared import exec_reconcile as er           # noqa: E402
from shared import execution_evidence as ee       # noqa: E402
from shared import queue_trigger as qt            # noqa: E402


def ev(reason, at=0):
    return {"reason": reason, "at": at}


def evidence(**kw):
    kw.setdefault("execution_id", "exec-1")
    return ee.make_evidence(**kw)


# ── evidence structure ────────────────────────────────────────────────────────
class EvidenceStructure(unittest.TestCase):
    def test_all_documented_fields_present(self):
        e = evidence()
        for f in ee.FIELDS:
            self.assertIn(f, e, "%s missing from the evidence structure" % f)

    def test_telemetry_failure_yields_unknown_not_false(self):
        """None means 'we could not look'. False would mean 'we looked and it was absent',
        which is what licenses a pre-container verdict."""
        e = evidence(arm_status="Failed", telemetry_ok=False, telemetry_error="query failed")
        self.assertIsNone(e["container_started"])
        self.assertIsNone(e["backoff_limit_exceeded"])
        self.assertEqual(e["events"], ())

    def test_telemetry_success_with_no_matching_events_yields_false(self):
        e = evidence(events=[ev("AssigningReplica")], telemetry_ok=True)
        self.assertIs(e["container_started"], False)
        self.assertIs(e["backoff_limit_exceeded"], False)

    def test_events_are_never_surfaced_when_telemetry_failed(self):
        e = evidence(events=[ev("BackoffLimitExceeded")], telemetry_ok=False)
        self.assertEqual(e["events"], (), "partial events must not leak when the fetch failed")
        self.assertIsNone(e["backoff_limit_exceeded"])

    def test_preflight_and_container_exit_are_separate_fields(self):
        e = evidence(preflight_exit_code=0, container_exit_code=137, telemetry_ok=True)
        self.assertEqual(e["preflight_exit_code"], 0)
        self.assertEqual(e["container_exit_code"], 137)

    def test_legacy_exit_code_is_not_promoted_to_container_exit(self):
        """The legacy key was overloaded; promoting it would recreate the conflation."""
        e = ee.from_legacy({"exit_code": 1, "events": [], "execution_status": "Failed"})
        self.assertIsNone(e["container_exit_code"])

    def test_age_since_terminal(self):
        self.assertIsNone(ee.age_since_terminal(evidence(), now=100))
        self.assertEqual(ee.age_since_terminal(evidence(terminal_observed_at=40), now=100), 60)


# ── collection ────────────────────────────────────────────────────────────────
_LA_COLS = ("ExecutionName_s", "ReplicaName_s", "TimeGenerated", "Reason_s", "Log_s")


def _la(rows, exec_name="e-1", cols=_LA_COLS):
    """Shape a fake Log Analytics response. 3-element rows are the common case
    (TimeGenerated, Reason_s, Log_s) attributed to `exec_name`; pass 5 to control the
    correlation columns."""
    full = [r if len(r) == 5 else [exec_name, "", r[0], r[1], r[2]] for r in rows]
    return {"tables": [{"columns": [{"name": c} for c in cols], "rows": full}]}


class Collection(unittest.TestCase):
    def setUp(self):
        os.environ["LOG_ANALYTICS_WORKSPACE_ID"] = "ws-test"

    def test_parses_events_and_container_exit_code(self):
        rows = [["t1", "ContainerStarted", ""],
                ["t2", "ContainerTerminated", "was terminated with exit code '137' and reason"]]
        events, exit_code, err = qt.fetch_lifecycle_events("e-1", http=lambda q: _la(rows))
        self.assertEqual([x["reason"] for x in events], ["ContainerStarted", "ContainerTerminated"])
        self.assertEqual(exit_code, 137)
        self.assertIsNone(err)

    def test_empty_result_is_unknown_not_empty(self):
        events, exit_code, err = qt.fetch_lifecycle_events("e-1", http=lambda q: _la([]))
        self.assertIsNone(events, "no rows means UNKNOWN, never 'nothing happened'")
        self.assertIsNone(exit_code)
        self.assertIn("no rows", err)

    def test_query_exception_is_unknown(self):
        def boom(q):
            raise RuntimeError("LA down")
        events, exit_code, err = qt.fetch_lifecycle_events("e-1", http=boom)
        self.assertIsNone(events)
        self.assertIn("RuntimeError", err)

    def test_rejects_malformed_execution_name(self):
        events, _, err = qt.fetch_lifecycle_events("e'; DROP TABLE--", http=lambda q: _la([]))
        self.assertIsNone(events)
        self.assertEqual(err, "invalid execution name")

    def test_arm_failure_does_not_invent_telemetry(self):
        def bad_outcome(x, y):
            raise RuntimeError("ARM down")
        e = qt.get_execution_evidence("e-1", get_outcome=bad_outcome,
                                      get_events=lambda x, http=None: ([ev("ContainerStarted")], None, None))
        self.assertIsNone(e["arm_status"])
        self.assertIs(e["container_started"], True)

    def test_telemetry_failure_does_not_invent_arm_status(self):
        e = qt.get_execution_evidence(
            "e-1", get_outcome=lambda x, y: {"execution_status": "Failed"},
            get_events=lambda x, http=None: (None, None, "no rows ingested yet"))
        self.assertEqual(e["arm_status"], "failed")
        self.assertFalse(e["telemetry_ok"])
        self.assertIsNone(e["backoff_limit_exceeded"])


# ── classification ────────────────────────────────────────────────────────────
class Classification(unittest.TestCase):
    def c(self, **kw):
        now = kw.pop("now", 10_000)
        return er.classify_execution(evidence(**kw), now=now)

    def test_confirmed_pre_container_failure_is_retryable(self):
        d = self.c(arm_status="Failed", telemetry_ok=True, terminal_observed_at=0,
                   events=[ev("AssigningReplica"), ev("BackoffLimitExceeded")])
        self.assertEqual(d["action"], er.ACTION_RETRY)
        self.assertEqual(d["failure_class"], er.CLASS_PRE_CONTAINER_PROVISIONING)
        self.assertTrue(d["is_infra"])

    def test_pre_container_class_is_in_INFRA_CLASSES(self):
        self.assertIn(er.CLASS_PRE_CONTAINER_PROVISIONING, er.INFRA_CLASSES)

    def test_successful_execution_requires_delivery_verification(self):
        d = self.c(arm_status="Succeeded", telemetry_ok=True, events=[ev("ContainerStarted")])
        self.assertEqual(d["action"], er.ACTION_VERIFY_DELIVERY)
        self.assertEqual(d["failure_class"], er.CLASS_SUCCESS)

    def test_container_started_then_nonzero_exit_is_application(self):
        d = self.c(arm_status="Failed", telemetry_ok=True, terminal_observed_at=0,
                   container_exit_code=1,
                   events=[ev("ContainerStarted"), ev("ContainerTerminated")])
        self.assertEqual(d["action"], er.ACTION_REFUND)
        self.assertEqual(d["failure_class"], er.CLASS_APPLICATION)
        self.assertFalse(d["is_infra"])

    def test_preflight_infra_exit_wins(self):
        d = self.c(arm_status="Failed", preflight_exit_code=43, telemetry_ok=True,
                   terminal_observed_at=0, events=[ev("ContainerStarted")])
        self.assertEqual(d["failure_class"], er.INFRA_EXIT[43])
        self.assertTrue(d["is_infra"])

    def test_passing_preflight_proves_container_started(self):
        """gpu_preflight runs INSIDE the container, so any recorded exit — including 0 —
        rules out a pre-container failure."""
        d = self.c(arm_status="Failed", preflight_exit_code=0, telemetry_ok=False,
                   terminal_observed_at=0, now=10_000)
        self.assertEqual(d["failure_class"], er.CLASS_APPLICATION)

    def test_terminal_with_no_events_yet_observes_not_retries(self):
        d = self.c(arm_status="Failed", telemetry_ok=False,
                   telemetry_error="no rows ingested yet", terminal_observed_at=9_990, now=10_000)
        self.assertEqual(d["action"], er.ACTION_NONE)
        self.assertEqual(d["failure_class"], er.CLASS_PENDING)

    def test_log_analytics_failure_never_retries(self):
        d = self.c(arm_status="Failed", telemetry_ok=False, telemetry_error="RuntimeError: LA down",
                   terminal_observed_at=9_000, now=10_000)
        self.assertNotEqual(d["action"], er.ACTION_RETRY)
        self.assertEqual(d["action"], er.ACTION_NONE)

    def test_pulling_image_without_container_started_is_not_pre_container(self):
        """No BackoffLimitExceeded means the scheduler has not given up; that is a stall
        question, not a provisioning-failure verdict."""
        d = self.c(arm_status="Failed", telemetry_ok=True, terminal_observed_at=9_000,
                   now=10_000, events=[ev("AssigningReplica"), ev("PullingImage")])
        self.assertNotEqual(d["action"], er.ACTION_RETRY)

    def test_backoff_AFTER_container_started_is_application_not_retryable(self):
        """Conflicting evidence: the container demonstrably ran, so this is not pre-container."""
        d = self.c(arm_status="Failed", telemetry_ok=True, terminal_observed_at=0,
                   events=[ev("ContainerStarted"), ev("BackoffLimitExceeded")])
        self.assertEqual(d["action"], er.ACTION_REFUND)
        self.assertEqual(d["failure_class"], er.CLASS_APPLICATION)

    def test_container_started_without_termination_is_still_running(self):
        d = er.classify_execution(
            evidence(arm_status="Running", telemetry_ok=True, events=[ev("ContainerStarted", 1)]),
            now=100_000)
        self.assertEqual(d["action"], er.ACTION_NONE)
        self.assertEqual(d["failure_class"], er.CLASS_PENDING)

    def test_operator_stop_is_decided_before_retry(self):
        d = self.c(arm_status="Stopped", telemetry_ok=True, terminal_observed_at=0,
                   events=[ev("BackoffLimitExceeded")])
        self.assertEqual(d["failure_class"], er.CLASS_OPERATOR_STOPPED)
        self.assertNotEqual(d["action"], er.ACTION_RETRY)

    # ── window boundaries ────────────────────────────────────────────────────
    def test_inside_ingestion_grace_observes(self):
        d = self.c(arm_status="Failed", telemetry_ok=True, events=[ev("AssigningReplica")],
                   terminal_observed_at=10_000 - (er.INGESTION_GRACE_S - 1), now=10_000)
        self.assertEqual(d["action"], er.ACTION_NONE)

    def test_just_past_ingestion_grace_stops_waiting_on_grace(self):
        d = self.c(arm_status="Failed", telemetry_ok=True, events=[ev("AssigningReplica")],
                   terminal_observed_at=10_000 - (er.INGESTION_GRACE_S + 1), now=10_000)
        self.assertNotIn("ingestion grace", d["reason"])

    def test_past_max_observation_refunds_as_unclassified(self):
        d = self.c(arm_status="Failed", telemetry_ok=False, telemetry_error="LA down",
                   terminal_observed_at=10_000 - (er.MAX_OBSERVATION_S + 1), now=10_000)
        self.assertEqual(d["action"], er.ACTION_REFUND)
        self.assertEqual(d["failure_class"], er.CLASS_UNCLASSIFIED_TERMINAL_FAILURE)

    def test_unclassified_is_NOT_claimed_as_infra(self):
        """We refund because the user got nothing, but we do not fabricate a cause."""
        d = self.c(arm_status="Failed", telemetry_ok=False, telemetry_error="LA down",
                   terminal_observed_at=0, now=10_000 + er.MAX_OBSERVATION_S)
        self.assertFalse(d["is_infra"])
        self.assertNotIn(er.CLASS_UNCLASSIFIED_TERMINAL_FAILURE, er.INFRA_CLASSES)

    def test_no_fabricated_events_can_produce_a_retry(self):
        """Exhaustive: with telemetry unavailable, NO arm status may yield ACTION_RETRY."""
        for status in ("failed", "degraded", "cancelled", "stopped", "succeeded", "running"):
            for age in (0, er.INGESTION_GRACE_S + 1, er.MAX_OBSERVATION_S + 1):
                d = er.classify_execution(
                    evidence(arm_status=status, telemetry_ok=False, telemetry_error="x",
                             terminal_observed_at=0), now=age)
                self.assertNotEqual(d["action"], er.ACTION_RETRY,
                                    "status=%s age=%s produced a retry with no telemetry"
                                    % (status, age))


# ── correlation against REAL Log Analytics row shapes ─────────────────────────
class Correlation(unittest.TestCase):
    """ACA does not populate ExecutionName_s on every row. A production row observed on
    bettersnapai-sqldiag3-cpu carried ExecutionName_s='' with EventSource_s
    'ContainerAppController'. Filtering on that column alone would have discarded the
    execution's own lifecycle rows and reported 'nothing happened'."""

    def setUp(self):
        os.environ["LOG_ANALYTICS_WORKSPACE_ID"] = "ws-test"

    def test_blank_execution_name_matched_by_replica_name(self):
        rows = [["", "e-1-abc123", "t1", "ContainerStarted", ""]]
        events, _, err = qt.fetch_lifecycle_events("e-1", http=lambda q: _la(rows))
        self.assertIsNone(err)
        self.assertEqual([e["reason"] for e in events], ["ContainerStarted"])

    def test_execution_level_row_matched_by_log_text(self):
        rows = [["", "", "t1", "BackoffLimitExceeded",
                 "Job execution e-1 has failed: BackoffLimitExceeded"]]
        events, _, err = qt.fetch_lifecycle_events("e-1", http=lambda q: _la(rows))
        self.assertIsNone(err)
        self.assertEqual([e["reason"] for e in events], ["BackoffLimitExceeded"])

    def test_similarly_named_execution_never_cross_matches(self):
        """'e-1' must not absorb rows belonging to 'e-12' — the exact-name anchor and the
        trailing hyphen on the replica prefix are what prevent it."""
        rows = [["e-12", "e-12-abc", "t1", "ContainerStarted", "container of e-12 started"],
                ["", "e-1-xyz", "t2", "BackoffLimitExceeded", ""]]
        events, _, err = qt.fetch_lifecycle_events("e-1", http=lambda q: _la(rows))
        self.assertIsNone(err)
        self.assertEqual([e["reason"] for e in events], ["BackoffLimitExceeded"],
                         "rows for e-12 leaked into e-1's evidence")

    def _log_only(self, log, exec_name="e-1"):
        """One execution-level row: no ExecutionName_s, no ReplicaName_s, so ONLY the Log_s
        fallback can match it."""
        events, _, _ = qt.fetch_lifecycle_events(
            exec_name, http=lambda q: _la([["", "", "t1", "ContainerStarted", log]]))
        return events is not None

    def test_log_fallback_rejects_longer_identifiers(self):
        """'e-1-other' is a DIFFERENT execution, not a replica of 'e-1'. Suffix matching
        belongs to the ReplicaName_s rule alone, so hyphen is a name character here."""
        self.assertFalse(self._log_only("Job execution e-1-other has failed"))
        self.assertFalse(self._log_only("Job execution e-12 has failed"))

    def test_log_fallback_rejects_preceding_collisions(self):
        self.assertFalse(self._log_only("execution xe-1 failed"))
        self.assertFalse(self._log_only("execution x-e-1 failed"))

    def test_log_fallback_accepts_delimited_exact_matches(self):
        for log in ("execution e-1 failed",
                    "Job Execution 'e-1'",
                    'Job Execution "e-1" ended',
                    "(e-1)",
                    "replica for e-1, giving up",
                    "failed execution: e-1"):
            self.assertTrue(self._log_only(log), "should match: %r" % log)

    def test_log_fallback_accepts_end_of_string(self):
        self.assertTrue(self._log_only("BackoffLimitExceeded for e-1"))

    def test_replica_rule_still_matches_the_suffixed_replica(self):
        """The suffix that Log_s now rejects is exactly what ReplicaName_s must still accept."""
        rows = [["", "e-1-abc", "t1", "ContainerStarted", ""]]
        events, _, err = qt.fetch_lifecycle_events("e-1", http=lambda q: _la(rows))
        self.assertIsNone(err)
        self.assertEqual([e["reason"] for e in events], ["ContainerStarted"])

    def test_replica_rule_does_not_match_a_longer_execution_name(self):
        rows = [["", "e-1-other-xyz", "t1", "ContainerStarted", ""]]
        events, _, _ = qt.fetch_lifecycle_events("e-1-other", http=lambda q: _la(rows))
        self.assertIsNotNone(events)
        events, _, err = qt.fetch_lifecycle_events("e-12", http=lambda q: _la(rows))
        self.assertIsNone(events, "'e-1-other-xyz' is not a replica of 'e-12'")
        self.assertIn("no rows for this execution", err)

    def test_replica_prefix_requires_the_hyphen(self):
        rows = [["", "e-1abc", "t1", "ContainerStarted", ""]]
        events, _, err = qt.fetch_lifecycle_events("e-1", http=lambda q: _la(rows))
        self.assertIsNone(events, "'e-1abc' is a different execution, not a replica of 'e-1'")
        self.assertIn("no rows for this execution", err)

    def test_only_unrelated_rows_is_unknown_not_empty_observation(self):
        rows = [["e-99", "e-99-a", "t1", "ContainerStarted", ""]]
        events, exit_code, err = qt.fetch_lifecycle_events("e-1", http=lambda q: _la(rows))
        self.assertIsNone(events, "unrelated rows must not read as 'we looked and saw nothing'")
        self.assertIsNone(exit_code)
        self.assertIn("no rows for this execution", err)
        # and that unknown must never become a retry
        e = qt.get_execution_evidence(
            "e-1", get_outcome=lambda x, y: {"execution_status": "Failed"},
            get_events=lambda x, http=None: (events, exit_code, err))
        self.assertFalse(e["telemetry_ok"])
        self.assertNotEqual(er.classify_execution(e, now=1e9)["action"], er.ACTION_RETRY)

    def test_missing_columns_fail_closed_as_unknown(self):
        rows = [["t1", "ContainerStarted", ""]]
        body = {"tables": [{"columns": [{"name": n} for n in
                                        ("TimeGenerated", "Reason_s", "Log_s")], "rows": rows}]}
        events, _, err = qt.fetch_lifecycle_events("e-1", http=lambda q: body)
        self.assertIsNone(events)
        self.assertIn("unexpected columns", err)

    def test_unexpected_column_order_is_read_by_name(self):
        cols = ("Log_s", "Reason_s", "TimeGenerated", "ReplicaName_s", "ExecutionName_s")
        rows = [["", "ContainerStarted", "t1", "", "e-1"]]
        events, _, err = qt.fetch_lifecycle_events("e-1", http=lambda q: _la(rows, cols=cols))
        self.assertIsNone(err)
        self.assertEqual([e["reason"] for e in events], ["ContainerStarted"])

    def test_query_projects_all_five_columns(self):
        seen = {}

        def cap(q):
            seen["q"] = q
            return _la([])
        qt.fetch_lifecycle_events("e-1", http=cap)
        for col in _LA_COLS:
            self.assertIn(col, seen["q"])


# ── event timestamp format ────────────────────────────────────────────────────
class EventTimestamps(unittest.TestCase):
    """startup_stall._first does min() over e['time'] and compares it numerically to `now`.
    An ISO string there raises or mis-orders, so the collector must emit epoch seconds."""

    def setUp(self):
        os.environ["LOG_ANALYTICS_WORKSPACE_ID"] = "ws-test"

    def test_time_is_numeric_epoch_and_at_keeps_the_raw_value(self):
        raw = "2026-08-28T12:00:00Z"
        events, _, _ = qt.fetch_lifecycle_events(
            "e-1", http=lambda q: _la([[raw, "ContainerStarted", ""]]))
        self.assertIsInstance(events[0]["time"], float)
        self.assertEqual(events[0]["at"], raw)

    def test_seven_digit_fractional_seconds_parse(self):
        """Log Analytics emits 7 fractional digits; datetime accepts at most 6."""
        events, _, _ = qt.fetch_lifecycle_events(
            "e-1", http=lambda q: _la([["2026-08-28T12:00:00.1234567Z", "ContainerStarted", ""]]))
        self.assertIsNotNone(events[0]["time"])

    def test_unparseable_timestamp_is_none_not_a_guess(self):
        events, _, _ = qt.fetch_lifecycle_events(
            "e-1", http=lambda q: _la([["not-a-time", "ContainerStarted", ""]]))
        self.assertIsNone(events[0]["time"])

    def test_end_to_end_startup_stall_still_fires_on_collected_events(self):
        """Offline integration: LA response -> get_execution_evidence -> classify_execution.
        The image pull started and never completed, so the stall deadline must still trip."""
        rows = [["2026-08-28T12:00:00Z", "AssigningReplica", ""],
                ["2026-08-28T12:00:05Z", "PullingImage", ""]]
        collected = qt.fetch_lifecycle_events("e-1", http=lambda q: _la(rows))
        e = qt.get_execution_evidence(
            "e-1", get_outcome=lambda x, y: {"execution_status": "Running"},
            get_events=lambda x, http=None: collected)
        pull_at = collected[0][1]["time"]
        d = er.classify_execution(e, now=pull_at + 3600)
        self.assertEqual(d["action"], er.ACTION_REFUND)
        self.assertIn(d["failure_class"], er.INFRA_CLASSES)

    def test_end_to_end_healthy_pull_does_not_trip_the_stall(self):
        rows = [["2026-08-28T12:00:00Z", "AssigningReplica", ""],
                ["2026-08-28T12:00:05Z", "PullingImage", ""],
                ["2026-08-28T12:00:40Z", "PulledImage", ""],
                ["2026-08-28T12:00:45Z", "ContainerStarted", ""]]
        collected = qt.fetch_lifecycle_events("e-1", http=lambda q: _la(rows))
        e = qt.get_execution_evidence(
            "e-1", get_outcome=lambda x, y: {"execution_status": "Running"},
            get_events=lambda x, http=None: collected)
        started_at = collected[0][-1]["time"]
        d = er.classify_execution(e, now=started_at + 60)
        self.assertEqual(d["action"], er.ACTION_NONE)


# ── PodDeletion detail ────────────────────────────────────────────────────────
class PodDeletionEvidence(unittest.TestCase):
    """The reason name 'PodDeletion' is identical for a clean teardown and a failed one; the
    outcome lives in the message. Storing only the reason discards the discriminating fact."""

    def _detail(self, log):
        return ee.make_evidence(
            telemetry_ok=True,
            events=[{"reason": "PodDeletion", "time": 1, "at": "t1", "log": log}])

    def test_failed_status_message_is_preserved(self):
        msg = "PodDeletion: pod e-1-abc has exited with status Failed"
        e = self._detail(msg)
        self.assertEqual(e["pod_deletion"], "PodDeletion")
        self.assertEqual(e["pod_deletion_detail"], msg)

    def test_succeeded_status_message_is_preserved(self):
        msg = "PodDeletion: pod e-1-abc has exited with status Succeeded"
        e = self._detail(msg)
        self.assertEqual(e["pod_deletion"], "PodDeletion")
        self.assertEqual(e["pod_deletion_detail"], msg)
        self.assertNotEqual(e["pod_deletion_detail"], self._detail(
            "PodDeletion: pod e-1-abc has exited with status Failed")["pod_deletion_detail"])

    def test_detail_is_none_when_telemetry_unavailable(self):
        e = ee.make_evidence(telemetry_ok=False, events=[{"reason": "PodDeletion", "log": "x"}])
        self.assertIsNone(e["pod_deletion"])
        self.assertIsNone(e["pod_deletion_detail"])

    def test_collector_carries_the_log_through(self):
        os.environ["LOG_ANALYTICS_WORKSPACE_ID"] = "ws-test"
        msg = "pod e-1-abc has exited with status Failed"
        events, _, _ = qt.fetch_lifecycle_events(
            "e-1", http=lambda q: _la([["2026-08-28T12:00:00Z", "PodDeletion", msg]]))
        self.assertEqual(ee.make_evidence(telemetry_ok=True,
                                          events=events)["pod_deletion_detail"], msg)


# ── ingestion grace is a REQUIRED clause of the retry decision ────────────────
class RetryGrace(unittest.TestCase):
    """Log Analytics ingests late and out of order, so the pre-container SHAPE can appear
    before a ContainerStarted row for the same execution has landed. Retrying then would
    re-dispatch an A100 for a run whose container actually started."""

    def shape(self, *, terminal_observed_at, now, events=None):
        return er.classify_execution(
            evidence(arm_status="Failed", telemetry_ok=True,
                     terminal_observed_at=terminal_observed_at,
                     events=events or [ev("AssigningReplica"), ev("BackoffLimitExceeded")]),
            now=now)

    def test_unknown_terminal_timestamp_is_pending_not_retry(self):
        d = self.shape(terminal_observed_at=None, now=10_000)
        self.assertEqual(d["action"], er.ACTION_NONE)
        self.assertEqual(d["failure_class"], er.CLASS_PENDING)

    def test_one_second_below_grace_is_pending(self):
        d = self.shape(terminal_observed_at=10_000 - (er.INGESTION_GRACE_S - 1), now=10_000)
        self.assertEqual(d["action"], er.ACTION_NONE)
        self.assertEqual(d["failure_class"], er.CLASS_PENDING)

    def test_exactly_at_grace_retries(self):
        d = self.shape(terminal_observed_at=10_000 - er.INGESTION_GRACE_S, now=10_000)
        self.assertEqual(d["action"], er.ACTION_RETRY)
        self.assertEqual(d["failure_class"], er.CLASS_PRE_CONTAINER_PROVISIONING)

    def test_one_second_past_grace_retries(self):
        d = self.shape(terminal_observed_at=10_000 - (er.INGESTION_GRACE_S + 1), now=10_000)
        self.assertEqual(d["action"], er.ACTION_RETRY)

    def test_late_container_started_row_flips_the_verdict_to_application(self):
        """The exact race the grace exists for: the same execution, re-read after the
        ContainerStarted row lands. It must become an application failure, never a retry."""
        early = self.shape(terminal_observed_at=0, now=er.INGESTION_GRACE_S - 1)
        self.assertEqual(early["action"], er.ACTION_NONE)
        late = self.shape(terminal_observed_at=0, now=er.INGESTION_GRACE_S + 1,
                          events=[ev("AssigningReplica"), ev("BackoffLimitExceeded"),
                                  ev("ContainerStarted", 5)])
        self.assertEqual(late["action"], er.ACTION_REFUND)
        self.assertEqual(late["failure_class"], er.CLASS_APPLICATION)

    def test_no_retry_is_possible_below_grace_for_any_evidence(self):
        for started in (ev("ContainerStarted"), None):
            for age in range(0, er.INGESTION_GRACE_S, 17):
                evs = [ev("BackoffLimitExceeded")] + ([started] if started else [])
                d = er.classify_execution(
                    evidence(arm_status="Failed", telemetry_ok=True,
                             terminal_observed_at=10_000 - age, events=evs), now=10_000)
                self.assertNotEqual(d["action"], er.ACTION_RETRY,
                                    "age=%s started=%s retried inside the grace"
                                    % (age, bool(started)))


class RetryResetContract(unittest.TestCase):
    """Documented here, implemented with redispatch in a later phase."""

    def test_migration_033_documents_the_per_attempt_reset(self):
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "migrations", "033_provisioning_retry.sql")
        if not os.path.exists(p):
            self.skipTest("migration 033 not present")
        with open(p, encoding="utf-8") as fh:
            doc = fh.read()
        self.assertIn("PER ACA EXECUTION ATTEMPT", doc.upper())
        self.assertIn("first_terminal_observed_at = NULL", doc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
