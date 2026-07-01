"""Concurrency integration tests — require a REAL SQL Server.

These prove the guarantees that can't be unit-tested with mocks, by running the
ACTUAL production code (shared/job_reservation.py, shared/gpu_lease.py) from many
threads at once:

  - PER_USER_DAILY_CAP: 10 simultaneous submits from one user -> exactly 5 succeed
  - GLOBAL_DAILY_CAP:    50 simultaneous global submits        -> exactly 25 succeed
  - dispatch lease:      N threads acquire the lease           -> exactly 1 wins
  - missing lease row:   acquire fails closed (returns None)

Gated: skipped unless TEST_SQL_CONN is set to a full pyodbc connection string for
a DISPOSABLE test database, e.g.

  export TEST_SQL_CONN="DRIVER={ODBC Driver 18 for SQL Server};SERVER=...;DATABASE=bettersnap_test;UID=...;PWD=...;Encrypt=yes"
  python -m unittest tests.test_concurrency_integration

setUp creates its own users/jobs/GpuDispatchLease tables (DROP+CREATE) in that
DB, so it is self-contained and safe to re-run. DO NOT point it at production.
"""
import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

TEST_SQL_CONN = os.environ.get("TEST_SQL_CONN")


@unittest.skipUnless(TEST_SQL_CONN, "set TEST_SQL_CONN to run concurrency integration tests")
class ConcurrencyIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import pyodbc
        cls.pyodbc = pyodbc
        # Point the production modules' connection factory at the test DB.
        from unittest import mock
        cls._patchers = [
            mock.patch("shared.job_reservation.new_connection", new=cls._connect),
            mock.patch("shared.gpu_lease.new_connection", new=cls._connect),
        ]
        for p in cls._patchers:
            p.start()

    @classmethod
    def tearDownClass(cls):
        for p in cls._patchers:
            p.stop()

    @staticmethod
    def _connect():
        import pyodbc
        return pyodbc.connect(TEST_SQL_CONN)

    def _exec(self, sql):
        conn = self._connect()
        try:
            conn.cursor().execute(sql)
            conn.commit()
        finally:
            conn.close()

    def setUp(self):
        # Fresh, self-contained schema in the test DB.
        self._exec("IF OBJECT_ID('dbo.jobs','U') IS NOT NULL DROP TABLE dbo.jobs;")
        self._exec("IF OBJECT_ID('dbo.users','U') IS NOT NULL DROP TABLE dbo.users;")
        self._exec("IF OBJECT_ID('dbo.GpuDispatchLease','U') IS NOT NULL DROP TABLE dbo.GpuDispatchLease;")
        self._exec("""
            CREATE TABLE dbo.users (
                user_id VARCHAR(128) PRIMARY KEY,
                credits_remaining INT NOT NULL
            );""")
        self._exec("""
            CREATE TABLE dbo.jobs (
                job_id INT IDENTITY(1,1) PRIMARY KEY,
                user_id VARCHAR(128) NOT NULL,
                status VARCHAR(32) NOT NULL,
                input_blob_path VARCHAR(512) NULL,
                job_params NVARCHAR(MAX) NULL,
                external_execution_id VARCHAR(128) NULL,
                created_at DATETIME2 NOT NULL DEFAULT GETUTCDATE()
            );""")
        self._exec("""
            CREATE TABLE dbo.GpuDispatchLease (
                lease_name VARCHAR(64) PRIMARY KEY,
                owner_id VARCHAR(128) NULL,
                expires_at DATETIME2 NULL,
                last_dispatch_at DATETIME2 NULL
            );""")
        self._exec("INSERT INTO dbo.GpuDispatchLease (lease_name) VALUES ('gpu-dispatch');")

    def test_per_user_daily_cap_under_concurrency(self):
        from shared.job_reservation import reserve_job_slot
        self._exec("INSERT INTO dbo.users (user_id, credits_remaining) VALUES ('u1', 1000);")

        def submit(_):
            return reserve_job_slot("u1", "inputs/u1/x.jpg", "{}", per_user_cap=5, global_cap=10_000).ok

        with ThreadPoolExecutor(max_workers=10) as ex:
            results = list(ex.map(submit, range(10)))
        self.assertEqual(sum(results), 5, "exactly 5 should pass the per-user cap")

    def test_global_daily_cap_under_concurrency(self):
        from shared.job_reservation import reserve_job_slot
        for i in range(50):
            self._exec(f"INSERT INTO dbo.users (user_id, credits_remaining) VALUES ('g{i}', 1000);")

        def submit(i):
            return reserve_job_slot(f"g{i}", "inputs/x.jpg", "{}", per_user_cap=1000, global_cap=25).ok

        with ThreadPoolExecutor(max_workers=50) as ex:
            results = list(ex.map(submit, range(50)))
        self.assertEqual(sum(results), 25, "exactly 25 should pass the global cap")

    def test_dispatch_lease_single_winner(self):
        from shared.gpu_lease import acquire_dispatch_lease

        def grab(_):
            return acquire_dispatch_lease() is not None

        with ThreadPoolExecutor(max_workers=20) as ex:
            results = list(ex.map(grab, range(20)))
        self.assertEqual(sum(results), 1, "exactly one thread should win the lease")

    def test_missing_lease_row_fails_closed(self):
        from shared.gpu_lease import acquire_dispatch_lease
        self._exec("DELETE FROM dbo.GpuDispatchLease;")
        self.assertIsNone(acquire_dispatch_lease(), "no lease row -> must fail closed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
