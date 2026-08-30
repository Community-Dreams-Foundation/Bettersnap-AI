import hashlib
import hmac
import os
import time
import json
import requests
from shared.keyvault import get_secret
from shared.plans import PLANS

STRIPE_API_BASE = "https://api.stripe.com/v1"

# Plan economics are DERIVED from the single source of truth (shared/plans.py) so the billing
# numbers can never drift from what submit_job enforces. One-time = image packs (1 credit == 1
# image, so credits == images). Monthly = credit-based (credits_per_image = 5). The ONLY other
# source is the Stripe dashboard price — reconcile that with reconcile_prices().
def _cents(p) -> int:
    return int(round(p.price_usd * 100))

ONE_TIME_PLANS = {
    tier: {
        "credits":          PLANS[tier].image_count * PLANS[tier].credits_per_image,
        "images":           PLANS[tier].image_count,
        "original_cents":   _cents(PLANS[tier]),
        "discounted_cents": _cents(PLANS[tier]),
    }
    for tier in ("basic", "pro", "expert")
}

MONTHLY_PLANS = {
    tier: {
        "credits":     PLANS[f"monthly_{tier}"].monthly_images * PLANS[f"monthly_{tier}"].credits_per_image,
        "images":      PLANS[f"monthly_{tier}"].monthly_images,
        "price_cents": _cents(PLANS[f"monthly_{tier}"]),
    }
    for tier in ("basic", "pro", "expert")
}


def _secret_key() -> str:
    return get_secret("stripe-secret-key")


def _price_id(plan_type: str, plan: str) -> str:
    # Key Vault secret names:
    # stripe-price-onetime-basic, stripe-price-onetime-pro, stripe-price-onetime-expert
    # stripe-price-monthly-basic, stripe-price-monthly-pro, stripe-price-monthly-expert
    return get_secret(f"stripe-price-{plan_type}-{plan}")


def _post(path: str, data: dict, idempotency_key: str | None = None) -> dict:
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
    resp = requests.post(
        f"{STRIPE_API_BASE}/{path}",
        auth=(_secret_key(), ""),
        data=data,
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _get(path: str) -> dict:
    resp = requests.get(f"{STRIPE_API_BASE}/{path}", auth=(_secret_key(), ""), timeout=15)
    resp.raise_for_status()
    return resp.json()


def reconcile_prices() -> list:
    """Verify the DERIVED cents (from plans.py) match the ACTUAL Stripe price amounts — the
    third source of truth. Returns a list of mismatches (empty = all aligned). Run as an ops
    check / in CI so a Stripe-dashboard edit that diverges from plans.py is caught early."""
    out = []
    for kind, table, cents_key in (
        ("onetime", ONE_TIME_PLANS, "discounted_cents"),
        ("monthly", MONTHLY_PLANS, "price_cents"),
    ):
        for tier, info in table.items():
            expected = info[cents_key]
            try:
                actual = _get(f"prices/{_price_id(kind, tier)}").get("unit_amount")
            except Exception as e:
                out.append({"plan": f"{kind}-{tier}", "error": str(e)})
                continue
            if actual != expected:
                out.append({"plan": f"{kind}-{tier}",
                            "plans_py_cents": expected, "stripe_cents": actual})
    return out


def _maybe_email(params: dict, email: str) -> dict:
    """Only attach customer_email when it's a real address. Stripe rejects an EMPTY
    customer_email with `400 Invalid email address` — and Entra tokens frequently have
    no `email` claim, so passing "" broke every checkout. Omitting it lets Stripe's
    hosted page collect the email instead. (Credits are granted off metadata.user_id,
    not the email, so nothing downstream depends on this.)"""
    if email and "@" in email:
        params["customer_email"] = email
    return params


def monthly_plan_for_price_id(price_id: str) -> str | None:
    """Resolve a Stripe monthly Price ID to the BetterSnap plan tier."""
    if not price_id:
        return None
    for plan in MONTHLY_PLANS:
        if hmac.compare_digest(price_id, _price_id("monthly", plan)):
            return plan
    return None


def subscription_period_end(subscription: dict) -> int | None:
    """Return the effective period end across legacy and current Stripe response shapes."""
    if subscription.get("cancel_at"):
        return int(subscription["cancel_at"])
    if subscription.get("current_period_end"):
        return int(subscription["current_period_end"])
    item_period_ends = [
        int(item["current_period_end"])
        for item in subscription.get("items", {}).get("data", [])
        if item.get("current_period_end")
    ]
    return min(item_period_ends) if item_period_ends else None


def create_onetime_checkout(user_id: str, email: str, plan: str, success_url: str, cancel_url: str) -> dict:
    price_id = _price_id("onetime", plan)
    return _post("checkout/sessions", _maybe_email({
        "mode": "payment",
        "line_items[0][price]": price_id,
        "line_items[0][quantity]": "1",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata[user_id]": user_id,
        "metadata[plan]": plan,
        "metadata[payment_type]": "one_time",
    }, email))


def create_org_checkout(organization_id: str, admin_email: str, seats: int,
                         price_per_seat_cents: int, success_url: str, cancel_url: str) -> dict:
    return _post("checkout/sessions", _maybe_email({
        "mode": "payment",
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][product_data][name]": "BetterSnap Teams — seats",
        "line_items[0][price_data][unit_amount]": str(price_per_seat_cents),
        "line_items[0][quantity]": str(seats),
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata[organization_id]": organization_id,
        "metadata[payment_type]": "org_seats",
    }, admin_email))


def expire_org_checkout_session(checkout_session_id: str) -> dict:
    """Ask Stripe to EXPIRE an open Checkout Session, making its hosted page unpayable.

    This is the operation that was missing: cancelling our own row released the
    organization's live slot while the customer's original Stripe URL stayed payable, so
    a "replaced" checkout could still be paid. Only Stripe can retire a Stripe page.

    Returns the updated Session. Stripe refuses to expire a session that is already
    complete, which is the behaviour we want — the caller must not release or replace a
    session that may have been paid.
    """
    return _post(f"checkout/sessions/{checkout_session_id}/expire", {})


def get_checkout_session(checkout_session_id: str) -> dict:
    """Read a Checkout Session's authoritative state from Stripe."""
    return _get(f"checkout/sessions/{checkout_session_id}")


def create_org_seats_checkout(organization_id: str, admin_email: str, quote,
                              success_url: str, cancel_url: str,
                              quote_id: str = "", idempotency_key: str | None = None,
                              expires_at: int | None = None) -> dict:
    """Teams checkout for the GRADUATED contract — one Stripe line item per price band.

    `quote` is a shared.teams_pricing.TeamsQuote. Its breakdown is rendered as separate
    line items ("seats 1-9 @ $35", "seat 10 @ $32") rather than one blended unit price,
    for two reasons:

      • Stripe's `unit_amount` is integer cents, so a blended rate is often not
        representable — 24 seats is $795/24 = $33.125 per seat. Rounding either way makes
        Stripe's total disagree with the authorised total, and the webhook's amount check
        would then reject a legitimate payment.
      • The customer's Stripe receipt shows the same band breakdown the quote showed
        them, so the discount is visible rather than implied.

    The line items are DERIVED from the quote the server computed. No amount from the
    client reaches this function. `_assert_total_matches` re-adds them as a last guard
    before the network call, so a future edit to the breakdown logic cannot silently
    charge a total different from the one recorded in the payment snapshot.

    `payment_method_types` is deliberately NOT set: omitting it lets Stripe serve the
    methods enabled on the account (cards, wallets, local methods), which is both wider
    coverage and one less thing to keep in sync. Automatic tax is likewise not enabled —
    turning it on changes what the customer is charged, so it is a pricing decision, not
    an implementation detail.
    """
    computed = sum(b.subtotal_cents for b in quote.breakdown)
    if computed != quote.total_cents:
        raise ValueError(
            f"teams checkout refused: line items total {computed} but the quote says "
            f"{quote.total_cents}"
        )

    params = {
        "mode": "payment",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata[organization_id]": organization_id,
        "metadata[payment_type]": "org_seats",
        # Stamped so fulfilment can refuse a session priced under a contract that has
        # since been superseded, instead of granting credits at a stale rate.
        "metadata[pricing_version]": quote.pricing_version,
        "metadata[plan_id]": quote.plan_id,
        "metadata[seats]": str(quote.seats),
        "metadata[credits_per_seat]": str(quote.credits_per_seat),
        "metadata[expected_total_cents]": str(quote.total_cents),
        "metadata[quote_id]": quote_id,
    }
    # BOUNDED LIFETIME. Stripe accepts expires_at between 30 minutes and 24 hours from
    # creation; anything outside that is rejected. A session that never expires is a
    # payable page with no end date, which is exactly what made a "replaced" checkout
    # dangerous. Stripe then emits checkout.session.expired, and THAT event — not a local
    # timer — is what releases the organization's live slot.
    if expires_at is not None:
        params["expires_at"] = str(int(expires_at))
    for i, band in enumerate(quote.breakdown):
        label = (
            f"{quote.plan_name} — seat {band.lower}"
            if band.seats == 1 else
            f"{quote.plan_name} — seats {band.lower}-{band.lower + band.seats - 1}"
        )
        params[f"line_items[{i}][price_data][currency]"] = quote.currency
        params[f"line_items[{i}][price_data][product_data][name]"] = label
        params[f"line_items[{i}][price_data][product_data][description]"] = (
            f"{band.seats} seat(s) at ${band.unit_cents / 100:.2f} each · "
            f"{quote.credits_per_seat} headshots per seat"
        )
        params[f"line_items[{i}][price_data][unit_amount]"] = str(band.unit_cents)
        params[f"line_items[{i}][quantity]"] = str(band.seats)

    # The idempotency key is DETERMINISTIC (organization + quote), not random. Replaying
    # it — a double-click, a retry after an ambiguous timeout, or recovery of a stranded
    # 'creating' attempt — makes Stripe return THE ORIGINAL session rather than creating a
    # second payable one. Without it, a retry after a network timeout is a second charge
    # opportunity even though the first session may already exist.
    return _post("checkout/sessions", _maybe_email(params, admin_email), idempotency_key)


def create_topup_checkout(user_id: str, email: str, pack: str, success_url: str, cancel_url: str) -> dict:
    """Credit top-up for an ACTIVE monthly subscriber — buy more images generated from the
    EXISTING model (no new training, no plan change). Reuses the one-time pack sizes + Stripe
    prices, so no new Stripe setup is needed; the webhook grants those images at the account's
    monthly credit rate. (A dedicated, cheaper top-up price can be swapped in later.)"""
    price_id = _price_id("onetime", pack)
    return _post("checkout/sessions", _maybe_email({
        "mode": "payment",
        "line_items[0][price]": price_id,
        "line_items[0][quantity]": "1",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata[user_id]": user_id,
        "metadata[plan]": pack,
        "metadata[payment_type]": "topup",
    }, email))


def create_monthly_checkout(
    user_id: str,
    email: str,
    plan: str,
    success_url: str,
    cancel_url: str,
    checkout_token: str,
    expires_at: int,
) -> dict:
    price_id = _price_id("monthly", plan)
    return _post("checkout/sessions", _maybe_email({
        "mode": "subscription",
        "line_items[0][price]": price_id,
        "line_items[0][quantity]": "1",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata[user_id]": user_id,
        "metadata[plan]": plan,
        "metadata[payment_type]": "monthly",
        "metadata[checkout_token]": checkout_token,
        "subscription_data[metadata][user_id]": user_id,
        "subscription_data[metadata][plan]": plan,
        "expires_at": str(expires_at),
    }, email), idempotency_key=f"monthly-checkout-{checkout_token}")


def cancel_subscription(stripe_subscription_id: str) -> dict:
    # Schedule cancellation at period end (not immediate). The returned subscription carries
    # current_period_end — the exact date access ends — which the caller records for the UI.
    return _post(f"subscriptions/{stripe_subscription_id}", {
        "cancel_at_period_end": "true",
    })


def get_subscription(stripe_subscription_id: str) -> dict:
    """Retrieve the complete subscription after an update response omits period fields."""
    return _get(f"subscriptions/{stripe_subscription_id}")


def upgrade_subscription(
    stripe_subscription_id: str,
    subscription_item_id: str,
    plan: str,
) -> dict:
    """Upgrade an existing monthly subscription and invoice the prorated difference now."""
    price_id = _price_id("monthly", plan)
    return _post(
        f"subscriptions/{stripe_subscription_id}",
        {
            "items[0][id]": subscription_item_id,
            "items[0][price]": price_id,
            "proration_behavior": "always_invoice",
            "payment_behavior": "pending_if_incomplete",
        },
        idempotency_key=(
            f"monthly-upgrade-{stripe_subscription_id}-{plan}-{int(time.time() // 300)}"
        ),
    )


def find_checkout_session_by_token(checkout_token: str) -> dict | None:
    """Find a recent Checkout Session by the reservation token stored in its metadata."""
    sessions = _get("checkout/sessions?limit=100").get("data", [])
    return next(
        (
            session for session in sessions
            if session.get("metadata", {}).get("checkout_token") == checkout_token
        ),
        None,
    )


def reactivate_subscription(stripe_subscription_id: str) -> dict:
    """Undo a pending period-end cancellation — the subscription keeps renewing."""
    return _post(f"subscriptions/{stripe_subscription_id}", {
        "cancel_at_period_end": "false",
    })


def create_billing_portal(
    stripe_customer_id: str,
    return_url: str,
    mode: str = "payment_method_update",
) -> dict:
    """Create either a targeted card-update flow or the full Stripe billing portal."""
    data = {
        "customer": stripe_customer_id,
        "return_url": return_url,
    }
    if mode == "payment_method_update":
        data.update({
            "flow_data[type]": "payment_method_update",
            "flow_data[after_completion][type]": "redirect",
            "flow_data[after_completion][redirect][return_url]": return_url,
        })
    elif mode != "manage":
        raise ValueError("unsupported billing portal mode")
    return _post("billing_portal/sessions", data)

def verify_webhook(payload_bytes: bytes, sig_header: str) -> dict:
    # env override first (features-stripe), then Key Vault (dev) — either source works.
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET") or get_secret("stripe-webhook-secret")

    # Preserve repeated v1 fields. During webhook-secret rotation Stripe can send more than
    # one v1 signature; a dict would silently keep only the last and could drop the valid one.
    timestamp = ""
    signatures = []
    for part in (sig_header or "").split(","):
        key, separator, value = part.strip().partition("=")
        if not separator:
            continue
        if key == "t" and not timestamp:
            timestamp = value
        elif key == "v1":
            signatures.append(value)

    if not timestamp or not signatures:
        raise ValueError("Invalid Stripe-Signature header")

    if abs(time.time() - int(timestamp)) > 300:
        raise ValueError("Webhook timestamp too old")

    signed_payload = f"{timestamp}.{payload_bytes.decode('utf-8')}"
    expected = hmac.new(
        webhook_secret.encode("utf-8"),
        signed_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not any(hmac.compare_digest(expected, sig) for sig in signatures):
        raise ValueError("Invalid webhook signature")

    return json.loads(payload_bytes)
