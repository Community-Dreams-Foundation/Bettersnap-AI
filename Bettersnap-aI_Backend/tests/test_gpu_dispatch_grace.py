"""Dispatch grace lifecycle tests; no Azure or SQL Server required."""
import importlib.util
import pathlib
import unittest
from unittest import mock


# test_dispatch_logic intentionally installs a lightweight shared.gpu_lease stub.
# Load the real source under a private module name so discovery order cannot replace it.
_PATH = pathlib.Path(__file__).resolve().parents[1] / "shared" / "gpu_lease.py"
_SPEC = importlib.util.spec_from_file_location("shared.gpu_lease_grace_test", _PATH)
gpu_lease = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gpu_lease)


class _Cursor:
    def __init__(self):
        self.executed = []

    def execute(self, sql, *params):
        self.executed.append((" ".join(sql.lower().split()), params))
        return self


class _Connection:
    def __init__(self):
        self.cur = _Cursor()
        self.committed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True

    def close(self):
        pass


class DispatchGraceTests(unittest.TestCase):
    def test_execution_visibility_clears_only_the_owners_pending_marker(self):
        conn = _Connection()
        with mock.patch.object(gpu_lease, "new_connection", return_value=conn):
            gpu_lease.clear_dispatch_pending("owner-1")

        self.assertTrue(conn.committed)
        sql, params = conn.cur.executed[0]
        self.assertIn("set last_dispatch_at = null", sql)
        self.assertIn("owner_id = ?", sql)
        self.assertEqual(params, (gpu_lease.LEASE_NAME, "owner-1"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
