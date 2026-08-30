"""Teams checkout lifecycle: quote issuance/consumption and single-open-attempt control.

Every function here takes a CURSOR and neither commits nor rolls back — the caller owns
the transaction, exactly as shared/org_credits.py and shared/job_reservation.py do. That
matters because the whole point of this module is that quote consumption and attempt
reservation must land in ONE transaction with the caller's other work.

THE INVARIANT THIS MODULE EXISTS TO HOLD: an organization may have at most ONE payable
Stripe Checkout Session in flight. Rejecting a duplicate at the webhook is too late — by
then the customer may have paid twice. Two mechanisms, belt and braces:

  * an ORGANIZATION-SCOPED applock serialises concurrent checkout requests for one org
    (scoped, so unrelated organizations never block each other — unlike the server-wide
    'submit-job' lock);
  * dbo.organization_live_checkout holds the invariant in the DATABASE as a primary key,
    so even a lock timeout or a second app instance cannot produce two live attempts.

ORDERING. A Stripe Session must never exist without a durable server record, so an
attempt is inserted as 'creating' and COMMITTED before Stripe is called, then promoted to
'pending' once the session id is known. If the process dies in between, the attempt is
recoverable rather than orphaned: `idempotency_key` is derived deterministically from
(organization, quote), so re-calling Stripe returns THE SAME session instead of a second
payable one.

RECOVERING A STRANDED 'creating' ATTEMPT — what actually works
--------------------------------------------------------------
THE SUPPORTED AUTOMATIC MECHANISM IS REPLAYING CHECKOUT SESSION *CREATION* WITH THE SAME
DETERMINISTIC IDEMPOTENCY KEY. Stripe returns the original Session for a replayed key, so
the retry recovers it and the attempt is promoted to 'pending'.

There is NO lookup-by-idempotency-key operation. Stripe's API does not expose one, and
this application does not implement one — an earlier note in this codebase implied it
did, which was wrong. Nothing here can ask "which session did key X create?"; it can only
re-issue the same create call and observe what comes back.

Recovery therefore deliberately does NOT re-check quote expiry or the current pricing
version (see `validate_quote_for_recovery`). Those are new-purchase rules. Applying them
to a replay would strand the organization's single live slot the moment the quote aged
out or the price list changed, with no automatic way back — and it would gain nothing,
because a replay cannot create a new charge at a new price.

BOUNDED MANUAL PROCEDURE, when automatic recovery cannot settle an attempt
(the admin never retries, or the replay keeps failing):

  1. Read-only: find live attempts stuck in 'creating' past their expires_at:
       SELECT a.attempt_id, a.organization_id, a.quote_id, a.idempotency_key,
              a.expected_total_cents, a.created_at, a.expires_at
       FROM dbo.organization_live_checkout l
       JOIN dbo.organization_checkout_sessions a ON a.attempt_id = l.attempt_id
       WHERE a.status = 'creating' AND a.expires_at < SYSUTCDATETIME();
  2. Read-only at Stripe: list Checkout Sessions for that organization
     (metadata[organization_id]) created around `created_at`, and find any whose
     metadata[quote_id] matches the attempt. This is a SEARCH, not a key lookup.
  3a. If a Session EXISTS and is open: let the admin retry checkout with the same quote —
      the replay promotes the attempt. Do not hand-edit the row.
  3b. If a Session EXISTS and is complete/paid: do NOT release the slot. Let the payment
      webhook fulfil, or replay the event from the Stripe dashboard.
  3c. If NO Session exists, Stripe never created one. Only then settle it by hand:
        UPDATE dbo.organization_checkout_sessions
           SET status = 'failed', settled_at = SYSUTCDATETIME()
         WHERE attempt_id = ? AND status = 'creating';
        DELETE FROM dbo.organization_live_checkout WHERE attempt_id = ?;
      Both statements in ONE transaction, guarded on status = 'creating'.
  4. Never delete the attempt row: it is the audit trail for a session that may exist.
"""
import uuid
from datetime import datetime, timedelta, timezone

# Quote lifetime. Enforced SERVER-SIDE at consumption, against the persisted expires_at —
# not the advisory value the client was shown.
QUOTE_TTL_SECONDS = 30 * 60

# Terminal states an attempt can be moved to before it is replaced.
SETTLED_STATES = ("paid", "failed", "expired", "cancelled")
LIVE_STATES = ("creating", "pending")

# How long a Stripe Checkout Session stays payable. Stripe accepts 30 minutes to 24 hours
# from creation and rejects anything outside that, so this must stay inside the range.
# One hour: long enough for a customer to fetch a card, short enough that an abandoned
# session frees the workspace the same session rather than tomorrow.
CHECKOUT_SESSION_TTL_SECONDS = 60 * 60
STRIPE_MIN_SESSION_TTL_SECONDS = 30 * 60
STRIPE_MAX_SESSION_TTL_SECONDS = 24 * 60 * 60


def checkout_session_expires_at(now=None):
    """Absolute expiry to request from Stripe, clamped into Stripe's accepted range.

    Clamped rather than asserted because a config typo must not take checkout down —
    but it must also never silently send a value Stripe will reject.
    """
    now = now or datetime.now(timezone.utc)
    ttl = min(max(CHECKOUT_SESSION_TTL_SECONDS, STRIPE_MIN_SESSION_TTL_SECONDS),
              STRIPE_MAX_SESSION_TTL_SECONDS)
    return now + timedelta(seconds=ttl)


# ── Errors ───────────────────────────────────────────────────────────────────

class CheckoutError(Exception):
    """Base. `code` is machine-readable; `http_status` is how the endpoint answers."""
    code = "CHECKOUT_ERROR"
    http_status = 400


class QuoteRequired(CheckoutError):
    code = "QUOTE_REQUIRED"


class QuoteMalformed(CheckoutError):
    code = "QUOTE_MALFORMED"


class QuoteNotFound(CheckoutError):
    code = "QUOTE_NOT_FOUND"
    http_status = 409


class QuoteExpired(CheckoutError):
    code = "QUOTE_EXPIRED"
    http_status = 409


class QuoteAlreadyUsed(CheckoutError):
    code = "QUOTE_ALREADY_USED"
    http_status = 409


class QuoteOwnerMismatch(CheckoutError):
    """Presented by someone other than the user it was issued to. A quote is not bearer
    currency — it is not enough to hold one, you must be who it was issued to."""
    code = "QUOTE_OWNER_MISMATCH"
    http_status = 409


class QuoteOrganizationMismatch(CheckoutError):
    code = "QUOTE_ORGANIZATION_MISMATCH"
    http_status = 409


class QuoteVersionSuperseded(CheckoutError):
    code = "QUOTE_VERSION_SUPERSEDED"
    http_status = 409


class CheckoutLockUnavailable(CheckoutError):
    """Another checkout request for this organization is mid-flight."""
    code = "CHECKOUT_IN_PROGRESS"
    http_status = 409


class CheckoutAlreadyOpen(CheckoutError):
    """A live attempt exists for a DIFFERENT quote.

    Deliberately NOT auto-replaced. Cancelling our row would release the organization
    while the customer's original Stripe page stayed payable — only Stripe can retire a
    Stripe page, and that is a network call which may fail or race a payment. So the
    request is refused and the admin is pointed at the open checkout.
    """
    code = "CHECKOUT_ALREADY_OPEN"
    http_status = 409


class CheckoutNotCancellable(CheckoutError):
    """The live attempt cannot be expired right now (wrong state, or Stripe says it is
    complete). Fail closed and keep the slot."""
    code = "CHECKOUT_NOT_CANCELLABLE"
    http_status = 409


# ── Identifiers ──────────────────────────────────────────────────────────────

def parse_quote_id(raw) -> str:
    """Normalise a caller-supplied quote id, or FAIL CLOSED.

    Deliberately raises rather than returning None: an unparseable quote id previously
    became a NULL column and the purchase continued, which meant a malformed id bought
    exactly as much as a valid one. There is no such thing as a partially valid quote.
    """
    if raw is None or raw == "":
        raise QuoteRequired("A quote is required to start checkout.")
    try:
        return str(uuid.UUID(str(raw)))
    except (AttributeError, TypeError, ValueError):
        raise QuoteMalformed(f"Quote id is not a valid identifier.")


def idempotency_key(organization_id, quote_id) -> str:
    """Deterministic Stripe idempotency key for (organization, quote).

    Because it is derived rather than random, a retry — a double-click, a client retry
    after a timeout, or recovery of a stranded 'creating' attempt — replays the SAME key
    and Stripe returns the ORIGINAL session. That is what makes an ambiguous network
    result safe: the second call cannot produce a second payable page.
    """
    return f"teams:{str(organization_id).lower()}:{str(quote_id).lower()}"


# ── Organization-scoped lock ─────────────────────────────────────────────────

def acquire_org_checkout_lock(cur, organization_id, timeout_ms: int = 5000) -> bool:
    """Serialise checkout requests for ONE organization. Transaction-scoped, so it is
    released by the caller's COMMIT or ROLLBACK — never leaked."""
    cur.execute(
        "DECLARE @rc INT; "
        "EXEC @rc = sp_getapplock @Resource = ?, @LockMode = 'Exclusive', "
        "@LockOwner = 'Transaction', @LockTimeout = ?; "
        "SELECT @rc",
        f"teams-checkout:{str(organization_id).lower()}", timeout_ms,
    )
    row = cur.fetchone()
    return bool(row) and row[0] >= 0


# ── Quotes ───────────────────────────────────────────────────────────────────

def issue_quote(cur, user_id, organization_id, quote, ttl_seconds: int = QUOTE_TTL_SECONDS):
    """Persist a priced quote and return (quote_id, expires_at).

    `quote` is a shared.teams_pricing.TeamsQuote. Every field that checkout will later
    validate against is stored HERE, so consumption never has to trust the client.
    """
    import json

    quote_id = str(uuid.uuid4())
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    cur.execute(
        """INSERT INTO teams_quotes
            (quote_id, user_id, organization_id, seats, total_cents, credits_per_seat,
             pricing_version, plan_id, currency, breakdown_json, expires_at, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'issued')""",
        quote_id, str(user_id), str(organization_id) if organization_id else None,
        quote.seats, quote.total_cents, quote.credits_per_seat, quote.pricing_version,
        quote.plan_id, quote.currency,
        json.dumps([b.to_dict() for b in quote.breakdown]),
        expires_at.replace(tzinfo=None),
    )
    return quote_id, expires_at


def load_quote_for_update(cur, quote_id):
    """Read a quote under UPDLOCK/HOLDLOCK so two concurrent checkouts cannot both
    consume it. Returns a dict, or None when there is no such quote."""
    cur.execute(
        """SELECT quote_id, user_id, organization_id, seats, total_cents,
                  credits_per_seat, pricing_version, plan_id, currency,
                  expires_at, status, consumed_by_attempt
           FROM teams_quotes WITH (UPDLOCK, HOLDLOCK)
           WHERE quote_id = ?""",
        quote_id,
    )
    row = cur.fetchone()
    if not row:
        return None
    keys = ("quote_id", "user_id", "organization_id", "seats", "total_cents",
            "credits_per_seat", "pricing_version", "plan_id", "currency",
            "expires_at", "status", "consumed_by_attempt")
    return dict(zip(keys, row))


def _same_id(left, right) -> bool:
    """UNIQUEIDENTIFIER comparison. pyodbc renders GUIDs uppercase while callers carry
    lowercase, so a plain string compare would reject the same person. Parsed, not
    lowercased — a non-GUID fails closed rather than silently 'matching'."""
    try:
        return uuid.UUID(str(left)) == uuid.UUID(str(right))
    except (AttributeError, TypeError, ValueError):
        return False


def _validate_quote_identity(row, user_id, organization_id):
    """Checks that apply to BOTH paths: the quote exists and belongs to this caller and
    this workspace. Never relaxed — not for recovery, not for anything."""
    if row is None:
        raise QuoteNotFound("That quote could not be found. Please price your team again.")
    if not _same_id(row["user_id"], user_id):
        raise QuoteOwnerMismatch("That quote was not issued to you.")
    if row["organization_id"] is not None and not _same_id(row["organization_id"],
                                                           organization_id):
        raise QuoteOrganizationMismatch("That quote is for a different workspace.")


def validate_quote_for_new_checkout(row, user_id, organization_id, pricing_version,
                                    now=None):
    """Raise unless a BRAND NEW purchase may be made from this quote.

    Strictest path, and the only one that can lead to a new charge: the quote must be
    unspent, unexpired, and priced under the CURRENT contract. An expired or superseded
    quote can never buy anything.
    """
    now = now or datetime.now(timezone.utc)
    _validate_quote_identity(row, user_id, organization_id)

    if row["status"] == "consumed":
        raise QuoteAlreadyUsed("That quote has already been used.")
    if row["status"] != "issued":
        raise QuoteExpired("That quote is no longer valid.")

    # expires_at comes back NAIVE from SQL Server; compare against naive UTC.
    expires = row["expires_at"]
    reference = now.replace(tzinfo=None) if now.tzinfo else now
    if expires is not None and expires <= reference:
        raise QuoteExpired("That quote has expired. Please review the updated price.")

    if row["pricing_version"] != pricing_version:
        raise QuoteVersionSuperseded(
            "Pricing has changed since that quote was issued.")


def validate_quote_for_recovery(row, user_id, organization_id, live_attempt,
                                supported_versions=None):
    """Raise unless this request may RESUME the given stranded 'creating' attempt.

    WHY EXPIRY AND PRICING VERSION ARE NOT CHECKED HERE. Recovery is not a purchase. The
    money was already authorised when the attempt was reserved, and the deterministic
    idempotency key means replaying the Stripe call returns THE ORIGINAL session at the
    ORIGINAL price — it cannot create a new charge at a new price. Applying the
    new-purchase rules here would mean that the moment a quote aged out (30 minutes) or
    the price list changed, the attempt could never be replayed and the organization's
    single live slot would be stuck forever with no automatic way out. Refusing to
    recover does not protect anyone; it strands them.

    What IS still required, and is what makes this safe:
      * the quote was consumed by EXACTLY this attempt;
      * the attempt is still 'creating' — a pending/paid/settled attempt has a real
        session and must not be re-driven through creation;
      * caller and organization still match;
      * the deterministic idempotency key still matches the one stored on the attempt.
    """
    _validate_quote_identity(row, user_id, organization_id)

    if live_attempt is None or live_attempt.get("status") != "creating":
        raise QuoteAlreadyUsed("That quote has already been used.")

    if row["status"] != "consumed" or not _same_id(row.get("consumed_by_attempt"),
                                                   live_attempt["attempt_id"]):
        # Someone else's attempt consumed it, or it was never consumed at all.
        raise QuoteAlreadyUsed("That quote has already been used.")

    expected_key = idempotency_key(organization_id, row["quote_id"])
    if live_attempt.get("idempotency_key") != expected_key:
        # The stored key does not derive from this (organization, quote). Replaying it
        # would not be guaranteed to return the original session.
        raise QuoteAlreadyUsed("That checkout cannot be resumed.")

    # The version must still be one we can FULFIL. Recovering an attempt whose contract
    # has been retired would recreate a payable session that the webhook would then be
    # unable to validate — taking money we could never turn into entitlement. A SUPERSEDED
    # version is fine (v1 while v2 is current); a RETIRED one is not.
    if supported_versions is not None and \
            live_attempt.get("pricing_version") not in supported_versions:
        raise QuoteVersionSuperseded(
            "That checkout was priced under a contract that is no longer supported.")


def consume_quote(cur, quote_id, attempt_id) -> bool:
    """Mark a quote spent. Guarded on status='issued' so a concurrent consumer loses.
    Returns True only if THIS call consumed it."""
    cur.execute(
        """UPDATE teams_quotes
           SET status = 'consumed', consumed_at = SYSUTCDATETIME(), consumed_by_attempt = ?
           WHERE quote_id = ? AND status = 'issued'""",
        attempt_id, quote_id,
    )
    return cur.rowcount == 1


# ── Attempts ─────────────────────────────────────────────────────────────────

def find_live_attempt(cur, organization_id):
    """The organization's single live attempt, if any. Locked for the transaction.

    Returns the FULL immutable snapshot, including currency, plan and band breakdown,
    because recovery has to rebuild the original Stripe request from these values rather
    than recomputing anything at today's prices.
    """
    cur.execute(
        """SELECT a.attempt_id, a.checkout_session_id, a.checkout_url, a.quote_id,
                  a.idempotency_key, a.status, a.seats, a.credits_per_seat,
                  a.expected_total_cents, a.pricing_version, a.currency, a.plan_id,
                  a.breakdown_json
           FROM organization_live_checkout l WITH (UPDLOCK, HOLDLOCK)
           JOIN organization_checkout_sessions a ON a.attempt_id = l.attempt_id
           WHERE l.organization_id = ?""",
        str(organization_id),
    )
    row = cur.fetchone()
    if not row:
        return None
    keys = ("attempt_id", "checkout_session_id", "checkout_url", "quote_id",
            "idempotency_key", "status", "seats", "credits_per_seat",
            "expected_total_cents", "pricing_version", "currency", "plan_id",
            "breakdown_json")
    return dict(zip(keys, row))


def settle_attempt(cur, attempt_id, status: str) -> bool:
    """Move a live attempt to a terminal state and RELEASE the organization.

    Deleting the live row is what makes cancellation reversible in the sense that
    matters: the workspace is immediately free to start a new checkout, while the settled
    attempt remains on record for audit.
    """
    if status not in SETTLED_STATES:
        raise ValueError(f"{status!r} is not a settled state")
    cur.execute(
        """UPDATE organization_checkout_sessions
           SET status = ?, settled_at = SYSUTCDATETIME()
           WHERE attempt_id = ? AND status IN ('creating', 'pending')""",
        status, attempt_id,
    )
    changed = cur.rowcount == 1
    cur.execute("DELETE FROM organization_live_checkout WHERE attempt_id = ?", attempt_id)
    return changed


def find_attempt_by_session(cur, checkout_session_id):
    """Locate an attempt by its Stripe session id, locked. Used by expiry handling."""
    cur.execute(
        """SELECT attempt_id, organization_id, status
           FROM organization_checkout_sessions WITH (UPDLOCK, HOLDLOCK)
           WHERE checkout_session_id = ?""",
        checkout_session_id,
    )
    row = cur.fetchone()
    if not row:
        return None
    return {"attempt_id": row[0], "organization_id": row[1], "status": row[2]}


def expire_attempt(cur, attempt_id) -> bool:
    """Mark a PENDING attempt expired and release the organization.

    Guarded on 'pending' only. An attempt that has since become 'paid' must never be
    expired — that is the race where an expiry event and a payment webhook arrive
    together, and the payment has to win.
    """
    cur.execute(
        """UPDATE organization_checkout_sessions
           SET status = 'expired', settled_at = SYSUTCDATETIME()
           WHERE attempt_id = ? AND status = 'pending'""",
        attempt_id,
    )
    if cur.rowcount != 1:
        return False
    cur.execute("DELETE FROM organization_live_checkout WHERE attempt_id = ?", attempt_id)
    return True


def reserve_attempt(cur, organization_id, quote_row, user_id, quote_id, expires_at=None):
    """Insert a 'creating' attempt and claim the organization's single live slot.

    Returns the new attempt_id. The caller MUST commit before calling Stripe: the whole
    point is that no Stripe Session can exist without this row already durable.
    """
    import json

    attempt_id = str(uuid.uuid4())
    expires = expires_at or checkout_session_expires_at()
    cur.execute(
        """INSERT INTO organization_checkout_sessions
            (attempt_id, organization_id, quote_id, idempotency_key, pricing_version,
             plan_id, seats, credits_per_seat, expected_total_cents, currency,
             breakdown_json, status, created_by_user_id, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'creating', ?, ?)""",
        attempt_id, str(organization_id), quote_id,
        idempotency_key(organization_id, quote_id),
        quote_row["pricing_version"], quote_row["plan_id"], quote_row["seats"],
        quote_row["credits_per_seat"], quote_row["total_cents"], quote_row["currency"],
        json.dumps(quote_row.get("breakdown") or []), str(user_id),
        expires.replace(tzinfo=None) if expires.tzinfo else expires,
    )
    # PRIMARY KEY on organization_id: a concurrent request that got past the applock
    # still cannot insert a second live row.
    cur.execute(
        "INSERT INTO organization_live_checkout (organization_id, attempt_id) "
        "VALUES (?, ?)",
        str(organization_id), attempt_id,
    )
    return attempt_id


def promote_attempt(cur, attempt_id, checkout_session_id, checkout_url) -> bool:
    """creating -> pending, recording the Stripe session. Idempotent: promoting an
    already-pending attempt to the same session id is a no-op, not an error."""
    cur.execute(
        """UPDATE organization_checkout_sessions
           SET status = 'pending', checkout_session_id = ?, checkout_url = ?
           WHERE attempt_id = ? AND status IN ('creating', 'pending')""",
        checkout_session_id, checkout_url, attempt_id,
    )
    return cur.rowcount == 1
