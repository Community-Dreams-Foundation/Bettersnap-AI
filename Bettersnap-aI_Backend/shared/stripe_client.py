import hashlib
import hmac
import time
import json
import requests
from shared.keyvault import get_secret

STRIPE_API_BASE = "https://api.stripe.com/v1"

# 1 job = 20 credits = 4 image variations
CREDITS_PER_JOB = 20

# One-time = image packs. No credit concept for the user: internally 1 credit == 1 image,
# so "credits" granted == images. (credits == images below.)
ONE_TIME_PLANS = {
    "basic":  {"credits": 30, "images": 30, "original_cents": 3500, "discounted_cents": 3500},
    "pro":    {"credits": 50, "images": 50, "original_cents": 4500, "discounted_cents": 4500},
    "expert": {"credits": 70, "images": 70, "original_cents": 6500, "discounted_cents": 6500},
}

MONTHLY_PLANS = {
    "basic":  {"credits": 100, "images": 20, "price_cents": 2500},
    "pro":    {"credits": 200, "images": 40, "price_cents": 4500},
    "expert": {"credits": 300, "images": 60, "price_cents": 6500},
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
    return _post(f"subscriptions/{stripe_subscription_id}", {
        "cancel_at_period_end": "true",
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
