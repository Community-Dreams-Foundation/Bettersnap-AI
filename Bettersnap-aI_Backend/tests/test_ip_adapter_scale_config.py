"""B2: IP_ADAPTER_SCALE must default to the FROZEN 0.2, never the rejected 0.6.

DECISIONS.md A1 froze IP_ADAPTER_SCALE at 0.2 after an A/B (female + male) showed 0.6
over-conditions (reference bleed, fights the attire/scene prompt). The risk this guards:
main.py reads `float(os.environ.get("IP_ADAPTER_SCALE", "<default>"))`, so if the code
default is 0.6 and the deploy env var is ever dropped, production silently reverts to the
known-bad value. This test pins the default at 0.2 across every configuration source.

main.py imports torch/diffusers and cannot be imported here, so — following the same
source-scan pattern as tests/test_invoice_subscription_id.py — we assert on the real source
text, and separately verify the parse semantics with a mirror of the exact expression.

Run: python -m unittest tests.test_ip_adapter_scale_config   (from the backend dir)
"""
import os
import re
import sys
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(BACKEND_DIR)

FROZEN_DEFAULT = "0.2"
REJECTED_VALUE = "0.6"


def _resolve_scale(raw):
    """Mirror of main.py's exact expression: float(os.environ.get("IP_ADAPTER_SCALE", "0.2")).
    os.environ.get returns None when unset; main.py passes the default string in that case."""
    return float(raw if raw is not None else FROZEN_DEFAULT)


class ParseSemanticsTests(unittest.TestCase):
    def test_unset_uses_frozen_default(self):
        self.assertEqual(_resolve_scale(None), 0.2)

    def test_explicit_env_override_still_works(self):
        # A/B testing must still be able to set any valid scale via the env var.
        self.assertEqual(_resolve_scale("0.6"), 0.6)
        self.assertEqual(_resolve_scale("0.4"), 0.4)

    def test_zero_disables(self):
        # 0 is a valid, meaningful value (disables IP-Adapter) — must not be treated as unset.
        self.assertEqual(_resolve_scale("0"), 0.0)

    def test_invalid_value_fails_fast(self):
        # A non-numeric env value raises at startup (fail-fast) rather than silently running
        # at a wrong scale — the safe behavior for a frozen quality knob. If a future change
        # adds a fallback, it MUST log loudly; it must never silently substitute a value.
        with self.assertRaises(ValueError):
            _resolve_scale("abc")


class ConfigSourceTests(unittest.TestCase):
    """Every configuration source must agree on 0.2; none may still carry 0.6."""

    def _read(self, *parts):
        with open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as f:
            return f.read()

    def test_main_py_code_default_is_frozen_value(self):
        src = self._read("main.py")
        m = re.search(r'IP_ADAPTER_SCALE\s*=\s*float\(os\.environ\.get\(\s*"IP_ADAPTER_SCALE"\s*,\s*"([0-9.]+)"\s*\)\)', src)
        self.assertIsNotNone(m, "IP_ADAPTER_SCALE default expression not found in main.py")
        self.assertEqual(m.group(1), FROZEN_DEFAULT,
                         f"main.py code default must be {FROZEN_DEFAULT}, got {m.group(1)}")

    def test_job_yaml_sets_it_explicitly(self):
        src = self._read("job.yaml")
        self.assertRegex(
            src,
            r'name:\s*IP_ADAPTER_SCALE\s*\n\s*value:\s*"0\.2"',
            "job.yaml must pin IP_ADAPTER_SCALE=0.2 so a deploy can't fall back to a code default")

    def test_env_example_matches_frozen_value(self):
        src = self._read(".env.example")
        m = re.search(r'IP_ADAPTER_SCALE="([0-9.]+)"', src)
        self.assertIsNotNone(m, "IP_ADAPTER_SCALE not found in .env.example")
        self.assertEqual(m.group(1), FROZEN_DEFAULT)

    def test_no_config_source_still_ships_the_rejected_value(self):
        for parts in (("main.py",), (".env.example",), ("job.yaml",)):
            src = self._read(*parts)
            # The rejected value may appear in prose (comments explaining WHY 0.6 was rejected),
            # but never as an assigned IP_ADAPTER_SCALE default/value.
            self.assertNotRegex(
                src,
                r'IP_ADAPTER_SCALE"?\s*[,:=]\s*"?0\.6',
                f"{parts[0]} still assigns the rejected 0.6 to IP_ADAPTER_SCALE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
