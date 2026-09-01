"""Focused contracts for the bounded customer dashboard and paginated history APIs."""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import function_app


class _Request:
    def __init__(self, params=None, token="token", route_params=None):
        self.headers = {"Authorization": f"Bearer {token}"}
        self.params = params or {}
        self.route_params = route_params or {}


class _Cursor:
    def __init__(self, recent_rows=(), total_images=0, history_rows=(), history_total=0, detail_row=None):
        self.recent_rows = list(recent_rows)
        self.total_images = total_images
        self.history_rows = list(history_rows)
        self.history_total = history_total
        self.detail_row = detail_row
        self._one = None
        self._all = []

    def execute(self, sql, *params):
        normalized = " ".join(sql.split()).lower()
        if "select coalesce(sum" in normalized:
            self._one = (self.total_images,)
        elif "select top 6" in normalized:
            self._all = self.recent_rows
        elif "select count(*) from jobs" in normalized:
            self._one = (self.history_total,)
        elif "offset ? rows fetch next ? rows only" in normalized:
            self._all = self.history_rows
        elif "from jobs where job_id = ? and user_id = ?" in normalized:
            self._one = self.detail_row
        else:
            raise AssertionError(f"Unexpected SQL: {normalized}")

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class DashboardSummaryTests(unittest.TestCase):
    def _body(self, response):
        return json.loads(response.get_body().decode())

    def test_summary_is_bounded_and_signs_only_completed_jobs(self):
        rows = [
            ("job-complete", "completed", "generate", "linkedin", '["results/a.png"]', None, None),
            ("job-running", "processing", "generate", "resume", None, None, None),
        ]
        cursor = _Cursor(recent_rows=rows, total_images=14)
        subscription = {"subscription_type": "monthly", "credits_remaining": 180}
        with mock.patch.object(function_app, "get_user_id", return_value="user-1"), \
             mock.patch.object(function_app, "get_db", return_value=_Connection(cursor)), \
             mock.patch.object(function_app, "_subscription_status_payload", return_value=subscription), \
             mock.patch.object(function_app, "_signed_result_urls", return_value=["https://signed/a"]):
            response = function_app.user_dashboard_summary(_Request())

        body = self._body(response)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["subscription"], subscription)
        self.assertEqual(body["total_images_generated"], 14)
        self.assertEqual(len(body["recent_jobs"]), 2)
        self.assertEqual(body["recent_jobs"][0]["result_urls"], ["https://signed/a"])
        self.assertEqual(body["recent_jobs"][1]["result_urls"], [])

    def test_summary_rejects_missing_or_invalid_token(self):
        with mock.patch.object(function_app, "get_user_id", side_effect=ValueError("bad token")):
            response = function_app.user_dashboard_summary(_Request())
        self.assertEqual(response.status_code, 401)

    def test_summary_does_not_sign_expired_one_time_results(self):
        old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=4)
        rows = [("job-old", "completed", "generate", "linkedin", '["results/old.png"]', old, old)]
        cursor = _Cursor(recent_rows=rows)
        subscription = {"subscription_type": "one_time", "credits_remaining": 0}
        with mock.patch.object(function_app, "get_user_id", return_value="user-1"), \
             mock.patch.object(function_app, "get_db", return_value=_Connection(cursor)), \
             mock.patch.object(function_app, "_subscription_status_payload", return_value=subscription), \
             mock.patch.object(function_app, "_signed_result_urls") as signed:
            response = function_app.user_dashboard_summary(_Request())

        self.assertEqual(self._body(response)["recent_jobs"][0]["result_urls"], [])
        signed.assert_not_called()


class UserJobsPaginationTests(unittest.TestCase):
    def _body(self, response):
        return json.loads(response.get_body().decode())

    def test_jobs_clamps_page_values_and_returns_pagination_metadata(self):
        rows = [("job-1", "completed", "generate", "linkedin", '["results/a.png"]', None, None)]
        cursor = _Cursor(history_rows=rows, history_total=45)
        with mock.patch.object(function_app, "get_user_id", return_value="user-1"), \
             mock.patch.object(function_app, "get_db", return_value=_Connection(cursor)):
            response = function_app.user_jobs(_Request({"limit": "1000", "offset": "-4"}))

        body = self._body(response)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["total"], 45)
        self.assertEqual(body["limit"], 100)
        self.assertEqual(body["offset"], 0)
        self.assertEqual(body["jobs"][0]["output_blob_path"], ["results/a.png"])

    def test_direct_job_detail_stays_available_outside_history_page(self):
        job_id = "11111111-1111-4111-8111-111111111111"
        row = (job_id, "completed", "generate", "linkedin", '{"gender":"female"}',
               '["results/a.png"]', None, None)
        cursor = _Cursor(detail_row=row)
        with mock.patch.object(function_app, "get_user_id", return_value="user-1"), \
             mock.patch.object(function_app, "get_db", return_value=_Connection(cursor)):
            response = function_app.user_job_details(_Request(route_params={"job_id": job_id}))

        body = self._body(response)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["job_id"], job_id)
        self.assertEqual(body["job_params"]["gender"], "female")


if __name__ == "__main__":
    unittest.main(verbosity=2)
