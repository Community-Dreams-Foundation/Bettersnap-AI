"""Regression tests for the GPU container's self-marked failure path."""
import ast
import json
import logging
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN_PATH = ROOT / "main.py"


def _load_update_job_status(connection_factory):
    """Load only this function from main.py, avoiding GPU/Azure imports."""
    tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"), filename=str(MAIN_PATH))
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "update_job_status"
    )
    module = ast.Module(body=[function], type_ignores=[])
    namespace = {
        "json": json,
        "time": type("NoSleep", (), {"sleep": staticmethod(lambda _seconds: None)}),
        "log": logging.getLogger("container-refund-test"),
        "get_db_connection": connection_factory,
    }
    exec(compile(module, str(MAIN_PATH), "exec"), namespace)
    return namespace["update_job_status"]


class _Cursor:
    def __init__(self, transition_rowcount, credit_cost):
        self.transition_rowcount = transition_rowcount
        self.credit_cost = credit_cost
        self.rowcount = 0
        self.executed = []

    def execute(self, sql, *params):
        normalized = " ".join(sql.lower().split())
        self.executed.append((normalized, params))
        if "update jobs" in normalized:
            self.rowcount = self.transition_rowcount
        return self

    def fetchone(self):
        return (json.dumps({"credit_cost": self.credit_cost}),)


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def close(self):
        pass


class ContainerFailureRefundTests(unittest.TestCase):
    def test_container_failed_transition_refunds_full_credit_cost(self):
        cursor = _Cursor(transition_rowcount=1, credit_cost=30)
        connection = _Connection(cursor)
        update = _load_update_job_status(lambda: connection)

        update("job-1", "failed", max_attempts=1)

        refunds = [params for sql, params in cursor.executed
                   if "credits_remaining = credits_remaining + ?" in sql]
        self.assertEqual(refunds, [(30, "job-1")])
        self.assertEqual(connection.commits, 1)

    def test_already_terminal_job_is_not_refunded_again(self):
        cursor = _Cursor(transition_rowcount=0, credit_cost=30)
        connection = _Connection(cursor)
        update = _load_update_job_status(lambda: connection)

        update("job-1", "failed", max_attempts=1)

        self.assertFalse(any("update users" in sql for sql, _ in cursor.executed))
        self.assertEqual(connection.commits, 1)

    def test_generation_exception_routes_through_failed_status_writer(self):
        source = MAIN_PATH.read_text(encoding="utf-8")
        self.assertIn('update_job_status(job_id, "failed")', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
