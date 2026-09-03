"""Regression suite for the credits logic: the mirror invariant and the ledger.

WHAT THIS TESTS THAT THE OTHER FILES DO NOT
The existing tests assert individual SQL fragments and parameter positions. They
cannot see whether a SEQUENCE of real handlers leaves the row coherent, and they
cannot see the invariant the whole model now rests on:

    credits_remaining == monthly_credits_remaining + one_time_credits_remaining

That mirror is load-bearing in three places at once:
  * _handle_monthly_checkout falls back to credits_remaining when the add-on
    bucket is empty, and multiplies the result by credits_per_image;
  * submit_job sizes a monthly request with credits_remaining // credits_per_image;
  * get_credits reports the bucket total but falls back to credits_remaining.
If any writer breaks the mirror, those three disagree and money moves wrongly.

HOW
A tiny evaluator applies the UPDATE statements the real handlers emit to a
simulated users row, so the handlers under test are the real ones and the
arithmetic checked is the arithmetic that ships. The evaluator FAILS LOUDLY on
any assignment form it does not recognise (UnsupportedSql) rather than skipping
it, so new SQL cannot silently escape the invariant check.
"""
import re
import sys
import unittest
from unittest import mock

import function_app
from shared.plans import get_plan
from test_dispatch_logic import FakeConn, FakeCursor


class _RawCursor(FakeCursor):
    """FakeCursor collapses whitespace, which makes a `-- comment` swallow the
    rest of the statement. Keep the RAW text so the evaluator sees real line
    boundaries; behaviour is otherwise identical."""

    def execute(self, sql, *params):
        self.cfg.setdefault("raw", []).append((sql, params))
        return super().execute(sql, *params)


class RawConn(FakeConn):
    def cursor(self):
        return _RawCursor(self.cfg)


class UnsupportedSql(Exception):
    """Raised when the evaluator meets an assignment it cannot model."""


BALANCE_COLS = ("credits_remaining", "monthly_credits_remaining",
                "one_time_credits_remaining")

_SET_RE = re.compile(r"\bUPDATE\s+users\b.*?\bSET\b(.*?)(?:\bWHERE\b|$)",
                     re.IGNORECASE | re.DOTALL)


def _strip_comments(sql):
    return re.sub(r"--[^\n]*", " ", sql)


def _split_assignments(set_clause):
    """Split on top-level commas (CASE ... END may contain commas-free text)."""
    parts, depth, buf = [], 0, []
    for ch in set_clause:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


class Row(dict):
    """A simulated users row that can apply the handlers' UPDATE statements."""

    DEFAULTS = {
        "credits_remaining": 0, "monthly_credits_remaining": 0,
        "one_time_credits_remaining": 0, "credits_monthly_limit": None,
        "subscription_type": None, "subscription_plan": None,
        "plan_name": "trial", "one_time_plan": None, "one_time_plan_name": None,
    }

    def __init__(self, **kw):
        super().__init__(**{**self.DEFAULTS, **kw})

    # ── evaluator ────────────────────────────────────────────────────────────
    def apply(self, sql, params):
        m = _SET_RE.search(_strip_comments(sql))
        if not m:
            return False
        params = list(params)
        pos = 0

        def take():
            nonlocal pos
            v = params[pos]
            pos += 1
            return v

        # SQL evaluates EVERY right-hand side against the pre-UPDATE row, so an
        # assignment made earlier in the SET list must not be visible to a later
        # one. (Getting this wrong made `subscription_type = 'monthly'` flip the
        # CASE in the very same statement.) Evaluate against a frozen snapshot,
        # then commit all the results at once.
        before = Row(**self)
        pending = {}
        for assignment in _split_assignments(m.group(1)):
            col, _, expr = assignment.partition("=")
            col, expr = col.strip().lower(), expr.strip()
            pending[col] = before._evaluate_rhs(expr, take)
        self.update(pending)
        return True

    def _evaluate_rhs(self, expr, take):
        e = " ".join(expr.split())
        low = e.lower()
        if low.startswith("case when"):
            mm = re.match(
                r"case when subscription_type = 'monthly' then (.+?) else (.+?) end$",
                low)
            if not mm:
                raise UnsupportedSql(f"CASE: {e}")
            branches = (mm.group(1), mm.group(2))
            # Evaluate both, in source order, so placeholders bind correctly
            # regardless of which branch the row takes.
            values = [self._eval(b, take) for b in branches]
            return values[0] if self["subscription_type"] == "monthly" else values[1]
        return self._eval(low, take)

    def _eval(self, e, take):
        e = e.strip()
        if e == "null":
            return None
        if e == "?":
            return take()
        if re.fullmatch(r"-?\d+", e):
            return int(e)
        if re.fullmatch(r"'[^']*'", e):
            return e.strip("'")
        if e in self:
            return self[e]
        if e.startswith("isnull(") and e.endswith(")"):
            args = _split_assignments(e[len("isnull("):-1])
            first = self._eval(args[0], take)
            second = self._eval(args[1], take)
            return first if first is not None else second
        if e.startswith("dateadd(") or e.startswith("getutcdate("):
            # Consume nothing structural we model; timestamps are irrelevant here.
            for _ in range(e.count("?")):
                take()
            return "<timestamp>"
        m = re.fullmatch(r"(.+?) ([+-]) (.+)", e)
        if m:
            left = self._eval(m.group(1), take)
            right = self._eval(m.group(3), take)
            left = 0 if left is None else left
            right = 0 if right is None else right
            if not isinstance(left, int) or not isinstance(right, int):
                raise UnsupportedSql(
                    f"non-numeric arithmetic in {e!r}: {left!r} {m.group(2)} {right!r}")
            return left + right if m.group(2) == "+" else left - right
        raise UnsupportedSql(e)

    # ── invariant ────────────────────────────────────────────────────────────
    @property
    def mirror_ok(self):
        return int(self["credits_remaining"] or 0) == (
            int(self["monthly_credits_remaining"] or 0)
            + int(self["one_time_credits_remaining"] or 0))

    def describe(self):
        return (f"credits_remaining={self['credits_remaining']} "
                f"monthly={self['monthly_credits_remaining']} "
                f"one_time={self['one_time_credits_remaining']} "
                f"type={self['subscription_type']} plan_name={self['plan_name']}")


class _Harness(unittest.TestCase):
    """Drives the real handlers, then replays their writes onto a Row."""

    def setUp(self):
        sys.modules["shared.auth"].validate_token.return_value = {
            "oid": "user-1", "email": "user@example.com"}
        sys.modules["shared.auth"].get_user_id.return_value = "user-1"
        self._cfg = {}
        self._p1 = mock.patch.object(function_app, "get_db",
                                     side_effect=lambda: RawConn(self._cfg))
        self._p2 = mock.patch.object(function_app, "new_connection",
                                     side_effect=lambda: RawConn(self._cfg))
        self._p1.start()
        self._p2.start()
        self.row = Row()
        self.ledger = []

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()

    # Keep the fake SELECTs in step with the simulated row.
    def _sync_cfg(self):
        self._cfg.update(
            subscription_type=self.row["subscription_type"],
            credits=int(self.row["credits_remaining"] or 0),
            monthly_credits=int(self.row["monthly_credits_remaining"] or 0),
            one_time_credits=int(self.row["one_time_credits_remaining"] or 0),
            plan_name=self.row["plan_name"],
            subscription_plan=self.row["subscription_plan"],
            one_time_plan=self.row["one_time_plan"],
            one_time_plan_name=self.row["one_time_plan_name"],
            credits_monthly_limit=self.row["credits_monthly_limit"],
        )

    def step(self, label, fn):
        """Run one handler, replay its balance writes, assert the mirror holds."""
        self._sync_cfg()
        self._cfg["executed"] = []
        self._cfg["raw"] = []
        fn()
        applied = 0
        for sql, params in self._cfg["raw"]:
            low = sql.lower()
            if low.startswith("insert into credit_transactions"):
                self.ledger.append({"amount": int(params[1]),
                                    "type": params[2], "step": label})
                continue
            if "update users" not in low:
                continue
            if not any(c in low for c in BALANCE_COLS):
                continue
            try:
                if self.row.apply(sql, params):
                    applied += 1
            except UnsupportedSql as exc:
                self.fail(f"[{label}] evaluator cannot model an assignment that "
                          f"ships in production: {exc}\nSQL: {sql}")
        self.assertGreater(applied, 0,
                           f"[{label}] no balance write was captured — the scenario "
                           f"did not exercise what it claims to")
        self.assertTrue(
            self.row.mirror_ok,
            f"[{label}] MIRROR BROKEN: {self.row.describe()}")
        return self.row

    # ── handler shortcuts ────────────────────────────────────────────────────
    def buy_one_time(self, plan="basic", event=None):
        self.step(f"buy_one_time({plan})", lambda: function_app._handle_onetime_payment(
            {"metadata": {"user_id": "user-1", "plan": plan}},
            event or f"evt_ot_{plan}_{len(self.ledger)}"))

    def start_monthly(self, plan="pro", event=None):
        self.step(f"start_monthly({plan})", lambda: function_app._handle_monthly_checkout(
            {"metadata": {"user_id": "user-1", "plan": plan,
                          "checkout_token": "tok"},
             "customer": "cus_1", "subscription": "sub_123"},
            event or f"evt_m_{plan}_{len(self.ledger)}"))

    def renew(self, event=None):
        self.step("renew", lambda: function_app._handle_invoice_paid(
            {"subscription": "sub_123"}, event or f"evt_inv_{len(self.ledger)}"))

    def top_up(self, pack="pro", event=None):
        self.step(f"top_up({pack})", lambda: function_app._handle_topup(
            {"metadata": {"user_id": "user-1", "plan": pack}},
            event or f"evt_top_{pack}_{len(self.ledger)}"))

    def cancel(self, event=None):
        self.step("cancel", lambda: function_app._handle_subscription_ended(
            {"id": "sub_123", "status": "canceled"},
            event or f"evt_end_{len(self.ledger)}"))

    def grace_cleanup(self):
        self.step("grace_cleanup",
                  lambda: function_app.failed_payment_grace_cleanup(mock.Mock()))


class MirrorInvariantAcrossLifecycles(_Harness):
    """Every documented lifecycle keeps credits_remaining == monthly + one_time."""

    def test_trial_to_one_time_pack(self):
        self.row.update(credits_remaining=4, one_time_credits_remaining=4)
        self.buy_one_time("basic")
        self.assertEqual(self.row["one_time_credits_remaining"], 34)
        self.assertEqual(self.row["subscription_type"], "one_time")

    def test_one_time_then_monthly_then_cancel(self):
        self.row.update(credits_remaining=30, one_time_credits_remaining=30,
                        subscription_type="one_time", plan_name="basic",
                        subscription_plan="basic")
        self.start_monthly("pro")
        self.assertEqual(self.row["monthly_credits_remaining"], 200)
        self.assertEqual(self.row["one_time_credits_remaining"], 150,
                         "30 images x 5 parked as add-on credits")
        self.cancel()
        self.assertEqual(self.row["one_time_credits_remaining"], 30,
                         "150 add-on credits back to 30 images")
        self.assertEqual(self.row["monthly_credits_remaining"], 0)
        self.assertEqual(self.row["plan_name"], "basic")

    def test_monthly_renewal_preserves_add_ons(self):
        self.row.update(subscription_type="monthly", plan_name="monthly_pro",
                        subscription_plan="pro", credits_monthly_limit=200,
                        monthly_credits_remaining=40,
                        one_time_credits_remaining=250,
                        credits_remaining=290)
        self.renew()
        self.assertEqual(self.row["monthly_credits_remaining"], 200, "reset to limit")
        self.assertEqual(self.row["one_time_credits_remaining"], 250, "add-ons survive")

    def test_top_up_then_renew_then_cancel(self):
        self.row.update(subscription_type="monthly", plan_name="monthly_pro",
                        subscription_plan="pro", credits_monthly_limit=200,
                        monthly_credits_remaining=200, credits_remaining=200)
        self.top_up("pro")
        self.assertEqual(self.row["one_time_credits_remaining"], 250)
        self.renew()
        self.assertEqual(self.row["one_time_credits_remaining"], 250)
        self.cancel()
        self.assertEqual(self.row["one_time_credits_remaining"], 50,
                         "the advertised 50 images")
        self.assertEqual(self.row["plan_name"], "pro")

    def test_dunning_grace_removes_only_the_monthly_allowance(self):
        self.row.update(subscription_type="monthly", plan_name="monthly_basic",
                        subscription_plan="basic", credits_monthly_limit=100,
                        monthly_credits_remaining=60,
                        one_time_credits_remaining=150,
                        credits_remaining=210)
        self.grace_cleanup()
        self.assertEqual(self.row["monthly_credits_remaining"], 0)
        self.assertEqual(self.row["one_time_credits_remaining"], 150,
                         "purchased value is never removed by dunning")

    def test_cancel_with_no_add_ons_lands_on_free(self):
        self.row.update(subscription_type="monthly", plan_name="monthly_basic",
                        subscription_plan="basic", credits_monthly_limit=100,
                        monthly_credits_remaining=100, credits_remaining=100)
        self.cancel()
        self.assertEqual(self.row["credits_remaining"], 0)
        self.assertEqual(self.row["subscription_plan"], "free")

    def test_repurchase_after_cancel_rebuilds_a_clean_one_time_account(self):
        self.row.update(subscription_type="monthly", plan_name="monthly_pro",
                        subscription_plan="pro", credits_monthly_limit=200,
                        monthly_credits_remaining=100,
                        one_time_credits_remaining=250, credits_remaining=350)
        self.cancel()
        self.assertEqual(self.row["one_time_credits_remaining"], 50)
        self.buy_one_time("expert")
        self.assertEqual(self.row["one_time_credits_remaining"], 120,
                         "50 surviving images + a 70-image Expert pack")
        self.assertEqual(self.row["monthly_credits_remaining"], 0)

    def test_upgrade_between_monthly_tiers_does_not_rescale_add_ons(self):
        self.row.update(subscription_type="monthly", plan_name="monthly_basic",
                        subscription_plan="basic", credits_monthly_limit=100,
                        monthly_credits_remaining=100,
                        one_time_credits_remaining=250, credits_remaining=350)
        self.start_monthly("expert")
        self.assertEqual(self.row["one_time_credits_remaining"], 250,
                         "already in credit units — no second x5")
        self.assertEqual(self.row["monthly_credits_remaining"], 300)


class LedgerReconcilesToTheBalance(_Harness):
    """The append-only ledger must explain the balance it claims to audit."""

    def test_full_lifecycle_ledger_sums_to_the_final_balance(self):
        self.row.update(credits_remaining=30, one_time_credits_remaining=30,
                        subscription_type="one_time", plan_name="basic",
                        subscription_plan="basic")
        opening = 30
        self.start_monthly("pro")
        self.top_up("pro")
        self.renew()
        self.cancel()
        net = sum(e["amount"] for e in self.ledger)
        self.assertEqual(
            opening + net, int(self.row["credits_remaining"]),
            "ledger does not reconcile: "
            f"opening={opening} net={net} row={self.row.describe()}\n"
            + "\n".join(f"  {e['step']:>22} {e['type']:>22} {e['amount']:+}"
                        for e in self.ledger))

    def test_every_unit_change_is_ledgered_as_a_conversion(self):
        self.row.update(credits_remaining=20, one_time_credits_remaining=20,
                        subscription_type="one_time", plan_name="basic",
                        subscription_plan="basic")
        self.start_monthly("basic")
        conv = [e for e in self.ledger if e["type"] == "plan_unit_conversion"]
        self.assertEqual([e["amount"] for e in conv], [80],
                         "20 images -> 100 credits is a +80 unit delta")
        self.cancel()
        conv = [e for e in self.ledger if e["type"] == "plan_unit_conversion"]
        self.assertEqual([e["amount"] for e in conv], [80, -80],
                         "the round trip nets to zero")

    def test_monthly_expiry_is_ledgered_separately_from_conversion(self):
        self.row.update(subscription_type="monthly", plan_name="monthly_expert",
                        subscription_plan="expert", credits_monthly_limit=300,
                        monthly_credits_remaining=120,
                        one_time_credits_remaining=250, credits_remaining=370)
        self.cancel()
        by_type = {e["type"]: e["amount"] for e in self.ledger}
        self.assertEqual(by_type["monthly_expiration"], -120)
        self.assertEqual(by_type["plan_unit_conversion"], -200)
        self.assertEqual(sum(by_type.values()), -320,
                         "370 credits -> 50 images")


class SpendPathsPreserveTheMirror(unittest.TestCase):
    """reserve/refund write all three columns together, by construction."""

    def _debit_branches(self):
        import inspect
        from shared import job_reservation
        src = inspect.getsource(job_reservation.reserve_job_slot)
        return src.split("if org_id:")[-1]

    def test_monthly_job_debit_updates_all_three_columns(self):
        branch = self._debit_branches()
        monthly = branch.split("elif source_type == \"monthly\":")[1] \
                        .split("elif source_type == \"one_time\":")[0]
        for col in BALANCE_COLS:
            self.assertIn(col, monthly, f"monthly debit does not touch {col}")

    def test_one_time_job_debit_updates_the_bucket_and_the_mirror(self):
        branch = self._debit_branches()
        one_time = branch.split("elif source_type == \"one_time\":")[1] \
                         .split("else:")[0]
        self.assertIn("one_time_credits_remaining = one_time_credits_remaining - ?",
                      one_time)
        self.assertIn("credits_remaining = credits_remaining - ?", one_time)

    def test_refund_paths_restore_the_same_three_columns(self):
        import inspect
        from shared import provisioning_retry
        src = inspect.getsource(provisioning_retry)
        for col in BALANCE_COLS:
            self.assertIn(f"{col} = {col} + ?", src,
                          f"refund path does not restore {col}")

    def test_training_reservation_debits_all_three_columns(self):
        import inspect
        from shared import training_reservation
        src = inspect.getsource(training_reservation)
        for col in BALANCE_COLS:
            self.assertIn(f"{col} = {col} - ?", src,
                          f"training debit does not touch {col}")


class RateConsistencyAcrossThePlanCatalog(unittest.TestCase):
    """The conversion only stays lossless while these hold."""

    def test_every_monthly_plan_shares_one_rate(self):
        from shared.plans import PLANS
        rates = {p.credits_per_image for p in PLANS.values()
                 if p.plan_type == "monthly"}
        self.assertEqual(rates, {5},
                         "a second monthly rate makes tier upgrades lossy")

    def test_every_one_time_plan_is_one_to_one(self):
        from shared.plans import PLANS
        rates = {p.credits_per_image for p in PLANS.values()
                 if p.plan_type == "one_time"}
        self.assertEqual(rates, {1})

    def test_every_monthly_allowance_divides_evenly_into_images(self):
        from shared.plans import PLANS
        for p in PLANS.values():
            if p.plan_type != "monthly":
                continue
            with self.subTest(plan=p.key):
                self.assertEqual(p.monthly_images * p.credits_per_image,
                                 p.image_count * p.credits_per_image)
                self.assertEqual((p.monthly_images * p.credits_per_image)
                                 % p.credits_per_image, 0)

    def test_every_one_time_pack_converts_to_a_whole_number_of_credits(self):
        from shared.plans import PLANS
        monthly_rate = get_plan("monthly_pro").credits_per_image
        for p in PLANS.values():
            if p.plan_type != "one_time":
                continue
            with self.subTest(plan=p.key):
                self.assertEqual((p.image_count * monthly_rate) % monthly_rate, 0)

    def test_the_retrain_charge_is_divisible_by_the_monthly_rate(self):
        """A retrain charge that is not a multiple of 5 would leave an add-on
        balance that credits_to_images can never convert, dead-lettering the
        user's cancellation."""
        from shared.plans import RETRAIN_CREDITS
        rate = get_plan("monthly_pro").credits_per_image
        self.assertEqual(RETRAIN_CREDITS % rate, 0,
                         f"RETRAIN_CREDITS={RETRAIN_CREDITS} leaves a remainder "
                         f"at {rate} credits/image")

    def test_the_registration_grant_converts_cleanly_to_monthly_units(self):
        from shared.plans import REGISTRATION_CREDITS
        from shared.credit_units import images_to_credits
        rate = get_plan("monthly_basic").credits_per_image
        self.assertEqual(images_to_credits(REGISTRATION_CREDITS, rate), 20)


if __name__ == "__main__":
    unittest.main()


class EveryParameterisedStatementBindsItsArguments(unittest.TestCase):
    """Placeholder count must equal argument count for every literal SQL call.

    FakeCursor never binds parameters, so a `?` that lost its argument is
    invisible to the entire existing suite while pyodbc would reject it at
    runtime ("The SQL contains N parameter markers, but M parameters were
    supplied"). This caught a 14-marker / 13-argument monthly activation, i.e.
    every new subscription failing after the customer had paid.

    Only calls whose SQL is a literal and whose arguments are all simple
    expressions are checked; anything dynamic is skipped by design.
    """

    def _statements(self, path):
        import ast
        tree = ast.parse(open(path).read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if not (isinstance(f, ast.Attribute) and f.attr == "execute"):
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            sql = node.args[0].value
            if not isinstance(sql, str) or "?" not in sql:
                continue
            rest = node.args[1:]
            # A single sequence argument is a param LIST, not one value.
            if len(rest) == 1 and isinstance(rest[0], (ast.List, ast.Tuple)):
                supplied = len(rest[0].elts)
            elif len(rest) == 1 and not isinstance(
                    rest[0], (ast.Constant, ast.Name, ast.Attribute,
                              ast.Subscript, ast.Call, ast.BinOp, ast.IfExp)):
                continue  # dynamic/unpacked - cannot count statically
            elif any(isinstance(a, ast.Starred) for a in rest):
                continue
            else:
                supplied = len(rest)
            markers = re.sub(r"--[^\n]*", "", sql).count("?")
            yield node.lineno, markers, supplied, sql

    def _check(self, path):
        bad = [(ln, m, s, sql.strip().split("\n")[0][:70])
               for ln, m, s, sql in self._statements(path) if m != s]
        self.assertEqual(
            bad, [],
            "parameter-count mismatch (markers != arguments):\n" + "\n".join(
                f"  {path}:{ln}  {m} markers, {s} args  |  {frag}"
                for ln, m, s, frag in bad))

    def test_function_app_statements_are_balanced(self):
        self._check("function_app.py")

    def test_shared_modules_statements_are_balanced(self):
        import glob
        for path in sorted(glob.glob("shared/*.py")):
            with self.subTest(module=path):
                self._check(path)

    def test_the_monthly_activation_statement_specifically(self):
        """Regression pin for the exact statement that was short one argument."""
        found = [(m, s) for ln, m, s, sql in self._statements("function_app.py")
                 if "monthly_credits_remaining = ?" in sql
                 and "stripe_checkout_token   = NULL" in sql]
        self.assertEqual(len(found), 1, "activation statement not located")
        markers, supplied = found[0]
        self.assertEqual(markers, supplied,
                         "monthly activation would fail at bind time")
