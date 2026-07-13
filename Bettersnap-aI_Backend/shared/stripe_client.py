import hashlib
import hmac
import time
import json
import requests
from shared.keyvault import get_secret

STRIPE_API_BASE = "https://api.stripe.com/v1"

# 1 job = 20 credits = 4 image variations
CREDITS_PER_JOB = 20

ONE_TIME_PLANS = {
    "basic":  {"credits": 100, "images": 20, "original_cents": 1900, "discounted_cents": 1000},
    "pro":    {"credits": 200, "images": 40, "original_cents": 3500, "discounted_cents": 1700},
    "expert": {"credits": 300, "images": 60, "original_cents": 5500, "discounted_cents": 2800},
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


def create_onetime_checkout(user_id: str, email: str, plan: str, success_url: str, cancel_url: str) -> dict:
    price_id = _price_id("onetime", plan)
    return _post("checkout/sessions", {
        "mode": "payment",
        "customer_email": email,
        "line_items[0][price]": price_id,
        "line_items[0][quantity]": "1",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata[user_id]": user_id,
        "metadata[plan]": plan,
        "metadata[payment_type]": "one_time",
    })


def create_monthly_checkout(user_id: str, email: str, plan: str, success_url: str, cancel_url: str) -> dict:
    price_id = _price_id("monthly", plan)
    return _post("checkout/sessions", {
        "mode": "subscription",
        "customer_email": email,
        "line_items[0][price]": price_id,
        "line_items[0][quantity]": "1",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata[user_id]": user_id,
        "metadata[plan]": plan,
        "metadata[payment_type]": "monthly",
        "subscription_data[metadata][user_id]": user_id,
        "subscription_data[metadata][plan]": plan,
    })


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
