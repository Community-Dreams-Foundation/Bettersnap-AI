"""Regression tests for the GPU container's self-marked failure path.

The container refunds a failed job itself, in its own process, with its own SQL. That
refund has to be the exact inverse of reserve_job_slot's debit — and for a long time it
was the inverse of only one of the four branches that debit uses:

    UPDATE users SET credits_remaining = credits_remaining + ?

which is the legacy MIRROR column. Spendable balance lives in the BUCKET columns, so a
one-time customer whose job failed saw a dashboard reading 30 credits (it falls back to
the mirror when both buckets are 0) while their next submit was refused 402 for no
credits. Org members had it worse: their seat pool paid, and the refund landed in
users.credits_remaining — the wrong table entirely.

These tests pin the refund to the balance that actually paid, one case per debit branch.
"""
import ast
import json
import logging
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN_PATH = ROOT / "main.py"

# The two functions under test, lifted out of main.py without importing it (main.py pulls
# in torch/diffusers/azure at module scope and cannot load on a CPU test runner).
_WANTED = ("update_job_status", "refund_failed_job")


def _load_update_job_status(connection_factory):
    tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"), filename=str(MAIN_PATH))
    wanted = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in _WANTED
    ]
    missing = set(_WANTED) - {node.name for node in wanted}
    assert not missing, f"main.py no longer defines {missing}"

    module = ast.Module(body=wanted, type_ignores=[])
    namespace = {
        "json": json,
        "time": type("NoSleep", (), {"sleep": staticmethod(lambda _seconds: None)}),
        "log": logging.getLogger("container-refund-test"),
        "get_db_connection": connection_factory,
    }
    exec(compile(module, str(MAIN_PATH), "exec"), namespace)
    return namespace["update_job_status"]


class _Cursor:
    """Fake cursor that records every statement and answers the job-row SELECT."""

    def __init__(self, transition_rowcount=1, job_row=None, org_member_exists=True):
        self.transition_rowcount = transition_rowcount
        self.job_row = job_row
        self.org_member_exists = org_member_exists
        self.rowcount = 0
        self.executed = []
        self._next_fetch = None

    def execute(self, sql, *params):
        normalized = " ".join(sql.lower().split())
        self.executed.append((normalized, params))
        if "update jobs" in normalized:
            self.rowcount = self.transition_rowcount
        elif "select user_id, organization_id" in normalized:
            self._next_fetch = self.job_row
        elif "update organization_members" in normalized:
            self.rowcount = 1 if self.org_member_exists else 0
        return self

    def fetchone(self):
        return self._next_fetch

    # -- assertions helpers -------------------------------------------------
    def statements(self, needle):
        return [params for sql, params in self.executed if needle in sql]

    @property
    def touched_users(self):
        return any(sql.startswith("update users") for sql, _ in self.executed)

    @property
    def touched_org(self):
        return any("update organization_members" in sql for sql, _ in self.executed)


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


def _run(cursor):
    connection = _Connection(cursor)
    update = _load_update_job_status(lambda: connection)
    update("job-1", "failed", max_attempts=1)
    return connection


def _params(**kw):
    return json.dumps(kw)


class ContainerFailureRefundTests(unittest.TestCase):
    def test_one_time_job_refunds_the_bucket_not_just_the_mirror(self):
        """THE BUG: a one-time refund that only credits the mirror is unspendable.

        reserve_job_slot gates the next submit on one_time_credits_remaining, so returning
        30 credits to credits_remaining alone leaves the customer refused at 402 while the
        dashboard cheerfully reads 30."""
        cursor = _Cursor(job_row=("user-1", None, "one_time",
                                  _params(credit_cost=30, monthly_credit_cost=0,
                                          one_time_credit_cost=30)))
        _run(cursor)

        bucket = cursor.statements("one_time_credits_remaining = one_time_credits_remaining + ?")
        self.assertEqual(bucket, [(30, 30, "user-1")])
        # And specifically NOT the old mirror-only statement.
        self.assertEqual(
            cursor.statements("update users set credits_remaining = credits_remaining + ?"), [])

    def test_monthly_job_refunds_both_buckets_using_the_recorded_split(self):
        """A monthly charge can overflow into the one-time bucket, so the refund has to
        follow the split reserve_job_slot recorded rather than dump the total anywhere."""
        cursor = _Cursor(job_row=("user-1", None, "monthly",
                                  _params(credit_cost=30, monthly_credit_cost=18,
                                          one_time_credit_cost=12)))
        _run(cursor)

        split = cursor.statements("monthly_credits_remaining = monthly_credits_remaining + ?")
        self.assertEqual(split, [(18, 12, 18, 12, "user-1")])

    def test_org_job_refunds_the_seat_pool_and_never_the_user(self):
        """The seat pool paid. Crediting users.credits_remaining instead would repay the
        wrong balance AND leave the member's seat short."""
        cursor = _Cursor(job_row=("user-1", "org-9", "one_time",
                                  _params(credit_cost=30, one_time_credit_cost=30)))
        _run(cursor)

        self.assertEqual(cursor.statements("update organization_members"), [(30, "user-1", "org-9")])
        self.assertFalse(cursor.touched_users, "org refund must not touch users")

    def test_org_refund_with_no_membership_row_refunds_nothing(self):
        """No membership row means nothing was repaid. Do not silently redirect the money
        to the user's personal balance to make the write 'succeed'."""
        cursor = _Cursor(job_row=("user-1", "org-9", "one_time",
                                  _params(credit_cost=30, one_time_credit_cost=30)),
                         org_member_exists=False)
        _run(cursor)

        self.assertFalse(cursor.touched_users)

    def test_legacy_row_without_a_recorded_split_refunds_the_mirror(self):
        """The split is written by the same branch that debits the buckets, so its absence
        proves the debit came from the mirror alone. Mirror-only is the correct inverse
        here — not a fallback guess."""
        cursor = _Cursor(job_row=("user-1", None, None, _params(credit_cost=4)))
        _run(cursor)

        self.assertEqual(
            cursor.statements("update users set credits_remaining = credits_remaining + ?"),
            [(4, "user-1")])

    def test_missing_credit_cost_still_refunds_one(self):
        cursor = _Cursor(job_row=("user-1", None, None, None))
        _run(cursor)

        self.assertEqual(
            cursor.statements("update users set credits_remaining = credits_remaining + ?"),
            [(1, "user-1")])

    def test_already_terminal_job_is_not_refunded_again(self):
        cursor = _Cursor(transition_rowcount=0,
                         job_row=("user-1", None, "one_time",
                                  _params(credit_cost=30, one_time_credit_cost=30)))
        connection = _run(cursor)

        self.assertFalse(cursor.touched_users)
        self.assertFalse(cursor.touched_org)
        self.assertEqual(connection.commits, 1)

    def test_refund_is_committed(self):
        cursor = _Cursor(job_row=("user-1", None, "one_time",
                                  _params(credit_cost=30, one_time_credit_cost=30)))
        connection = _run(cursor)
        self.assertEqual(connection.commits, 1)

    def test_generation_exception_routes_through_failed_status_writer(self):
        source = MAIN_PATH.read_text(encoding="utf-8")
        self.assertIn('update_job_status(job_id, "failed")', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
