"""Proves the GPU preflight runs FIRST in entrypoint.py and that a preflight failure exits
before any train/infer dispatch — i.e. CUDA failure cannot fall through to a workload.

Two layers:
  1. Source guard: `gpu_preflight.run_preflight()` appears before any dispatch in entrypoint.
  2. Live integration: actually run entrypoint.py as a subprocess. On a box without a GPU
     (and without torch) the real preflight classifies PROBE_ERROR (44) and exits BEFORE
     the dispatch — no training/inference is attempted, no Azure touched.

Run: python -m unittest tests.test_preflight_ordering   (from the backend dir)
"""
import os
import subprocess
import sys
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(BACKEND_DIR)
ENTRYPOINT = os.path.join(REPO_ROOT, "entrypoint.py")


class SourceOrderingTests(unittest.TestCase):
    def setUp(self):
        with open(ENTRYPOINT, encoding="utf-8") as f:
            self.src = f.read()

    def test_preflight_is_imported_and_called(self):
        self.assertIn("import gpu_preflight", self.src)
        self.assertIn("gpu_preflight.run_preflight()", self.src)

    def test_preflight_call_precedes_every_dispatch(self):
        pre = self.src.index("gpu_preflight.run_preflight()")
        # every way the entrypoint hands off to real work
        dispatch_markers = [
            self.src.index("_exec(TRAIN_DIR"),
            self.src.index("_exec(INFER_DIR"),
            self.src.index('subprocess.run(["python3.11", TRAIN_SCRIPT]'),
        ]
        for d in dispatch_markers:
            self.assertLess(pre, d, "preflight must run before any dispatch")


class LiveEntrypointIntegrationTests(unittest.TestCase):
    """Actually execute entrypoint.py. On this GPU-less/torch-less box the preflight must
    exit with a distinct failure code and NEVER print the training/generation phase lines."""

    def _run_entrypoint(self, mode):
        env = dict(os.environ)
        env["MODE"] = mode
        env["JOB_ID"] = "TEST-JOB"
        env["USER_ID"] = "test-user"
        env.pop("STORAGE_CONNECTION_STRING", None)  # keep the diagnostic write a no-op
        return subprocess.run([sys.executable, ENTRYPOINT], cwd=REPO_ROOT, env=env,
                              capture_output=True, text=True, timeout=120)

    def test_infer_mode_blocked_by_preflight(self):
        p = self._run_entrypoint("infer")
        # distinct GPU-preflight failure code (42/43/44); here 44 = probe error (no torch)
        self.assertIn(p.returncode, (42, 43, 44),
                      f"expected a preflight exit code, got {p.returncode}\n{p.stdout}\n{p.stderr}")
        self.assertIn("::PREFLIGHT_RESULT::FAIL", p.stdout)
        # proof of no fall-through: the inference/generation path never announced itself
        self.assertNotIn("phase 2/2", p.stdout)

    def test_train_infer_mode_blocked_before_training(self):
        p = self._run_entrypoint("train_infer")
        self.assertIn(p.returncode, (42, 43, 44),
                      f"expected a preflight exit code, got {p.returncode}\n{p.stdout}\n{p.stderr}")
        self.assertIn("::PREFLIGHT_RESULT::FAIL", p.stdout)
        # the training phase line must NOT appear — preflight gated it
        self.assertNotIn("phase 1/2: training", p.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
