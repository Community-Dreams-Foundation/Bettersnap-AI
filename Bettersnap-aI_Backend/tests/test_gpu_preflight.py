"""Unit + integration tests for gpu_preflight.

These tests NEVER import torch, NEVER touch Azure, and NEVER launch a GPU/model workload —
every GPU/nvidia/blob fact is injected. Run:
    python -m unittest tests.test_gpu_preflight   (from the backend dir)
"""
import os
import sys
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(BACKEND_DIR)
# APPEND (not insert-at-0): REPO_ROOT holds main.py/entrypoint.py/catalog-adjacent modules,
# and shoving it to the front of sys.path shadows same-named modules for other test files
# under `unittest discover` (it broke test_dispatch_logic's imports). Appending still lets
# `import gpu_preflight` resolve without hijacking resolution for anyone else.
for p in (BACKEND_DIR, REPO_ROOT):
    if p not in sys.path:
        sys.path.append(p)

import gpu_preflight as gp  # noqa: E402


# ── synthetic probe records ──────────────────────────────────────────────────
def rec_ok():
    return {"cuda_available": True, "device_count": 1, "device_names": ["NVIDIA A100"],
            "nvidia_smi_rc": 0, "dev_nvidia": ["/dev/nvidia0 mode=0o..."], "probe_error": None}


def rec_no_gpu():
    # torch blind AND no hardware evidence -> 42
    return {"cuda_available": False, "device_count": 0, "device_names": [],
            "nvidia_smi_rc": None, "dev_nvidia": [], "probe_error": None}


def rec_gpu_unusable_smi():
    # nvidia-smi works (hardware present) but torch is blind -> 43  (the 2026-07-31 signature)
    return {"cuda_available": False, "device_count": 0, "device_names": [],
            "nvidia_smi_rc": 0, "dev_nvidia": [], "probe_error": None}


def rec_gpu_unusable_devnodes():
    # /dev/nvidia* present but torch blind -> 43
    return {"cuda_available": False, "device_count": 0, "device_names": [],
            "nvidia_smi_rc": None, "dev_nvidia": ["/dev/nvidia0 mode=0o20666 rdev=1"],
            "probe_error": None}


def rec_probe_error():
    return {"cuda_available": False, "device_count": 0, "device_names": [],
            "nvidia_smi_rc": None, "dev_nvidia": [], "probe_error": "ModuleNotFoundError: torch"}


class ClassifyTests(unittest.TestCase):
    def test_ok(self):
        ok, code, _ = gp.classify(rec_ok())
        self.assertTrue(ok); self.assertEqual(code, gp.EXIT_OK)

    def test_no_gpu_is_42(self):
        ok, code, _ = gp.classify(rec_no_gpu())
        self.assertFalse(ok); self.assertEqual(code, gp.EXIT_NO_GPU)

    def test_gpu_present_but_unusable_via_smi_is_43(self):
        ok, code, _ = gp.classify(rec_gpu_unusable_smi())
        self.assertFalse(ok); self.assertEqual(code, gp.EXIT_GPU_UNUSABLE)

    def test_gpu_present_but_unusable_via_devnodes_is_43(self):
        ok, code, _ = gp.classify(rec_gpu_unusable_devnodes())
        self.assertFalse(ok); self.assertEqual(code, gp.EXIT_GPU_UNUSABLE)

    def test_probe_error_is_44(self):
        ok, code, _ = gp.classify(rec_probe_error())
        self.assertFalse(ok); self.assertEqual(code, gp.EXIT_PROBE_ERROR)

    def test_device_count_zero_despite_available_flag_is_failure(self):
        # a torch that says available but exposes 0 devices must NOT pass
        r = rec_ok(); r["device_count"] = 0
        ok, code, _ = gp.classify(r)
        self.assertFalse(ok)

    def test_exit_codes_are_stable(self):
        self.assertEqual((gp.EXIT_OK, gp.EXIT_NO_GPU, gp.EXIT_GPU_UNUSABLE, gp.EXIT_PROBE_ERROR),
                         (0, 42, 43, 44))


class NoCpuFallbackProofTests(unittest.TestCase):
    """Formal proof: the ONLY way classify() returns ok=True is a usable CUDA device, and
    run_preflight() NEVER returns None on any failure (so a caller can never fall through
    into CPU work)."""

    FAILING = [rec_no_gpu, rec_gpu_unusable_smi, rec_gpu_unusable_devnodes, rec_probe_error]

    def test_classify_ok_requires_cuda(self):
        for maker in self.FAILING:
            ok, _, _ = gp.classify(maker())
            self.assertFalse(ok, f"{maker.__name__} must not classify ok")
        # and the positive case
        self.assertTrue(gp.classify(rec_ok())[0])

    def test_run_preflight_never_returns_on_failure(self):
        for maker in self.FAILING:
            calls = []
            with self.assertRaises(SystemExit) as cm:
                gp.run_preflight(exit_fn=lambda c: calls.append(c),  # returns (does not raise)
                                 probe_fn=maker, write_fn=lambda r: True,
                                 print_fn=lambda *_a, **_k: None)
            expected = gp.classify(maker())[1]
            self.assertEqual(calls, [expected], f"{maker.__name__}: exit_fn code")
            # the trailing `raise SystemExit` guarantees no fall-through even if exit_fn returns
            self.assertEqual(cm.exception.code, expected)


class RunPreflightPassTests(unittest.TestCase):
    def test_pass_returns_none_and_does_not_exit(self):
        calls, prints = [], []
        out = gp.run_preflight(exit_fn=lambda c: calls.append(c), probe_fn=rec_ok,
                               write_fn=lambda r: True, print_fn=lambda *a, **k: prints.append(a))
        self.assertIsNone(out)
        self.assertEqual(calls, [], "exit_fn must not be called on success")
        self.assertTrue(any("::PREFLIGHT_RESULT::PASS" in str(a) for a in prints))


class BlobFailureCannotChangeExitTests(unittest.TestCase):
    """Requirement 5: a blob-write failure — returning False OR raising — must not stop the
    process exiting with the correct GPU failure code."""

    def _run_expect(self, write_fn, expected_code):
        calls = []
        with self.assertRaises(SystemExit) as cm:
            gp.run_preflight(exit_fn=lambda c: calls.append(c), probe_fn=rec_no_gpu,
                             write_fn=write_fn, print_fn=lambda *_a, **_k: None)
        self.assertEqual(calls, [expected_code])
        self.assertEqual(cm.exception.code, expected_code)

    def test_write_returns_false(self):
        self._run_expect(lambda r: False, gp.EXIT_NO_GPU)

    def test_write_raises(self):
        def boom(r):
            raise RuntimeError("blob endpoint unreachable")
        self._run_expect(boom, gp.EXIT_NO_GPU)


class WriteDiagnosticTests(unittest.TestCase):
    def test_no_connection_string_returns_false_no_raise(self):
        saved = os.environ.pop("STORAGE_CONNECTION_STRING", None)
        try:
            self.assertFalse(gp.write_diagnostic({"execution_id": "x"}))
        finally:
            if saved is not None:
                os.environ["STORAGE_CONNECTION_STRING"] = saved


class BuildRecordTests(unittest.TestCase):
    def test_record_has_required_and_result(self):
        r = gp.build_record(rec_no_gpu(), False, gp.EXIT_NO_GPU, "reason")
        self.assertEqual(r["required"], {"min_cuda_devices": 1})
        self.assertEqual(r["result"]["exit_code"], gp.EXIT_NO_GPU)
        self.assertFalse(r["result"]["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
