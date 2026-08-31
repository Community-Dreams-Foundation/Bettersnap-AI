"""Exhaustive tests for the Teams graduated pricing contract (`teams_basic_v1`).

`shared.teams_pricing` is the ONE place Teams money is computed — the quote endpoint,
the Stripe line items and the webhook's amount validation all derive from it. A silent
arithmetic change here would either overcharge customers or let an underpaid session
fulfil, so this module pins the contract numerically rather than by inspection:

  - the three worked examples from the approved contract, to the cent;
  - EVERY legal seat count 10..49 recomputed independently of the implementation;
  - the four boundaries that decide purchasability (9/10 and 49/50);
  - monotonicity, which is the property that makes graduated pricing safe to sell —
    a banded-FLAT table with these numbers inverts (24x$32=$768 > 25x$29=$725);
  - type rejection, including bool, which `isinstance(x, int)` would otherwise accept.

Run:  python -m unittest tests.test_teams_pricing   (from the backend dir)
"""
import unittest

from shared.teams_pricing import (
    BANDS,
    CONTACT_SALES_MIN,
    CREDITS_PER_SEAT,
    CURRENCY,
    MAX_SEATS,
    MIN_SEATS,
    PLAN_ID,
    PRICING_VERSION,
    BelowMinimumSeats,
    ContactSalesRequired,
    InvalidSeatCount,
    TeamsPricingError,
    contact_sales_payload,
    normalize_seats,
    price_cents,
    quote,
)


def reference_total_cents(seats):
    """Independent re-derivation of the v2 contract, written from the prose, not the code.

    The prose: "every seat pays the same rate, and the rate improves as the team grows —
    $32.00 per seat at 10 seats, $29.00 per seat at 20." Two points on a straight line fix
    it completely: the rate drops (3200 - 2900) / (20 - 10) = 30 cents per extra seat.

    Derived here from the ANCHORS rather than from V2_BASE_UNIT_CENTS / V2_STEP_CENTS, so
    it cannot agree with the implementation by sharing its constants.
    """
    rate_at_10, rate_at_20 = 3200, 2900
    step = (rate_at_10 - rate_at_20) // 10
    unit = rate_at_10 - step * (seats - 10)
    return seats * unit


class TestContractExamples(unittest.TestCase):
    """The prices stated for v2: one rate for the whole order, improving with size."""

    def test_ten_seats_is_thirty_two_dollars_per_seat(self):
        q = quote(10)
        self.assertEqual(q.total_cents, 32_000)
        self.assertEqual(q.effective_price_per_seat_cents, 3200)

    def test_twenty_seats_is_twenty_nine_dollars_per_seat(self):
        q = quote(20)
        self.assertEqual(q.total_cents, 58_000)
        self.assertEqual(q.effective_price_per_seat_cents, 2900)

    def test_every_seat_pays_the_same_rate(self):
        """The v1 -> v2 change in one assertion. Under v1 a 10-seat order paid 9 seats at
        $35 and one at $32 ($347); under v2 the discount applies to the WHOLE order."""
        for seats in (10, 20, 33, 49):
            with self.subTest(seats=seats):
                q = quote(seats)
                self.assertEqual(q.total_cents % seats, 0,
                                 "total must divide evenly: one rate for every seat")
                self.assertEqual(q.total_cents, seats * q.effective_price_per_seat_cents)

    def test_a_bigger_team_always_gets_a_better_rate(self):
        rates = [quote(n).effective_price_per_seat_cents
                 for n in range(MIN_SEATS, MAX_SEATS + 1)]
        for smaller, larger in zip(rates, rates[1:]):
            self.assertLess(larger, smaller, "the rate must improve as the team grows")

class TestAgainstIndependentReference(unittest.TestCase):
    def test_every_legal_seat_count_matches_reference(self):
        for seats in range(MIN_SEATS, MAX_SEATS + 1):
            with self.subTest(seats=seats):
                self.assertEqual(quote(seats).total_cents, reference_total_cents(seats))

    def test_price_cents_agrees_with_quote(self):
        for seats in range(MIN_SEATS, MAX_SEATS + 1):
            with self.subTest(seats=seats):
                self.assertEqual(price_cents(seats), quote(seats).total_cents)


class TestBoundaries(unittest.TestCase):
    """9/10 and 49/50 are the two boundaries that decide whether money can change hands."""

    def test_nine_seats_is_below_minimum(self):
        with self.assertRaises(BelowMinimumSeats) as ctx:
            quote(9)
        self.assertEqual(ctx.exception.code, "BELOW_MINIMUM_SEATS")
        self.assertEqual(ctx.exception.minimum, MIN_SEATS)

    def test_ten_seats_is_purchasable(self):
        self.assertEqual(quote(10).seats, 10)

    def test_forty_nine_seats_is_purchasable(self):
        self.assertEqual(quote(49).seats, 49)

    def test_fifty_seats_requires_contact_sales(self):
        with self.assertRaises(ContactSalesRequired) as ctx:
            quote(50)
        self.assertEqual(ctx.exception.code, "CONTACT_SALES_REQUIRED")
        self.assertEqual(ctx.exception.threshold, CONTACT_SALES_MIN)

    def test_every_count_below_minimum_is_rejected(self):
        for seats in range(1, MIN_SEATS):
            with self.subTest(seats=seats):
                with self.assertRaises(BelowMinimumSeats):
                    quote(seats)

    def test_large_counts_are_rejected_not_priced(self):
        for seats in (50, 51, 100, 10_000):
            with self.subTest(seats=seats):
                with self.assertRaises(ContactSalesRequired):
                    quote(seats)

    def test_contact_sales_is_distinguishable_from_a_bad_request(self):
        """The endpoint answers 200-with-payload vs 400 off the EXCEPTION TYPE."""
        self.assertNotIsInstance(ContactSalesRequired(50), BelowMinimumSeats)
        self.assertNotIsInstance(ContactSalesRequired(50), InvalidSeatCount)
        self.assertIsInstance(ContactSalesRequired(50), TeamsPricingError)


class TestMonotonicity(unittest.TestCase):
    """Adding a seat must never reduce the bill. This is what banded-flat gets wrong."""

    def test_total_strictly_increases(self):
        previous = -1
        for seats in range(MIN_SEATS, MAX_SEATS + 1):
            total = quote(seats).total_cents
            with self.subTest(seats=seats):
                self.assertGreater(total, previous)
            previous = total

    def test_marginal_seat_never_costs_more_than_the_previous_one(self):
        """Marginal price is non-increasing — the discount only ever improves."""
        deltas = [
            quote(n).total_cents - quote(n - 1).total_cents
            for n in range(MIN_SEATS + 1, MAX_SEATS + 1)
        ]
        for i in range(1, len(deltas)):
            with self.subTest(seat=MIN_SEATS + 1 + i):
                self.assertLessEqual(deltas[i], deltas[i - 1])

    def test_crossing_into_a_cheaper_band_still_costs_more(self):
        """The exact inversion a flat table would produce."""
        self.assertGreater(quote(25).total_cents, quote(24).total_cents)

    def test_effective_per_seat_price_decreases_with_scale(self):
        self.assertGreater(
            quote(10).effective_price_per_seat_cents,
            quote(49).effective_price_per_seat_cents,
        )


class TestQuoteShape(unittest.TestCase):
    def test_carries_plan_identity_and_version(self):
        q = quote(10)
        self.assertEqual(q.plan_id, PLAN_ID)
        self.assertEqual(q.pricing_version, PRICING_VERSION)
        self.assertEqual(q.currency, CURRENCY)

    def test_credits_are_thirty_per_seat(self):
        self.assertEqual(CREDITS_PER_SEAT, 30)
        for seats in (10, 24, 25, 49):
            with self.subTest(seats=seats):
                q = quote(seats)
                self.assertEqual(q.credits_per_seat, 30)
                self.assertEqual(q.total_credits, seats * 30)

    def test_every_amount_is_an_integer_number_of_cents(self):
        for seats in range(MIN_SEATS, MAX_SEATS + 1):
            q = quote(seats)
            with self.subTest(seats=seats):
                self.assertIsInstance(q.total_cents, int)
                self.assertNotIsInstance(q.total_cents, bool)
                self.assertIsInstance(q.effective_price_per_seat_cents, int)
                for band in q.breakdown:
                    self.assertIsInstance(band.subtotal_cents, int)

    def test_breakdown_subtotals_sum_to_the_total(self):
        for seats in range(MIN_SEATS, MAX_SEATS + 1):
            q = quote(seats)
            with self.subTest(seats=seats):
                self.assertEqual(sum(b.subtotal_cents for b in q.breakdown), q.total_cents)

    def test_breakdown_seats_sum_to_the_order(self):
        for seats in range(MIN_SEATS, MAX_SEATS + 1):
            q = quote(seats)
            with self.subTest(seats=seats):
                self.assertEqual(sum(b.seats for b in q.breakdown), seats)

    def test_breakdown_is_one_band_covering_the_whole_order(self):
        """v2 charges one rate, so the breakdown is a single band spanning every seat.
        The shape is kept from v1 deliberately: one snapshot format, one validator and one
        UI render both versions."""
        for seats in (10, 25, 49):
            with self.subTest(seats=seats):
                bands = quote(seats).breakdown
                self.assertEqual(len(bands), 1)
                b = bands[0]
                self.assertEqual((b.lower, b.upper, b.seats), (1, seats, seats))
                self.assertEqual(b.subtotal_cents, seats * b.unit_cents)

    def test_quote_is_immutable(self):
        """Frozen so a caller cannot edit the price between quoting and charging."""
        q = quote(10)
        with self.assertRaises(Exception):
            q.total_cents = 1  # type: ignore[misc]

    def test_to_dict_is_json_serializable_and_complete(self):
        import json

        payload = quote(25).to_dict()
        json.dumps(payload)  # must not raise
        for key in (
            "plan_id", "pricing_version", "currency", "seats", "credits_per_seat",
            "total_credits", "total_cents", "effective_price_per_seat_cents", "breakdown",
        ):
            self.assertIn(key, payload)
        self.assertEqual(len(payload["breakdown"]), 1)
        # 25 seats at 3200 - 30*(25-10) = 2750 each.
        self.assertEqual(payload["total_cents"], 25 * 2750)


class TestSeatValidation(unittest.TestCase):
    def test_rejects_booleans(self):
        """isinstance(True, int) is True — without an explicit guard this would price."""
        for value in (True, False):
            with self.subTest(value=value):
                with self.assertRaises(InvalidSeatCount):
                    quote(value)

    def test_rejects_non_numeric_types(self):
        for value in (None, [], {}, object(), 3 + 4j):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(InvalidSeatCount):
                    quote(value)

    def test_rejects_fractional_counts(self):
        for value in (10.5, "10.5", -0.5):
            with self.subTest(value=value):
                with self.assertRaises(InvalidSeatCount):
                    quote(value)

    def test_rejects_zero_and_negative(self):
        for value in (0, -1, -50):
            with self.subTest(value=value):
                with self.assertRaises(InvalidSeatCount):
                    quote(value)

    def test_rejects_unparseable_strings(self):
        for value in ("", "ten", "1e3x", "10 seats", "0x0a"):
            with self.subTest(value=value):
                with self.assertRaises(InvalidSeatCount):
                    quote(value)

    def test_accepts_exactly_integral_alternatives(self):
        """A JSON body may deliver 10, 10.0 or "10"; all three mean ten people."""
        expected = quote(10).total_cents
        for value in (10, 10.0, "10", " 10 "):
            with self.subTest(value=value):
                self.assertEqual(quote(value).total_cents, expected)

    def test_normalize_seats_returns_a_plain_int(self):
        result = normalize_seats("25")
        self.assertIsInstance(result, int)
        self.assertNotIsInstance(result, bool)
        self.assertEqual(result, 25)

    def test_validation_precedes_the_minimum_check(self):
        """A bool must read as INVALID, never as 'below minimum' — different HTTP codes."""
        with self.assertRaises(InvalidSeatCount):
            quote(True)


class TestBandTable(unittest.TestCase):
    def test_bands_are_contiguous_and_cover_the_legal_range(self):
        self.assertEqual(BANDS[0].lower, 1)
        self.assertEqual(BANDS[-1].upper, MAX_SEATS)
        for earlier, later in zip(BANDS, BANDS[1:]):
            self.assertEqual(earlier.upper + 1, later.lower)

    def test_unit_prices_match_the_contract(self):
        self.assertEqual([b.unit_cents for b in BANDS], [3500, 3200, 2900])

    def test_unit_prices_are_non_increasing(self):
        for earlier, later in zip(BANDS, BANDS[1:]):
            self.assertLessEqual(later.unit_cents, earlier.unit_cents)

    def test_max_seats_is_one_below_contact_sales(self):
        self.assertEqual(MAX_SEATS, CONTACT_SALES_MIN - 1)


class TestContactSalesPayload(unittest.TestCase):
    def test_carries_no_price(self):
        """50+ is priced by a human. Leaking a computed total would undercut sales."""
        payload = contact_sales_payload(75)
        for forbidden in ("total_cents", "unit_price_cents", "breakdown", "amount"):
            self.assertNotIn(forbidden, payload)

    def test_is_machine_readable_and_serializable(self):
        import json

        payload = contact_sales_payload(60)
        json.dumps(payload)
        self.assertTrue(payload["contact_sales"])
        self.assertEqual(payload["code"], "CONTACT_SALES_REQUIRED")
        self.assertEqual(payload["seats"], 60)
        self.assertEqual(payload["threshold"], CONTACT_SALES_MIN)
        self.assertEqual(payload["pricing_version"], PRICING_VERSION)


class TestPurityOfTheModule(unittest.TestCase):
    """Phase-1 requirement: no Stripe, DB, HTTP or environment dependency."""

    def test_imports_nothing_from_the_service_layer(self):
        """No I/O dependency of any kind.

        Checks IMPORTS, not prose: the v1 fulfilment validator legitimately inspects a
        Stripe session DICT handed to it by the caller, so the word "Stripe" appears in
        its messages. What must never appear is a dependency — the module still performs
        no network, database or environment access.
        """
        import shared.teams_pricing as module

        source = open(module.__file__, encoding="utf-8").read()
        import_lines = [ln.strip() for ln in source.splitlines()
                        if ln.strip().startswith(("import ", "from "))]
        self.assertEqual(
            sorted(import_lines),
            sorted(["from dataclasses import dataclass",
                    "from typing import Any, Dict, Tuple"]),
            "teams_pricing must stay dependency-free",
        )
        for forbidden in (
            "import os", "import requests", "import stripe", "import pyodbc",
            "shared.db", "shared.stripe_client", "azure.", "get_secret(",
            "os.environ",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_quote_is_deterministic(self):
        self.assertEqual(quote(37).to_dict(), quote(37).to_dict())


if __name__ == "__main__":
    unittest.main()
