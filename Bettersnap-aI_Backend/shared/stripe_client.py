import hashlib
import hmac
import time
import json
import requests
from shared.keyvault import get_secret
from shared.plans import PLANS

STRIPE_API_BASE = "https://api.stripe.com/v1"

# 1 job = 20 credits = 4 image variations
CREDITS_PER_JOB = 20

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


def _post(path: str, data: dict) -> dict:
    resp = requests.post(
        f"{STRIPE_API_BASE}/{path}",
        auth=(_secret_key(), ""),
        data=data,
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


def create_monthly_checkout(user_id: str, email: str, plan: str, success_url: str, cancel_url: str) -> dict:
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
        "subscription_data[metadata][user_id]": user_id,
        "subscription_data[metadata][plan]": plan,
    }, email))


def cancel_subscription(stripe_subscription_id: str) -> dict:
    # Schedule cancellation at period end (not immediate). The returned subscription carries
    # current_period_end — the exact date access ends — which the caller records for the UI.
    return _post(f"subscriptions/{stripe_subscription_id}", {
        "cancel_at_period_end": "true",
    })


def reactivate_subscription(stripe_subscription_id: str) -> dict:
    """Undo a pending period-end cancellation — the subscription keeps renewing."""
    return _post(f"subscriptions/{stripe_subscription_id}", {
        "cancel_at_period_end": "false",
    })


def verify_webhook(payload_bytes: bytes, sig_header: str) -> dict:
    webhook_secret = get_secret("stripe-webhook-secret")

    parts = {k: v for k, v in (p.split("=", 1) for p in sig_header.split(","))}
    timestamp = parts.get("t", "")
    signatures = [v for k, v in parts.items() if k == "v1"]

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
