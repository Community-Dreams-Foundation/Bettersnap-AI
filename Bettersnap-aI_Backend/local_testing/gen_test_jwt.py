"""
Mint a test JWT for local upload testing.

shared/auth.py decodes with verify_signature=False, so the signature is never
checked locally — any HS256 token with a "sub" claim is accepted. The only
claim the backend reads on /upload and /jobs/submit is "sub" (the user_id).

Usage:
    python local_testing/gen_test_jwt.py --sub test-alice
    python local_testing/gen_test_jwt.py            # uses USER_ID from .env
"""
import argparse
import os

import jwt  # from pyjwt[crypto], already in requirements.txt


def load_env_user():
    # cheap .env read for USER_ID without extra deps
    if os.path.exists(".env"):
        with open(".env", encoding="utf-8") as fh:
            for line in fh:
                if line.strip().startswith("USER_ID="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sub", default=load_env_user() or "test-alice",
                    help="user_id to embed as the 'sub' claim")
    ap.add_argument("--email", default="test@example.com")
    ap.add_argument("--name", default="Test User")
    args = ap.parse_args()

    payload = {"sub": args.sub, "email": args.email, "name": args.name}
    # secret is irrelevant locally (signature not verified)
    token = jwt.encode(payload, "local-dev-not-verified", algorithm="HS256")
    print(token)


if __name__ == "__main__":
    main()
