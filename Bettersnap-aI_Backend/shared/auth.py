import os
import hmac
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
    # ── QA-ONLY TEST-AUTH PATH — env-gated, NON-PRODUCTION ONLY ───────────────────
    # Lets a single designated test account authenticate for end-to-end testing WITHOUT
    # the Entra email-OTP flow (no real inbox needed). It is COMPLETELY INERT unless BOTH
    # gates below are set on the Function App — which must ONLY ever be done on a sandbox /
    # test app, NEVER in production:
    #     TEST_AUTH_ENABLED = "1"
    #     TEST_AUTH_SECRET  = <a long random secret>     (token must equal "test:<secret>")
    # Optional identity overrides: TEST_AUTH_OID, TEST_AUTH_EMAIL.
    # Testers send   Authorization: Bearer test:<secret>   and get a session as the test user.
    # In production TEST_AUTH_ENABLED is unset, so this block is skipped entirely and the real
    # Entra validation below is the only path. If this warning ever appears in prod logs,
    # UNSET TEST_AUTH_ENABLED immediately.
    if os.environ.get("TEST_AUTH_ENABLED") == "1":
        secret = os.environ.get("TEST_AUTH_SECRET") or ""
        if secret and hmac.compare_digest(token, f"test:{secret}"):
            oid = os.environ.get("TEST_AUTH_OID", "11111111-1111-4111-8111-111111111111")
            email = os.environ.get("TEST_AUTH_EMAIL", "kumar-test@bettersnap.ai")
            logging.warning(
                "TEST-AUTH bypass used (non-prod only): oid=%s email=%s — if this is "
                "production, UNSET TEST_AUTH_ENABLED now.", oid, email)
            return {"oid": oid, "email": email, "preferred_username": email,
                    "name": "Kumar Test", "iss": "test-auth", "aud": "test-auth"}

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


# ── Super-Admin token validation — SEPARATE issuer + a required app role ──────
# The customer API above trusts the customer CIAM tenant. Admin-dashboard staff sign in against
# the INTERNAL work tenant instead (admin@bettersnap.ai etc.), so the admin API validates a
# DIFFERENT issuer/JWKS/audience AND requires an app role — a valid work token is not enough,
# the account must be assigned the admin role. All fail closed (missing config => raise => 401).
#   ADMIN_JWKS_URI — internal tenant JWKS, e.g. https://login.microsoftonline.com/<tid>/discovery/v2.0/keys
#   ADMIN_ISSUER   — internal tenant issuer, e.g. https://login.microsoftonline.com/<tid>/v2.0
#   ADMIN_AUD      — the admin API's audience (api://<admin-client-id> or the bare client-id GUID)
#   ADMIN_ROLE     — required role value in the token's `roles` claim (default "Admin")
_admin_jwks_client = None


class NotAdminError(Exception):
    """Token is otherwise valid but the account is NOT assigned the admin role -> 403 (not 401)."""


def _get_admin_jwks_client() -> PyJWKClient:
    global _admin_jwks_client
    if _admin_jwks_client is None:
        jwks_uri = os.environ.get("ADMIN_JWKS_URI")
        if not jwks_uri:
            raise RuntimeError("ADMIN_JWKS_URI not set — admin API refuses to validate (fail closed)")
        _admin_jwks_client = PyJWKClient(jwks_uri, cache_keys=True)
    return _admin_jwks_client


def require_admin(token: str) -> dict:
    """Validate an admin-dashboard token and require the admin role. Returns
    {oid, email, name, roles}. Raises NotAdminError (valid but not an admin -> 403) or any other
    Exception (invalid/expired/misconfigured -> 401). Callers distinguish the two."""
    # ── QA-ONLY admin test path — env-gated, NON-PRODUCTION ONLY ─────────────────
    # Lets Jayasri develop the dashboard BEFORE the internal-tenant app registration is wired.
    # Inert unless BOTH gates are set (sandbox app only, NEVER prod):
    #     ADMIN_TEST_ENABLED = "1"; ADMIN_TEST_SECRET = <long random>
    # She sends  Authorization: Bearer admin-test:<secret>  and gets an admin session.
    if os.environ.get("ADMIN_TEST_ENABLED") == "1":
        secret = os.environ.get("ADMIN_TEST_SECRET") or ""
        if secret and hmac.compare_digest(token, f"admin-test:{secret}"):
            email = os.environ.get("ADMIN_TEST_EMAIL", "admin@bettersnap.ai")
            logging.warning("ADMIN-TEST bypass used (non-prod only) email=%s — UNSET "
                            "ADMIN_TEST_ENABLED in production.", email)
            return {"oid": os.environ.get("ADMIN_TEST_OID", "00000000-0000-4000-8000-000000000001"),
                    "email": email, "name": "Admin Test", "roles": ["Admin"]}

    aud = os.environ.get("ADMIN_AUD")
    iss = os.environ.get("ADMIN_ISSUER")
    if not aud:
        raise RuntimeError("ADMIN_AUD not set — admin API refuses to validate (fail closed).")
    if not iss:
        raise RuntimeError("ADMIN_ISSUER not set — admin API refuses to validate (fail closed).")

    signing_key = _get_admin_jwks_client().get_signing_key_from_jwt(token)
    payload = jwt.decode(
        token, signing_key.key, algorithms=["RS256"], audience=aud, issuer=iss,
        options={"require": ["exp", "iss", "aud", "oid"], "verify_signature": True,
                 "verify_exp": True, "verify_aud": True, "verify_iss": True},
    )
    role_name = os.environ.get("ADMIN_ROLE", "Admin")
    roles = payload.get("roles") or []
    if role_name not in roles:
        raise NotAdminError(
            f"oid={payload.get('oid')} authenticated but lacks role '{role_name}' (roles={roles})")
    return {
        "oid": payload["oid"],
        "email": payload.get("preferred_username") or payload.get("email") or "",
        "name": payload.get("name") or "",
        "roles": roles,
    }
