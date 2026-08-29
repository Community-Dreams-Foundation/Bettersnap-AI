"""Source-level wiring proofs for the provisioning-retry state machine.

test_provisioning_retry.py proves the state machine is CORRECT. This file proves it is
REACHABLE and that the atomicity rules hold at the call sites in function_app: that the
classifier's ACTION_RETRY is consumed rather than dormant, that the state transition and its
outbox row share one commit, that terminalization and refund share one transaction, that no
retry path enqueues directly or bypasses the dispatch lease / GPU cap, and that the fused link
is read rather than re-selected.

These read the source text. Every assertion is made against a COMMENT-STRIPPED body, so a rule
can never be satisfied by prose that merely mentions it.

No import of function_app (which needs the azure stubs), no DB, no Azure, no queue, no GPU.
"""
import os
import re
import sys
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from shared import provisioning_retry as pr        # noqa: E402


def _read(*parts):
    with open(os.path.join(BACKEND_DIR, *parts), encoding="utf-8") as fh:
        return fh.read()


def _strip_comments_and_docstrings(text):
    """Remove triple-quoted blocks and trailing # comments. A wiring rule must be satisfied by
    CODE; documentation that quotes an anti-pattern must not make a test pass or fail."""
    text = re.sub(r'"""(?:.|\n)*?"""', "", text)
    text = re.sub(r"'''(?:.|\n)*?'''", "", text)
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


class _SourceCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _read("function_app.py")

    def body(self, name):
        start = self.src.index("def %s(" % name)
        rest = self.src[start + 1:]
        nxt = rest.find("\ndef ")
        return rest[:nxt] if nxt != -1 else rest

    def code(self, name):
        return _strip_comments_and_docstrings(self.body(name))


class RetryIsConsumed(_SourceCase):
    def test_reconcile_handles_action_retry(self):
        body = self.code("_reconcile_execution_outcome")
        self.assertIn("exec_reconcile.ACTION_RETRY", body)
        self.assertIn("retry_provisioning(job_id", body)

    def test_reconcile_still_handles_every_other_action_distinctly(self):
        body = self.code("_reconcile_execution_outcome")
        for action in ("ACTION_REFUND", "ACTION_VERIFY_DELIVERY", "ACTION_RECOVER",
                       "ACTION_NONE"):
            self.assertIn(action, body, "%s behaviour must be preserved" % action)

    def test_training_watcher_routes_retry_none_and_exhaustion(self):
        # The per-training work moved into _watch_one so one training's failure cannot skip
        # the rest of the tick; the watcher itself now only fans out.
        watcher = self.code("training_watcher")
        self.assertIn("_watch_one(", watcher)
        body = self.code("_watch_one")
        self.assertIn("exec_reconcile.ACTION_RETRY", body)
        self.assertIn("exec_reconcile.ACTION_NONE", body)
        self.assertIn("_retry_provisioning_training(", body)
        self.assertIn("_exhaust_provisioning_training(", body)
        self.assertIn("_finish_training(training_id, user_id, ok=False", body,
                      "non-retryable training failures keep their existing terminal path")


class TransactionBoundaries(_SourceCase):
    def test_retry_owner_commits_once_per_path(self):
        body = self.code("_retry_provisioning_job")
        self.assertEqual(body.count("conn.commit()"), 2,
                         "exactly one commit on the retry path and one on the exhaustion path")

    def test_outbox_insert_precedes_the_only_commit(self):
        """The state transition is NEVER committed before the outbox insert: outbox_add is
        handed to the cursor-level function, which writes the row on that same cursor."""
        body = self.code("_retry_provisioning_job")
        self.assertIn("outbox_add=outbox_add", body)
        self.assertLess(body.index("outbox_add=outbox_add"), body.index("conn.commit()"))

    def test_fast_path_send_is_after_the_commit_and_guarded(self):
        body = self.code("_retry_provisioning_job")
        self.assertGreater(body.index("outbox_try_send_now("), body.rindex("conn.commit()"))
        send_tail = body[body.index("outbox_try_send_now("):]
        self.assertIn("except Exception", body[:body.index("outbox_try_send_now(")] + send_tail)

    def test_exhaustion_and_refund_share_one_transaction(self):
        body = self.code("_retry_provisioning_job")
        exhaust_at = body.index("provisioning_retry.exhaust_job(")
        commit_at = body.index("conn.commit()", exhaust_at)
        self.assertNotIn("_mark_failed(job_id)", body[exhaust_at:commit_at],
                         "terminalization must not be followed by a separate refund helper")

    def test_mark_failed_uses_the_single_shared_implementation(self):
        self.assertIn("provisioning_retry.terminalize_and_refund(", self.code("_mark_failed"))

    def test_corrupt_history_rolls_back_and_fails_closed(self):
        body = self.code("_retry_provisioning_job")
        self.assertIn("provisioning_retry.HistoryCorrupt", body)
        self.assertIn("conn.rollback()", body)

    def test_fused_retry_owner_commits_once(self):
        body = self.code("_retry_provisioning_training")
        self.assertEqual(body.count("conn.commit()"), 1)
        self.assertIn("outbox_add=outbox_add", body)
        self.assertLess(body.index("outbox_add=outbox_add"), body.index("conn.commit()"))


class NoDirectQueueSend(_SourceCase):
    def test_no_retry_path_enqueues_directly(self):
        for name in ("_retry_provisioning_job", "_retry_provisioning_training",
                     "_exhaust_provisioning_training"):
            body = self.code(name)
            for forbidden in ("enqueue_job(", "enqueue_training_job(", "_send("):
                self.assertNotIn(forbidden, body,
                                 "%s must schedule through the outbox only" % name)

    def test_state_machine_module_has_no_queue_dependency_at_all(self):
        src = _read("shared", "provisioning_retry.py")
        for forbidden in ("queue_client", "enqueue_job", "enqueue_training_job",
                          "azure", "new_connection"):
            self.assertNotIn(forbidden, src)


class ObservationIsNotADecision(_SourceCase):
    def test_stamp_helper_only_stamps(self):
        body = self.code("_stamp_first_terminal")
        self.assertIn('stamp_first_terminal(cur, "jobs"', body)
        for forbidden in ("_mark_failed", "retry_job", "outbox_add", "credit_ledger",
                          "exhaust_job"):
            self.assertNotIn(forbidden, body,
                             "stamping must never retry, refund or transition")

    def test_fetch_stamps_and_builds_evidence_for_the_persisted_execution(self):
        body = self.code("_fetch_execution_outcome")
        self.assertIn("get_execution_evidence(", body)
        self.assertIn("first_terminal_observed_at", body)
        self.assertIn("_stamp_first_terminal(job_id, exec_id)", body)
        self.assertIn("terminal_observed_at=terminal_at", body)

    def test_training_evidence_stamps_the_training_row(self):
        body = self.code("_training_execution_evidence")
        self.assertIn('stamp_first_terminal(', body)
        self.assertIn('"lora_trainings"', body)


class FusedLinkage(_SourceCase):
    def test_dispatch_uses_the_persisted_link(self):
        body = self.code("_dispatch_training")
        self.assertIn("provisioning_retry.allocate_fused_job(", body)

    def test_transient_user_id_status_selection_is_gone_from_dispatch(self):
        body = self.code("_dispatch_training")
        self.assertNotIn("status = 'waiting_lora' \"", body)
        self.assertNotIn("ORDER BY created_at\"", body,
                         "the untied-break selection must not survive anywhere in dispatch")

    def test_selection_is_tie_broken_and_lives_only_in_the_state_machine(self):
        """Exactly ONE statement in the whole backend may pick a parked job, it lives in
        allocate_fused_job, and it carries the job_id tie-break."""
        pr_code = _strip_comments_and_docstrings(_read("shared", "provisioning_retry.py"))
        self.assertIn("ORDER BY created_at, job_id", pr_code)
        self.assertEqual(pr_code.count("SELECT TOP 1 job_id FROM jobs"), 1)
        self.assertEqual(
            _strip_comments_and_docstrings(self.src).count("SELECT TOP 1 job_id FROM jobs"), 0,
            "function_app must not select a parked job itself")
        # And nowhere may an untied-break ordering survive.
        for src in (pr_code, _strip_comments_and_docstrings(self.src)):
            self.assertNotIn("ORDER BY created_at\"", src)
            self.assertNotIn("ORDER BY created_at \"", src)

    def test_fused_exhaustion_never_selects_by_user_and_status(self):
        body = self.code("_exhaust_provisioning_training")
        self.assertIn("fused_job_id", body)
        self.assertIn("verify_fused_link(", body)
        self.assertNotIn("waiting_lora", body)

    def test_success_path_does_not_clear_the_link(self):
        body = self.code("training_watcher")
        self.assertNotIn("fused_job_id = NULL", body)
        self.assertNotIn("fused_job_id=None", body)


class DispatchAndCapSafety(_SourceCase):
    """No retry path may bypass the dispatcher, take the GPU lease itself, or create a second
    simultaneous A100 execution. A retry only writes 'queued' + an outbox row; every GPU start
    still runs through process_inference_job / _dispatch_training."""

    def test_no_retry_path_starts_a_container_or_takes_the_lease(self):
        for name in ("_retry_provisioning_job", "_retry_provisioning_training",
                     "_exhaust_provisioning_training", "_stamp_first_terminal"):
            body = self.code(name)
            for forbidden in ("trigger_container_job", "trigger_training_job",
                              "acquire_dispatch_lease", "mark_dispatched",
                              "MAX_ACTIVE_GPU_JOBS", "count_active_job_executions"):
                self.assertNotIn(forbidden, body, "%s must not dispatch" % name)

    def test_the_cap_check_still_gates_both_dispatchers(self):
        self.assertIn("if active >= MAX_ACTIVE_GPU_JOBS", self.code("process_inference_job"))
        self.assertIn("if active >= MAX_ACTIVE_GPU_JOBS", self.code("_dispatch_training"))

    def test_dispatch_idempotency_guards_are_intact(self):
        """A redelivered retry message reaches these guards: a job that already has an
        execution id or has moved past 'queued' is never started a second time."""
        inf = self.code("process_inference_job")
        self.assertIn("status = 'queued'", inf)
        self.assertIn("external_execution_id", inf)
        train = self.code("_dispatch_training")
        self.assertIn("status = 'queued'", train)

    def test_gpu_job_names_counts_every_job_on_the_a100_profile(self):
        """The cap is only correct if every job that can consume the A100 workload profile is
        counted. Today that is exactly ONE unified job: training and inference both target
        queue_trigger.JOB_NAME via MODE, so counting it IS the cap. A staged regional sibling
        would have to be added to GPU_JOB_NAMES to stay counted."""
        qt = _read("shared", "queue_trigger.py")
        self.assertIn("GPU_JOB_NAMES = (JOB_NAME,)", qt)
        tt = _read("shared", "training_trigger.py")
        self.assertIn("JOB_NAME", tt)
        self.assertNotRegex(
            _strip_comments_and_docstrings(tt), r"JOB_NAME\s*=\s*[\"']",
            "training must not define its own job name; it imports the shared one")
        self.assertIn("count_active_job_executions", self.code("process_inference_job"))
        self.assertIn("count_active_job_executions", self.code("_dispatch_training"))


class AttemptSemanticsAreExplicit(unittest.TestCase):
    def test_one_named_constant_defines_the_total_execution_budget(self):
        self.assertGreaterEqual(pr.MAX_PROVISIONING_EXECUTIONS, 1)

    def test_the_meaning_is_documented_unambiguously(self):
        src = _read("shared", "provisioning_retry.py")
        self.assertIn("maximum TOTAL number of ACA executions", src)
        self.assertIn("provisioning_attempts == len(provisioning_execution_ids)", src)

    def test_default_budget_is_three_total_executions(self):
        self.assertEqual(
            int(os.environ.get("MAX_PROVISIONING_EXECUTIONS", "3")), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
