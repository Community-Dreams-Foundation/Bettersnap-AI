"""Atomic retrain reservation tests (no Azure or SQL required)."""
import unittest
from unittest import mock

from shared.training_reservation import reserve_training_slot


class _Cursor:
    def __init__(self, state):
        self.state = state
        self._row = None
        self.rowcount = 0

    def execute(self, sql, *params):
        normalized = " ".join(sql.lower().split())
        self.state["sql"].append(normalized)
        if "sp_getapplock" in normalized:
            self._row = (0,)
        elif "select lora_status, credits_remaining" in normalized:
            # 6 cols now: the retrain gate reads the one_time/monthly BUCKETS (effective balance),
            # not just legacy. Model a one-time 'basic' user whose balance sits in one_time.
            self._row = (
                self.state["status"], self.state["credits"],
                self.state["retrain_count"], self.state["plan_name"],
                self.state["one_time_credits"], self.state["monthly_credits"],
            )
        elif "count(*) from lora_trainings" in normalized:
            self._row = (self.state["training_count"],)
        elif "insert into lora_trainings" in normalized:
            self.state["training_count"] += 1
            self.state["training_credit_costs"] = (params[-2], params[-1])
            self._row = (f"training-{self.state['training_count']}",)
            self.rowcount = 1
        elif "update users set lora_status = 'training'" in normalized:
            self.state["status"] = "training"
            if "retrain_count = retrain_count + 1" in normalized:
                # retrain UPDATE params: (monthly_debit, one_time_debit, charge, user_id)
                monthly_charge, one_time_charge, charge = map(int, params[:3])
                self.state["retrain_count"] += 1
                self.state["credits"] -= charge
                self.state["monthly_credits"] -= monthly_charge
                self.state["one_time_credits"] -= one_time_charge
            self.rowcount = 1
        elif "insert into outbox" in normalized:
            self._row = (1000 + self.state["training_count"],)
            self.rowcount = 1
        return self

    def fetchone(self):
        return self._row


class _Conn:
    def __init__(self, state):
        self.state = state
        self.autocommit = True

    def cursor(self):
        return _Cursor(self.state)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class RetrainReservationTests(unittest.TestCase):
    def test_two_forced_retrains_charge_and_insert_only_once(self):
        state = {
            "status": "ready", "credits": 100, "retrain_count": 1,
            "plan_name": "basic", "monthly_credits": 0, "one_time_credits": 100,
            "training_count": 0, "sql": [],
        }
        with mock.patch(
            "shared.training_reservation.new_connection",
            side_effect=lambda: _Conn(state),
        ):
            first = reserve_training_slot(
                "user-1", [{"blob": "img.jpg"}], "woman", True,
                free_retrains=1, retrain_credits=10, max_per_day=3,
            )
            second = reserve_training_slot(
                "user-1", [{"blob": "img.jpg"}], "woman", True,
                free_retrains=1, retrain_credits=10, max_per_day=3,
            )

        self.assertTrue(first.ok)
        self.assertFalse(second.ok)
        self.assertEqual(second.reason, "already_training")
        self.assertEqual(state["credits"], 90)          # charged once, not twice
        self.assertEqual(state["one_time_credits"], 90)
        self.assertEqual(state["training_credit_costs"], (0, 10))
        self.assertEqual(state["training_count"], 1)    # one GPU-bound run
        self.assertTrue(any("sp_getapplock" in sql for sql in state["sql"]))
        self.assertTrue(any("updlock" in sql and "holdlock" in sql for sql in state["sql"]))

    def test_monthly_retrain_uses_monthly_then_add_on_credits(self):
        state = {
            "status": "ready", "credits": 17, "retrain_count": 1,
            "plan_name": "monthly_pro", "monthly_credits": 4, "one_time_credits": 13,
            "training_count": 0, "sql": [],
        }
        with mock.patch(
            "shared.training_reservation.new_connection",
            side_effect=lambda: _Conn(state),
        ):
            result = reserve_training_slot(
                "user-1", [{"blob": "img.jpg"}], "woman", True,
                free_retrains=1, retrain_credits=10, max_per_day=3,
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.monthly_credits_charged, 4)
        self.assertEqual(result.one_time_credits_charged, 6)
        self.assertEqual(state["monthly_credits"], 0)
        self.assertEqual(state["one_time_credits"], 7)
        self.assertEqual(state["credits"], 7)
        self.assertEqual(state["training_credit_costs"], (4, 6))


if __name__ == "__main__":
    unittest.main(verbosity=2)
