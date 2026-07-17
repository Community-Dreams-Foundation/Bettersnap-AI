import pyodbc

_connection = None


def _conn_str():
    # Lazy import so importing this module (and new_connection) doesn't pull the
    # Azure SDK — lets the concurrency integration tests run with only pyodbc +
    # a test SQL Server, no Azure deps.
    from .keyvault import get_secret
    password = get_secret("Db-Password")
    return (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        "SERVER=bettersnap-srv.database.windows.net,1433;"
        "DATABASE=bettersnap-db;"
        "UID=CloudSAe874642e;"
        f"PWD={password};"
        "Encrypt=yes;"
        "TrustServerCertificate=no;"
        "Connection Timeout=60;"
    )


def get_db():
    """Cached connection for simple read endpoints AND single-statement writes
    (register, accept_terms, update_profile, delete_job, admin requeue/fail).

    autocommit=True is DELIBERATE and load-bearing: pyodbc defaults to
    autocommit=False, which opens an implicit transaction on the first statement.
    On this *cached, never-closed* connection, any handler that executed a write
    (or even a SELECT) and then failed/timed out before conn.commit() left that
    transaction OPEN — holding the row/table lock forever. Concurrent signups then
    blocked on that lock and chained into a pile-up of stuck sessions that took
    registration down product-wide (every new user's INSERT hung). With autocommit
    each statement commits immediately, so no lock can outlive a request and the
    leftover conn.commit() calls in these handlers become harmless no-ops.

    For MULTI-statement atomic work (daily-cap check+insert, GPU dispatch lease,
    Stripe webhook grants) use new_connection() instead — those manage their own
    transaction and close the connection in a finally (close → rollback), so they
    cannot leak. This connection is still NOT safe for concurrent use across
    threads; the atomic paths must not share it.
    """
    global _connection
    try:
        if _connection is not None:
            _connection.cursor().execute("SELECT 1")
            return _connection
    except Exception:
        _connection = None

    _connection = pyodbc.connect(_conn_str(), autocommit=True)
    return _connection


def new_connection():
    """Fresh, isolated connection for transactional work. Caller must close it."""
    return pyodbc.connect(_conn_str())
