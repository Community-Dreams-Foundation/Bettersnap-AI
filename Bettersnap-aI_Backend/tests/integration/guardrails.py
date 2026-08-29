"""Refuse to touch anything that is not the disposable local test database.

This module is PURE and has no DB dependency, so every rule below is unit-tested offline in
tests/test_integration_harness_offline.py without a server.

The harness runs destructive DDL (it creates and drops the whole schema), so the cost of a
misdirected connection is total. Every check here is a REFUSAL, not a warning:

  * host must be localhost / 127.0.0.1 / ::1
  * port must be 11433 -- the default SQL port 1433 is refused outright, so a stray local or
    tunnelled production instance cannot be hit even by accident
  * database must be exactly `bettersnap_test`
  * any Azure SQL hostname, or any known production database name, aborts

Credentials are never logged: `safe_summary()` renders host/port/database only, and the
password is read from the environment and passed straight to the driver.
"""
import os
import re

REQUIRED_PORT = 11433
REQUIRED_DATABASE = "bettersnap_test"
ALLOWED_HOSTS = ("localhost", "127.0.0.1", "::1")

# The port a real SQL Server listens on. Refused so a tunnel, a locally-installed instance, or
# a copy-pasted production string can never be the target.
FORBIDDEN_PORTS = (1433,)

# Substrings that mean "this is not a disposable local database".
FORBIDDEN_HOST_PATTERNS = (
    r"\.database\.windows\.net",
    r"\.database\.azure\.com",
    r"\.database\.chinacloudapi\.cn",
    r"\.database\.usgovcloudapi\.net",
    r"\.sql\.azuresynapse\.net",
    r"azure",
)

# Database names that exist outside this harness. Anything resembling them aborts.
FORBIDDEN_DATABASE_NAMES = ("bettersnap", "bettersnapdb", "bettersnap_prod",
                            "bettersnap-prod", "master", "msdb", "model", "tempdb")

PASSWORD_ENV = "MSSQL_SA_PASSWORD"


class UnsafeTarget(RuntimeError):
    """The requested target is not the disposable local test database."""


def check_host(host):
    value = (host or "").strip()
    lowered = value.lower()
    for pattern in FORBIDDEN_HOST_PATTERNS:
        if re.search(pattern, lowered):
            raise UnsafeTarget(
                "host %r matches the forbidden pattern %r; this harness runs destructive DDL "
                "and may only target a disposable local container" % (value, pattern))
    if lowered not in ALLOWED_HOSTS:
        raise UnsafeTarget(
            "host %r is not local; allowed hosts are %s"
            % (value, ", ".join(ALLOWED_HOSTS)))
    return lowered


def check_port(port):
    try:
        value = int(port)
    except (TypeError, ValueError):
        raise UnsafeTarget("port %r is not an integer" % (port,))
    if value in FORBIDDEN_PORTS:
        raise UnsafeTarget(
            "port %d is the default SQL Server port and is refused outright; the harness "
            "requires %d so a real instance cannot be reached by accident"
            % (value, REQUIRED_PORT))
    if value != REQUIRED_PORT:
        raise UnsafeTarget("port %d is not the required test port %d"
                           % (value, REQUIRED_PORT))
    return value


def check_database(database):
    value = (database or "").strip()
    if value != REQUIRED_DATABASE:
        lowered = value.lower()
        if lowered in FORBIDDEN_DATABASE_NAMES:
            raise UnsafeTarget(
                "database %r is a real database name; this harness only ever targets %r"
                % (value, REQUIRED_DATABASE))
        raise UnsafeTarget("database %r is not %r" % (value, REQUIRED_DATABASE))
    return value


def check_target(host, port, database):
    """All three, together. Returns the normalised triple or raises."""
    return check_host(host), check_port(port), check_database(database)


def read_password(env=None):
    """The SA password, from the environment only. Never a default, never a literal, and
    never returned anywhere it could be logged -- callers pass it straight to the driver."""
    source = os.environ if env is None else env
    value = source.get(PASSWORD_ENV)
    if not value:
        raise UnsafeTarget(
            "%s is not set. Generate a throwaway password into the environment for this run; "
            "the harness will not invent or default one." % PASSWORD_ENV)
    return value


def safe_summary(host, port, database):
    """What may be printed. Deliberately excludes any credential."""
    return "%s:%s/%s" % (host, port, database)


def redact(text, password=None):
    """Strip a password out of driver text before it reaches a log or a report."""
    if not text:
        return ""
    out = str(text)
    secret = password if password is not None else os.environ.get(PASSWORD_ENV)
    if secret:
        out = out.replace(secret, "***")
    # Also mask anything that looks like a connection-string credential.
    out = re.sub(r"(?i)(PWD|Password)\s*=\s*[^;]*", r"\1=***", out)
    return out


def connection_string(host, port, database, password, driver="ODBC Driver 18 for SQL Server"):
    """Build the ODBC string AFTER validating the target. Never logged."""
    host, port, database = check_target(host, port, database)
    return (
        "DRIVER={%s};SERVER=%s,%d;DATABASE=%s;UID=sa;PWD=%s;"
        "Encrypt=yes;TrustServerCertificate=yes;Connection Timeout=15;"
        % (driver, host, port, database, password)
    )
