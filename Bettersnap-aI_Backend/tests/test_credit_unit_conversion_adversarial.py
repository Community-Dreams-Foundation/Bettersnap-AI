"""Adversarial coverage for the credits/images unit-conversion work (commit db0df6f).

The shipped tests assert the happy path with both balance columns already in
agreement and with a plan key the production writers never actually store. These
tests attack the boundaries that happy path cannot see:

  * which column monthly activation converts when the two disagree
  * the images_to_credits guard on the activation path
  * a non-divisible add-on balance at the cancellation webhook
  * the one-time -> monthly -> cancel round trip
  * re-activation of an already-monthly account (no double conversion)
  * the review's 3.2 defect: a 50-image add-on after cancellation
  * the credits_per_image fallback in _subscription_status_payload
  * Teams seats, which must be untouched by every one of the above

The Fix1..Fix4 classes near the bottom are the regression cover for the four
defects these tests originally exposed; they fail against the pre-fix code.
"""
import json
import sys
import unittest
from unittest import mock

import function_app
from shared.credit_units import credits_to_images, images_to_credits
from test_dispatch_logic import FakeConn, _HttpRequest


class _WebhookBase(unittest.TestCase):
    def setUp(self):
        sys.modules["shared.auth"].validate_token.return_value = {
            "oid": "user-1", "email": "user@example.com"}
        sys.modules["shared.auth"].get_user_id.return_value = "user-1"
        self._cfg = {}
        self._get_db = mock.patch.object(
            function_app, "get_db", side_effect=lambda: FakeConn(self._cfg))
        self._new_connection = mock.patch.object(
            function_app, "new_connection", side_effect=lambda: FakeConn(self._cfg))
        self._get_db.start()
        self._new_connection.start()

    def tearDown(self):
        self._get_db.stop()
        self._new_connection.stop()

    def _activate_monthly(self, plan="pro", event="evt_activate"):
        function_app._handle_monthly_checkout(
            {"metadata": {"user_id": "user-1", "plan": plan,
                          "checkout_token": "checkout-token"},
             "customer": "cus_123", "subscription": "sub_123"},
            event)

    def _end_subscription(self, event="evt_ended"):
        function_app._handle_subscription_ended(
            {"id": "sub_123", "status": "canceled"}, event)

    def _activation_write(self):
        return next((sql, params) for sql, params in self._cfg["executed"]
                    if "monthly_credits_remaining" in sql.lower()
                    and "stripe_checkout_token" in sql.lower()
                    and "update users set" in sql.lower())

    def _downgrade_write(self):
        return next((sql, params) for sql, params in self._cfg["executed"]
                    if "monthly_credits_remaining = 0" in sql.lower()
                    and "update users set" in sql.lower())

    def _ledger_rows(self):
        return [params for sql, params in self._cfg["executed"]
                if "insert into credit_transactions" in sql.lower()]


class ActivationReadsTheSpendableBucket(_WebhookBase):
    """Activation now prefers one_time_credits_remaining (see Fix1 below).

    get_credits documents credits_remaining as the STALE legacy aggregate and
    refuses to trust it, falling back to the buckets because a paid balance
    showed as 0. Anything that scales that column by credits_per_image amplifies
    drift 5x at the money boundary, so the invariant below stays pinned.
    """

    def test_invariant_the_two_columns_must_agree_before_activation(self):
        """The 5x scale is only safe while every writer keeps the columns equal.

        job_reservation.reserve_job_slot's legacy branch (no source_type)
        debits credits_remaining ALONE, leaving one_time_credits_remaining
        untouched. submit_job always passes source_type today, so the branch is
        unreachable from the API - this test pins that fact, because the day
        anything reaches it, activation converts the wrong number.
        """
        import inspect
        from shared import job_reservation
        src = inspect.getsource(job_reservation.reserve_job_slot)
        legacy_debit = ("UPDATE users SET credits_remaining = credits_remaining - ? "
                        "WHERE user_id = ?")
        self.assertIn(legacy_debit, src,
                      "legacy single-column debit branch moved; recheck activation")
        submit_src = inspect.getsource(function_app.submit_job)
        self.assertIn("source_type=plan.plan_type", submit_src,
                      "submit_job stopped pinning source_type -> legacy debit reachable "
                      "-> monthly activation will convert a stale balance")


class ActivationUsesTheGuardedConversion(_WebhookBase):
    """credit_units exists to fail closed, and activation now routes through it."""

    def test_activation_calls_images_to_credits(self):
        import inspect
        src = inspect.getsource(function_app._handle_monthly_checkout)
        self.assertIn("images_to_credits(old_persistent, monthly_rate)", src)

    def test_the_guard_rejects_a_negative_balance(self):
        with self.assertRaises(ValueError):
            images_to_credits(-3, 5)


class NonDivisibleAddOnIsDeadLettered(_WebhookBase):
    """A remainder is permanent, so it must not be answered with a retry.

    Reachable today via superadmin credit adjustment, which writes an arbitrary
    amount straight into one_time_credits_remaining.
    """

    def test_superadmin_adjustment_can_create_the_remainder(self):
        import inspect
        src = inspect.getsource(function_app.admin_adjust_credits)
        self.assertIn("one_time_credits_remaining = ?", src)
        self.assertNotIn("credits_per_image", src,
                         "adjustment writes raw credits with no divisibility check")


class RoundTripPreservesImageValue(_WebhookBase):
    def test_one_time_thirty_images_survives_monthly_and_cancellation(self):
        self._cfg.update(subscription_type="one_time", credits=30,
                         one_time_credits=30, plan_name="basic",
                         subscription_plan="basic")
        self._activate_monthly(plan="basic")
        _, act = self._activation_write()
        self.assertEqual(act[5], 150)

        self._cfg.update(plan_name="monthly_basic", subscription_plan="basic",
                         monthly_credits=100, one_time_credits=150)
        self._end_subscription()
        _, down = self._downgrade_write()
        self.assertEqual(down[3], 30, "30 images in, 30 images out")
        self.assertEqual(down[4], 30, "both balance columns land on the same number")
        self.assertEqual(down[0], "basic")
        self.assertEqual(down[2], "basic")

    def test_expired_monthly_allowance_is_not_converted_into_images(self):
        self._cfg.update(plan_name="monthly_expert", subscription_plan="expert",
                         monthly_credits=120, one_time_credits=250)
        self._end_subscription()
        _, down = self._downgrade_write()
        self.assertEqual(down[3], 50, "only the 250 add-on credits convert")
        rows = self._ledger_rows()
        amounts = [r[1] for r in rows]
        self.assertIn(-120, amounts, "monthly expiry is ledgered")
        self.assertIn(50 - 250, amounts, "unit conversion delta is ledgered")
        self.assertEqual(
            sum(amounts), -120 - 200,
            "ledger reconciles old aggregate (370) to new balance (50)")


class ReactivationDoesNotDoubleConvert(_WebhookBase):
    def test_already_monthly_account_keeps_its_add_on_credits_unscaled(self):
        self._cfg.update(subscription_type="monthly", credits=999,
                         one_time_credits=250, plan_name="monthly_pro")
        self._activate_monthly()
        _, params = self._activation_write()
        self.assertEqual(params[5], 250, "no second x5 on an already-monthly balance")
        rows = self._ledger_rows()
        conversions = [r[1] for r in rows if r[2] == "plan_unit_conversion"]
        self.assertEqual(conversions, [],
                         "credit_ledger.record skips a zero delta, so re-activation "
                         "leaves no conversion row - correct, and worth pinning")


class AddOnSurvivesCancellationAtItsAdvertisedSize(_WebhookBase):
    """Review section 3.2: buy a 50-image add-on, cancel, still see 50."""

    def test_fifty_image_add_on_reads_as_fifty_after_cancellation(self):
        self._cfg.update(subscription_type="monthly", plan_name="monthly_pro",
                         subscription_plan="pro")
        function_app._handle_topup(
            {"metadata": {"user_id": "user-1", "plan": "pro"}}, "evt_addon")
        grant_sql, grant = next(
            (sql, params) for sql, params in self._cfg["executed"]
            if "one_time_credits_remaining = one_time_credits_remaining + ?" in sql.lower()
            and "update users set" in sql.lower())
        self.assertEqual(grant[0], 250, "50 images granted as 250 credits")
        self.assertEqual(grant[2], "pro",
                         "one_time_plan stores the ONE-TIME key, not monthly_pro")

        self._cfg["executed"] = []
        self._cfg.update(monthly_credits=0, one_time_credits=250)
        self._end_subscription()
        _, down = self._downgrade_write()
        self.assertEqual(down[3], 50, "the advertised 50 images, not 250")
        self.assertEqual(down[2], "pro", "plan_name is no longer a monthly key")


class StatusPayloadUnits(_WebhookBase):
    def _status(self, row):
        self._cfg["sub_status_row"] = row
        cur = FakeConn(self._cfg).cursor()
        return function_app._subscription_status_payload(cur, "user-1")

    def test_monthly_payload_reports_images_at_the_monthly_rate(self):
        # (subscription_plan, subscription_type, credits, limit, renewed,
        #  failed, cancel_at, one_time, monthly, plan_name)
        body = self._status(("pro", "monthly", 120, 200, None, None, None,
                             250, 120, "monthly_pro"))
        self.assertEqual(body["credits_per_image"], 5)
        self.assertEqual(body["balance_unit"], "credits")
        self.assertEqual(body["images_remaining"], 74)
        self.assertEqual(body["add_on_images_remaining"], 50)

    def test_short_legacy_row_still_decodes_at_the_monthly_rate(self):
        """A read replica that has not projected plan_name falls back to
        subscription_plan, which for a monthly user is the BARE tier ('pro').
        get_plan('pro') is the ONE-TIME pack, so the rate must be recovered."""
        legacy_row = ("pro", "monthly", 120, 200, None, None, None, 250, 120)
        body = self._status(legacy_row)
        self.assertEqual(body["credits_per_image"], 5)
        self.assertEqual(body["images_remaining"], 74)

    def test_non_monthly_account_zeroes_the_legacy_add_on_field(self):
        body = self._status(("basic", "one_time", 30, None, None, None, None,
                             30, 0, "basic"))
        self.assertEqual(body["images_remaining"], 30)
        self.assertEqual(body["balance_unit"], "images")
        self.assertEqual(
            body["add_on_credits_remaining"], 0,
            "CONTRACT CHANGE: used to mirror one_time_credits_remaining (30)")
        self.assertEqual(body["one_time_credits_remaining"], 30)

    def test_monthly_images_ignore_the_five_image_session_minimum(self):
        """monthly plans set min_session_images=5, so a balance of 5 credits
        reports '1 image available' that can never actually be submitted."""
        body = self._status(("pro", "monthly", 5, 200, None, None, None,
                             0, 5, "monthly_pro"))
        self.assertEqual(body["images_remaining"], 1)
        from shared.plans import get_plan
        self.assertEqual(get_plan("monthly_pro").min_session_images, 5)


if __name__ == "__main__":
    unittest.main()


# ─────────────────────────────────────────────────────────────────────────────
# Regression cover for fixes 1-4. These FAIL before the fix and pass after.
# ─────────────────────────────────────────────────────────────────────────────

class Fix1ActivationConvertsTheSpendableBucket(_WebhookBase):
    def test_drifted_legacy_column_no_longer_inflates_the_grant(self):
        self._cfg.update(subscription_type="one_time", credits=100,
                         one_time_credits=20, plan_name="basic")
        self._activate_monthly()
        _, params = self._activation_write()
        self.assertEqual(params[5], 100, "20 spendable images x 5, not 100 x 5")

    def test_pre_migration_row_still_falls_back_to_the_legacy_column(self):
        """get_credits' rule: trust the bucket, fall back only when it is empty."""
        self._cfg.update(subscription_type="one_time", credits=30,
                         one_time_credits=0, plan_name="basic")
        self._activate_monthly()
        _, params = self._activation_write()
        self.assertEqual(params[5], 150, "30 legacy images x 5")

    def test_negative_balance_is_rejected_instead_of_scaled(self):
        self._cfg.update(subscription_type="one_time", credits=-3,
                         one_time_credits=-3, plan_name="basic")
        with self.assertRaises(function_app.RetryableStripeWebhookError):
            self._activate_monthly()


class Fix2MonthlyRateNeverFallsBackToAOneTimePlan(_WebhookBase):
    def _status(self, row):
        self._cfg["sub_status_row"] = row
        cur = FakeConn(self._cfg).cursor()
        return function_app._subscription_status_payload(cur, "user-1")

    def test_null_plan_name_recovers_the_monthly_rate_from_the_tier(self):
        body = self._status(("pro", "monthly", 120, 200, None, None, None,
                             250, 120, None))
        self.assertEqual(body["credits_per_image"], 5)
        self.assertEqual(body["images_remaining"], 74)

    def test_bare_tier_plan_name_recovers_the_monthly_rate(self):
        body = self._status(("pro", "monthly", 120, 200, None, None, None,
                             250, 120, "pro"))
        self.assertEqual(body["credits_per_image"], 5)
        self.assertEqual(body["images_remaining"], 74)

    def test_correct_monthly_plan_name_is_untouched(self):
        body = self._status(("pro", "monthly", 120, 200, None, None, None,
                             250, 120, "monthly_pro"))
        self.assertEqual(body["credits_per_image"], 5)
        self.assertEqual(body["images_remaining"], 74)

    def test_one_time_account_keeps_a_one_to_one_rate(self):
        body = self._status(("basic", "one_time", 30, None, None, None, None,
                             30, 0, "basic"))
        self.assertEqual(body["credits_per_image"], 1)
        self.assertEqual(body["images_remaining"], 30)


class Fix3RemainderIsDeadLetteredNotRetried(_WebhookBase):
    def test_downgrade_does_not_ask_stripe_to_retry_forever(self):
        self._cfg.update(plan_name="monthly_pro", monthly_credits=0,
                         one_time_credits=252)
        # Must NOT raise: a remainder is permanent, and repeated non-2xx responses
        # can get the whole webhook endpoint disabled by Stripe.
        self._end_subscription()

    def test_the_account_is_left_untouched_for_support(self):
        self._cfg.update(plan_name="monthly_pro", monthly_credits=0,
                         one_time_credits=252)
        self._end_subscription()
        self.assertFalse(
            any("monthly_credits_remaining = 0" in sql.lower()
                and "update users set" in sql.lower()
                for sql, _ in self._cfg["executed"]),
            "no partial downgrade; the row is preserved exactly as-is")

    def test_an_alert_is_raised(self):
        self._cfg.update(plan_name="monthly_pro", monthly_credits=0,
                         one_time_credits=252)
        with mock.patch.object(function_app, "_write_event") as ev:
            self._end_subscription()
        ev.assert_called_once()
        self.assertEqual(ev.call_args.args[1], "billing.unit_conversion_blocked")


class Fix4TrialAccountsKeepTheirTrialKey(unittest.TestCase):
    def test_trial_is_preserved_instead_of_becoming_a_paid_basic_pack(self):
        from shared.credit_units import one_time_key_for_monthly
        self.assertEqual(one_time_key_for_monthly("trial"), "trial")

    def test_paid_tiers_and_unknown_keys_are_unchanged(self):
        from shared.credit_units import one_time_key_for_monthly
        self.assertEqual(one_time_key_for_monthly("monthly_pro"), "pro")
        self.assertEqual(one_time_key_for_monthly("expert"), "expert")
        self.assertEqual(one_time_key_for_monthly("teams_basic"), "basic")
        self.assertEqual(one_time_key_for_monthly(None), "basic")

    def test_a_cancelled_trial_upgrade_lands_back_on_trial_pricing(self):
        from shared.credit_units import one_time_key_for_monthly
        from shared.plans import get_plan
        key = one_time_key_for_monthly("trial")
        self.assertEqual(get_plan(key).credits_per_image, 1)
        self.assertEqual(get_plan(key).price_usd, 0,
                         "a cancelled trial must not read as a paying customer")


class TeamsIsUnaffectedByTheUnitConversion(_WebhookBase):
    """Seats are a separate ledger. None of the four fixes may reach them.

    Teams balances live in organization_members, not in users.credits_remaining /
    one_time_credits_remaining, and a seat is always 1 unit per image. The status
    payload returns early for an active seat, before any rate resolution runs.
    """

    def _seat_status(self, seat_credits=30):
        seat = {"credits_remaining": seat_credits, "organization_id": "org-1",
                "credits_granted": 30, "role": "member", "organization_name": "Acme"}
        self._cfg["sub_status_row"] = ("pro", "monthly", 120, 200, None, None,
                                       None, 250, 120, None)
        cur = FakeConn(self._cfg).cursor()
        with mock.patch.object(function_app, "_active_org_seat", return_value=seat):
            return function_app._subscription_status_payload(cur, "user-1")

    def test_seat_payload_returns_before_any_rate_resolution(self):
        body = self._seat_status()
        # A poisoned personal row (monthly, NULL plan_name) would trip fix 2 if the
        # seat branch did not return first. It must not even be consulted.
        self.assertEqual(body["credits_per_image"], 1)
        self.assertEqual(body["balance_unit"], "images")
        self.assertEqual(body["images_remaining"], 30)
        self.assertEqual(body["add_on_images_remaining"], 0)
        self.assertEqual(body["subscription_plan"], "teams_basic")

    def test_partially_spent_seat_reports_images_one_for_one(self):
        body = self._seat_status(seat_credits=12)
        self.assertEqual(body["images_remaining"], 12)
        self.assertEqual(body["credits_remaining"], 12)

    def test_seat_plan_is_one_credit_per_image(self):
        from shared.plans import get_plan
        seat = get_plan("teams_basic")
        self.assertEqual(seat.credits_per_image, 1)
        self.assertEqual(seat.plan_type, "one_time")

    def test_fix4_did_not_change_how_a_team_key_downgrades(self):
        from shared.credit_units import one_time_key_for_monthly
        self.assertEqual(one_time_key_for_monthly("teams_basic"), "basic")

    def test_seat_spend_never_touches_the_personal_balance_columns(self):
        """reserve_job_slot debits organization_members for an org job, so the
        columns fix 1 reads are untouched by any amount of Teams usage."""
        import inspect
        from shared import job_reservation
        src = inspect.getsource(job_reservation.reserve_job_slot)
        org_branch = src.split("if org_id:")[-1].split("elif source_type")[0]
        self.assertIn("UPDATE organization_members", org_branch)
        self.assertNotIn("UPDATE users", org_branch)

    def test_a_team_member_subscribing_personally_converts_only_their_own_row(self):
        # Personal columns still hold the registration grant; the seat is elsewhere.
        self._cfg.update(subscription_type="one_time", credits=4,
                         one_time_credits=4, plan_name="trial")
        self._activate_monthly()
        _, params = self._activation_write()
        self.assertEqual(params[5], 20, "4 personal trial images x 5, seat untouched")


class TopUpIsNeverPricedAtTheOneTimeRate(_WebhookBase):
    """Live bug: a monthly subscriber's add-on granted 1x instead of 5x.

    _handle_topup priced the pack with get_plan(plan_name), which maps a NULL,
    unknown or BARE-TIER key to the trial plan at 1 credit/image. A monthly row
    whose plan_name is 'pro' rather than 'monthly_pro' therefore granted 50
    credits for a 50-image pack instead of 250 - the customer paid for 50 images
    and received 10.
    """

    def _topup(self, plan_name, subscription_plan="pro", pack="pro"):
        self._cfg.update(subscription_type="monthly", plan_name=plan_name,
                         subscription_plan=subscription_plan)
        self._cfg["executed"] = []  # each call is inspected on its own
        function_app._handle_topup(
            {"metadata": {"user_id": "user-1", "plan": pack}},
            f"evt_top_{plan_name}_{pack}")
        _, params = next(
            (sql, p) for sql, p in self._cfg["executed"]
            if "one_time_credits_remaining = one_time_credits_remaining + ?" in sql.lower()
            and "update users set" in sql.lower())
        return params[0]

    def test_correct_monthly_plan_name_grants_five_times(self):
        self.assertEqual(self._topup("monthly_pro"), 250)

    def test_bare_tier_plan_name_still_grants_five_times(self):
        self.assertEqual(self._topup("pro"), 250,
                         "recovered from subscription_plan, not priced at 1x")

    def test_null_plan_name_still_grants_five_times(self):
        self.assertEqual(self._topup(None), 250)

    def test_trial_plan_name_still_grants_five_times(self):
        self.assertEqual(self._topup("trial"), 250)

    def test_every_pack_size_is_scaled(self):
        self.assertEqual(self._topup("pro", pack="basic"), 150)   # 30 images
        self.assertEqual(self._topup("pro", pack="expert"), 350)  # 70 images

    def test_an_unresolvable_rate_grants_nothing(self):
        self._cfg.update(subscription_type="monthly", plan_name=None,
                         subscription_plan=None)
        with self.assertRaises(function_app.RetryableStripeWebhookError):
            function_app._handle_topup(
                {"metadata": {"user_id": "user-1", "plan": "pro"}}, "evt_bad")
        self.assertFalse(
            any("one_time_credits_remaining = one_time_credits_remaining + ?" in sql.lower()
                for sql, _ in self._cfg["executed"]),
            "no grant at a guessed rate")


class DowngradeIsNeverPricedAtTheOneTimeRate(_WebhookBase):
    """The mirror of the same defect: converting add-ons at 1 credit/image on
    cancellation would hand the customer FIVE TIMES the images they own."""

    def test_bare_tier_plan_name_converts_at_the_monthly_rate(self):
        self._cfg.update(plan_name="pro", subscription_plan="pro",
                         monthly_credits=0, one_time_credits=250)
        self._end_subscription()
        _, down = self._downgrade_write()
        self.assertEqual(down[3], 50, "250 credits are 50 images, not 250")

    def test_correct_plan_name_is_unchanged(self):
        self._cfg.update(plan_name="monthly_pro", subscription_plan="pro",
                         monthly_credits=0, one_time_credits=250)
        self._end_subscription()
        _, down = self._downgrade_write()
        self.assertEqual(down[3], 50)


class ResolveMonthlyPlanContract(unittest.TestCase):
    def test_monthly_keys_pass_through(self):
        from shared.plans import resolve_monthly_plan
        plan, recovered = resolve_monthly_plan("monthly_expert", "expert")
        self.assertEqual(plan.key, "monthly_expert")
        self.assertFalse(recovered)

    def test_bare_tier_and_null_are_recovered_and_flagged(self):
        from shared.plans import resolve_monthly_plan
        for stored in ("pro", None, "trial", "free", "basic"):
            with self.subTest(plan_name=stored):
                plan, recovered = resolve_monthly_plan(stored, "pro")
                self.assertEqual(plan.key, "monthly_pro")
                self.assertTrue(recovered)

    def test_recovery_can_fall_back_to_the_plan_name_itself(self):
        from shared.plans import resolve_monthly_plan
        plan, recovered = resolve_monthly_plan("expert", None)
        self.assertEqual(plan.key, "monthly_expert")
        self.assertTrue(recovered)

    def test_unresolvable_returns_none_so_callers_fail_closed(self):
        from shared.plans import resolve_monthly_plan
        plan, recovered = resolve_monthly_plan(None, None)
        self.assertIsNone(plan)
        self.assertTrue(recovered)

    def test_it_never_returns_a_one_time_rate(self):
        from shared.plans import resolve_monthly_plan, PLANS
        for stored in list(PLANS) + [None, "", "garbage"]:
            for sub in list(PLANS) + [None]:
                plan, _ = resolve_monthly_plan(stored, sub)
                if plan is not None:
                    self.assertEqual(plan.credits_per_image, 5,
                                     f"{stored!r}/{sub!r} resolved to a 1x rate")


class TheFiveTimesRateAppliesOnlyToMonthlyAddOns(_WebhookBase):
    """Scope guard: the 5x rate belongs to ONE flow only.

    Add-on credits bought on an ALREADY-ACTIVE monthly plan are the only purchase
    priced at credits_per_image = 5. A standalone one-time pack, a first-time
    monthly signup's own allowance, and a Teams seat must all stay 1:1.
    """

    def _one_time_purchase(self, plan="pro"):
        self._cfg["executed"] = []
        function_app._handle_onetime_payment(
            {"metadata": {"user_id": "user-1", "plan": plan}}, f"evt_ot_{plan}")
        _, params = next(
            (sql, p) for sql, p in self._cfg["executed"]
            if "subscription_type = 'one_time'" in sql.lower()
            and "update users set" in sql.lower())
        return params[2]

    def test_a_standalone_one_time_pack_is_never_scaled(self):
        self.assertEqual(self._one_time_purchase("pro"), 50,
                         "a 50-image pack is 50 credits, NOT 250")
        self.assertEqual(self._one_time_purchase("basic"), 30)
        self.assertEqual(self._one_time_purchase("expert"), 70)

    def test_topup_refuses_a_non_monthly_account_outright(self):
        """The same Stripe pack bought by a non-subscriber must not reach the
        5x path at all — the handler's WHERE clause gates on subscription_type."""
        self._cfg.update(subscription_type="one_time", plan_name="basic")
        with self.assertRaises(function_app.RetryableStripeWebhookError):
            function_app._handle_topup(
                {"metadata": {"user_id": "user-1", "plan": "pro"}}, "evt_nm")
        self.assertFalse(
            any("one_time_credits_remaining = one_time_credits_remaining + ?" in sql.lower()
                for sql, _ in self._cfg["executed"]),
            "nothing granted to a non-monthly account")

    def test_the_monthly_signup_allowance_is_not_the_add_on_path(self):
        """A new monthly subscription's own allowance comes from MONTHLY_PLANS
        and is already expressed in credits — it must not be scaled again."""
        from shared.stripe_client import MONTHLY_PLANS
        self.assertEqual(MONTHLY_PLANS["pro"]["credits"], 200,
                         "40 images x 5, computed once in the catalog")
        self._cfg.update(subscription_type="one_time", credits=0,
                         one_time_credits=0, plan_name="trial")
        self._activate_monthly("pro")
        _, params = self._activation_write()
        self.assertEqual(params[4], 200, "the allowance is used as-is")

    def test_a_teams_seat_is_one_credit_per_image(self):
        from shared.plans import get_plan
        self.assertEqual(get_plan("teams_basic").credits_per_image, 1)

    def test_resolve_monthly_plan_is_not_used_on_any_one_time_path(self):
        import inspect
        src = inspect.getsource(function_app._handle_onetime_payment)
        self.assertNotIn("resolve_monthly_plan", src,
                         "a one-time pack must never be priced at the monthly rate")


class LegacyUnscaledCarryOverBlocksTheDowngrade(_WebhookBase):
    """Live bug: add-on stuck at 154 credits after the monthly plan expired.

    154 = 150 (a correctly-scaled 30-image add-on) + 4. The 4 is the
    REGISTRATION_CREDITS trial grant, carried into the add-on bucket UNSCALED by
    the PRE-conversion activation, which copied credits_remaining verbatim:

        one_time_credits_remaining =
            CASE WHEN subscription_type = 'monthly'
                 THEN one_time_credits_remaining ELSE credits_remaining END

    so the bucket now mixes 1x legacy units with 5x monthly units. 154 % 5 = 4,
    credits_to_images refuses it, and the downgrade is abandoned with the row
    untouched — which is why the balance stays at 154 rather than becoming 30.

    CONFIRMED ORIGIN (reported from a live account): the remainder is the free
    trial allowance that did not convert when the account switched to monthly.
    Those 4 credits are therefore 4 IMAGES, not 4 monthly credits: the account
    owns 34 images (30 add-on + 4 trial), and the correct repair rescales the
    remainder to 20 credits rather than discarding it.

    Failing closed is still correct — the code cannot distinguish 4 legacy
    images from 4 stray monthly credits, and the two answers differ by 4 images
    of customer value — but the repair is now known.
    """

    def _expire_with(self, add_on, monthly=0):
        self._cfg.update(plan_name="monthly_basic", subscription_plan="basic",
                         monthly_credits=monthly, one_time_credits=add_on)
        self._cfg["executed"] = []
        self._end_subscription(event=f"evt_end_{add_on}_{monthly}")

    def test_one_hundred_and_fifty_four_is_refused(self):
        self._expire_with(154)
        self.assertFalse(
            any("monthly_credits_remaining = 0" in sql.lower()
                and "update users set" in sql.lower()
                for sql, _ in self._cfg["executed"]),
            "the downgrade must not run on a mixed-unit balance")

    def test_the_balance_is_left_exactly_as_it_was(self):
        self._expire_with(154)
        self.assertFalse(
            any("update users set" in sql.lower()
                and "credits_remaining" in sql.lower()
                for sql, _ in self._cfg["executed"]),
            "no partial write — support must see the original 154")

    def test_an_operator_alert_is_raised_with_the_numbers(self):
        with mock.patch.object(function_app, "_write_event") as ev:
            self._expire_with(154)
        ev.assert_called_once()
        self.assertEqual(ev.call_args.args[1], "billing.unit_conversion_blocked")
        self.assertEqual(ev.call_args.kwargs["detail"]["add_on_credits"], 154)
        self.assertEqual(ev.call_args.kwargs["detail"]["credits_per_image"], 5)

    def test_the_event_claim_is_rolled_back_so_a_resend_can_succeed(self):
        """Recovery path: repair the balance, then resend the event from Stripe.

        The handler rolls its processed_stripe_events claim back before
        returning, so the same event id is NOT recorded as handled and a
        redelivery is free to complete once the remainder is gone.
        """
        import inspect
        src = inspect.getsource(function_app._handle_subscription_ended)
        blocked = src.split("except ValueError as exc:")[1].split("return")[0]
        self.assertIn("conn.rollback()", blocked,
                      "claim must be released so a resend is not swallowed")

    def test_the_correct_repair_rescales_the_trial_images(self):
        """+16 credits (4 trial images x 5, less the 4 already there) -> 170,
        which converts cleanly to the 34 images the account actually owns."""
        self._expire_with(170)
        _, down = self._downgrade_write()
        self.assertEqual(down[3], 34, "30 add-on images + 4 unconverted trial images")
        self.assertEqual(down[4], 34)

    def test_discarding_the_remainder_would_silently_cost_four_images(self):
        """The tempting repair (trim to 150) balances the books by taking paid-for
        value off the customer. Pinned so nobody 'fixes' it this way."""
        self._expire_with(150)
        _, down = self._downgrade_write()
        self.assertEqual(down[3], 30)

    def test_the_two_repairs_and_what_each_costs(self):
        remainder = 154 % 5
        self.assertEqual(remainder, 4)
        # Repair A - drop the remainder: the customer loses 4 legacy images.
        self.assertEqual((154 - remainder) // 5, 30)
        # Repair B - rescale the remainder as legacy IMAGES (4 x 5 = 20 credits),
        # i.e. adjust by +16, keeping every image the customer actually owns.
        rescaled = (154 - remainder) + remainder * 5
        self.assertEqual(rescaled, 170)
        self.assertEqual(rescaled - 154, 16, "the admin adjustment amount")
        self.assertEqual(rescaled // 5, 34)
