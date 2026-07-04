import os
import jwt
import logging
from shared.keyvault import get_secret

_secret = None

def get_secret_key():
    global _secret
    if _secret is None:
        _secret = get_secret("supabase-jwt-secret")
    return _secret

# ── Token validation policy ───────────────────────────────────────────────
# INTERIM hardening (this pass): Supabase HS256, signature + exp ENFORCED.
# The previous code decoded with verify_signature=False — it accepted any
# forged token, so `sub` was fully attacker-controlled. That is closed here.
#
# Supabase tokens are HS256 (symmetric secret from Key Vault), aud="authenticated",
# iss="https://<project-ref>.supabase.co/auth/v1". aud/iss are read from env so we
# don't hardcode the project ref:
#   - SUPABASE_JWT_AUD defaults to "authenticated" (the Supabase standard) and is
#     always enforced.
#   - SUPABASE_JWT_ISS is enforced ONLY when set. It is left unset by default
#     because a WRONG issuer value locks every user out; set it once the project
#     ref is confirmed (find it in the Supabase dashboard URL / JWT `iss` claim).
# ES256 was dropped from the algorithm list: with a symmetric secret only HS256 is
# valid, and allowing both invites an algorithm-confusion downgrade.
#
# The real fix (later milestone) is Azure AD RS256/JWKS — pyjwt[crypto] is already
# installed for that.
_EXPECTED_AUD = os.environ.get("SUPABASE_JWT_AUD", "authenticated")
_EXPECTED_ISS = os.environ.get("SUPABASE_JWT_ISS")  # e.g. https://<ref>.supabase.co/auth/v1


def validate_token(token: str) -> dict:
    try:
        secret = get_secret_key()
        options = {
            "require": ["exp", "sub"],
            "verify_signature": True,
            "verify_exp": True,
        }
        decode_kwargs = {"algorithms": ["HS256"], "options": options}
        if _EXPECTED_AUD:
            decode_kwargs["audience"] = _EXPECTED_AUD
        else:
            options["verify_aud"] = False
        if _EXPECTED_ISS:
            decode_kwargs["issuer"] = _EXPECTED_ISS
        else:
            logging.warning(
                "SUPABASE_JWT_ISS not set — issuer is NOT enforced. "
                "Set it to fully close the auth gate."
            )

        payload = jwt.decode(token, secret, **decode_kwargs)
        logging.info(f"Token validated: sub={payload.get('sub')}")
        return payload
    except Exception as e:
        logging.warning(f"Token validation failed: {e}")
        raise


def get_user_id(token: str) -> str:
    payload = validate_token(token)
    return payload["sub"]
