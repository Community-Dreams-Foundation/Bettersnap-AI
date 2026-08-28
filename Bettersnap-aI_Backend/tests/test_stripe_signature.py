"""Stripe signature rotation and authentication logging tests."""
import hashlib
import hmac
import importlib.util
import json
import os
import pathlib
import unittest
from unittest import mock

from shared import stripe_client


_AUTH_PATH = pathlib.Path(__file__).resolve().parents[1] / "shared" / "auth.py"
_AUTH_SPEC = importlib.util.spec_from_file_location("shared.auth_privacy_test", _AUTH_PATH)
auth = importlib.util.module_from_spec(_AUTH_SPEC)
_AUTH_SPEC.loader.exec_module(auth)


class StripeSignatureTests(unittest.TestCase):
    SECRET = "whsec_test"
    TIMESTAMP = 1_800_000_000
    PAYLOAD = json.dumps({"id": "evt_test"}, separators=(",", ":")).encode()

    def _signature(self):
        signed = f"{self.TIMESTAMP}.{self.PAYLOAD.decode()}".encode()
        return hmac.new(self.SECRET.encode(), signed, hashlib.sha256).hexdigest()

    def test_any_v1_signature_may_match_during_secret_rotation(self):
        header = f"t={self.TIMESTAMP},v1={self._signature()},v1=wrong-last-signature"
        with mock.patch.object(stripe_client, "get_secret", return_value=self.SECRET), \
             mock.patch.object(stripe_client.time, "time", return_value=self.TIMESTAMP):
            event = stripe_client.verify_webhook(self.PAYLOAD, header)

        self.assertEqual(event["id"], "evt_test")

    def test_rejects_when_none_of_multiple_v1_signatures_match(self):
        header = f"t={self.TIMESTAMP},v1=wrong-one,v1=wrong-two"
        with mock.patch.object(stripe_client, "get_secret", return_value=self.SECRET), \
             mock.patch.object(stripe_client.time, "time", return_value=self.TIMESTAMP):
            with self.assertRaisesRegex(ValueError, "Invalid webhook signature"):
                stripe_client.verify_webhook(self.PAYLOAD, header)


class AuthLoggingTests(unittest.TestCase):
    def test_successful_validation_does_not_log_oid(self):
        key = type("SigningKey", (), {"key": "public-key"})()
        jwks = mock.Mock()
        jwks.get_signing_key_from_jwt.return_value = key
        payload = {"oid": "private-user-oid"}

        with mock.patch.dict(os.environ, {
            "ENTRA_AUD": "audience",
            "ENTRA_ISSUER": "issuer",
        }, clear=False), \
             mock.patch.object(auth, "_get_jwks_client", return_value=jwks), \
             mock.patch.object(auth.jwt, "decode", return_value=payload), \
             mock.patch.object(auth.logging, "info") as info:
            result = auth.validate_token("token")

        self.assertEqual(result, payload)
        info.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
