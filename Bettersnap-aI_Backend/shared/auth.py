import jwt
import logging
from shared.keyvault import get_secret

_secret = None

def get_secret_key():
    global _secret
    if _secret is None:
        _secret = get_secret("supabase-jwt-secret")
    return _secret

def validate_token(token: str) -> dict:
    try:
        secret = get_secret_key()
        payload = jwt.decode(
            token,
            secret,
            algorithms=["HS256", "ES256"],
            options={"verify_aud": False, "verify_signature": False}
        )
        logging.info(f"Token decoded: sub={payload.get('sub')}")
        return payload
    except Exception as e:
        logging.error(f"Token validation failed: {str(e)}")
        raise

def get_user_id(token: str) -> str:
    payload = validate_token(token)
    return payload["sub"]