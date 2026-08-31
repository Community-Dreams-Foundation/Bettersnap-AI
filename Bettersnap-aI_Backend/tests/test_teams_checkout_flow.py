"""Teams checkout lifecycle: quotes, single-open-session control, fulfilment.

Distinct from tests/test_teams_pricing.py, which pins the arithmetic. These cover the
parts where MONEY CAN MOVE TWICE or move WITHOUT AUTHORISATION:

  - a quote is a durable server record: owned, scoped, expiring, single-use;
  - a malformed or missing quote id fails closed BEFORE Stripe and before any DB write;
  - an organization may have at most ONE live payable attempt — double-clicks and retries
    reuse it rather than opening a second payable Stripe page;
  - the durable record is written BEFORE Stripe is called, so no session can exist that
    the server has no record of;
  - a snapshot-less webhook fails closed unless explicitly recorded as legacy;
  - fulfilment validates the money that arrived against the snapshot, and replays cannot
    grant twice.

True multi-connection concurrency is proven separately against a real SQL Server in
tests/integration/test_teams_checkout_concurrency.py — these prove the single-request
decision logic.

Run:  python -m unittest tests.test_teams_checkout_flow   (from the backend dir)
"""
import json
import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

# Importing this module installs the azure/shared stubs and imports function_app.
from tests.test_org_teams import (  # noqa: E402
    FakeRequest, _auth_as, _patched, _teams_checkout_on,
)
import function_app  # noqa: E402
from shared import teams_checkout, teams_pricing  # noqa: E402

ADMIN = "ABCDEF01-2345-6789-ABCD-EF0123456789"
ORG = "11111111-2222-4333-8444-555555555555"
QUOTE = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
OTHER_USER = "99999999-8888-4777-8666-555555555555"
OTHER_ORG = "22222222-3333-4444-8555-666666666666"
# attempt_id is a UNIQUEIDENTIFIER, and the recovery path compares it by PARSING rather
# than by string equality — so the fixture has to be a real GUID, not a label.
ATT1 = "7c9e6679-7425-40de-944b-e07fc1f90ae7"


#: Distinguishes "caller did not specify" from "caller explicitly passed None/''".
#: Without it, `_snapshot(version=None)` silently became a VALID v1 snapshot and the
#: unknown-version subtests passed vacuously.
_UNSET = object()

def _quote_row(user=ADMIN, org=ORG, seats=10, total=_UNSET, credits=30,
               version=None, status="issued", expires_in=1800, consumed_by=None,
               breakdown=_UNSET):
    """Row shape returned by teams_checkout.load_quote_for_update's SELECT.

    expires_at is NAIVE, exactly as SQL Server returns it.

    breakdown_json is the LAST column and is not optional: reserve_attempt copies it onto
    the attempt, and fulfilment validates the payment against it. These fixtures used to
    omit it entirely, which is precisely why they did not catch the loader dropping it.
    """
    expires = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=expires_in)
    is_v1 = (version or teams_pricing.PRICING_VERSION) == "teams_basic_v1"
    if breakdown is _UNSET:
        bands = _v1_bands(seats) if is_v1 else _v2_band(seats)
    else:
        bands = breakdown
    if total is _UNSET:
        total = _v1_total(seats) if is_v1 else _v2_total(seats)
    return (QUOTE, user, org, seats, total, credits,
            version or teams_pricing.PRICING_VERSION, teams_pricing.PLAN_ID, "usd",
            expires, status, consumed_by,
            None if bands is None else
            bands if isinstance(bands, str) else json.dumps(bands))


#: The band breakdown as it is persisted on an attempt — the authorised terms recovery
#: must replay verbatim rather than recomputing.
LIVE_BREAKDOWN = [
    {"from_seat": 1, "to_seat": 10, "unit_price_cents": 3200,
     "seats": 10, "subtotal_cents": 32_000},
]


def _live(attempt_id, quote_id=QUOTE, status="pending", url="https://checkout.stripe.com/c/1",
          session_id="cs_live", seats=10, total=_UNSET, credits=30,
          version=None, breakdown=None, idem=None):
    """Row shape returned by teams_checkout.find_live_attempt's SELECT.

    Total and breakdown follow the attempt's OWN pricing version, so a fixture pinned to
    a superseded version stays internally consistent when the current one moves on.
    """
    resolved = version or teams_pricing.PRICING_VERSION
    is_v1 = resolved == "teams_basic_v1"
    if total is _UNSET:
        total = _v1_total(seats) if is_v1 else _v2_total(seats)
    if breakdown is None:
        breakdown = _v1_bands(seats) if is_v1 else _v2_band(seats)
    return (attempt_id, session_id, url, quote_id,
            idem if idem is not None else teams_checkout.idempotency_key(ORG, quote_id),
            status, seats, credits, total,
            resolved, "usd", teams_pricing.PLAN_ID,
            json.dumps(breakdown))


def _paid_session(session_id="cs_1", org=ORG, amount=32_000, currency="usd",
                  seats=10, credits=30, created=None):
    s = {
        "id": session_id, "amount_total": amount, "currency": currency,
        "payment_intent": "pi_1",
        "metadata": {"organization_id": org, "payment_type": "org_seats",
                     "seats": str(seats), "credits_per_seat": str(credits)},
    }
    if created is not None:
        s["created"] = created
    return s


def _v1_total(seats):
    """The GRADUATED total v1 charged, for fixtures that still exercise v1."""
    return sum(3500 if n <= 9 else 3200 if n <= 24 else 2900 for n in range(1, seats + 1))


def _v2_band(seats):
    """The v2 breakdown: ONE band covering the whole order at a single rate."""
    unit = 3200 - 30 * (seats - 10)
    return [{"from_seat": 1, "to_seat": seats, "unit_price_cents": unit,
             "seats": seats, "subtotal_cents": seats * unit}]


def _v2_total(seats):
    return seats * (3200 - 30 * (seats - 10))


def _v1_bands(seats):
    """The v1 graduated breakdown for `seats`, built from the frozen v1 table."""
    out, lower = [], 1
    for lo, hi, unit in ((1, 9, 3500), (10, 24, 3200), (25, 49, 2900)):
        n = min(seats, hi) - lo + 1
        if n <= 0:
            continue
        out.append({"from_seat": lo, "to_seat": hi, "unit_price_cents": unit,
                    "seats": n, "subtotal_cents": n * unit})
        lower = lo + n
    return out



def _snapshot(org=ORG, seats=10, credits=30, total=_UNSET, currency="usd",
              version=_UNSET, status="pending", attempt=ATT1, breakdown=_UNSET,
              plan=_UNSET):
    """Row shape returned by function_app._load_checkout_snapshot's SELECT.

    The last element is breakdown_json — the version's validator proves the bands are
    contiguous, complete and correctly priced rather than trusting a bare total.
    """
    is_v1 = (version or teams_pricing.PRICING_VERSION) == "teams_basic_v1"
    if breakdown is _UNSET:
        bands = _v1_bands(seats) if is_v1 else _v2_band(seats)
    else:
        bands = breakdown
    if total is _UNSET:
        total = _v1_total(seats) if is_v1 else _v2_total(seats)
    return (org,
            teams_pricing.PRICING_VERSION if version is _UNSET else version,
            teams_pricing.PLAN_ID if plan is _UNSET else plan,
            seats, credits, total, currency, status, attempt,
            None if bands is None else json.dumps(bands)
            if not isinstance(bands, str) else bands)


def _checkout(body, cfg=None, org_row=None, caller=ADMIN, stripe=None, stripe_raises=None):
    """Drive create_org_payment_intent end to end against the fake DB."""
    cfg = dict(cfg or {})
    cfg.setdefault("payment_intent_org_row",
                   org_row or (ADMIN, 10, "pending_payment"))
    conn, p1, p2 = _patched(cfg)
    auth1, auth2 = _auth_as(caller)
    flag = _teams_checkout_on(); flag.start()
    patch = mock.patch.object(
        function_app, "create_org_seats_checkout",
        side_effect=stripe_raises,
        return_value=stripe or {"url": "https://checkout.stripe.com/c/new", "id": "cs_new"})
    m = patch.start()
    p1.start(); p2.start(); auth1.start(); auth2.start()
    try:
        resp = function_app.create_org_payment_intent(
            FakeRequest(body=body, route_params={"organization_id": ORG}))
    finally:
        p1.stop(); p2.stop(); auth1.stop(); auth2.stop(); patch.stop(); flag.stop()
    return resp, m, cfg, conn


# ═══════════════════════════════════════════════════════════════════════════
# Quote id handling — fail closed
# ═══════════════════════════════════════════════════════════════════════════
class QuoteIdIsMandatory(unittest.TestCase):
    """A malformed quote id must never degrade into 'proceed without one'."""

    def test_missing_quote_id_is_refused_without_calling_stripe(self):
        resp, m, cfg, _ = _checkout({})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(json.loads(resp.body)["error"], "QUOTE_REQUIRED")
        m.assert_not_called()
        self.assertEqual(cfg.get("executed", []), [], "no DB write may occur")

    def test_malformed_quote_id_is_refused_without_calling_stripe(self):
        for bad in ("not-a-uuid", "abcdef01", "", "  ", 12345, None, [], {}):
            with self.subTest(quote_id=bad):
                resp, m, cfg, _ = _checkout({"quote_id": bad})
                self.assertIn(resp.status_code, (400,))
                self.assertIn(json.loads(resp.body)["error"],
                              ("QUOTE_MALFORMED", "QUOTE_REQUIRED"))
                m.assert_not_called()
                self.assertEqual(cfg.get("executed", []), [])

    def test_parse_quote_id_raises_rather_than_returning_none(self):
        with self.assertRaises(teams_checkout.QuoteRequired):
            teams_checkout.parse_quote_id(None)
        with self.assertRaises(teams_checkout.QuoteMalformed):
            teams_checkout.parse_quote_id("nope")


# ═══════════════════════════════════════════════════════════════════════════
# Quote validation
# ═══════════════════════════════════════════════════════════════════════════
class QuoteValidation(unittest.TestCase):
    def _expect(self, quote_row, code, caller=ADMIN):
        resp, m, _, _ = _checkout({"quote_id": QUOTE}, cfg={"quote_row": quote_row},
                                  caller=caller)
        self.assertEqual(json.loads(resp.body)["error"], code)
        self.assertEqual(resp.status_code, 409)
        m.assert_not_called()

    def test_unknown_quote(self):
        self._expect(None, "QUOTE_NOT_FOUND")

    def test_expired_quote(self):
        self._expect(_quote_row(expires_in=-1), "QUOTE_EXPIRED")

    def test_consumed_quote_cannot_be_reused(self):
        self._expect(_quote_row(status="consumed", consumed_by="some-other-attempt"),
                     "QUOTE_ALREADY_USED")

    def test_quote_belonging_to_another_user(self):
        """A quote is not bearer currency."""
        self._expect(_quote_row(user=OTHER_USER), "QUOTE_OWNER_MISMATCH")

    def test_quote_scoped_to_another_organization(self):
        self._expect(_quote_row(org=OTHER_ORG), "QUOTE_ORGANIZATION_MISMATCH")

    def test_quote_from_a_superseded_contract(self):
        self._expect(_quote_row(version="teams_basic_v0"), "QUOTE_VERSION_SUPERSEDED")

    def test_org_agnostic_quote_is_accepted(self):
        """A quote priced before the workspace existed may still be spent on it."""
        resp, m, _, _ = _checkout({"quote_id": QUOTE},
                                  cfg={"quote_row": _quote_row(org=None)})
        self.assertEqual(resp.status_code, 200)
        m.assert_called_once()

    def test_guid_casing_does_not_break_ownership(self):
        """pyodbc renders UNIQUEIDENTIFIER uppercase; callers carry lowercase."""
        resp, m, _, _ = _checkout(
            {"quote_id": QUOTE},
            cfg={"quote_row": _quote_row(user=ADMIN.lower(), org=ORG.lower())},
            caller=ADMIN.upper())
        self.assertEqual(resp.status_code, 200)
        m.assert_called_once()

    def test_the_charge_comes_from_the_persisted_quote_not_the_request(self):
        """Body seats/total are ignored entirely; the stored quote is authoritative."""
        resp, m, _, _ = _checkout(
            {"quote_id": QUOTE, "seats": 49, "total_cents": 1},
            cfg={"quote_row": _quote_row(seats=10, total=32_000)})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(json.loads(resp.body)["total_cents"], 32_000)
        self.assertEqual(m.call_args[0][2].seats, 10)

    def test_displayed_total_disagreement_is_refused(self):
        # A number the client claims that the stored quote does not agree with.
        resp, m, _, _ = _checkout({"quote_id": QUOTE, "quoted_total_cents": 99_999},
                                  cfg={"quote_row": _quote_row()})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(json.loads(resp.body)["error"], "QUOTE_STALE")
        self.assertEqual(json.loads(resp.body)["total_cents"], 32_000)
        m.assert_not_called()

    def test_quote_is_consumed_exactly_once(self):
        _, _, cfg, _ = _checkout({"quote_id": QUOTE}, cfg={"quote_row": _quote_row()})
        consumes = [s for s, _ in cfg["executed"] if "UPDATE teams_quotes" in s]
        self.assertEqual(len(consumes), 1)

    def test_losing_the_consume_race_aborts(self):
        """Guarded on status='issued': a concurrent consumer wins, this one refuses."""
        resp, m, _, conn = _checkout(
            {"quote_id": QUOTE},
            cfg={"quote_row": _quote_row(), "quote_consume_rowcount": 0})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(json.loads(resp.body)["error"], "QUOTE_ALREADY_USED")
        m.assert_not_called()
        self.assertTrue(conn.rolled_back)


# ═══════════════════════════════════════════════════════════════════════════
# Single open payable session
# ═══════════════════════════════════════════════════════════════════════════
class SingleOpenSession(unittest.TestCase):
    def test_double_click_reuses_the_existing_session(self):
        """The decisive test: a second request for the SAME quote must return the SAME
        payable page, not create another one. Two open sessions are two charges."""
        resp, m, _, _ = _checkout(
            {"quote_id": QUOTE},
            cfg={"quote_row": _quote_row(status="consumed", consumed_by=ATT1),
                 "live_attempt_row": _live(ATT1)})
        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.body)
        self.assertTrue(body["reused"])
        self.assertEqual(body["session_id"], "cs_live")
        m.assert_not_called()          # ZERO Stripe creations on the second click

    def test_a_live_attempt_for_a_different_quote_is_REFUSED_not_replaced(self):
        """THE P0. Cancelling our row would release the workspace while the customer's
        ORIGINAL Stripe page stayed payable — two payable URLs, one live slot. Only
        Stripe can retire a Stripe page, so this refuses instead."""
        other = "9a9e6679-7425-40de-944b-e07fc1f90ae9"
        resp, m, cfg, _ = _checkout(
            {"quote_id": QUOTE},
            cfg={"quote_row": _quote_row(),
                 "live_attempt_row": _live(other, quote_id=str(uuid.uuid4()))})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(json.loads(resp.body)["error"], "CHECKOUT_ALREADY_OPEN")
        m.assert_not_called()                       # ZERO additional Stripe sessions
        self.assertFalse(cfg.get("live_slot_released"),
                         "the existing checkout must NOT be released locally")
        self.assertFalse(
            any("UPDATE organization_checkout_sessions" in s and "settled_at" in s
                for s, _ in cfg["executed"]),
            "the existing attempt must not be settled behind Stripe's back")

    def test_the_already_open_response_leaks_nothing_sensitive(self):
        other = "9a9e6679-7425-40de-944b-e07fc1f90ae9"
        resp, _, _, _ = _checkout(
            {"quote_id": QUOTE},
            cfg={"quote_row": _quote_row(),
                 "live_attempt_row": _live(other, quote_id=str(uuid.uuid4()))})
        body = json.loads(resp.body)
        self.assertEqual(set(body) - {"error", "message", "seats", "total_cents",
                                      "status"}, set())
        # Never hand back another session's payable URL or its identifiers.
        for leak in ("checkout_url", "session_id", "attempt_id", "quote_id",
                     "created_by_user_id", "idempotency_key"):
            self.assertNotIn(leak, body)

    def test_the_durable_record_is_written_before_stripe_is_called(self):
        _, m, cfg, _ = _checkout({"quote_id": QUOTE}, cfg={"quote_row": _quote_row()})
        statements = [s for s, _ in cfg["executed"]]
        insert_at = next(i for i, s in enumerate(statements)
                         if "INSERT INTO organization_checkout_sessions" in s)
        live_at = next(i for i, s in enumerate(statements)
                       if "INSERT INTO organization_live_checkout" in s)
        self.assertLess(insert_at, live_at)
        # And Stripe was called only after the transaction committed.
        m.assert_called_once()

    def test_a_deterministic_idempotency_key_is_sent_to_stripe(self):
        _, m, _, _ = _checkout({"quote_id": QUOTE}, cfg={"quote_row": _quote_row()})
        key = m.call_args.kwargs["idempotency_key"]
        self.assertEqual(key, teams_checkout.idempotency_key(ORG, QUOTE))
        # Deterministic: the same inputs always produce the same key.
        self.assertEqual(key, teams_checkout.idempotency_key(ORG.lower(), QUOTE.upper()))

    def test_recovering_a_stranded_creating_attempt_replays_the_same_key(self):
        """Stripe may already hold a session for this attempt. Replaying the key returns
        the ORIGINAL rather than opening a second payable page."""
        resp, m, cfg, _ = _checkout(
            {"quote_id": QUOTE},
            cfg={"quote_row": _quote_row(status="consumed", consumed_by=ATT1),
                 "live_attempt_row": _live(ATT1, status="creating",
                                           url=None, session_id=None)})
        self.assertEqual(resp.status_code, 200)
        m.assert_called_once()
        self.assertEqual(m.call_args.kwargs["idempotency_key"],
                         teams_checkout.idempotency_key(ORG, QUOTE))
        # No SECOND attempt row was created for the recovery.
        inserts = [s for s, _ in cfg["executed"]
                   if "INSERT INTO organization_checkout_sessions" in s]
        self.assertEqual(inserts, [])

    def test_lock_contention_is_refused_not_queued_into_a_second_session(self):
        resp, m, _, _ = _checkout({"quote_id": QUOTE},
                                  cfg={"quote_row": _quote_row(), "applock_rc": -1})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(json.loads(resp.body)["error"], "CHECKOUT_IN_PROGRESS")
        m.assert_not_called()

    def test_an_already_active_org_cannot_open_a_checkout(self):
        resp, m, _, _ = _checkout({"quote_id": QUOTE},
                                  cfg={"quote_row": _quote_row()},
                                  org_row=(ADMIN, 10, "active"))
        self.assertEqual(resp.status_code, 409)
        m.assert_not_called()

    def test_checkout_never_writes_seats_onto_the_organization(self):
        """Opening a checkout is not a purchase. Seats become authoritative only at
        verified fulfilment, so a cancelled attempt leaves no trace on the workspace."""
        _, _, cfg, _ = _checkout({"quote_id": QUOTE},
                                 cfg={"quote_row": _quote_row(seats=25, total=82_400)})
        self.assertFalse(
            any("UPDATE organizations SET seats_purchased" in s for s, _ in cfg["executed"]),
            "seats_purchased must not change merely because checkout was opened",
        )


# ═══════════════════════════════════════════════════════════════════════════
# Stranded 'creating' recovery vs. new purchase
# ═══════════════════════════════════════════════════════════════════════════
class StrandedAttemptRecovery(unittest.TestCase):
    """Two paths that must not share rules.

    A NEW purchase requires an unspent, unexpired, current-contract quote. RECOVERY of an
    already-authorised 'creating' attempt must NOT apply those rules — otherwise the
    workspace's single live slot becomes permanently stuck the moment the quote ages out
    (30 minutes) or the price list changes, with no automatic way back.
    """

    def _recover(self, quote_row, live, stripe=None):
        return _checkout({"quote_id": QUOTE},
                         cfg={"quote_row": quote_row, "live_attempt_row": live},
                         stripe=stripe)

    # ── New purchases stay strict ────────────────────────────────────────
    def test_new_checkout_rejects_an_expired_quote(self):
        resp, m, _, _ = _checkout({"quote_id": QUOTE},
                                  cfg={"quote_row": _quote_row(expires_in=-1)})
        self.assertEqual(json.loads(resp.body)["error"], "QUOTE_EXPIRED")
        m.assert_not_called()

    def test_new_checkout_rejects_a_superseded_pricing_version(self):
        resp, m, _, _ = _checkout({"quote_id": QUOTE},
                                  cfg={"quote_row": _quote_row(version="teams_basic_v0")})
        self.assertEqual(json.loads(resp.body)["error"], "QUOTE_VERSION_SUPERSEDED")
        m.assert_not_called()

    def test_new_checkout_rejects_a_consumed_quote(self):
        resp, m, _, _ = _checkout(
            {"quote_id": QUOTE},
            cfg={"quote_row": _quote_row(status="consumed", consumed_by=ATT1)})
        self.assertEqual(json.loads(resp.body)["error"], "QUOTE_ALREADY_USED")
        m.assert_not_called()

    # ── Recovery is not blocked by age or repricing ──────────────────────
    def test_recovers_after_the_quote_has_expired(self):
        resp, m, _, _ = self._recover(
            _quote_row(status="consumed", consumed_by=ATT1, expires_in=-3600),
            _live(ATT1, status="creating", url=None, session_id=None))
        self.assertEqual(resp.status_code, 200)
        m.assert_called_once()

    def test_recovers_after_pricing_changed_without_changing_its_price(self):
        """A newer CURRENT contract must not rewrite an already-authorised amount.

        The attempt stays on v1 — a SUPERSEDED but still SUPPORTED version. (A version
        that has been retired outright is a different case and is refused; see
        CrossStageV1SurvivesV2.test_a_retired_version_fails_closed_on_recovery.)
        """
        with _as_v2_current():
            resp, m, _, _ = self._recover(
                _quote_row(status="consumed", consumed_by=ATT1,
                           version="teams_basic_v1", total=34_700),
                _live(ATT1, status="creating", url=None, session_id=None,
                      version="teams_basic_v1"))
        self.assertEqual(resp.status_code, 200)
        replayed = m.call_args[0][2]
        self.assertEqual(replayed.total_cents, 34_700)
        self.assertEqual(replayed.pricing_version, "teams_basic_v1")
        self.assertEqual(json.loads(resp.body)["pricing_version"], "teams_basic_v1")

    def test_the_replayed_request_matches_the_persisted_snapshot_exactly(self):
        live = _live(ATT1, status="creating", url=None, session_id=None,
                     seats=25, total=82_400, credits=30,
                     breakdown=[
                         {"from_seat": 1, "to_seat": 9, "unit_price_cents": 3500,
                          "seats": 9, "subtotal_cents": 31_500},
                         {"from_seat": 10, "to_seat": 24, "unit_price_cents": 3200,
                          "seats": 15, "subtotal_cents": 48_000},
                         {"from_seat": 25, "to_seat": 49, "unit_price_cents": 2900,
                          "seats": 1, "subtotal_cents": 2900},
                     ])
        resp, m, _, _ = self._recover(
            _quote_row(status="consumed", consumed_by=ATT1, seats=25, total=82_400), live)
        self.assertEqual(resp.status_code, 200)
        q = m.call_args[0][2]
        self.assertEqual(q.seats, 25)
        self.assertEqual(q.total_cents, 82_400)
        self.assertEqual(q.credits_per_seat, 30)
        self.assertEqual(q.currency, "usd")
        self.assertEqual(q.plan_id, teams_pricing.PLAN_ID)
        self.assertEqual([b.subtotal_cents for b in q.breakdown], [31_500, 48_000, 2900])
        self.assertEqual(sum(b.subtotal_cents for b in q.breakdown), 82_400)
        self.assertEqual(m.call_args.kwargs["quote_id"], QUOTE)
        self.assertEqual(m.call_args.kwargs["idempotency_key"],
                         teams_checkout.idempotency_key(ORG, QUOTE))

    def test_recovery_does_not_reprice_at_todays_rates(self):
        """The stored total wins even when it disagrees with the live contract."""
        odd = [{"from_seat": 1, "to_seat": 10, "unit_price_cents": 2000, "seats": 10,
                "subtotal_cents": 20_000}]
        resp, m, _, _ = self._recover(
            _quote_row(status="consumed", consumed_by=ATT1, total=20_000),
            _live(ATT1, status="creating", url=None, session_id=None,
                  total=20_000, breakdown=odd))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(m.call_args[0][2].total_cents, 20_000)
        self.assertNotEqual(m.call_args[0][2].total_cents,
                            teams_pricing.quote(10).total_cents)

    def test_recovery_creates_exactly_one_stripe_session(self):
        resp, m, cfg, _ = self._recover(
            _quote_row(status="consumed", consumed_by=ATT1, expires_in=-1),
            _live(ATT1, status="creating", url=None, session_id=None))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(m.call_count, 1)
        self.assertEqual([s for s, _ in cfg["executed"]
                          if "INSERT INTO organization_checkout_sessions" in s], [])
        self.assertEqual([s for s, _ in cfg["executed"]
                          if "UPDATE teams_quotes" in s], [],
                         "recovery must not re-consume the quote")

    # ── Recovery cannot be abused ────────────────────────────────────────
    def test_a_different_attempt_cannot_recover_the_consumed_quote(self):
        other = "9a9e6679-7425-40de-944b-e07fc1f90ae9"
        resp, m, _, _ = self._recover(
            _quote_row(status="consumed", consumed_by=other),
            _live(ATT1, status="creating", url=None, session_id=None))
        self.assertEqual(json.loads(resp.body)["error"], "QUOTE_ALREADY_USED")
        m.assert_not_called()

    def test_only_a_creating_attempt_may_use_this_path(self):
        """'pending' has a real session and is REUSED, not re-driven through creation.
        Settled states are not live at all, so they never reach recovery."""
        pending, m, _, _ = self._recover(
            _quote_row(status="consumed", consumed_by=ATT1, expires_in=-1),
            _live(ATT1, status="pending"))
        self.assertEqual(pending.status_code, 200)
        self.assertTrue(json.loads(pending.body)["reused"])
        m.assert_not_called()          # reuse returns the stored URL, no Stripe call

        for settled in ("cancelled", "expired", "failed", "paid"):
            with self.subTest(status=settled):
                # find_live_attempt only ever returns live rows; a settled attempt has
                # no live row, so the request is a NEW purchase and the expired quote
                # is correctly refused.
                resp, m2, _, _ = _checkout(
                    {"quote_id": QUOTE},
                    cfg={"quote_row": _quote_row(status="consumed", consumed_by=ATT1,
                                                 expires_in=-1),
                         "live_attempt_row": None})
                self.assertEqual(json.loads(resp.body)["error"], "QUOTE_ALREADY_USED")
                m2.assert_not_called()

    def test_a_mismatched_idempotency_key_refuses_recovery(self):
        resp, m, _, _ = self._recover(
            _quote_row(status="consumed", consumed_by=ATT1, expires_in=-1),
            _live(ATT1, status="creating", url=None, session_id=None,
                  idem="teams:someone-else:whatever"))
        self.assertEqual(json.loads(resp.body)["error"], "QUOTE_ALREADY_USED")
        m.assert_not_called()

    def test_recovery_still_enforces_owner_and_organization(self):
        for row in (_quote_row(status="consumed", consumed_by=ATT1, user=OTHER_USER),
                    _quote_row(status="consumed", consumed_by=ATT1, org=OTHER_ORG)):
            with self.subTest():
                resp, m, _, _ = self._recover(
                    row, _live(ATT1, status="creating", url=None, session_id=None))
                self.assertEqual(resp.status_code, 409)
                m.assert_not_called()

    def test_a_corrupted_breakdown_fails_loudly_rather_than_charging_something_else(self):
        resp, m, _, _ = self._recover(
            _quote_row(status="consumed", consumed_by=ATT1),
            _live(ATT1, status="creating", url=None, session_id=None,
                  total=32_000,
                  breakdown=[{"from_seat": 1, "to_seat": 9, "unit_price_cents": 1,
                              "seats": 9, "subtotal_cents": 9}]))
        self.assertEqual(resp.status_code, 502)
        m.assert_not_called()


class QuoteFromSnapshot(unittest.TestCase):
    def test_rebuilds_without_recomputing(self):
        q = teams_pricing.quote_from_snapshot(
            seats=25, credits_per_seat=30, total_cents=82_400,
            # Explicit v1 bands: this proves a SUPERSEDED version rebuilds verbatim,
            # so it must not borrow whatever shape the current version happens to use.
            breakdown=[
                {"from_seat": 1, "to_seat": 9, "unit_price_cents": 3500, "seats": 9,
                 "subtotal_cents": 31_500},
                {"from_seat": 10, "to_seat": 24, "unit_price_cents": 3200, "seats": 15,
                 "subtotal_cents": 48_000},
                {"from_seat": 25, "to_seat": 49, "unit_price_cents": 2900, "seats": 1,
                 "subtotal_cents": 2900}],
            pricing_version="teams_basic_v0")
        self.assertEqual(q.total_cents, 82_400)
        self.assertEqual(q.pricing_version, "teams_basic_v0")
        self.assertEqual(q.total_credits, 750)

    def test_refuses_a_breakdown_that_does_not_sum_to_the_total(self):
        with self.assertRaises(ValueError):
            teams_pricing.quote_from_snapshot(
                seats=10, credits_per_seat=30, total_cents=34_700,
                breakdown=[{"from_seat": 1, "to_seat": 9, "unit_price_cents": 1,
                            "seats": 9, "subtotal_cents": 9}],
                pricing_version="teams_basic_v1")

    def test_refuses_an_empty_breakdown(self):
        with self.assertRaises(ValueError):
            teams_pricing.quote_from_snapshot(
                seats=10, credits_per_seat=30, total_cents=34_700, breakdown=[],
                pricing_version="teams_basic_v1")


# ═══════════════════════════════════════════════════════════════════════════
# Versioned fulfilment: a v1 order survives a v2 deployment
# ═══════════════════════════════════════════════════════════════════════════
V2 = "teams_basic_v2"


def _as_v2_current():
    """Simulate v2 becoming the current contract, v1 still supported for fulfilment."""
    return mock.patch.multiple(
        teams_pricing,
        CURRENT_PRICING_VERSION=V2,
        PRICING_VERSION=V2,
        SUPPORTED_PRICING_VERSIONS={
            "teams_basic_v1": teams_pricing.validate_teams_basic_v1_snapshot,
            V2: teams_pricing.validate_teams_basic_v1_snapshot,
        },
    )


class CrossStageV1SurvivesV2(unittest.TestCase):
    """THE cross-stage bug. Fulfilment used to require the snapshot's version to equal
    TODAY's version, so a customer quoted, charged and paid under v1 would be refused the
    moment v2 shipped: money taken, workspace never activated, credits never granted."""

    def test_v1_order_recovers_and_fulfils_after_v2_becomes_current(self):
        # ── 1-2. A v1 quote was issued and a v1 attempt reserved (still 'creating'). ──
        v1_quote = _quote_row(status="consumed", consumed_by=ATT1, seats=10,
                              total=34_700, version="teams_basic_v1", expires_in=-3600)
        v1_attempt = _live(ATT1, status="creating", url=None, session_id=None,
                           seats=10, total=34_700, credits=30,
                           version="teams_basic_v1", breakdown=_v1_bands(10))

        # ── 3-5. v2 is now current. Recovery must replay the ORIGINAL v1 terms. ──
        with _as_v2_current():
            resp, m, _, _ = _checkout(
                {"quote_id": QUOTE},
                cfg={"quote_row": v1_quote, "live_attempt_row": v1_attempt})
        self.assertEqual(resp.status_code, 200)
        m.assert_called_once()
        q = m.call_args[0][2]
        self.assertEqual(q.pricing_version, "teams_basic_v1")
        self.assertEqual(q.seats, 10)
        self.assertEqual(q.total_cents, 34_700)
        self.assertEqual(q.credits_per_seat, 30)
        self.assertEqual(q.currency, "usd")
        self.assertEqual([b.subtotal_cents for b in q.breakdown], [31_500, 3200])
        self.assertEqual(m.call_args.kwargs["quote_id"], QUOTE)
        self.assertEqual(m.call_args.kwargs["idempotency_key"],
                         teams_checkout.idempotency_key(ORG, QUOTE))

        # ── 6-9. The paid webhook arrives, still under v2. It must fulfil. ──
        cfg = {"checkout_snapshot": _snapshot(seats=10, total=34_700,
                                              version="teams_basic_v1"),
               "webhook_org_row": (ADMIN, 30)}
        conn, p1, p2 = _patched(cfg)
        p1.start(); p2.start()
        try:
            with _as_v2_current():
                function_app._handle_org_payment(
                    _paid_session(amount=34_700, seats=10, credits=30),
                    "checkout_session:cs_1")
        finally:
            p1.stop(); p2.stop()

        self.assertTrue(conn.committed, "a paid v1 order must still fulfil under v2")
        self.assertTrue(
            any("UPDATE organizations SET status = 'active'" in s
                for s, _ in cfg["executed"]), "organization must be activated")
        grant = [p for s, p in cfg["executed"]
                 if "SET credits_granted = ?, credits_remaining = ?" in s]
        self.assertEqual(grant[0][0], 30, "exactly 30 credits per paid member")
        self.assertEqual(grant[0][1], 30)
        pay = [p for s, p in cfg["executed"] if "UPDATE organization_payments" in s]
        self.assertIn("teams_basic_v1", [str(v) for v in pay[0]],
                      "the payment row must record the version actually paid")

        # ── 10. Replay is a strict no-op. ──
        cfg2 = {"claim_event_rowcount": 0,
                "checkout_snapshot": _snapshot(version="teams_basic_v1"),
                "webhook_org_row": (ADMIN, 30)}
        conn2, q1, q2 = _patched(cfg2)
        q1.start(); q2.start()
        try:
            with _as_v2_current():
                function_app._handle_org_payment(_paid_session(), "checkout_session:cs_1")
        finally:
            q1.stop(); q2.stop()
        self.assertFalse(conn2.committed)
        self.assertFalse(any("SET credits_granted" in s for s, _ in cfg2["executed"]))

    def test_a_new_v1_quote_cannot_be_issued_once_v2_is_current(self):
        """Changing the current version must not allow new v1 purchases."""
        with _as_v2_current():
            resp, m, _, _ = _checkout(
                {"quote_id": QUOTE},
                cfg={"quote_row": _quote_row(version="teams_basic_v1")})
        self.assertEqual(json.loads(resp.body)["error"], "QUOTE_VERSION_SUPERSEDED")
        m.assert_not_called()

    def test_a_retired_version_fails_closed_on_recovery(self):
        """SUPPORTED is not the same as 'anything stored'. Retired => no recovery."""
        with mock.patch.dict(teams_pricing.SUPPORTED_PRICING_VERSIONS, {}, clear=True):
            resp, m, _, _ = _checkout(
                {"quote_id": QUOTE},
                cfg={"quote_row": _quote_row(status="consumed", consumed_by=ATT1,
                                             expires_in=-1),
                     "live_attempt_row": _live(ATT1, status="creating", url=None,
                                               session_id=None)})
        self.assertEqual(resp.status_code, 409)
        m.assert_not_called()


class VersionedSnapshotValidation(unittest.TestCase):
    def _problems(self, snapshot_row, session=None):
        keys = ("organization_id", "pricing_version", "plan_id", "seats",
                "credits_per_seat", "expected_total_cents", "currency", "status",
                "attempt_id", "breakdown_json")
        snap = dict(zip(keys, snapshot_row))
        # Mirrors _load_checkout_snapshot exactly, including its handling of a column
        # that does not parse as JSON — which must surface as a validator problem, not
        # as an exception escaping the webhook.
        try:
            snap["breakdown"] = (json.loads(snap["breakdown_json"])
                                 if snap["breakdown_json"] else [])
        except (TypeError, ValueError):
            snap["breakdown"] = None
        return function_app._org_payment_mismatches(
            snap, session or _paid_session(amount=snap["expected_total_cents"]))

    def test_a_correct_v1_snapshot_has_no_problems(self):
        self.assertEqual(self._problems(_snapshot(version="teams_basic_v1")), [])

    def test_an_unknown_version_fails_closed(self):
        for bad in ("teams_basic_v99", "", None, "'; DROP TABLE--"):
            with self.subTest(version=bad):
                problems = self._problems(_snapshot(version=bad))
                self.assertTrue(problems)
                self.assertIn("supported set", problems[0])

    def test_a_retired_version_fails_closed(self):
        with mock.patch.dict(teams_pricing.SUPPORTED_PRICING_VERSIONS, {}, clear=True):
            self.assertTrue(self._problems(_snapshot(version="teams_basic_v1")))

    def test_wrong_plan_id_fails(self):
        self.assertTrue(self._problems(_snapshot(plan="teams_enterprise")))

    def test_unsupported_currency_fails(self):
        self.assertTrue(self._problems(_snapshot(currency="eur")))

    def test_illegal_seat_count_fails(self):
        for seats in (9, 50, 0, -1):
            with self.subTest(seats=seats):
                self.assertTrue(self._problems(
                    _snapshot(seats=seats, breakdown=_v1_bands(max(seats, 1)))))

    def test_wrong_credits_per_seat_fails(self):
        self.assertTrue(self._problems(_snapshot(credits=10)))
        self.assertTrue(self._problems(_snapshot(credits=31)))

    def test_malformed_breakdown_fails(self):
        for bad in (None, [], [{"nope": 1}], "not-a-list"):
            with self.subTest(breakdown=bad):
                self.assertTrue(self._problems(_snapshot(breakdown=bad)))

    def test_subtotal_that_is_not_unit_times_seats_fails(self):
        bands = _v1_bands(10)
        bands[0]["subtotal_cents"] = 31_499
        self.assertTrue(self._problems(_snapshot(breakdown=bands)))

    def test_a_gap_in_the_bands_fails(self):
        bands = _v1_bands(10)
        bands[1]["from_seat"] = 11          # seat 10 unaccounted for
        self.assertTrue(self._problems(_snapshot(breakdown=bands)))

    def test_an_overlap_in_the_bands_fails(self):
        bands = _v1_bands(25)
        bands[1]["from_seat"] = 9           # seat 9 charged twice
        self.assertTrue(self._problems(_snapshot(breakdown=bands)))

    def test_missing_seats_fail(self):
        bands = _v1_bands(10)
        bands.pop()                          # covers 9, order says 10
        self.assertTrue(self._problems(_snapshot(seats=10, breakdown=bands)))

    def test_a_wrong_unit_price_fails(self):
        bands = _v1_bands(10)
        bands[0]["unit_price_cents"] = 3000
        bands[0]["subtotal_cents"] = 27_000
        self.assertTrue(self._problems(_snapshot(breakdown=bands)))

    def test_subtotals_that_do_not_sum_to_the_total_fail(self):
        self.assertTrue(self._problems(_snapshot(total=34_701)))

    def test_stripe_amount_currency_and_metadata_must_agree(self):
        base = _snapshot(version="teams_basic_v1")
        self.assertTrue(self._problems(base, _paid_session(amount=32_000)))
        self.assertTrue(self._problems(base, _paid_session(currency="eur")))
        self.assertTrue(self._problems(base, _paid_session(seats=25)))
        self.assertTrue(self._problems(base, _paid_session(credits=10)))

    def test_the_validator_does_not_consult_todays_pricing_function(self):
        """Frozen by value: changing the live band table must not change v1's verdict."""
        good = _snapshot(version="teams_basic_v1")
        with mock.patch.object(teams_pricing, "BANDS", ()):
            self.assertEqual(self._problems(good), [])


# ═══════════════════════════════════════════════════════════════════════════
# Failure injection around the Stripe boundary
# ═══════════════════════════════════════════════════════════════════════════
class FailureInjection(unittest.TestCase):
    def test_db_reservation_then_stripe_timeout_leaves_a_recoverable_attempt(self):
        resp, m, cfg, _ = _checkout(
            {"quote_id": QUOTE}, cfg={"quote_row": _quote_row()},
            stripe_raises=TimeoutError("stripe timed out"))
        self.assertEqual(resp.status_code, 502)
        # The attempt row EXISTS (written before Stripe), so the session — if Stripe did
        # create one — is not orphaned, and the retry replays the same idempotency key.
        self.assertTrue(any("INSERT INTO organization_checkout_sessions" in s
                            for s, _ in cfg["executed"]))
        self.assertTrue(any("INSERT INTO organization_live_checkout" in s
                            for s, _ in cfg["executed"]))

    def test_retry_after_an_ambiguous_network_result_creates_no_second_charge(self):
        """First call times out; the retry finds the live 'creating' attempt and replays
        the SAME idempotency key, so Stripe returns the original session."""
        first, _, _, _ = _checkout({"quote_id": QUOTE}, cfg={"quote_row": _quote_row()},
                                   stripe_raises=TimeoutError("boom"))
        self.assertEqual(first.status_code, 502)
        second, m2, cfg2, _ = _checkout(
            {"quote_id": QUOTE},
            cfg={"quote_row": _quote_row(status="consumed", consumed_by=ATT1),
                 "live_attempt_row": _live(ATT1, status="creating",
                                           url=None, session_id=None)})
        self.assertEqual(second.status_code, 200)
        self.assertEqual(m2.call_count, 1)
        self.assertEqual(m2.call_args.kwargs["idempotency_key"],
                         teams_checkout.idempotency_key(ORG, QUOTE))
        self.assertEqual([s for s, _ in cfg2["executed"]
                          if "INSERT INTO organization_checkout_sessions" in s], [])

    def test_stripe_success_then_db_failure_refuses_the_handoff(self):
        """The session exists at Stripe but we could not record it. Do NOT hand the
        customer a page whose payment we could not later validate."""
        resp, m, _, _ = _checkout({"quote_id": QUOTE},
                                  cfg={"quote_row": _quote_row(), "promote_raises": True})
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(json.loads(resp.body)["error"], "CHECKOUT_NOT_RECORDED")
        m.assert_called_once()

    def test_reservation_failure_refuses_before_stripe(self):
        resp, m, _, _ = _checkout({"quote_id": QUOTE},
                                  cfg={"quote_row": _quote_row(),
                                       "attempt_insert_raises": True})
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(json.loads(resp.body)["error"], "CHECKOUT_NOT_RECORDED")
        m.assert_not_called()

    def test_losing_the_live_slot_race_refuses_before_stripe(self):
        """The organization_live_checkout PRIMARY KEY is the backstop if two requests
        somehow both pass the applock."""
        resp, m, _, _ = _checkout({"quote_id": QUOTE},
                                  cfg={"quote_row": _quote_row(),
                                       "live_insert_raises": True})
        self.assertEqual(resp.status_code, 500)
        m.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# POST /teams/pricing-quote
# ═══════════════════════════════════════════════════════════════════════════
class PricingQuoteEndpoint(unittest.TestCase):
    def _call(self, body, enabled=True, authed=True, cfg=None):
        cfg = cfg if cfg is not None else {}
        conn, p1, p2 = _patched(cfg)
        auth1, auth2 = _auth_as(ADMIN)
        flag = _teams_checkout_on() if enabled else mock.patch.dict(os.environ, {})
        if not enabled:
            os.environ.pop("TEAMS_CHECKOUT_ENABLED", None)
        if not authed:
            auth1 = mock.patch.object(
                function_app, "validate_token", side_effect=Exception("bad token"))
        flag.start(); p1.start(); p2.start(); auth1.start(); auth2.start()
        try:
            return function_app.teams_pricing_quote(FakeRequest(body=body))
        finally:
            p1.stop(); p2.stop(); auth1.stop(); auth2.stop(); flag.stop()

    def test_requires_authentication(self):
        self.assertEqual(self._call({"seats": 10}, authed=False).status_code, 401)

    def test_fails_closed_when_the_flag_is_absent(self):
        resp = self._call({"seats": 10}, enabled=False)
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(json.loads(resp.body)["error"], "TEAMS_CHECKOUT_DISABLED")

    def test_prices_ten_seats_at_the_contract_total(self):
        body = json.loads(self._call({"seats": 10}).body)
        # v2: every seat at $32.00, so one band and a total that divides evenly.
        self.assertEqual(body["total_cents"], 32_000)
        self.assertEqual(body["credits_per_seat"], 30)
        self.assertEqual(body["total_credits"], 300)
        self.assertEqual(len(body["breakdown"]), 1)

    def test_the_quote_is_persisted_with_everything_checkout_will_validate(self):
        cfg = {}
        resp = self._call({"seats": 25, "organization_id": ORG}, cfg=cfg)
        self.assertEqual(resp.status_code, 200)
        params = cfg["issued_quote_params"]
        self.assertIn(25, params)                              # seats
        self.assertIn(68_750, params)                          # total_cents (25 x 2750)
        self.assertIn(30, params)                              # credits_per_seat
        self.assertIn(teams_pricing.PRICING_VERSION, params)   # contract
        self.assertIn(ADMIN, [str(p) for p in params])         # owner
        self.assertIn(ORG, [str(p) for p in params])           # organization scope

    def test_quote_ids_are_unique_per_issue(self):
        a = json.loads(self._call({"seats": 12}).body)["quote_id"]
        b = json.loads(self._call({"seats": 12}).body)["quote_id"]
        self.assertNotEqual(a, b)
        uuid.UUID(a); uuid.UUID(b)

    def test_a_failed_persist_does_not_hand_back_an_unusable_quote(self):
        resp = self._call({"seats": 10}, cfg={"quote_insert_raises": True})
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(json.loads(resp.body)["error"], "QUOTE_NOT_ISSUED")

    def test_below_minimum_and_contact_sales(self):
        low = self._call({"seats": 9})
        self.assertEqual(low.status_code, 400)
        self.assertEqual(json.loads(low.body)["minimum_seats"], 10)
        high = self._call({"seats": 50})
        self.assertEqual(high.status_code, 200)
        self.assertTrue(json.loads(high.body)["contact_sales"])
        self.assertNotIn("total_cents", json.loads(high.body))

    def test_rejects_a_missing_or_malformed_seat_count(self):
        for body in ({}, {"seats": "many"}, {"seats": True}, {"seats": 10.5}):
            with self.subTest(body=body):
                self.assertEqual(self._call(body).status_code, 400)


# ═══════════════════════════════════════════════════════════════════════════
# _handle_org_payment — fulfilment must match what was authorised
# ═══════════════════════════════════════════════════════════════════════════
class WebhookFulfilment(unittest.TestCase):
    def _run(self, cfg, session):
        conn, p1, p2 = _patched(cfg)
        p1.start(); p2.start()
        try:
            function_app._handle_org_payment(session, "checkout_session:cs_1")
        finally:
            p1.stop(); p2.stop()
        return conn

    def test_matching_payment_is_fulfilled(self):
        cfg = {"checkout_snapshot": _snapshot(), "webhook_org_row": (ADMIN, 10)}
        conn = self._run(cfg, _paid_session())
        self.assertTrue(conn.committed)
        self.assertFalse(conn.rolled_back)
        self.assertTrue(cfg.get("live_slot_released"),
                        "a settled attempt must release the organization's live slot")

    def test_grants_the_authorised_thirty_not_the_stale_org_default(self):
        cfg = {"checkout_snapshot": _snapshot(credits=30), "webhook_org_row": (ADMIN, 10)}
        conn = self._run(cfg, _paid_session())
        self.assertTrue(conn.committed)
        self.assertTrue(
            any("UPDATE organizations SET credits_per_seat" in s for s, _ in cfg["executed"]),
            "or every later invitee is silently short-changed")
        grant = [p for s, p in cfg["executed"]
                 if "SET credits_granted = ?, credits_remaining = ?" in s]
        self.assertEqual(grant[0][0], 30)

    def test_seats_become_authoritative_at_fulfilment(self):
        cfg = {"checkout_snapshot": _snapshot(seats=25, total=68_750),
               "webhook_org_row": (ADMIN, 10)}
        self._run(cfg, _paid_session(amount=68_750, seats=25))
        self.assertTrue(
            any("UPDATE organizations SET seats_purchased" in s for s, _ in cfg["executed"]))

    def test_mismatches_fail_closed(self):
        cases = {
            "underpayment": (_snapshot(), _paid_session(amount=1_000)),
            "overpayment": (_snapshot(), _paid_session(amount=99_999)),
            "currency": (_snapshot(currency="usd"), _paid_session(currency="eur")),
            "seats": (_snapshot(seats=10), _paid_session(seats=25, amount=68_750)),
            "version": (_snapshot(version="teams_basic_v0"), _paid_session()),
            # A total no legal 10-seat order could carry under any supported version.
            "impossible snapshot": (_snapshot(seats=10, total=99_999),
                                    _paid_session(amount=99_999)),
            "other org": (_snapshot(org=OTHER_ORG), _paid_session(org=ORG)),
        }
        for name, (snap, session) in cases.items():
            with self.subTest(case=name):
                cfg = {"checkout_snapshot": snap, "webhook_org_row": (ADMIN, 10)}
                conn = self._run(cfg, session)
                self.assertTrue(conn.rolled_back)
                self.assertFalse(conn.committed)

    def test_already_paid_snapshot_is_a_no_op(self):
        cfg = {"checkout_snapshot": _snapshot(status="paid"),
               "webhook_org_row": (ADMIN, 10)}
        conn = self._run(cfg, _paid_session())
        self.assertTrue(conn.rolled_back)
        self.assertFalse(conn.committed)

    def test_duplicate_paid_webhook_cannot_grant_twice(self):
        cfg = {"claim_event_rowcount": 0, "checkout_snapshot": _snapshot(),
               "webhook_org_row": (ADMIN, 10)}
        conn = self._run(cfg, _paid_session())
        self.assertFalse(conn.committed)

    def test_failure_to_settle_the_attempt_fails_closed(self):
        cfg = {"checkout_snapshot": _snapshot(), "webhook_org_row": (ADMIN, 10),
               "snapshot_update_rowcount": 0}
        conn = self._run(cfg, _paid_session())
        self.assertTrue(conn.rolled_back)
        self.assertFalse(conn.committed)

    # ── Snapshot-less sessions: fail closed unless explicitly recorded ────
    def test_unknown_no_snapshot_session_grants_nothing(self):
        """THE blocker this replaced: 'no snapshot' must not mean 'fulfil anyway'."""
        cfg = {"checkout_snapshot": None, "legacy_allowlist_row": None,
               "webhook_org_row": (ADMIN, 10)}
        conn = self._run(cfg, _paid_session(session_id="cs_unknown"))
        self.assertTrue(conn.rolled_back)
        self.assertFalse(conn.committed)
        self.assertFalse(
            any("UPDATE organizations SET status = 'active'" in s for s, _ in cfg["executed"]),
            "an unknown session must never activate an organization")

    def test_signed_metadata_alone_is_not_proof(self):
        """A fully-formed, signed org session with no allowlist row is still refused."""
        cfg = {"checkout_snapshot": None, "legacy_allowlist_row": None,
               "webhook_org_row": (ADMIN, 10)}
        conn = self._run(cfg, _paid_session(session_id="cs_forged", created=1_600_000_000))
        self.assertFalse(conn.committed)

    def test_recorded_legacy_session_reconciles_once(self):
        cfg = {"checkout_snapshot": None,
               "legacy_allowlist_row": (ORG, datetime(2026, 1, 1), None, "open"),
               "webhook_org_row": (ADMIN, 10)}
        conn = self._run(cfg, _paid_session(session_id="cs_legacy"))
        self.assertTrue(conn.committed)

    def test_a_consumed_legacy_entry_cannot_grant_again(self):
        cfg = {"checkout_snapshot": None,
               "legacy_allowlist_row": (ORG, datetime(2026, 1, 1), None, "consumed"),
               "webhook_org_row": (ADMIN, 10)}
        conn = self._run(cfg, _paid_session(session_id="cs_legacy"))
        self.assertTrue(conn.rolled_back)
        self.assertFalse(conn.committed)

    def test_legacy_entry_for_another_org_is_refused(self):
        cfg = {"checkout_snapshot": None,
               "legacy_allowlist_row": (OTHER_ORG, datetime(2026, 1, 1), None, "open"),
               "webhook_org_row": (ADMIN, 10)}
        conn = self._run(cfg, _paid_session(session_id="cs_legacy"))
        self.assertFalse(conn.committed)

    def test_legacy_amount_must_match_the_recorded_inventory(self):
        cfg = {"checkout_snapshot": None,
               "legacy_allowlist_row": (ORG, datetime(2026, 1, 1), 20_000, "open"),
               "webhook_org_row": (ADMIN, 10)}
        conn = self._run(cfg, _paid_session(session_id="cs_legacy", amount=34_700))
        self.assertFalse(conn.committed)

    def test_a_session_created_after_the_cutoff_requires_a_snapshot(self):
        cutoff = "2026-08-01T00:00:00+00:00"
        after = int(datetime(2026, 8, 15, tzinfo=timezone.utc).timestamp())
        cfg = {"checkout_snapshot": None,
               "legacy_allowlist_row": (ORG, datetime(2026, 1, 1), None, "open"),
               "webhook_org_row": (ADMIN, 10)}
        with mock.patch.object(function_app, "TEAMS_SNAPSHOT_CUTOFF_UTC", cutoff):
            conn = self._run(cfg, _paid_session(session_id="cs_late", created=after))
        self.assertTrue(conn.rolled_back)
        self.assertFalse(conn.committed)

    def test_a_session_created_before_the_cutoff_is_admitted(self):
        cutoff = "2026-08-01T00:00:00+00:00"
        before = int(datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp())
        cfg = {"checkout_snapshot": None,
               "legacy_allowlist_row": (ORG, datetime(2026, 1, 1), None, "open"),
               "webhook_org_row": (ADMIN, 10)}
        with mock.patch.object(function_app, "TEAMS_SNAPSHOT_CUTOFF_UTC", cutoff):
            conn = self._run(cfg, _paid_session(session_id="cs_early", created=before))
        self.assertTrue(conn.committed)

    def test_legacy_replay_cannot_grant_twice(self):
        cfg = {"checkout_snapshot": None, "claim_event_rowcount": 0,
               "legacy_allowlist_row": (ORG, datetime(2026, 1, 1), None, "open"),
               "webhook_org_row": (ADMIN, 10)}
        conn = self._run(cfg, _paid_session(session_id="cs_legacy"))
        self.assertFalse(conn.committed)

    def test_legacy_grants_that_orgs_own_entitlement(self):
        cfg = {"checkout_snapshot": None,
               "legacy_allowlist_row": (ORG, datetime(2026, 1, 1), None, "open"),
               "webhook_org_row": (ADMIN, 10)}
        conn = self._run(cfg, _paid_session(session_id="cs_legacy"))
        self.assertTrue(conn.committed)
        grant = [p for s, p in cfg["executed"]
                 if "SET credits_granted = ?, credits_remaining = ?" in s]
        self.assertEqual(grant[0][0], 10)


# ═══════════════════════════════════════════════════════════════════════════
# Webhook state gate — only 'pending' may fulfil
# ═══════════════════════════════════════════════════════════════════════════
class FulfilmentStateGate(unittest.TestCase):
    """Money arriving against an attempt this server does not consider payable must
    grant nothing. An unfulfilled real payment is recoverable by hand; credits granted
    against a retired attempt are not."""

    def _run(self, snapshot_status):
        cfg = {"checkout_snapshot": _snapshot(status=snapshot_status),
               "webhook_org_row": (ADMIN, 10)}
        conn, p1, p2 = _patched(cfg)
        p1.start(); p2.start()
        try:
            function_app._handle_org_payment(_paid_session(), "checkout_session:cs_1")
        finally:
            p1.stop(); p2.stop()
        return conn, cfg

    def test_pending_is_the_only_state_that_fulfils(self):
        conn, _ = self._run("pending")
        self.assertTrue(conn.committed)

    def test_paid_is_an_idempotent_no_op(self):
        conn, cfg = self._run("paid")
        self.assertFalse(conn.committed)
        self.assertTrue(conn.rolled_back)
        self.assertFalse(any("credits_granted" in s for s, _ in cfg["executed"]))

    def test_every_non_payable_state_fails_closed(self):
        for state in ("cancelled", "expired", "failed", "creating",
                      "unknown_future_state", ""):
            with self.subTest(state=state):
                conn, cfg = self._run(state)
                self.assertFalse(conn.committed, f"{state} must not fulfil")
                self.assertTrue(conn.rolled_back)
                self.assertFalse(
                    any("UPDATE organizations SET status = 'active'" in s
                        for s, _ in cfg["executed"]),
                    f"{state} must not activate the organization")
                self.assertFalse(
                    any("SET credits_granted" in s for s, _ in cfg["executed"]),
                    f"{state} must not grant credits")

    def test_the_old_stripe_url_paid_after_a_local_cancellation_grants_nothing(self):
        """The concrete P0 scenario: a session we cancelled locally is paid anyway."""
        conn, cfg = self._run("cancelled")
        self.assertFalse(conn.committed)
        self.assertFalse(any("SET credits_granted" in s for s, _ in cfg["executed"]))


# ═══════════════════════════════════════════════════════════════════════════
# checkout.session.expired
# ═══════════════════════════════════════════════════════════════════════════
class CheckoutExpiredEvent(unittest.TestCase):
    def _run(self, cfg, session_id="cs_1"):
        conn, p1, p2 = _patched(cfg)
        p1.start(); p2.start()
        try:
            function_app._handle_org_checkout_expired(
                {"id": session_id, "metadata": {"payment_type": "org_seats"}}, "evt_exp")
        finally:
            p1.stop(); p2.stop()
        return conn

    def test_a_pending_attempt_is_expired_and_the_slot_released(self):
        cfg = {"attempt_by_session": (ATT1, ORG, "pending")}
        conn = self._run(cfg)
        self.assertTrue(conn.committed)
        self.assertTrue(cfg.get("live_slot_released"))

    def test_expiry_never_activates_or_grants(self):
        cfg = {"attempt_by_session": (ATT1, ORG, "pending")}
        self._run(cfg)
        for s, _ in cfg["executed"]:
            self.assertNotIn("SET credits_granted", s)
            self.assertNotIn("status = 'active'", s)

    def test_expiry_racing_a_paid_webhook_loses_to_the_payment(self):
        cfg = {"attempt_by_session": (ATT1, ORG, "paid")}
        conn = self._run(cfg)
        self.assertFalse(conn.committed)
        self.assertFalse(cfg.get("live_slot_released"),
                         "a paid attempt's slot must not be released by an expiry event")

    def test_a_lost_update_race_leaves_the_row_alone(self):
        """The row said 'pending' at read time but moved before the guarded UPDATE."""
        cfg = {"attempt_by_session": (ATT1, ORG, "pending"), "expire_rowcount": 0}
        conn = self._run(cfg)
        self.assertFalse(conn.committed)

    def test_duplicate_expiry_events_are_idempotent(self):
        cfg = {"attempt_by_session": (ATT1, ORG, "expired")}
        conn = self._run(cfg)
        self.assertFalse(conn.committed)

    def test_an_unknown_session_is_ignored(self):
        conn = self._run({"attempt_by_session": None})
        self.assertFalse(conn.committed)

    def test_a_non_teams_expiry_does_not_reach_this_handler(self):
        """Routing check: only payment_type='org_seats' is dispatched here."""
        import inspect
        src = inspect.getsource(function_app.stripe_webhook)
        self.assertIn('metadata.get("payment_type") == "org_seats"', src)
        self.assertIn("_handle_org_checkout_expired", src)


# ═══════════════════════════════════════════════════════════════════════════
# POST /orgs/{id}/checkout/cancel — Stripe first, its answer decides
# ═══════════════════════════════════════════════════════════════════════════
class CancelCheckout(unittest.TestCase):
    def _cancel(self, cfg, stripe_return=None, stripe_raises=None, caller=ADMIN):
        cfg = dict(cfg)
        cfg.setdefault("cancel_admin_row", (ADMIN,))
        conn, p1, p2 = _patched(cfg)
        auth1, auth2 = _auth_as(caller)
        patch = mock.patch.object(
            function_app, "expire_org_checkout_session",
            side_effect=stripe_raises,
            return_value=stripe_return or {"id": "cs_live", "status": "expired"})
        m = patch.start()
        p1.start(); p2.start(); auth1.start(); auth2.start()
        try:
            resp = function_app.cancel_org_checkout(
                FakeRequest(route_params={"organization_id": ORG}))
        finally:
            p1.stop(); p2.stop(); auth1.stop(); auth2.stop(); patch.stop()
        return resp, m, cfg, conn

    def test_stripe_confirmed_expiry_releases_the_slot(self):
        cfg = {"live_attempt_row": _live(ATT1)}
        resp, m, cfg, conn = self._cancel(cfg)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(json.loads(resp.body)["status"], "cancelled")
        m.assert_called_once_with("cs_live")
        self.assertTrue(cfg.get("live_slot_released"))

    def test_stripe_is_asked_BEFORE_any_local_state_change(self):
        cfg = {"live_attempt_row": _live(ATT1)}
        _, m, cfg, _ = self._cancel(cfg)
        m.assert_called_once()
        # No expiry UPDATE may precede the Stripe call — asserted by the failure cases
        # below, where Stripe refuses and nothing local changed.

    def test_a_complete_session_is_not_released(self):
        """Stripe says it is complete — the payment webhook owns that outcome."""
        cfg = {"live_attempt_row": _live(ATT1)}
        resp, _, cfg, _ = self._cancel(cfg, stripe_return={"status": "complete"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(json.loads(resp.body)["error"], "CHECKOUT_NOT_CANCELLABLE")
        self.assertFalse(cfg.get("live_slot_released"))

    def test_a_stripe_failure_retains_the_slot_and_fails_closed(self):
        cfg = {"live_attempt_row": _live(ATT1)}
        resp, _, cfg, _ = self._cancel(cfg, stripe_raises=TimeoutError("network"))
        self.assertEqual(resp.status_code, 502)
        self.assertFalse(cfg.get("live_slot_released"))

    def test_an_ambiguous_status_retains_the_slot(self):
        for status in ("open", "", None, "weird"):
            with self.subTest(status=status):
                cfg = {"live_attempt_row": _live(ATT1)}
                resp, _, cfg, _ = self._cancel(cfg, stripe_return={"status": status})
                self.assertEqual(resp.status_code, 409)
                self.assertFalse(cfg.get("live_slot_released"))

    def test_a_creating_attempt_cannot_be_cancelled_here(self):
        """No confirmed Stripe session to expire; recovery is a same-quote retry."""
        cfg = {"live_attempt_row": _live(ATT1, status="creating",
                                         url=None, session_id=None)}
        resp, m, _, _ = self._cancel(cfg)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(json.loads(resp.body)["error"], "CHECKOUT_NOT_CANCELLABLE")
        m.assert_not_called()

    def test_no_open_checkout_is_404(self):
        resp, m, _, _ = self._cancel({"live_attempt_row": None})
        self.assertEqual(resp.status_code, 404)
        m.assert_not_called()

    def test_a_non_admin_is_refused_before_stripe(self):
        cfg = {"live_attempt_row": _live(ATT1), "cancel_admin_row": (OTHER_USER,)}
        resp, m, _, _ = self._cancel(cfg)
        self.assertEqual(resp.status_code, 403)
        m.assert_not_called()

    def test_a_payment_winning_the_race_leaves_reconciliation_to_the_webhook(self):
        """Stripe expired it, but our row moved meanwhile — most likely to 'paid'."""
        cfg = {"live_attempt_row": _live(ATT1), "expire_rowcount": 0}
        resp, _, _, _ = self._cancel(cfg)
        self.assertEqual(resp.status_code, 202)
        self.assertTrue(json.loads(resp.body)["reconciling"])

    def test_requires_authentication(self):
        conn, p1, p2 = _patched({"live_attempt_row": _live(ATT1)})
        auth = mock.patch.object(function_app, "validate_token",
                                 side_effect=Exception("bad"))
        p1.start(); p2.start(); auth.start()
        try:
            resp = function_app.cancel_org_checkout(
                FakeRequest(route_params={"organization_id": ORG}))
        finally:
            p1.stop(); p2.stop(); auth.stop()
        self.assertEqual(resp.status_code, 401)


# ═══════════════════════════════════════════════════════════════════════════
# Bounded Stripe session lifetime
# ═══════════════════════════════════════════════════════════════════════════
class SessionExpiryWindow(unittest.TestCase):
    def test_the_requested_expiry_is_inside_stripes_accepted_range(self):
        now = datetime(2026, 8, 30, tzinfo=timezone.utc)
        expires = teams_checkout.checkout_session_expires_at(now)
        delta = (expires - now).total_seconds()
        self.assertGreaterEqual(delta, teams_checkout.STRIPE_MIN_SESSION_TTL_SECONDS)
        self.assertLessEqual(delta, teams_checkout.STRIPE_MAX_SESSION_TTL_SECONDS)

    def test_a_misconfigured_ttl_is_clamped_rather_than_sent_raw(self):
        now = datetime(2026, 8, 30, tzinfo=timezone.utc)
        for bad in (1, 0, 10 * 24 * 60 * 60):
            with self.subTest(ttl=bad):
                with mock.patch.object(teams_checkout, "CHECKOUT_SESSION_TTL_SECONDS", bad):
                    delta = (teams_checkout.checkout_session_expires_at(now)
                             - now).total_seconds()
                self.assertGreaterEqual(delta, teams_checkout.STRIPE_MIN_SESSION_TTL_SECONDS)
                self.assertLessEqual(delta, teams_checkout.STRIPE_MAX_SESSION_TTL_SECONDS)

    def test_checkout_sends_an_expiry_to_stripe_and_stores_the_same_one(self):
        resp, m, cfg, _ = _checkout({"quote_id": QUOTE}, cfg={"quote_row": _quote_row()})
        self.assertEqual(resp.status_code, 200)
        sent = m.call_args.kwargs["expires_at"]
        self.assertIsInstance(sent, int)
        insert = [p for s, p in cfg["executed"]
                  if "INSERT INTO organization_checkout_sessions" in s][0]
        stored = next(v for v in insert if isinstance(v, datetime))
        self.assertEqual(int(stored.replace(tzinfo=timezone.utc).timestamp()), sent)


# ═══════════════════════════════════════════════════════════════════════════
# GET /teams/checkout-status
# ═══════════════════════════════════════════════════════════════════════════
class FakeStatusRequest:
    def __init__(self, params=None, auth="Bearer test-token"):
        self.params = params or {}
        self.route_params = {}
        self.headers = {"Authorization": auth}

    def get_json(self):
        return {}


class CheckoutStatusEndpoint(unittest.TestCase):
    def _call(self, cfg, session_id="cs_1", caller=ADMIN):
        conn, p1, p2 = _patched(cfg)
        auth1, auth2 = _auth_as(caller)
        p1.start(); p2.start(); auth1.start(); auth2.start()
        try:
            return function_app.teams_checkout_status(
                FakeStatusRequest(params={"session_id": session_id}))
        finally:
            p1.stop(); p2.stop(); auth1.stop(); auth2.stop()

    def _row(self, session_status="paid", org_status="active", admin=ADMIN):
        return (ORG, session_status, 10, 30, 32_000, "usd",
                teams_pricing.PRICING_VERSION, org_status, admin)

    def test_requires_a_session_id(self):
        self.assertEqual(self._call({}, session_id="").status_code, 400)

    def test_unknown_session_is_404(self):
        self.assertEqual(self._call({"checkout_status_row": None}).status_code, 404)

    def test_another_admins_session_is_404_not_403(self):
        cfg = {"checkout_status_row": self._row(admin=OTHER_USER)}
        self.assertEqual(self._call(cfg).status_code, 404)

    def test_paid_only_when_the_org_is_actually_active(self):
        body = json.loads(self._call({"checkout_status_row": self._row()}).body)
        self.assertEqual(body["state"], "paid")
        self.assertEqual(body["credits_per_seat"], 30)

    def test_paid_snapshot_but_inactive_org_is_not_paid(self):
        cfg = {"checkout_status_row": self._row(org_status="pending_payment")}
        self.assertEqual(json.loads(self._call(cfg).body)["state"], "pending")

    def test_creating_and_pending_both_read_as_pending(self):
        for st in ("creating", "pending"):
            with self.subTest(status=st):
                cfg = {"checkout_status_row": self._row(st, "pending_payment")}
                self.assertEqual(json.loads(self._call(cfg).body)["state"], "pending")

    def test_terminal_states_are_reported_verbatim(self):
        for st in ("failed", "expired", "cancelled"):
            with self.subTest(status=st):
                cfg = {"checkout_status_row": self._row(st, "pending_payment")}
                self.assertEqual(json.loads(self._call(cfg).body)["state"], st)


# ═══════════════════════════════════════════════════════════════════════════
# E2E boundary + return-URL allowlist
# ═══════════════════════════════════════════════════════════════════════════
class PaidSeatCanGenerate(unittest.TestCase):
    """NO inference, GPU, queue or LoRA code is touched — this asserts the BOUNDARY:
    credits granted by fulfilment are the ones a member's generation spends."""

    def _reserve(self, cfg, credit_cost=1):
        from tests.test_org_teams import FakeConn
        from shared.job_reservation import reserve_job_slot
        conn = FakeConn(cfg)
        with mock.patch("shared.job_reservation.new_connection", return_value=conn), \
             mock.patch("shared.job_reservation.outbox_add", return_value=12345):
            result = reserve_job_slot(
                user_id="member-1", input_blob_path="", job_params="{}",
                per_user_cap=5, global_cap=25, credit_cost=credit_cost)
        return result, conn

    def test_a_paid_seat_holder_generates_from_their_own_thirty(self):
        cfg = {"applock_rc": 0, "membership_row": ("org-1", 30, "member-1"),
               "personal_credits": 0, "new_job_id": 77}
        result, conn = self._reserve(cfg)
        self.assertTrue(result.ok)
        sql = [s.lower() for s, _ in cfg["executed"]]
        self.assertEqual(len([s for s in sql if "update organization_members set credits_remaining = credits_remaining -" in s]), 1)
        self.assertEqual(len([s for s in sql if "update users set credits_remaining = credits_remaining -" in s]), 0)

    def test_the_full_thirty_image_session_is_affordable(self):
        cfg = {"applock_rc": 0, "membership_row": ("org-1", 30, "member-1"),
               "personal_credits": 0, "new_job_id": 78}
        self.assertTrue(self._reserve(cfg, credit_cost=30)[0].ok)

    def test_an_exhausted_seat_is_refused(self):
        cfg = {"applock_rc": 0, "membership_row": ("org-1", 0, "member-1"),
               "personal_credits": 999, "new_job_id": 79}
        self.assertFalse(self._reserve(cfg)[0].ok)

    def test_no_shared_pool_to_overdraw_onto(self):
        cfg = {"applock_rc": 0, "membership_row": ("org-1", 30, "member-1"),
               "personal_credits": 0, "new_job_id": 80}
        self.assertFalse(self._reserve(cfg, credit_cost=31)[0].ok)


class ReturnUrlAllowlist(unittest.TestCase):
    def test_unlisted_origin_falls_back_to_production(self):
        success, cancel = function_app._teams_checkout_urls({"origin": "https://evil.test"})
        self.assertTrue(success.startswith("https://bettersnap.ai/"))
        self.assertTrue(cancel.startswith("https://bettersnap.ai/"))

    def test_prefix_lookalike_is_rejected(self):
        success, _ = function_app._teams_checkout_urls(
            {"origin": "https://bettersnap.ai.evil.com"})
        self.assertNotIn("evil.com", success)

    def test_configured_origin_is_allowed(self):
        with mock.patch.dict(os.environ,
                             {"TEAMS_CHECKOUT_ALLOWED_ORIGINS": "http://localhost:5173"}):
            success, _ = function_app._teams_checkout_urls({"origin": "http://localhost:5173"})
        self.assertTrue(success.startswith("http://localhost:5173/"))

    def test_the_path_is_server_controlled(self):
        with mock.patch.dict(os.environ,
                             {"TEAMS_CHECKOUT_ALLOWED_ORIGINS": "http://localhost:5173"}):
            success, cancel = function_app._teams_checkout_urls(
                {"origin": "http://localhost:5173",
                 "success_url": "http://localhost:5173/anywhere?x=1"})
        self.assertIn(function_app.TEAMS_CHECKOUT_SUCCESS_PATH, success)
        self.assertNotIn("anywhere", success)
        self.assertNotIn("anywhere", cancel)

    def test_success_url_carries_the_stripe_session_placeholder(self):
        success, _ = function_app._teams_checkout_urls({})
        self.assertIn("{CHECKOUT_SESSION_ID}", success)


# ═══════════════════════════════════════════════════════════════════════════
# The band breakdown must survive the trip from quote to attempt
# ═══════════════════════════════════════════════════════════════════════════
class BreakdownTravelsFromQuoteToAttempt(unittest.TestCase):
    """A LIVE money bug this suite failed to catch, reproduced.

    reserve_attempt read quote_row.get("breakdown") -- a key load_quote_for_update never
    selected -- and `or []` turned that None into an empty list that looked entirely
    valid. So every attempt was written with an EMPTY breakdown, and every paid Teams
    checkout was then refused at fulfilment by validate_teams_basic_v1_snapshot ("the
    stored band breakdown is missing or malformed"): the customer was charged, the
    workspace stayed pending_payment, and no credits were granted. Observed in production
    on a real $347 payment (org 790E84DC, attempt 2B7E9865) whose quote held the correct
    two bands while its attempt held [].

    These fixtures previously omitted breakdown_json altogether, which is exactly why the
    suite agreed with the bug instead of catching it.
    """

    def _attempt_params(self, cfg):
        rows = [p for st, p in cfg["executed"]
                if "INSERT INTO organization_checkout_sessions" in st]
        self.assertEqual(len(rows), 1, "exactly one attempt row must be inserted")
        return rows[0]

    #: position of breakdown_json in reserve_attempt's INSERT parameter list
    BREAKDOWN_PARAM = 10

    def test_the_attempt_stores_the_quotes_real_bands(self):
        resp, m, cfg, _ = _checkout({"quote_id": QUOTE},
                                    cfg={"quote_row": _quote_row()})
        self.assertEqual(resp.status_code, 200)
        stored = json.loads(self._attempt_params(cfg)[self.BREAKDOWN_PARAM])
        self.assertEqual(stored, _v2_band(10))
        self.assertTrue(stored, "an empty breakdown makes the payment unfulfillable")

    def test_what_checkout_STORES_is_what_fulfilment_ACCEPTS(self):
        """Closes the loop. The create path and the webhook path are validated against
        the same bytes, so the two can never again disagree about what a valid attempt
        looks like."""
        resp, m, cfg, _ = _checkout({"quote_id": QUOTE},
                                    cfg={"quote_row": _quote_row()})
        stored = json.loads(self._attempt_params(cfg)[self.BREAKDOWN_PARAM])
        snapshot = {
            "plan_id": teams_pricing.PLAN_ID,
            "pricing_version": teams_pricing.PRICING_VERSION,
            "currency": "usd", "seats": 10, "credits_per_seat": 30,
            "expected_total_cents": 32_000, "breakdown": stored,
        }
        session = {"amount_total": 32_000, "currency": "usd"}
        self.assertEqual(
            teams_pricing.snapshot_validator(teams_pricing.PRICING_VERSION)(snapshot, session), [],
            "what checkout stored was rejected by fulfilment")

    def test_a_quote_with_no_breakdown_is_refused_before_stripe_is_called(self):
        """An attempt that could never be fulfilled must not be created, and the customer
        must never reach a payment page for it.

        This surfaces as a 500 rather than a mapped 4xx, which is right: once the loader
        carries the breakdown, a quote without one is a SERVER defect, not something the
        customer did. What matters is that it fails CLOSED -- no charge can be taken
        against terms fulfilment would later refuse."""
        for bad in (None, [], "", "not json"):
            with self.subTest(breakdown=bad):
                resp, m, cfg, _ = _checkout(
                    {"quote_id": QUOTE},
                    cfg={"quote_row": _quote_row(breakdown=bad)})
                self.assertGreaterEqual(resp.status_code, 400)
                m.assert_not_called()          # no Stripe session, so no way to pay
                self.assertFalse(
                    [st for st, _ in cfg["executed"]
                     if "INSERT INTO organization_checkout_sessions" in st],
                    "no unfulfillable attempt may be recorded")

if __name__ == "__main__":
    unittest.main()
