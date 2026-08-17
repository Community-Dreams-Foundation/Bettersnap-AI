import os
import pyodbc

# Enable ODBC connection pooling process-wide (must be set before the first connect).
# With pooling, pyodbc.connect() with the same connection string hands back a physical
# connection from the driver-manager pool instead of doing a fresh TCP+TLS handshake — so
# opening a connection per request (below) is cheap, and the per-request connections are
# thread-isolated.
pyodbc.pooling = True

# Infra config from app settings, not hardcoded. Fallbacks preserve current deploys; set
# SQL_SERVER / SQL_DATABASE / SQL_UID / SQL_PASSWORD_SECRET to move environments.
_SERVER      = os.environ.get("SQL_SERVER",   "bettersnap-srv.database.windows.net")
_DATABASE    = os.environ.get("SQL_DATABASE", "bettersnap-db")
_UID         = os.environ.get("SQL_UID",      "CloudSAe874642e")
_TIMEOUT     = os.environ.get("SQL_CONN_TIMEOUT", "60")
_PWD_SECRET  = os.environ.get("SQL_PASSWORD_SECRET", "Db-Password")

# Required for inserts/updates on tables carrying filtered indexes (007_retention).
# Pooled SQL Server connections retain session state, so assert these on every checkout.
_REQUIRED_SESSION_OPTIONS = """
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
SET ANSI_PADDING ON;
SET ANSI_WARNINGS ON;
SET ARITHABORT ON;
SET CONCAT_NULL_YIELDS_NULL ON;
SET NUMERIC_ROUNDABORT OFF;
"""


def _conn_str():
    # Lazy import so importing this module doesn't pull the Azure SDK (the concurrency
    # tests import shared.db with only pyodbc + a test SQL Server). The Key Vault read is
    # cached in shared.keyvault, so this is a cheap in-process hit, not a network call.
    from .keyvault import get_secret
    password = get_secret(_PWD_SECRET)
    return (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        f"SERVER={_SERVER},1433;"
        f"DATABASE={_DATABASE};"
        f"UID={_UID};"
        f"PWD={password};"
        "Encrypt=yes;"
        "TrustServerCertificate=no;"
        f"Connection Timeout={_TIMEOUT};"
    )


def _connect(*, autocommit=False):
    conn = pyodbc.connect(_conn_str(), autocommit=autocommit)
    try:
        cur = conn.cursor()
        cur.execute(_REQUIRED_SESSION_OPTIONS)
        cur.execute(
            "SELECT SESSIONPROPERTY('QUOTED_IDENTIFIER'), "
            "SESSIONPROPERTY('ANSI_NULLS')"
        )
        if tuple(cur.fetchone()) != (1, 1):
            raise RuntimeError("required SQL session options are not enabled")
        return conn
    except Exception:
        conn.close()
        raise


def get_db():
    """Autocommit connection for simple reads and single-statement writes (register,
    accept_terms, update_profile, delete_job, admin requeue/fail, status endpoints).

    Returns a FRESH connection per call, drawn from the ODBC pool — NOT a shared module
    global. The old shared global was not safe under the Functions worker's concurrent
    invocations: two threads driving one pyodbc connection corrupts the protocol. A
    per-call connection is thread-isolated, and pooling means we don't pay a real TCP+TLS
    handshake each time.

    autocommit=True is DELIBERATE and load-bearing: pyodbc defaults to autocommit=False,
    which opens an implicit transaction on the first statement and can hold a lock past the
    request. With autocommit each statement self-commits, so no lock outlives a request and
    the leftover conn.commit() calls in these handlers stay harmless no-ops. Callers need
    not close it — it returns to the pool when it goes out of scope at the end of the
    invocation.

    For MULTI-statement atomic work (daily-cap check+insert, GPU dispatch lease, Stripe
    webhook grants) use new_connection() instead — it manages its own transaction and
    closes in a finally (close → rollback), so it cannot leak.
    """
    return _connect(autocommit=True)


def new_connection():
    """Fresh, isolated (non-autocommit) connection for transactional work. Caller must
    close it (close → rollback of anything uncommitted). Also draws from the ODBC pool."""
    return _connect()
