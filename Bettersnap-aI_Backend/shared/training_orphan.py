"""Durable marker for a training that cannot be closed normally.

WHY THIS EXISTS
`lora_trainings.user_id` is NOT NULL but has NO foreign key to `users` (migration 004;
migration 022 records the convention: "carry NO FK, matching lora_trainings (004). Validate in
application code"). So a training can outlive its owner, and when it does:

  * `UPDATE users SET lora_status = ...`      matches 0 rows
  * `UPDATE users SET ...credits...`          matches 0 rows

Rolling back and retrying was WRONG. Once a users UPDATE has returned rowcount 0 the row is
PROVEN absent; running the same statement again cannot recreate it. Retrying forever just
produces one CRITICAL per watcher tick and never terminalizes the run.

So the condition is recorded ONCE, durably, and the training is closed.

AMOUNTS ARE VALIDATED, NEVER COERCED
Migration 026 declares monthly_credit_cost / one_time_credit_cost as `INT NOT NULL DEFAULT 0`
with NO CHECK constraint, so a negative value is representable in the schema. An earlier
version of this module used `max(0, int(value or 0))`, which would have silently clamped a
corrupt -20 to 0 and then labelled the training a FREE orphan -- turning an accounting
corruption into a confident claim that nothing was owed.

A charge is now accepted only when `type(value) is int and value >= 0`. `type(...) is int`
rather than `isinstance(...)` because bool subclasses int, and True would otherwise be read as
a charge of 1. Anything else -- bool, float, str, Decimal, None, negative -- is REJECTED, and
the training is closed as `accounting_invalid` with `aggregate_owed: null`: we do not know what
is owed and will not pretend to.

WHERE IT IS STORED, AND WHY THAT IS SAFE
`lora_trainings.error` is NVARCHAR(1000) and already survives the terminal transition, so no
new column and no migration are needed -- but only under three guarantees this module provides:

  1. THE MARKER GOES FIRST. The ordinary terminal write uses `error = COALESCE(error, ?)`,
     which KEEPS a root cause already recorded by _record_training_error. A marker appended
     behind that would frequently never be stored at all. This path therefore writes
     `error = ?` with the marker at offset 0.
  2. THE MARKER CANNOT BE TRUNCATED. It is built first, its length is checked against the
     column budget, and only the ORIGINAL error is trimmed to fit after it. The diagnostic
     `observed` block is dropped before the marker itself is ever at risk, and a marker that
     still cannot fit raises rather than being silently cut.
  3. IT PARSES DETERMINISTICALLY. The payload is a JSON object read with raw_decode, which
     stops exactly at the closing brace, so arbitrary original-error text after the separator
     -- including braces, quotes or another ' | ' -- cannot confuse the parser.

Operator queries -- TWO conditions, TWO prefixes, deliberately not conflated:

    -- the owning users row is gone
    SELECT * FROM dbo.lora_trainings WHERE error LIKE 'ORPHAN_USER:%';
    -- the user EXISTS but the stored retrain charge is not a trustworthy amount
    SELECT * FROM dbo.lora_trainings WHERE error LIKE 'TRAINING_ACCOUNTING_INVALID:%';

The predicate is ANCHORED at offset 0, so it is sargable in principle. It is NOT currently
index-backed: no index on `lora_trainings.error` exists, so this is a table scan today. That is
acceptable at the current row count and is called out here so nobody assumes otherwise.

PURE module: no DB, no Azure, no I/O.
"""
import json

# Anchored at offset 0 so `LIKE 'ORPHAN_USER:%'` is a prefix comparison rather than a search
# for a substring anywhere in the column.
MARKER_PREFIX = "ORPHAN_USER:"
# A DISTINCT prefix, because this is a different condition with a different lifecycle. Storing
# an existing user's corrupt charge metadata under ORPHAN_USER: falsely claimed the user was
# gone, and sent an operator looking for a deleted row that is sitting right there.
ACCOUNTING_MARKER_PREFIX = "TRAINING_ACCOUNTING_INVALID:"
SEPARATOR = " | "

# dbo.lora_trainings.error is NVARCHAR(1000) (migration 004).
ERROR_COLUMN_MAX = 1000

# Bounds the diagnostic rendering of a rejected value so it can never crowd out the marker.
OBSERVED_VALUE_MAX = 32

# Payload shape version. Present so a future reader can tell which fields to expect instead of
# inferring it from which keys happen to be there.
SCHEMA_VERSION = 1

# Why the training could not be closed normally. Exactly one applies, and each is implied by
# the validated amounts -- see _reason_for.
REASON_FREE = "free_training_user_missing"        # amounts valid, both zero: nothing owed
REASON_PAID = "paid_training_user_missing"        # amounts valid, aggregate > 0: owed, unpayable
REASON_INVALID = "accounting_invalid"             # amounts NOT valid: owed amount is UNKNOWN

_VALID_REASONS = (REASON_FREE, REASON_PAID, REASON_INVALID)


class OrphanMarkerTooLong(RuntimeError):
    """The structured marker alone exceeds the column budget.

    Never silently truncated: a half-written marker is worse than none, because it would parse
    as absent while looking present."""


class ReasonAmountMismatch(ValueError):
    """The reason contradicts the validated amounts.

    free must mean both amounts are zero; paid must mean the aggregate is positive;
    accounting_invalid must mean the amounts could not be validated. Allowing any other pairing
    would let the marker assert something the numbers do not support."""


def is_valid_charge(value):
    """A charge amount is trustworthy ONLY as a non-negative plain int.

    `type(value) is int` deliberately, not isinstance: bool subclasses int, so True would
    otherwise be accepted as a charge of 1. float/str/Decimal/None are rejected rather than
    converted — coercion is how a corrupt value becomes a confident number."""
    return type(value) is int and value >= 0


def _describe(value):
    """A bounded, safe rendering of a rejected value, for operator diagnosis."""
    return {"type": type(value).__name__, "repr": repr(value)[:OBSERVED_VALUE_MAX]}


def _reason_for(monthly_ok, one_time_ok, monthly, one_time):
    if not (monthly_ok and one_time_ok):
        return REASON_INVALID
    return REASON_PAID if (monthly + one_time) > 0 else REASON_FREE


def check_reason(reason, monthly_owed, one_time_owed, amounts_valid):
    """Raise unless the reason is exactly the one the amounts imply."""
    if reason not in _VALID_REASONS:
        raise ReasonAmountMismatch("unknown orphan reason %r" % (reason,))
    if not amounts_valid:
        if reason != REASON_INVALID:
            raise ReasonAmountMismatch(
                "amounts could not be validated, so the reason must be %s, not %r"
                % (REASON_INVALID, reason))
        return
    if reason == REASON_INVALID:
        raise ReasonAmountMismatch(
            "amounts validated cleanly, so %s is not applicable" % REASON_INVALID)
    aggregate = monthly_owed + one_time_owed
    if reason == REASON_FREE and aggregate != 0:
        raise ReasonAmountMismatch(
            "%s requires both amounts to be zero, got monthly=%s one_time=%s"
            % (REASON_FREE, monthly_owed, one_time_owed))
    if reason == REASON_PAID and aggregate <= 0:
        raise ReasonAmountMismatch(
            "%s requires a positive aggregate, got %s" % (REASON_PAID, aggregate))


def build_accounting_invalid_marker(training_id, user_id, *, monthly_owed, one_time_owed,
                                    original_error=None, column_max=ERROR_COLUMN_MAX):
    """Marker for an EXISTING user whose stored retrain charge cannot be trusted.

    Separate prefix and separate lifecycle from the orphan case. The user is present, so the
    training must still resolve their LoRA state and their parked jobs — only the REFUND is
    withheld, because the amount is unknown. See _finish_training.
    """
    return _build(ACCOUNTING_MARKER_PREFIX, training_id, user_id,
                  monthly_owed=monthly_owed, one_time_owed=one_time_owed,
                  original_error=original_error, column_max=column_max,
                  force_invalid=True)


def build_orphan_marker(training_id, user_id, *, monthly_owed=0, one_time_owed=0,
                        original_error=None, column_max=ERROR_COLUMN_MAX):
    """Marker for a training whose owning users row DOES NOT EXIST.

    The REASON IS DERIVED from the validated amounts, never passed in — that is what makes a
    reason/amount mismatch unrepresentable rather than merely discouraged.

    Returns (marker_text, payload). When either amount fails validation the payload carries
    `amounts_valid: false`, `aggregate_owed: null` (NOT zero, and not a guess), and a bounded
    `observed` block naming the offending type and value.
    """
    return _build(MARKER_PREFIX, training_id, user_id, monthly_owed=monthly_owed,
                  one_time_owed=one_time_owed, original_error=original_error,
                  column_max=column_max)


def _build(prefix, training_id, user_id, *, monthly_owed, one_time_owed,
           original_error=None, column_max=ERROR_COLUMN_MAX, force_invalid=False):
    """Shared construction. The PREFIX names the lifecycle (missing user vs corrupt amounts);
    the REASON is derived from the validated amounts and describes what is owed."""
    monthly_ok = is_valid_charge(monthly_owed) and not force_invalid
    one_time_ok = is_valid_charge(one_time_owed) and not force_invalid
    amounts_valid = monthly_ok and one_time_ok
    reason = _reason_for(monthly_ok, one_time_ok,
                         monthly_owed if monthly_ok else 0,
                         one_time_owed if one_time_ok else 0)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "training_id": str(training_id),
        "user_id": str(user_id),
        "amounts_valid": amounts_valid,
        "reason": reason,
    }
    if amounts_valid:
        check_reason(reason, monthly_owed, one_time_owed, True)
        payload["monthly_owed"] = monthly_owed
        payload["one_time_owed"] = one_time_owed
        payload["aggregate_owed"] = monthly_owed + one_time_owed
    else:
        # NULL, not 0: the amount owed is UNKNOWN, and claiming zero would read as "nothing
        # was charged" — exactly the mislabelling this guard exists to prevent.
        payload["monthly_owed"] = None
        payload["one_time_owed"] = None
        payload["aggregate_owed"] = None
        payload["observed"] = {}
        if not monthly_ok:
            payload["observed"]["monthly_credit_cost"] = _describe(monthly_owed)
        if not one_time_ok:
            payload["observed"]["one_time_credit_cost"] = _describe(one_time_owed)

    marker = _render(prefix, payload)
    if len(marker) > column_max:
        # Shed the DIAGNOSTIC block before the marker itself is ever at risk. The amounts and
        # the reason are load-bearing; `observed` is a convenience.
        payload.pop("observed", None)
        marker = _render(prefix, payload)
    if len(marker) > column_max:
        raise OrphanMarkerTooLong(
            "orphan marker is %d chars, over the %d-char budget for lora_trainings.error"
            % (len(marker), column_max))

    tail = (original_error or "").strip()
    if not tail:
        return marker, payload
    room = column_max - len(marker) - len(SEPARATOR)
    if room <= 0:
        # The marker fits but nothing else does. Keeping the marker whole is the priority.
        return marker, payload
    return marker + SEPARATOR + tail[:room], payload


def _render(prefix, payload):
    return prefix + json.dumps(payload, separators=(",", ":"), sort_keys=True)


def is_orphan_marker(text):
    """True ONLY for the missing-user marker."""
    return bool(text) and str(text).startswith(MARKER_PREFIX)


def is_accounting_marker(text):
    """True ONLY for the existing-user corrupt-amounts marker."""
    return bool(text) and str(text).startswith(ACCOUNTING_MARKER_PREFIX)


def marker_prefix_of(text):
    """Which marker (if any) this error text carries."""
    for prefix in (ACCOUNTING_MARKER_PREFIX, MARKER_PREFIX):
        if text and str(text).startswith(prefix):
            return prefix
    return None


def parse_marker(text):
    """Payload of EITHER marker, or None. The two prefixes are disjoint, so this cannot
    confuse a missing-user case with an accounting-corruption one."""
    prefix = marker_prefix_of(text)
    if prefix is None:
        return None
    try:
        payload, _end = json.JSONDecoder().raw_decode(str(text), len(prefix))
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def parse_orphan_marker(text):
    """The structured payload, or None when this is not an orphan marker.

    raw_decode stops at the JSON object's closing brace, so whatever the preserved original
    error contains cannot affect parsing."""
    if not is_orphan_marker(text):
        return None
    try:
        payload, _end = json.JSONDecoder().raw_decode(str(text), len(MARKER_PREFIX))
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def original_error_from(text):
    """Whatever of the original error survived after EITHER marker, or ''."""
    prefix = marker_prefix_of(text)
    if prefix is None:
        return ""
    try:
        _payload, end = json.JSONDecoder().raw_decode(str(text), len(prefix))
    except ValueError:
        return ""
    rest = str(text)[end:]
    return rest[len(SEPARATOR):] if rest.startswith(SEPARATOR) else rest.lstrip()
