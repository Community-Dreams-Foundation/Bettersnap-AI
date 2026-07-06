import os
import jwt
import logging
from jwt import PyJWKClient

# ── Token validation policy — Entra External ID (Azure AD), RS256 + JWKS ──────
# Migrated off Supabase HS256. Tokens are now Entra access tokens signed with
# RS256; we validate the signature against the tenant's published JWKS and
# enforce iss / aud / exp. There is NO shared secret anymore, so the
# supabase-jwt-secret Key Vault read is gone.
#
# All three values come from app settings (Function App configuration):
#   ENTRA_JWKS_URI — jwks_uri from the tenant's OIDC discovery document, e.g.
#                    https://<tenant>.ciamlogin.com/<tenant-id>/discovery/v2.0/keys
#   ENTRA_ISSUER   — the exact `iss` claim, e.g.
#                    https://<tenant-id>.ciamlogin.com/<tenant-id>/v2.0
#   ENTRA_AUD      — the API audience the frontend requests a token for, e.g.
#                    api://d14bccac-4a37-4919-89a3-24272a0825bc
#                    (may instead be the bare client-id GUID — confirm from a
#                    real token before setting; see fail-closed note below).
#
# FAIL CLOSED: validate_token refuses to validate (raises) unless ENTRA_AUD,
# ENTRA_ISSUER and ENTRA_JWKS_URI are all set. ENTRA_AUD is intentionally left
# UNSET until a real token's `aud` is confirmed — so until then every call 401s
# rather than accepting a token against an unknown/blank audience. Reading env
# at call time (not import) keeps `import shared.auth` clean even with nothing
# configured yet.
_jwks_client = None


def _get_jwks_client() -> PyJWKClient:
    """Lazily build a cached PyJWKClient. Module-level singleton so a warm
    Function instance reuses it; PyJWKClient also caches the fetched JWK set and
    individual signing keys, so steady state does no network call per request."""
    global _jwks_client
    if _jwks_client is None:
        jwks_uri = os.environ.get("ENTRA_JWKS_URI")
        if not jwks_uri:
            raise RuntimeError("ENTRA_JWKS_URI not set — cannot fetch signing keys")
        _jwks_client = PyJWKClient(jwks_uri, cache_keys=True)
    return _jwks_client


def validate_token(token: str) -> dict:
    try:
        aud = os.environ.get("ENTRA_AUD")
        iss = os.environ.get("ENTRA_ISSUER")
        # Fail closed on missing config. AUD first because it is the value we
        # deliberately hold back until confirmed from a real token.
        if not aud:
            raise RuntimeError(
                "ENTRA_AUD not set — refusing to validate (fail closed). "
                "Set it to the confirmed token audience "
                "(api://<client-id> or the bare client-id GUID)."
            )
        if not iss:
            raise RuntimeError("ENTRA_ISSUER not set — refusing to validate (fail closed).")

        signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=aud,
            issuer=iss,
            options={
                # require: reject a token missing any of these outright — `oid`
                # included so get_user_id can never KeyError on a malformed token.
                "require": ["exp", "iss", "aud", "oid"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_aud": True,
                "verify_iss": True,
            },
        )
        logging.info(f"Token validated: oid={payload.get('oid')}")
        return payload
    except Exception as e:
        logging.warning(f"Token validation failed: {e}")
        raise


def get_user_id(token: str) -> str:
    payload = validate_token(token)
    # oid = Entra object ID (stable per-user GUID). Using it as users.user_id
    # keeps the existing GUID PK — no primary-key migration. (Note: `sub` is a
    # per-app pairwise subject and is NOT stable across apps, so it must not be
    # used as the identity key.)
    return payload["oid"]
