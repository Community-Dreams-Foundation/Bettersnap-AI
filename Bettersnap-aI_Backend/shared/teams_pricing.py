"""Single source of truth for BetterSnap **Teams** pricing.

PURE MODULE — no Stripe, no DB, no HTTP, no environment reads. It answers exactly one
question: "for N seats, what is owed, in integer cents, and how was that derived?"
Everything that charges money (Stripe line items, webhook validation, fulfilment)
derives from `quote()` so there is one arithmetic definition, not four.

Contract (approved 2026-08-29), pricing version `teams_basic_v1`:

  • ONE Teams plan: Basic. ONE-TIME payment, not a subscription.
  • Each PAID SEAT receives exactly 30 headshots, assigned STRICTLY PER MEMBER.
    Teams credits are never pooled — a seat's 30 belong to that member alone.
  • Minimum online purchase: 10 seats.
  • GRADUATED (marginal) pricing — each band prices only the seats that fall INSIDE it,
    exactly like income-tax brackets:

        seats  1 - 9    $35 each
        seats 10 - 24   $32 each
        seats 25 - 49   $29 each
        seats 50 +      Contact Sales — online checkout BLOCKED

    So a 10-seat team pays 9x$35 + 1x$32 = $347, NOT 10x$32 = $320.

WHY GRADUATED, NOT BANDED-FLAT. A flat per-band price inverts at the boundary with
these numbers: 24 seats x $32 = $768 but 25 seats x $29 = $725, so buying MORE seats
would cost LESS. Graduated pricing is monotonic by construction — the total is a sum of
non-negative band subtotals, so adding a seat can never reduce the bill. The test module
asserts that monotonicity across the whole legal range rather than trusting this prose.

WHY INTEGER CENTS EVERYWHERE. Stripe charges integer cents. Any float in the chain
risks a total that disagrees with what was authorised, which the webhook would then
reject as a mismatch. There is no float arithmetic in this module.
"""
from dataclasses import dataclass
from typing import Any, Dict, Tuple

# Stable identifiers. PRICING_VERSION is STAMPED INTO every quote, Stripe session and
# payment row: fulfilment re-computes the quote and refuses to grant credits if the
# version it stored no longer matches. Bump it whenever any number below changes, so
# in-flight checkouts priced under the old contract fail closed instead of silently
# fulfilling at a stale price.
PRICING_VERSION = "teams_basic_v1"
PLAN_ID = "teams_basic"
PLAN_NAME = "BetterSnap Teams - Basic"
CURRENCY = "usd"

# Headshots granted to EACH paid seat. Per-member, never pooled.
CREDITS_PER_SEAT = 30

# Online self-serve purchase window. Below MIN -> not purchasable; at/above
# CONTACT_SALES_MIN -> human sales motion, checkout blocked.
MIN_SEATS = 10
CONTACT_SALES_MIN = 50
MAX_SEATS = CONTACT_SALES_MIN - 1  # 49, the largest self-serve order


@dataclass(frozen=True)
class Band:
    """One marginal price band. `upper` is INCLUSIVE."""
    lower: int          # first seat number in this band (1-based)
    upper: int          # last seat number in this band, inclusive
    unit_cents: int     # price of each seat that falls inside this band


# Ordered, contiguous, 1-based. Validated by _assert_bands_wellformed at import.
BANDS: Tuple[Band, ...] = (
    Band(lower=1,  upper=9,  unit_cents=3500),
    Band(lower=10, upper=24, unit_cents=3200),
    Band(lower=25, upper=49, unit_cents=2900),
)


def _assert_bands_wellformed() -> None:
    """Fail at IMPORT time if the table is malformed.

    A gap or overlap would silently under- or over-charge every customer, so this is a
    hard failure at process start rather than a test someone might forget to run.
    """
    if not BANDS:
        raise ValueError("teams_pricing: BANDS must not be empty")
    if BANDS[0].lower != 1:
        raise ValueError("teams_pricing: BANDS must start at seat 1")
    for i, b in enumerate(BANDS):
        if b.upper < b.lower:
            raise ValueError(f"teams_pricing: band {i} is inverted ({b.lower}..{b.upper})")
        if b.unit_cents < 0:
            raise ValueError(f"teams_pricing: band {i} has a negative unit price")
        if i and BANDS[i - 1].upper + 1 != b.lower:
            raise ValueError(
                f"teams_pricing: bands {i - 1} and {i} are not contiguous "
                f"({BANDS[i - 1].upper} -> {b.lower})"
            )
    if BANDS[-1].upper != MAX_SEATS:
        raise ValueError(
            f"teams_pricing: last band must end at MAX_SEATS={MAX_SEATS}, "
            f"got {BANDS[-1].upper}"
        )


_assert_bands_wellformed()


# ── Errors ───────────────────────────────────────────────────────────────────
# Distinct types, because the HTTP layer maps them to DIFFERENT responses: an invalid
# seat count is a 400, while 50+ is a 200 carrying a Contact Sales payload. Collapsing
# them into one exception would make the endpoint guess from a message string.

class TeamsPricingError(Exception):
    """Base for every pricing rejection. Carries a stable machine-readable `code`."""
    code = "TEAMS_PRICING_ERROR"


class InvalidSeatCount(TeamsPricingError):
    """Seats is not a whole number (wrong type, bool, or < 1)."""
    code = "INVALID_SEAT_COUNT"


class BelowMinimumSeats(TeamsPricingError):
    """A real seat count, but under the 10-seat online minimum."""
    code = "BELOW_MINIMUM_SEATS"

    def __init__(self, seats: int):
        super().__init__(
            f"Teams checkout requires at least {MIN_SEATS} seats; got {seats}."
        )
        self.seats = seats
        self.minimum = MIN_SEATS


class ContactSalesRequired(TeamsPricingError):
    """50+ seats — online checkout is deliberately blocked."""
    code = "CONTACT_SALES_REQUIRED"

    def __init__(self, seats: int):
        super().__init__(
            f"Teams orders of {CONTACT_SALES_MIN} or more seats are handled by sales; "
            f"got {seats}."
        )
        self.seats = seats
        self.threshold = CONTACT_SALES_MIN


# ── Results ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BandCharge:
    """The seats of one order that landed in one band, and what they cost."""
    lower: int
    upper: int            # inclusive BAND bound (not the order's seat count)
    unit_cents: int
    seats: int            # how many of THIS order's seats fell in this band
    subtotal_cents: int   # seats * unit_cents

    def to_dict(self) -> Dict[str, Any]:
        return {
            "from_seat": self.lower,
            "to_seat": self.upper,
            "unit_price_cents": self.unit_cents,
            "seats": self.seats,
            "subtotal_cents": self.subtotal_cents,
        }


@dataclass(frozen=True)
class TeamsQuote:
    """A complete, self-describing price. `total_cents` is the ONLY authoritative amount."""
    plan_id: str
    plan_name: str
    pricing_version: str
    currency: str
    seats: int
    credits_per_seat: int
    total_credits: int
    total_cents: int
    # DISPLAY ONLY, rounded to the nearest cent — 24 seats is $795/24 = $33.125, which is
    # not representable in cents. Never charge or validate against this; it exists so the
    # UI can say "about $33/seat". total_cents is the number that gets authorised.
    effective_price_per_seat_cents: int
    breakdown: Tuple[BandCharge, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "plan_name": self.plan_name,
            "pricing_version": self.pricing_version,
            "currency": self.currency,
            "seats": self.seats,
            "credits_per_seat": self.credits_per_seat,
            "total_credits": self.total_credits,
            "total_cents": self.total_cents,
            "effective_price_per_seat_cents": self.effective_price_per_seat_cents,
            "breakdown": [b.to_dict() for b in self.breakdown],
        }


# ── The one arithmetic definition ────────────────────────────────────────────

def normalize_seats(seats: Any) -> int:
    """Coerce/validate a caller-supplied seat count to a positive whole number.

    Rejects bools EXPLICITLY. `isinstance(True, int)` is True in Python, so without this
    guard quote(True) would price a 1-seat order and quote(False) a 0-seat one. A JSON
    body deserializes `true` to a bool, so this is reachable straight off the wire.

    Accepts a float or a string ONLY when it is exactly integral (10.0, "10") — a seat
    count is a count of people; 10.5 seats is a client bug, not something to round away.
    """
    if isinstance(seats, bool):
        raise InvalidSeatCount("Seat count must be a whole number, not a boolean.")
    if isinstance(seats, int):
        value = seats
    elif isinstance(seats, float):
        if not seats.is_integer():
            raise InvalidSeatCount(f"Seat count must be a whole number; got {seats}.")
        value = int(seats)
    elif isinstance(seats, str):
        try:
            value = int(seats.strip())
        except (TypeError, ValueError):
            raise InvalidSeatCount(f"Seat count must be a whole number; got {seats!r}.")
    else:
        raise InvalidSeatCount(
            f"Seat count must be a whole number; got {type(seats).__name__}."
        )
    if value < 1:
        raise InvalidSeatCount(f"Seat count must be at least 1; got {value}.")
    return value


def quote(seats: Any) -> TeamsQuote:
    """Price `seats` under PRICING_VERSION, or raise.

    Raises InvalidSeatCount (not a whole number >= 1), BelowMinimumSeats (1..9), or
    ContactSalesRequired (>= 50). Every legal order returns a complete TeamsQuote.
    """
    count = normalize_seats(seats)
    if count >= CONTACT_SALES_MIN:
        raise ContactSalesRequired(count)
    if count < MIN_SEATS:
        raise BelowMinimumSeats(count)

    charges = []
    total = 0
    for band in BANDS:
        # Seats of THIS order falling inside this band. Bands entirely above the order
        # yield <= 0 and contribute nothing.
        in_band = min(count, band.upper) - band.lower + 1
        if in_band <= 0:
            continue
        subtotal = in_band * band.unit_cents
        total += subtotal
        charges.append(BandCharge(
            lower=band.lower, upper=band.upper, unit_cents=band.unit_cents,
            seats=in_band, subtotal_cents=subtotal,
        ))

    # Round half up in integer arithmetic — no float, so no representation drift.
    effective = (total + count // 2) // count

    return TeamsQuote(
        plan_id=PLAN_ID,
        plan_name=PLAN_NAME,
        pricing_version=PRICING_VERSION,
        currency=CURRENCY,
        seats=count,
        credits_per_seat=CREDITS_PER_SEAT,
        total_credits=count * CREDITS_PER_SEAT,
        total_cents=total,
        effective_price_per_seat_cents=effective,
        breakdown=tuple(charges),
    )


def price_cents(seats: Any) -> int:
    """Total owed, in integer cents, for a legal self-serve order. See `quote`."""
    return quote(seats).total_cents


def quote_from_snapshot(
    *, seats: int, credits_per_seat: int, total_cents: int, breakdown,
    pricing_version: str, plan_id: str = PLAN_ID, plan_name: str = PLAN_NAME,
    currency: str = CURRENCY,
) -> TeamsQuote:
    """Rebuild a quote from STORED values, recomputing nothing.

    Used only to replay a Stripe request that was already authorised. It must NOT call
    `quote()`: that would price the order under today's contract, so an attempt reserved
    yesterday would be replayed at a price the customer never agreed to — and the webhook,
    which validates against the stored snapshot, would then reject the payment it caused.
    Every number here comes from the immutable record.

    `breakdown` is the persisted band list (dicts as written by `BandCharge.to_dict`).
    The subtotals are asserted to sum to `total_cents`, so a corrupted snapshot fails
    loudly here rather than silently charging a different amount.
    """
    bands = []
    for b in breakdown or []:
        lower = int(b["from_seat"])
        count = int(b["seats"])
        bands.append(BandCharge(
            lower=lower, upper=int(b.get("to_seat", lower + count - 1)),
            unit_cents=int(b["unit_price_cents"]), seats=count,
            subtotal_cents=int(b["subtotal_cents"]),
        ))
    summed = sum(b.subtotal_cents for b in bands)
    if not bands or summed != int(total_cents):
        raise ValueError(
            f"teams_pricing: stored breakdown sums to {summed} but the authorised total "
            f"is {total_cents}; refusing to rebuild the quote"
        )

    count = int(seats)
    return TeamsQuote(
        plan_id=plan_id,
        plan_name=plan_name,
        pricing_version=pricing_version,
        currency=currency,
        seats=count,
        credits_per_seat=int(credits_per_seat),
        total_credits=count * int(credits_per_seat),
        total_cents=int(total_cents),
        effective_price_per_seat_cents=(int(total_cents) + count // 2) // count,
        breakdown=tuple(bands),
    )


# ── Versioned fulfilment validation ──────────────────────────────────────────
#
# PRICING_VERSION is the version NEW quotes are issued under. It is emphatically NOT the
# set of versions that may be FULFILLED. Conflating the two is a live money bug: a
# customer quoted and charged under v1 would, the moment v2 shipped, have their payment
# validated against v2 and refused — money taken, workspace never activated.
#
# So fulfilment resolves a validator by the version STORED on the attempt, from an
# explicit registry. Each validator encodes its own contract as FROZEN CONSTANTS and
# never calls quote(), because quote() always means "today's prices". A version absent
# from the registry (unknown, or deliberately retired) fails closed.

CURRENT_PRICING_VERSION = PRICING_VERSION

#: teams_basic_v1, frozen. Copied by value, never referenced from the live tables above —
#: editing BANDS for v2 must not silently redefine what v1 meant.
_V1_SPEC = {
    "plan_id": "teams_basic",
    "currencies": frozenset({"usd"}),
    "min_seats": 10,
    "max_seats": 49,
    "credits_per_seat": 30,
    "bands": ((1, 9, 3500), (10, 24, 3200), (25, 49, 2900)),
}


def _v1_unit_price_for_seat(seat_number: int):
    for lower, upper, unit in _V1_SPEC["bands"]:
        if lower <= seat_number <= upper:
            return unit
    return None


def validate_teams_basic_v1_snapshot(snapshot: Dict[str, Any],
                                     session: Dict[str, Any]) -> list:
    """Prove a `teams_basic_v1` payment matches what was authorised. Returns problems.

    Self-contained by design: every rule below is checked against `_V1_SPEC`, so this
    keeps returning the right answer for a v1 order long after v2 has become current.
    """
    problems = []
    spec = _V1_SPEC

    if snapshot.get("plan_id") != spec["plan_id"]:
        problems.append(
            f"plan_id {snapshot.get('plan_id')!r} is not the v1 Basic plan")

    currency = (snapshot.get("currency") or "").lower()
    if currency not in spec["currencies"]:
        problems.append(f"currency {currency!r} is not supported under v1")

    try:
        seats = int(snapshot.get("seats"))
    except (TypeError, ValueError):
        problems.append(f"seats {snapshot.get('seats')!r} is not a whole number")
        return problems
    if not (spec["min_seats"] <= seats <= spec["max_seats"]):
        problems.append(
            f"seats {seats} is outside the v1 self-serve range "
            f"{spec['min_seats']}..{spec['max_seats']}")

    if snapshot.get("credits_per_seat") != spec["credits_per_seat"]:
        problems.append(
            f"credits_per_seat {snapshot.get('credits_per_seat')!r} is not "
            f"{spec['credits_per_seat']} as v1 requires")

    try:
        total = int(snapshot.get("expected_total_cents"))
    except (TypeError, ValueError):
        problems.append("authorised total is not a whole number of cents")
        return problems

    # ── Band breakdown: contiguous, non-overlapping, complete, correctly priced ──
    bands = snapshot.get("breakdown")
    if not isinstance(bands, list) or not bands:
        problems.append("the stored band breakdown is missing or malformed")
        return problems

    covered = 0          # seats accounted for so far
    running = 0          # subtotal accumulator
    expected_next = 1    # the seat number the next band must start at
    for i, band in enumerate(bands):
        try:
            lower = int(band["from_seat"])
            count = int(band["seats"])
            unit = int(band["unit_price_cents"])
            subtotal = int(band["subtotal_cents"])
        except (KeyError, TypeError, ValueError):
            problems.append(f"band {i} is malformed")
            return problems
        if count <= 0:
            problems.append(f"band {i} covers {count} seats")
            return problems
        if lower != expected_next:
            problems.append(
                f"band {i} starts at seat {lower}, expected {expected_next} "
                f"({'gap' if lower > expected_next else 'overlap'})")
            return problems
        if subtotal != unit * count:
            problems.append(
                f"band {i} subtotal {subtotal} != {unit} x {count}")
        band_unit = _v1_unit_price_for_seat(lower)
        last_unit = _v1_unit_price_for_seat(lower + count - 1)
        if band_unit is None or last_unit is None:
            problems.append(f"band {i} falls outside the v1 price table")
        elif band_unit != last_unit:
            problems.append(f"band {i} spans two v1 price bands")
        elif unit != band_unit:
            problems.append(
                f"band {i} unit price {unit} != v1 price {band_unit} for seat {lower}")
        covered += count
        running += subtotal
        expected_next = lower + count

    if covered != seats:
        problems.append(
            f"bands cover {covered} seats but {seats} were purchased "
            f"({'missing' if covered < seats else 'duplicate'} seats)")
    if running != total:
        problems.append(f"band subtotals sum to {running}, authorised total is {total}")

    # ── What Stripe actually reported ───────────────────────────────────────
    if session.get("amount_total") != total:
        problems.append(
            f"Stripe amount {session.get('amount_total')} != authorised {total}")
    stripe_currency = (session.get("currency") or "").lower()
    if stripe_currency != currency:
        problems.append(
            f"Stripe currency {stripe_currency!r} != authorised {currency!r}")

    metadata = session.get("metadata", {}) or {}
    for field, expected in (("seats", seats),
                            ("credits_per_seat", spec["credits_per_seat"])):
        raw = metadata.get(field)
        if raw is None:
            continue  # absent metadata is not evidence of tampering; the snapshot rules
        try:
            if int(raw) != int(expected):
                problems.append(f"Stripe metadata {field}={raw} != authorised {expected}")
        except (TypeError, ValueError):
            problems.append(f"Stripe metadata {field}={raw!r} is not an integer")

    return problems


#: The ONLY versions that may be recovered and fulfilled. Anything not listed here —
#: unknown, or deliberately retired — fails closed. Add a v2 entry alongside v1 when v2
#: ships; do NOT replace v1 while v1 orders may still be in flight.
SUPPORTED_PRICING_VERSIONS = {
    "teams_basic_v1": validate_teams_basic_v1_snapshot,
}


def snapshot_validator(pricing_version: str):
    """The validator for a stored version, or None if that version is not supported."""
    return SUPPORTED_PRICING_VERSIONS.get(pricing_version)


def contact_sales_payload(seats: int) -> Dict[str, Any]:
    """Structured body for a 50+ enquiry. Deliberately carries NO price."""
    return {
        "contact_sales": True,
        "code": ContactSalesRequired.code,
        "seats": seats,
        "threshold": CONTACT_SALES_MIN,
        "pricing_version": PRICING_VERSION,
        "message": (
            f"Teams orders of {CONTACT_SALES_MIN}+ seats are priced individually. "
            "Our team will help you finalise pricing."
        ),
    }
