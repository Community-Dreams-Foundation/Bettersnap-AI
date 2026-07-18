"""Key Vault access with an in-process secret cache.

Secrets change rarely, so caching them with a short TTL removes a network round-trip to
Key Vault from EVERY DB connection and EVERY webhook, and avoids Key Vault's request-rate
throttling (~2000 req/10s) under real concurrency. The SecretClient (and its
DefaultAzureCredential) is built LAZILY on first use — not at import — so importing this
module never fails just because a managed identity isn't ready yet (mirrors the lazy
JWKS client in shared/auth.py).
"""
import os
import time
from threading import Lock

# Config, not hardcoded. Falls back to the known vault so existing deploys keep working.
VAULT_URL = os.environ.get("AZURE_KEYVAULT_URL", "https://bettersnapkeyvault.vault.azure.net/")

# How long a fetched secret is trusted before we re-read it. A rotated secret takes at
# most this long to propagate. Override with KEYVAULT_CACHE_TTL_SECONDS.
_SECRET_TTL = int(os.environ.get("KEYVAULT_CACHE_TTL_SECONDS", "600"))

_cache: "dict[str, tuple[float, str]]" = {}
_cache_lock = Lock()

_client = None
_client_lock = Lock()


def _get_client():
    """Build the SecretClient + credential once, lazily, on first use."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                # Import the Azure SDK lazily so `import shared.keyvault` stays cheap and
                # dependency-free (the concurrency tests import shared.db without Azure).
                from azure.identity import DefaultAzureCredential
                from azure.keyvault.secrets import SecretClient
                _client = SecretClient(vault_url=VAULT_URL, credential=DefaultAzureCredential())
    return _client


def get_secret(secret_name: str) -> str:
    """Return a secret's value, served from an in-process TTL cache when fresh."""
    now = time.time()
    with _cache_lock:
        cached = _cache.get(secret_name)
        if cached and (now - cached[0]) < _SECRET_TTL:
            return cached[1]
    # Fetch outside the lock (network call); last-writer-wins on the cache is fine.
    value = _get_client().get_secret(secret_name).value
    with _cache_lock:
        _cache[secret_name] = (now, value)
    return value
