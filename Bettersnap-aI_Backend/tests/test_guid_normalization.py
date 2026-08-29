"""UNIQUEIDENTIFIER equivalence — the defect the real SQL Server run exposed.

WHAT HAPPENED
`verify_fused_link` compared `str(jrow[0]) != str(user_id)`. SQL Server renders
`uniqueidentifier` as an UPPERCASE string through pyodbc, while every caller-side id in this
system is lowercase (Entra oids, `uuid.uuid4()`, queue payloads). The second fused allocator
therefore reported `wrong_user` for its own user, silently degrading fusing to plain
MODE=train and failing fused retries closed.

Measured on the real engine during case 8:

    str(user_id) from SQL : 448C5F40-56F7-4311-BFBC-B8D1215835A8
    caller passes         : 448c5f40-56f7-4311-bfbc-b8d1215835a8
    case-sensitive ==     : False

THE ASYMMETRY IS THE TRAP: `WHERE user_id = ?` is case-INSENSITIVE (SQL Server compares
uniqueidentifier as a binary type), so the SELECT finds the row and only the Python check
disagrees. No in-memory fake reproduces this, which is why the offline suite never saw it.

`already_refunded` carried the identical defect on the refund-ledger user comparison.

ACA execution names are NOT GUIDs and must keep comparing exactly — asserted below.

No Azure, no database, no queue, no GPU.
"""
import os
import sys
import unittest
import uuid

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from shared import provisioning_retry as pr                     # noqa: E402
from tests.test_provisioning_retry import DB, FakeCursor, FakeLedger   # noqa: E402

LOWER = "448c5f40-56f7-4311-bfbc-b8d1215835a8"
UPPER = "448C5F40-56F7-4311-BFBC-B8D1215835A8"
OTHER = "11111111-2222-3333-4444-555555555555"


class SameUserId(unittest.TestCase):
    """Parsed as UUIDs, never lowercased — and fail-closed on anything that is not one."""

    def test_uppercase_sql_guid_equals_lowercase_caller_guid(self):
        self.assertTrue(pr.same_user_id(UPPER, LOWER))
        self.assertTrue(pr.same_user_id(LOWER, UPPER))

    def test_identical_canonical_guids_are_equal(self):
        self.assertTrue(pr.same_user_id(LOWER, LOWER))
        self.assertTrue(pr.same_user_id(UPPER, UPPER))

    def test_a_real_uuid_object_compares_equal_to_its_string(self):
        self.assertTrue(pr.same_user_id(uuid.UUID(LOWER), UPPER))

    def test_braced_and_urn_forms_are_accepted(self):
        """uuid.UUID() canonicalises these; a .lower() comparison would not."""
        self.assertTrue(pr.same_user_id("{%s}" % UPPER, LOWER))
        self.assertTrue(pr.same_user_id("urn:uuid:%s" % LOWER, UPPER))
        self.assertTrue(pr.same_user_id(LOWER.replace("-", ""), UPPER))

    def test_different_guids_are_not_equal(self):
        self.assertFalse(pr.same_user_id(LOWER, OTHER))
        self.assertFalse(pr.same_user_id(UPPER, OTHER))

    def test_one_hex_digit_apart_is_not_equal(self):
        near = LOWER[:-1] + ("9" if LOWER[-1] != "9" else "a")
        self.assertFalse(pr.same_user_id(LOWER, near))

    def test_none_and_empty_fail_closed(self):
        for left, right in ((None, LOWER), (LOWER, None), (None, None),
                            ("", LOWER), (LOWER, ""), ("", "")):
            with self.subTest(pair=(left, right)):
                self.assertFalse(pr.same_user_id(left, right))

    def test_malformed_and_non_uuid_values_fail_closed(self):
        """A `.lower()` comparison would have called these EQUAL — which is exactly why the
        helper parses instead."""
        for value in ("not-a-guid", "USER-1", "user-1", "448c5f40", 12345, [], {},
                      LOWER + "x", "448c5f40-56f7-4311-bfbc-b8d1215835ag"):
            with self.subTest(value=value):
                self.assertFalse(pr.same_user_id(value, LOWER))
                self.assertFalse(pr.same_user_id(LOWER, value))

    def test_two_identical_non_uuid_strings_are_still_unequal(self):
        """Fail-closed: an unparseable owner is never treated as a match, even against
        itself."""
        self.assertFalse(pr.same_user_id("USER-1", "USER-1"))
        self.assertFalse(pr.same_user_id("user-1", "USER-1"))


class FusedLinkAcceptsMixedCase(unittest.TestCase):
    """The exact production path case 8 exercised."""

    def setUp(self):
        self.db = DB()
        self.cur = FakeCursor(self.db)

    def test_verify_fused_link_accepts_an_uppercase_stored_owner(self):
        self.db.add_job("J1", user_id=UPPER, status="processing")
        ok, reason = pr.verify_fused_link(self.cur, "J1", LOWER,
                                          pr.FUSED_RECLAIMABLE_STATES)
        self.assertTrue(ok, "the SQL-cased owner must match the caller-cased owner")
        self.assertEqual(reason, pr.LINK_OK)

    def test_verify_fused_link_still_rejects_a_genuinely_different_owner(self):
        self.db.add_job("J1", user_id=UPPER, status="processing")
        ok, reason = pr.verify_fused_link(self.cur, "J1", OTHER,
                                          pr.FUSED_RECLAIMABLE_STATES)
        self.assertFalse(ok)
        self.assertEqual(reason, pr.LINK_WRONG_USER)

    def test_verify_fused_link_fails_closed_on_a_malformed_owner(self):
        self.db.add_job("J1", user_id="not-a-guid", status="processing")
        ok, reason = pr.verify_fused_link(self.cur, "J1", LOWER,
                                          pr.FUSED_RECLAIMABLE_STATES)
        self.assertFalse(ok)
        self.assertEqual(reason, pr.LINK_WRONG_USER)

    def test_existing_link_reuse_works_with_mixed_case(self):
        """This is what failed on the real engine: the second allocator saw the persisted
        link, verified it, and rejected its own user."""
        self.db.add_training("T1", user_id=LOWER, fused_job_id="J1")
        self.db.add_job("J1", user_id=UPPER, status="waiting_lora")
        jid, why = pr.allocate_fused_job(self.cur, "T1", LOWER)
        self.assertEqual(jid, "J1")
        self.assertIn("reused", why)
        self.assertEqual(self.db.jobs["J1"]["status"], "processing")

    def test_fused_retry_reclaims_the_link_with_mixed_case(self):
        self.db.add_training("T1", user_id=LOWER, external_execution_id="e1",
                             fused_job_id="J1")
        self.db.add_job("J1", user_id=LOWER, status="processing")
        self.db.jobs["J1"]["user_id"] = UPPER          # as SQL would return it

        def fake_outbox_add(cur, queue, payload):
            cur.db.outbox.append({"queue": queue, "payload": payload})
            return len(cur.db.outbox)

        result = pr.retry_fused_training(self.cur, "T1", "e1",
                                         outbox_add=fake_outbox_add,
                                         queue_name="lora-training-jobs")
        self.assertEqual(result["plan"], pr.PLAN_RETRY,
                         "a mixed-case owner must not fail the fused retry closed")
        self.assertEqual(self.db.jobs["J1"]["status"], "waiting_lora")


class AlreadyRefundedAcceptsMixedCase(unittest.TestCase):
    """The second defect site: the refund-ledger user comparison."""

    def setUp(self):
        self.db = DB()
        self.cur = FakeCursor(self.db)
        self.ledger = FakeLedger()
        self.plan = {"total": 40, "user_id": LOWER, "target": pr.TARGET_USER,
                     "funding": pr.FUNDING_BUCKETED, "organization_id": None,
                     "aggregate_delta": 40, "monthly_delta": 0, "one_time_delta": 40}
        self.db.add_job("J1", user_id=LOWER, source_type="one_time")

    def test_correct_user_in_sql_casing_and_correct_amount_is_settled(self):
        self.ledger.add(self.db, UPPER, 40, self.ledger.REASON_JOB_REFUND, "J1")
        verdict, _rows = pr.already_refunded(self.cur, "J1", self.ledger, plan=self.plan)
        self.assertEqual(verdict, pr.REFUND_ROWS_SETTLED,
                         "an uppercase ledger user must match the lowercase plan user")

    def test_a_genuinely_different_user_is_still_a_conflict(self):
        self.ledger.add(self.db, OTHER, 40, self.ledger.REASON_JOB_REFUND, "J1")
        verdict, _rows = pr.already_refunded(self.cur, "J1", self.ledger, plan=self.plan)
        self.assertEqual(verdict, pr.REFUND_ROWS_CONFLICT)

    def test_right_user_wrong_amount_is_still_a_conflict(self):
        self.ledger.add(self.db, UPPER, 5, self.ledger.REASON_JOB_REFUND, "J1")
        verdict, _rows = pr.already_refunded(self.cur, "J1", self.ledger, plan=self.plan)
        self.assertEqual(verdict, pr.REFUND_ROWS_CONFLICT)

    def test_a_malformed_ledger_user_is_a_conflict_not_a_match(self):
        self.ledger.add(self.db, "not-a-guid", 40, self.ledger.REASON_JOB_REFUND, "J1")
        verdict, _rows = pr.already_refunded(self.cur, "J1", self.ledger, plan=self.plan)
        self.assertEqual(verdict, pr.REFUND_ROWS_CONFLICT)

    def test_compensation_does_not_pay_twice_when_the_casing_differs(self):
        """Before the fix this would have read as NOT settled and paid a SECOND time."""
        self.db.add_user(LOWER, 0, subscription_type="one_time")
        pr.mark_refund_pending(self.cur, "J1", self.plan)
        self.ledger.add(self.db, UPPER, 40, self.ledger.REASON_JOB_REFUND, "J1")
        state = pr.compensate_pending_refund(self.cur, "J1", credit_ledger=self.ledger)
        self.assertEqual(state, pr.REFUND_NONE)
        self.assertEqual(self.db.bal(LOWER), 0, "no second payment")
        self.assertEqual(
            len([r for r in self.db.ledger
                 if r.job_id == "J1" and r.transaction_type == "job_refund"]), 1)


class ExecutionNamesStayCaseSensitive(unittest.TestCase):
    """ACA execution names are opaque identifiers, not GUIDs. Normalising them would make two
    genuinely different executions look like one."""

    def test_plan_orphan_compares_execution_names_exactly(self):
        plan, why = pr.plan_orphan(status="processing", current_execution_id="exec-ABC",
                                   history=["exec-ABC"], candidate_execution_id="exec-abc",
                                   age=10, ceiling=1800)
        self.assertEqual(plan, pr.ORPHAN_RECOVER,
                         "'exec-abc' is a DIFFERENT execution from 'exec-ABC'")
        self.assertEqual(why, "exec-abc")

    def test_the_helper_is_not_applied_to_execution_comparisons(self):
        with open(os.path.join(BACKEND_DIR, "shared", "provisioning_retry.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        start = src.index("def plan_orphan(")
        region = src[start:src.index("def adopt_execution(")]
        self.assertIn("str(candidate_execution_id) != str(current_execution_id)", region)
        self.assertNotIn("same_user_id", region,
                         "execution names must never go through the GUID helper")

    def test_history_membership_stays_exact(self):
        plan, _why = pr.plan_orphan(status="processing", current_execution_id="exec-ABC",
                                    history=["exec-abc"], candidate_execution_id="exec-ABC",
                                    age=10, ceiling=1800)
        self.assertNotEqual(plan, pr.ORPHAN_RECOVER,
                            "the candidate equals the current execution, so nothing to adopt")


class OnlyGuidSitesWereChanged(unittest.TestCase):
    """Scoping: the helper is used at exactly the two audited user-id comparisons."""

    def setUp(self):
        with open(os.path.join(BACKEND_DIR, "shared", "provisioning_retry.py"),
                  encoding="utf-8") as fh:
            self.src = fh.read()

    def _code(self):
        return "\n".join(line.split("#", 1)[0] for line in self.src.splitlines())

    def test_the_helper_is_called_exactly_twice(self):
        calls = self._code().count("same_user_id(")
        self.assertEqual(calls, 3,
                         "one definition plus exactly two call sites: verify_fused_link and "
                         "already_refunded")

    def test_both_audited_sites_use_it(self):
        for marker in ("if not same_user_id(jrow[0], user_id):",
                       "user_ok = same_user_id(row_user, plan[\"user_id\"])"):
            with self.subTest(site=marker):
                self.assertIn(marker, self.src)

    def test_no_lower_based_guid_comparison_was_introduced(self):
        code = self._code()
        self.assertNotIn(".lower() == str(", code)
        self.assertNotIn("str(user_id).lower()", code)


if __name__ == "__main__":
    unittest.main(verbosity=2)
