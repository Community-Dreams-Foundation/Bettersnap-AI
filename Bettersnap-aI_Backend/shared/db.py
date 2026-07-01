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
    """Cached connection for simple read endpoints.

    NOTE: this is a single module-global connection and is NOT safe for
    concurrent use across threads. For transactional / locked critical sections
    (daily-cap check+insert, GPU dispatch lease) use new_connection() instead so
    each caller gets its own isolated transaction.
    """
    global _connection
    try:
        if _connection is not None:
            _connection.cursor().execute("SELECT 1")
            return _connection
    except Exception:
        _connection = None

    _connection = pyodbc.connect(_conn_str())
    return _connection


def new_connection():
    """Fresh, isolated connection for transactional work. Caller must close it."""
    return pyodbc.connect(_conn_str())
