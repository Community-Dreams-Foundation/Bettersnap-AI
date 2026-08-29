"""Strict validation of stored retrain charge amounts, and the accounting_invalid outcome.

THE DEFECT THIS CLOSES
`build_orphan_marker` used `max(0, int(value or 0))`. Migration 026 declares
monthly_credit_cost / one_time_credit_cost as `INT NOT NULL DEFAULT 0` with **no CHECK**, so a
negative value is representable in the schema. That clamp would have turned a corrupt -20 into
0, and the reason would then have been derived as FREE — an accounting corruption reported as a
confident "nothing was owed". `int()` would likewise have accepted True as 1 and "20" as 20.

A charge is now accepted only as a non-negative plain int. Anything else terminalizes the
training as `accounting_invalid` with `aggregate_owed: null`, mutating nothing.

No Azure, no database, no queue, no GPU.

Run: python -m unittest tests.test_training_orphan_amounts   (from the backend dir)
"""
import os
import sys
import unittest
from unittest import mock

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from shared import training_orphan as to                     # noqa: E402
from tests.test_training_orphan import _Base, TID, UID       # noqa: E402
from tests.test_fused_exhaustion_composed import function_app  # noqa: E402


BAD_AMOUNTS = [
    ("negative", -1),
    ("large_negative", -999999),
    ("bool_true", True),
    ("bool_false", False),
    ("float_whole", 20.0),
    ("float_fraction", 20.5),
    ("numeric_string", "20"),
    ("empty_string", ""),
    ("none", None),
    ("list", [20]),
    ("dict", {"v": 20}),
]


class ChargeValidation(unittest.TestCase):
    """A charge is trustworthy ONLY as a non-negative plain int. Never coerced, never clamped."""

    def test_valid_amounts_are_accepted(self):
        for value in (0, 1, 20, 999999999):
            with self.subTest(value=value):
                self.assertTrue(to.is_valid_charge(value))

    def test_every_invalid_shape_is_rejected(self):
        for label, value in BAD_AMOUNTS:
            with self.subTest(case=label):
                self.assertFalse(to.is_valid_charge(value),
                                 "%s (%r) must not be accepted as a charge" % (label, value))

    def test_bool_is_rejected_even_though_it_subclasses_int(self):
        """isinstance(True, int) is True — which is exactly why the check uses `type() is int`.
        Accepting True would silently mean a charge of 1."""
        self.assertTrue(isinstance(True, int))
        self.assertFalse(to.is_valid_charge(True))


class AccountingInvalidMarker(unittest.TestCase):
    def marker_for(self, monthly, one_time, **kw):
        return to.build_orphan_marker(TID, UID, monthly_owed=monthly,
                                      one_time_owed=one_time, **kw)

    def test_every_invalid_shape_produces_accounting_invalid(self):
        for label, value in BAD_AMOUNTS:
            with self.subTest(case=label):
                _marker, payload = self.marker_for(value, 0)
                self.assertEqual(payload["reason"], to.REASON_INVALID)
                self.assertFalse(payload["amounts_valid"])
                self.assertIsNone(payload["aggregate_owed"])

    def test_mixed_valid_and_invalid_is_still_invalid(self):
        _marker, payload = self.marker_for(20, -3)
        self.assertEqual(payload["reason"], to.REASON_INVALID)
        self.assertIsNone(payload["aggregate_owed"],
                          "a partially-valid pair must not report a partial amount")
        self.assertIsNone(payload["monthly_owed"])
        self.assertIsNone(payload["one_time_owed"])

    def test_aggregate_is_null_not_zero(self):
        """JSON null, so a reader cannot mistake "unknown" for "nothing was owed"."""
        marker, _ = self.marker_for(-5, -5)
        self.assertIn('"aggregate_owed":null', marker)

    def test_observed_values_are_preserved_for_diagnosis(self):
        _marker, payload = self.marker_for(20.5, "abc")
        observed = payload["observed"]
        self.assertEqual(observed["monthly_credit_cost"]["type"], "float")
        self.assertIn("20.5", observed["monthly_credit_cost"]["repr"])
        self.assertEqual(observed["one_time_credit_cost"]["type"], "str")
        self.assertIn("abc", observed["one_time_credit_cost"]["repr"])

    def test_only_the_offending_field_is_reported(self):
        _marker, payload = self.marker_for(20, -3)
        self.assertNotIn("monthly_credit_cost", payload["observed"])
        self.assertIn("one_time_credit_cost", payload["observed"])

    def test_observed_rendering_is_bounded(self):
        _marker, payload = self.marker_for("Q" * 5000, 0)
        rendered = payload["observed"]["monthly_credit_cost"]["repr"]
        self.assertLessEqual(len(rendered), to.OBSERVED_VALUE_MAX)

    def test_a_huge_observed_value_never_costs_us_the_marker(self):
        """Diagnostics are shed before the marker is ever at risk."""
        marker, payload = self.marker_for("Q" * 100000, "R" * 100000,
                                          original_error="Z" * 5000)
        self.assertLessEqual(len(marker), to.ERROR_COLUMN_MAX)
        self.assertIsNotNone(to.parse_orphan_marker(marker))
        self.assertEqual(payload["reason"], to.REASON_INVALID)

    def test_schema_version_is_present_in_every_shape(self):
        for monthly, one_time in ((0, 0), (20, 15), (-1, 0)):
            with self.subTest(amounts=(monthly, one_time)):
                _marker, payload = self.marker_for(monthly, one_time)
                self.assertEqual(payload["schema_version"], to.SCHEMA_VERSION)


class ReasonConsistency(unittest.TestCase):
    """The reason is DERIVED from the validated amounts, so a mismatch is unrepresentable."""

    def test_free_only_when_both_are_zero(self):
        _m, payload = to.build_orphan_marker(TID, UID, monthly_owed=0, one_time_owed=0)
        self.assertEqual(payload["reason"], to.REASON_FREE)

    def test_paid_only_when_the_aggregate_is_positive(self):
        for monthly, one_time in ((1, 0), (0, 1), (20, 15)):
            with self.subTest(amounts=(monthly, one_time)):
                _m, payload = to.build_orphan_marker(
                    TID, UID, monthly_owed=monthly, one_time_owed=one_time)
                self.assertEqual(payload["reason"], to.REASON_PAID)

    def test_free_with_a_positive_aggregate_is_rejected(self):
        with self.assertRaises(to.ReasonAmountMismatch):
            to.check_reason(to.REASON_FREE, 20, 15, True)

    def test_paid_with_a_zero_aggregate_is_rejected(self):
        with self.assertRaises(to.ReasonAmountMismatch):
            to.check_reason(to.REASON_PAID, 0, 0, True)

    def test_invalid_when_amounts_validated_is_rejected(self):
        with self.assertRaises(to.ReasonAmountMismatch):
            to.check_reason(to.REASON_INVALID, 20, 15, True)

    def test_free_or_paid_when_amounts_did_not_validate_is_rejected(self):
        for reason in (to.REASON_FREE, to.REASON_PAID):
            with self.subTest(reason=reason):
                with self.assertRaises(to.ReasonAmountMismatch):
                    to.check_reason(reason, 0, 0, False)

    def test_unknown_reason_is_rejected(self):
        with self.assertRaises(to.ReasonAmountMismatch):
            to.check_reason("something_else", 0, 0, True)


class AccountingInvalidTerminalization(_Base):
    """EXISTING user, corrupt stored charge. A DIFFERENT condition from an orphan.

    The earlier version of this class asserted that no user row was written -- which meant
    users.lora_status stayed 'training' FOREVER and reserve_training_slot refused every future
    retrain with `already_training`. That test locked in a P0. The lifecycle must complete;
    only the REFUND is withheld.
    """

    monthly_charge = -20
    one_time_charge = 15
    seed_user = True

    def marker(self):
        return to.parse_marker(self.db.trainings[TID]["error"])

    def lora_status(self):
        return (self.db.users.get("__lora__") or {}).get(UID)

    def test_uses_the_accounting_prefix_never_ORPHAN_USER(self):
        self.finish()
        stored = self.db.trainings[TID]["error"]
        self.assertTrue(to.is_accounting_marker(stored))
        self.assertFalse(to.is_orphan_marker(stored),
                         "the user EXISTS; claiming ORPHAN_USER sends operators hunting for "
                         "a deleted row that is sitting right there")

    def test_terminalizes_once_with_amounts_valid_false(self):
        self.finish()
        self.assertEqual(self.db.trainings[TID]["status"], "failed")
        payload = self.marker()
        self.assertEqual(payload["reason"], to.REASON_INVALID)
        self.assertFalse(payload["amounts_valid"])
        self.assertIsNone(payload["aggregate_owed"])
        self.assertEqual(payload["schema_version"], to.SCHEMA_VERSION)

    def test_the_corrupt_value_is_recorded_for_the_operator(self):
        self.finish()
        observed = self.marker()["observed"]["monthly_credit_cost"]
        self.assertEqual(observed["type"], "int")
        self.assertIn("-20", observed["repr"])

    def test_lora_status_never_stays_training(self):
        """THE P0. Left at 'training', the user can never retrain again."""
        self.finish()
        self.assertIsNotNone(self.lora_status())
        self.assertNotEqual(self.lora_status(), "training")

    def test_failed_run_without_an_adapter_resolves_to_failed(self):
        self.finish(ok=False)
        self.assertEqual(self.lora_status(), "failed")

    def test_failed_run_with_an_intact_prior_adapter_resolves_to_ready(self):
        with mock.patch.object(function_app, "_identity_adapter_exists", lambda _u: True):
            self.finish(ok=False)
        self.assertEqual(self.lora_status(), "ready",
                         "a failed run does not damage a previous adapter")

    def test_successful_run_resolves_to_ready(self):
        self.finish(ok=True, error=None)
        self.assertEqual(self.db.trainings[TID]["status"], "completed")
        self.assertEqual(self.lora_status(), "ready")

    def test_no_refund_and_no_retrain_refund_ledger_row(self):
        self.finish()
        # The parked jobs ARE refunded on their own credit_cost — that is the ordinary sweep
        # and is unrelated to the training's corrupt metadata. What must NOT happen is a
        # RETRAIN refund: no retrain_refund ledger row, and no monthly/one-time bucket
        # movement, because the retrain amount is unknown.
        self.assertEqual([r for r in self.db.ledger if r[2] == "retrain_refund"], [])
        self.assertEqual(self.db.users[UID]["monthly_credits_remaining"], 0,
                         "the monthly bucket must not move on an unknown amount")

    def test_a_negative_charge_can_never_become_a_negative_refund(self):
        """Unguarded, `credits_remaining + (-20 + 15)` would DEBIT the customer and ledger it
        as a credit. Every balance must be >= where it started."""
        before = dict(self.db.users[UID])
        self.finish()
        after = self.db.users[UID]
        for field in ("credits_remaining", "monthly_credits_remaining",
                      "one_time_credits_remaining"):
            self.assertGreaterEqual(after[field], before[field],
                                    "%s went DOWN on a refund path" % field)
        self.assertEqual([r for r in self.db.ledger if r[2] == "retrain_refund"], [])

    def test_duplicate_watcher_calls_are_idempotent(self):
        self.finish()
        first = dict(self.db.trainings[TID])
        status_after_first = self.lora_status()
        ledger_after_first = list(self.db.ledger)
        for _ in range(4):
            self.finish()
        self.assertEqual(self.db.trainings[TID]["status"], first["status"])
        self.assertEqual(self.db.trainings[TID]["error"], first["error"])
        self.assertEqual(self.lora_status(), status_after_first)
        self.assertEqual(self.db.ledger, ledger_after_first)

    def test_the_original_error_is_preserved_after_the_marker(self):
        self.db.trainings[TID]["error"] = "trainer exited 137 (OOM)"
        self.finish(error="watcher timeout")
        stored = self.db.trainings[TID]["error"]
        self.assertTrue(stored.startswith(to.ACCOUNTING_MARKER_PREFIX))
        self.assertEqual(to.original_error_from(stored), "trainer exited 137 (OOM)")
        self.assertIsNotNone(to.parse_marker(stored))

    def test_it_leaves_the_watcher_set_so_there_is_no_loop(self):
        self.finish()
        self.assertIn(self.db.trainings[TID]["status"], ("completed", "failed"),
                      "training_watcher only selects dispatching/training")


class AccountingInvalidParkedJobs(_Base):
    """Parked-job handling is derived from ADAPTER USABILITY, exactly as the normal path --
    the training's corrupt charge metadata says nothing about the customer's other jobs."""

    monthly_charge = -20
    one_time_charge = 15
    seed_user = True

    def test_unusable_adapter_fails_and_refunds_the_parked_jobs(self):
        self.finish(ok=False)
        for name in ("PARK1", "PARK2"):
            self.assertEqual(self.db.jobs[name]["status"], "failed")
        self.assertEqual(len([r for r in self.db.ledger if r[2] == "job_refund"]), 2,
                         "each parked job is refunded on its OWN credit_cost")

    def test_usable_adapter_releases_the_parked_jobs(self):
        released = []
        with mock.patch.object(function_app, "_identity_adapter_exists", lambda _u: True), \
             mock.patch.object(function_app, "outbox_add",
                               lambda cur, q, msg: released.append(msg) or 1), \
             mock.patch.object(function_app, "outbox_try_send_now", lambda *a, **k: True):
            self.finish(ok=False)
        for name in ("PARK1", "PARK2"):
            self.assertEqual(self.db.jobs[name]["status"], "queued")
        self.assertEqual(len(released), 2)

    def test_successful_run_releases_the_parked_jobs(self):
        released = []
        with mock.patch.object(function_app, "outbox_add",
                               lambda cur, q, msg: released.append(msg) or 1), \
             mock.patch.object(function_app, "outbox_try_send_now", lambda *a, **k: True):
            self.finish(ok=True, error=None)
        for name in ("PARK1", "PARK2"):
            self.assertEqual(self.db.jobs[name]["status"], "queued")
        self.assertEqual(len(released), 2)


class MarkerPrefixesAreDisjoint(unittest.TestCase):
    def test_a_missing_user_marker_never_uses_the_accounting_prefix(self):
        for monthly, one_time in ((0, 0), (20, 15), (-5, 0), ("x", 0)):
            with self.subTest(amounts=(monthly, one_time)):
                marker, _ = to.build_orphan_marker(TID, UID, monthly_owed=monthly,
                                                   one_time_owed=one_time)
                self.assertTrue(to.is_orphan_marker(marker))
                self.assertFalse(to.is_accounting_marker(marker))

    def test_an_accounting_marker_never_uses_the_orphan_prefix(self):
        marker, _ = to.build_accounting_invalid_marker(TID, UID, monthly_owed=-1,
                                                       one_time_owed=0)
        self.assertTrue(to.is_accounting_marker(marker))
        self.assertFalse(to.is_orphan_marker(marker))

    def test_both_parse_through_the_shared_reader(self):
        orphan, _ = to.build_orphan_marker(TID, UID, monthly_owed=20, one_time_owed=15)
        acct, _ = to.build_accounting_invalid_marker(TID, UID, monthly_owed=-1,
                                                     one_time_owed=0)
        self.assertEqual(to.parse_marker(orphan)["reason"], to.REASON_PAID)
        self.assertEqual(to.parse_marker(acct)["reason"], to.REASON_INVALID)

    def test_an_accounting_marker_always_reports_unknown_amounts(self):
        """Even if the values LOOK valid, this marker means we do not trust them."""
        _m, payload = to.build_accounting_invalid_marker(TID, UID, monthly_owed=20,
                                                         one_time_owed=15)
        self.assertFalse(payload["amounts_valid"])
        self.assertIsNone(payload["aggregate_owed"])
        self.assertEqual(payload["reason"], to.REASON_INVALID)


class BooleanChargeTerminalization(_Base):
    """True would have read as a charge of 1 under the old int() conversion."""

    monthly_charge = True
    one_time_charge = 0
    seed_user = True

    def test_bool_charge_is_accounting_invalid_and_resolves_the_lifecycle(self):
        self.finish()
        stored = self.db.trainings[TID]["error"]
        payload = to.parse_marker(stored)
        self.assertTrue(to.is_accounting_marker(stored))
        self.assertEqual(payload["reason"], to.REASON_INVALID)
        self.assertEqual(payload["observed"]["monthly_credit_cost"]["type"], "bool")
        self.assertEqual([r for r in self.db.ledger if r[2] == "retrain_refund"], [])
        self.assertEqual((self.db.users.get("__lora__") or {}).get(UID), "failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
