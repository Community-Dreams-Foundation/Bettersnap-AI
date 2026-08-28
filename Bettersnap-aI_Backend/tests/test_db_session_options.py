"""Regression tests for SQL options required by filtered indexes."""
import importlib.util
import pathlib
import sys
import types
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class Cursor:
    def __init__(self, properties=(1, 1)):
        self.properties = properties
        self.executed = []

    def execute(self, sql):
        self.executed.append(" ".join(sql.upper().split()))

    def fetchone(self):
        return self.properties


class Conn:
    def __init__(self, properties=(1, 1)):
        self.cur = Cursor(properties)
        self.closed = False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


class Pyodbc(types.ModuleType):
    pooling = False

    def __init__(self):
        super().__init__("pyodbc")
        self.connection = Conn()
        self.calls = []

    def connect(self, connection_string, **kwargs):
        self.calls.append((connection_string, kwargs))
        return self.connection


fake_pyodbc = Pyodbc()
original_pyodbc = sys.modules.get("pyodbc")
sys.modules["pyodbc"] = fake_pyodbc
spec = importlib.util.spec_from_file_location("db_under_test", ROOT / "shared" / "db.py")
db = importlib.util.module_from_spec(spec)
db.__package__ = "shared"
spec.loader.exec_module(db)
if original_pyodbc is None:
    del sys.modules["pyodbc"]
else:
    sys.modules["pyodbc"] = original_pyodbc
db._conn_str = lambda: "test"


class DatabaseSessionOptionTests(unittest.TestCase):
    def setUp(self):
        fake_pyodbc.connection = Conn()
        fake_pyodbc.calls.clear()

    def test_get_db_enables_and_asserts_filtered_index_options(self):
        conn = db.get_db()
        sql = " ".join(conn.cur.executed)
        self.assertIn("SET QUOTED_IDENTIFIER ON", sql)
        self.assertIn("SET ANSI_NULLS ON", sql)
        self.assertIn("SESSIONPROPERTY('QUOTED_IDENTIFIER')", sql)
        self.assertIn("CAST(SESSIONPROPERTY('QUOTED_IDENTIFIER') AS INT)", sql)
        self.assertIn("CAST(SESSIONPROPERTY('ANSI_NULLS') AS INT)", sql)
        self.assertEqual(fake_pyodbc.calls[0][1], {"autocommit": True})

    def test_new_connection_enables_same_options_without_autocommit(self):
        conn = db.new_connection()
        sql = " ".join(conn.cur.executed)
        self.assertIn("SET QUOTED_IDENTIFIER ON", sql)
        self.assertEqual(fake_pyodbc.calls[0][1], {"autocommit": False})

    def test_failed_assertion_closes_connection(self):
        fake_pyodbc.connection = Conn(properties=(0, 1))
        with self.assertRaisesRegex(RuntimeError, "session options"):
            db.new_connection()
        self.assertTrue(fake_pyodbc.connection.closed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
