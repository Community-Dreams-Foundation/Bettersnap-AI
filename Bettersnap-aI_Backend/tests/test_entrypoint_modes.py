"""Local test of entrypoint.py's mode routing — no GPU, no container.

Runs the real entrypoint.py with os.execvp / subprocess.run / os.chdir stubbed, so we can
assert WHICH script each MODE would launch. This is the exact logic that shipped a
non-training image once already (built Dockerfile instead of Dockerfile.unified), and the
`"train_infer".startswith("train")` trap is a second way to lose a 30-minute run silently.
"""
import os
import runpy
import unittest
from unittest import mock

# entrypoint.py lives at the REPO ROOT, two levels up from Bettersnap-aI_Backend/tests/.
ENTRYPOINT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "entrypoint.py")


class Routing(unittest.TestCase):
    def _run(self, env, train_rc=0):
        """Execute entrypoint.py under stubs; return (execed_script, subprocess_calls, exit_code)."""
        calls, execed, chdirs = [], [], []

        def fake_exec(prog, argv):
            execed.append(argv[-1])
            raise SystemExit(0)          # execvp never returns; emulate that

        def fake_run(cmd, **kw):
            calls.append((cmd, kw.get("cwd")))
            return mock.Mock(returncode=train_rc)

        code = None
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("os.execvp", fake_exec), \
             mock.patch("os.chdir", chdirs.append), \
             mock.patch("subprocess.run", fake_run):
            try:
                runpy.run_path(ENTRYPOINT, run_name="__main__")
            except SystemExit as e:
                code = e.code
        return (execed[0] if execed else None), calls, code

    # ── single-phase modes ────────────────────────────────────────────────────
    def test_default_is_infer(self):
        script, calls, _ = self._run({})
        self.assertEqual(script, "main.py")
        self.assertEqual(calls, [])

    def test_explicit_infer(self):
        script, calls, _ = self._run({"MODE": "infer"})
        self.assertEqual(script, "main.py")
        self.assertEqual(calls, [])

    def test_train_only(self):
        script, calls, _ = self._run({"MODE": "train"})
        self.assertEqual(script, "run_training.py")
        self.assertEqual(calls, [], "train-only must not spawn a second phase")

    # ── fused mode ────────────────────────────────────────────────────────────
    def test_fused_runs_training_then_execs_main(self):
        script, calls, _ = self._run(
            {"MODE": "train_infer", "JOB_ID": "j1", "USER_ID": "u1"})
        self.assertEqual(len(calls), 1, "training must run exactly once")
        self.assertIn("run_training.py", calls[0][0])
        self.assertEqual(calls[0][1], "/workspace")
        self.assertEqual(script, "main.py", "generation must follow a successful train")

    def test_fused_does_not_generate_when_training_fails(self):
        script, calls, code = self._run(
            {"MODE": "train_infer", "JOB_ID": "j1", "USER_ID": "u1"}, train_rc=1)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(script, "must NOT generate on a failed/missing adapter")
        self.assertEqual(code, 1, "must surface the trainer's exit code to ACA")

    def test_fused_fails_fast_without_job_id(self):
        # The whole point: catch this in second 1, not after 30 min of A100.
        script, calls, code = self._run({"MODE": "train_infer", "USER_ID": "u1"})
        self.assertEqual(calls, [], "must not start training")
        self.assertIsNone(script)
        self.assertEqual(code, 2)

    def test_fused_fails_fast_without_user_id(self):
        script, calls, code = self._run({"MODE": "train_infer", "JOB_ID": "j1"})
        self.assertEqual(calls, [])
        self.assertEqual(code, 2)

    # ── the trap that motivated this test ─────────────────────────────────────
    def test_fused_is_not_swallowed_by_the_train_prefix(self):
        """'train_infer'.startswith('train') is True — if the prefix branch were
        ordered first, this would silently train and never generate."""
        script, calls, _ = self._run(
            {"MODE": "train_infer", "JOB_ID": "j1", "USER_ID": "u1"})
        self.assertNotEqual(script, "run_training.py")
        self.assertEqual(script, "main.py")

    def test_alias_spellings_and_whitespace_and_case(self):
        for m in ("train+infer", "train_and_infer", "fused",
                  "  TRAIN_INFER  ", "Train_Infer"):
            script, calls, _ = self._run(
                {"MODE": m, "JOB_ID": "j1", "USER_ID": "u1"})
            self.assertEqual(script, "main.py", f"{m!r} should be fused")
            self.assertEqual(len(calls), 1, f"{m!r} should train first")


if __name__ == "__main__":
    unittest.main(verbosity=2)
