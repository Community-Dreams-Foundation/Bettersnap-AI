"""Every timestamp that reaches JSON must carry the UTC marker.

THE BUG THIS EXISTS TO PREVENT
------------------------------
Timestamp columns are written with GETUTCDATE(), so the stored value IS UTC — but pyodbc
returns a NAIVE datetime, and both str() and .isoformat() then emit it with no offset:

    str(dt)         -> "2026-07-24 18:25:00"
    dt.isoformat()  -> "2026-07-24T18:25:00"

JavaScript's Date() parses a string with no offset as LOCAL time, so the browser treated a
UTC instant as already-local and rendered it unshifted. A customer in EDT saw a job they
created at 2:25 PM stamped "06:25 PM" — four hours in the future.

Note that .isoformat() looked correct and was equally broken, which is why the source guard
below rejects BOTH spellings rather than just str().

Fixed in the API, not the UI: the frontend already calls toLocaleString(undefined, ...), so
once the instant is unambiguous every viewer sees their OWN timezone. Formatting a fixed
timezone in the UI would have fixed one customer and broken everyone outside it.

Run: python -m unittest tests.test_utc_timestamps   (from the backend dir)
"""
import os
import re
import sys
import unittest
from datetime import datetime, timezone

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)


def _utc_iso(dt):
    """Mirror of function_app._utc_iso (importing function_app needs the Azure host)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class UtcIsoBehaviourTests(unittest.TestCase):
    def test_naive_datetime_is_marked_utc(self):
        # what pyodbc hands back for a GETUTCDATE() column
        self.assertEqual(_utc_iso(datetime(2026, 7, 24, 18, 25, 0)),
                         "2026-07-24T18:25:00Z")

    def test_output_is_unambiguous_to_javascript(self):
        """The whole point: the string must end in Z, and must NOT be the space-separated
        form, or Date() falls back to local-time parsing."""
        s = _utc_iso(datetime(2026, 7, 24, 18, 25, 0))
        self.assertTrue(s.endswith("Z"), f"no UTC marker: {s}")
        self.assertIn("T", s, f"space-separated form is parsed as local: {s}")
        self.assertNotIn("+00:00", s)

    def test_already_aware_datetime_is_idempotent(self):
        aware = datetime(2026, 7, 24, 18, 25, 0, tzinfo=timezone.utc)
        self.assertEqual(_utc_iso(aware), "2026-07-24T18:25:00Z")

    def test_non_utc_aware_datetime_is_converted_not_relabelled(self):
        """A tz-aware value in another zone must be CONVERTED to UTC, not stamped Z while
        keeping its wall-clock digits — that would shift the instant."""
        est = timezone(-__import__("datetime").timedelta(hours=4))
        self.assertEqual(_utc_iso(datetime(2026, 7, 24, 14, 25, 0, tzinfo=est)),
                         "2026-07-24T18:25:00Z")

    def test_none_passes_through(self):
        """completed_at is NULL while a job is still running — must not become "None"."""
        self.assertIsNone(_utc_iso(None))

    def test_microseconds_survive(self):
        self.assertEqual(_utc_iso(datetime(2026, 7, 24, 18, 25, 0, 123456)),
                         "2026-07-24T18:25:00.123456Z")


class NoNaiveSerialisationTests(unittest.TestCase):
    """Source guard: a new endpoint that hand-rolls str(row[n]) for a timestamp would
    reintroduce the bug silently, because the value still LOOKS like a date in the JSON."""

    def _src(self):
        with open(os.path.join(BACKEND_DIR, "function_app.py"), encoding="utf-8") as f:
            return f.read()

    def test_helper_exists(self):
        self.assertIn("def _utc_iso(", self._src())

    def test_no_timestamp_field_uses_str_or_bare_isoformat(self):
        # matches  "..._at": str(x)   and   "..._at": x.isoformat()
        bad = re.findall(
            r'"[a-z_]*(?:at|_time)"\s*:\s*(?:str\(|[A-Za-z0-9_\[\]]+\.isoformat)',
            self._src())
        self.assertEqual(bad, [], f"timestamp serialised without a UTC marker: {bad}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
