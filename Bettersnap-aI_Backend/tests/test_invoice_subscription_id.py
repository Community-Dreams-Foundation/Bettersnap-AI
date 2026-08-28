"""Guard against the silent renewal-failure bug.

Stripe removed the top-level invoice.subscription field. In the version this account now
sends (2026-05-27.dahlia) the id lives at invoice.parent.subscription_details.subscription.
Both invoice handlers read the old field and no-op'd, so every monthly renewal silently
failed to reset credits and every payment failure silently failed to flag dunning — while
the first month kept working (that goes through checkout.session.completed).

These payloads are shaped exactly like the live objects observed while diagnosing it.

Run: python -m unittest tests.test_invoice_subscription_id   (from the backend dir)
"""
import os
import re
import sys
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)


def _invoice_subscription_id(invoice):
    """Mirror of function_app._invoice_subscription_id (importing it needs the Azure host)."""
    sub = invoice.get("subscription")
    if sub:
        return sub
    parent = invoice.get("parent") or {}
    sub = (parent.get("subscription_details") or {}).get("subscription")
    if sub:
        return sub
    for line in (invoice.get("lines", {}) or {}).get("data", []):
        lp = (line.get("parent") or {}).get("subscription_item_details") or {}
        if lp.get("subscription"):
            return lp["subscription"]
    return None


SUB = "sub_1TwrYZ9UeBl2tXGwPVbtg6NU"

# What Stripe actually delivered for the renewal that exposed the bug (2026-05-27.dahlia):
# top-level subscription is None, id is under parent.subscription_details.
DAHLIA_RENEWAL = {
    "subscription": None,
    "billing_reason": "subscription_cycle",
    "parent": {"subscription_details": {"subscription": SUB}},
    "lines": {"data": [{"parent": {"subscription_item_details": {"subscription": SUB}}}]},
}

# Legacy shape (older API version) still has the top-level field.
LEGACY = {"subscription": SUB, "billing_reason": "subscription_cycle"}

# Only the line item carries it (defensive third fallback).
LINE_ONLY = {
    "subscription": None, "parent": None,
    "lines": {"data": [{"parent": {"subscription_item_details": {"subscription": SUB}}}]},
}

# A genuinely subscription-less invoice (manual one-off) must resolve to None so the
# handlers correctly no-op instead of touching a random row.
MANUAL = {"subscription": None, "parent": {"subscription_details": None},
          "lines": {"data": [{"parent": {"invoice_item_details": {}}}]}}


class InvoiceSubIdTests(unittest.TestCase):
    def test_dahlia_renewal_resolves(self):
        # THE regression: this returned None before the fix -> renewal no-op'd
        self.assertEqual(_invoice_subscription_id(DAHLIA_RENEWAL), SUB)

    def test_legacy_top_level_still_resolves(self):
        self.assertEqual(_invoice_subscription_id(LEGACY), SUB)

    def test_line_item_fallback(self):
        self.assertEqual(_invoice_subscription_id(LINE_ONLY), SUB)

    def test_manual_invoice_is_none(self):
        self.assertIsNone(_invoice_subscription_id(MANUAL))

    def test_top_level_wins_when_present(self):
        mixed = dict(LEGACY, parent={"subscription_details": {"subscription": "sub_OTHER"}})
        self.assertEqual(_invoice_subscription_id(mixed), SUB)


class NoHandlerReadsRawFieldTests(unittest.TestCase):
    """Both invoice handlers must go through the helper, not invoice.get('subscription')
    directly — that raw read is the bug, and it looks harmless in a diff."""

    def _src(self):
        with open(os.path.join(BACKEND_DIR, "function_app.py"), encoding="utf-8") as f:
            return f.read()

    def test_helper_exists(self):
        self.assertIn("def _invoice_subscription_id(", self._src())

    def test_invoice_handlers_use_the_helper(self):
        src = self._src()
        # the two invoice handlers must call the helper
        self.assertGreaterEqual(src.count("_invoice_subscription_id(invoice)"), 2,
                                "an invoice handler still reads the raw subscription field")
        # the raw invoice.get('subscription') read must exist EXACTLY ONCE — as the legacy
        # fallback inside the helper. A second occurrence means a handler reads it directly,
        # which is None on current API versions and silently breaks renewals. (Checkout uses
        # session.get on a different object, so it is not counted here.)
        self.assertEqual(src.count('invoice.get("subscription")'), 1,
                         "raw invoice.get('subscription') should appear only inside the "
                         "helper; a handler is reading the dead field directly")


if __name__ == "__main__":
    unittest.main(verbosity=2)
