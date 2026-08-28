"""Worker-level tests for the inference PROMPT-PLANNING + adapter-loading stage.

Regression cover for the run_inference NameError: main.py once called
`load_category_lora(category)` with `category` undefined, so EVERY generation
crashed at the adapter-loading stage before producing a single headshot (the job
was then refunded — the user paid for nothing). See main.py run_inference.

Two CUDA-free layers, so this runs in CI on stdlib alone (never imports
torch/diffusers/opencv):

  1. Prompt planning — catalog.build_combos_global + the category-qualified phrase
     lookups that run_inference feeds into every prompt (main.py ~line 949+). Pure
     functions, deterministic.
  2. Undefined-name guard — statically assert main.py has NO undefined names (the
     exact class of the `category` bug). Skipped if pyflakes is absent; CI installs
     it. This is the test-suite twin of the CI 'undefined-name gate'.

Run:  python -m unittest tests.test_prompt_planning   (from the backend dir)
"""
import os
import sys
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(BACKEND_DIR)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from shared import catalog  # noqa: E402

try:
    import pyflakes.api  # noqa: F401
    _HAS_PYFLAKES = True
except ImportError:
    _HAS_PYFLAKES = False


class PromptPlanning(unittest.TestCase):
    """Exercises the prompt-planning stage run_inference depends on, spanning the
    professional + personal categories a Pro/Expert job can mix."""

    def test_normalize_gender(self):
        self.assertEqual(catalog.normalize_gender("Woman"), "female")
        self.assertEqual(catalog.normalize_gender("he/him"), "male")
        self.assertEqual(catalog.normalize_gender(""), "neutral")
        self.assertEqual(catalog.normalize_gender(None), "neutral")

    def test_build_combos_global_cartesian_cross_category(self):
        # A Pro/Expert selection mixes categories — a professional attire with a
        # personal (beach) background, etc. Planning is their cartesian product.
        attire = ["business_suit.navy_suit_tie", "dating.crew_neck_sweater"]
        bg = ["business_suit.light_gray_studio", "beach_sunset.golden_hour_beach"]
        combos = catalog.build_combos_global(attire, bg)
        self.assertEqual(len(combos), 4)
        self.assertIn(
            ("business_suit.navy_suit_tie", "beach_sunset.golden_hour_beach"), combos
        )

    def test_build_combos_global_drops_invalid_refs(self):
        combos = catalog.build_combos_global(
            ["business_suit.navy_suit_tie", "business_suit.NOPE", "bad_ref_no_dot"],
            ["business_suit.light_gray_studio", "nope.nope"],
        )
        self.assertEqual(
            combos, [("business_suit.navy_suit_tie", "business_suit.light_gray_studio")]
        )

    def test_build_combos_global_empty_and_none(self):
        self.assertEqual(catalog.build_combos_global([], []), [])
        self.assertEqual(catalog.build_combos_global(None, None), [])

    def test_attire_phrase_ref_is_gender_aware(self):
        # Attires are gender-specific LISTS now: each gender has its own refs, and each
        # ref resolves to that gender's phrase. (navy_suit_tie is male; navy_pantsuit female.)
        male = catalog.attire_phrase_ref("business_suit.navy_suit_tie", "male")
        female = catalog.attire_phrase_ref("business_suit.navy_pantsuit", "female")
        self.assertIn("tie", male)
        self.assertIn("pantsuit", female)
        self.assertNotEqual(male, female)
        # A cross-gender or unknown gender key falls back safely, never crashes.
        self.assertTrue(catalog.attire_phrase_ref("business_suit.navy_suit_tie", "female"))
        self.assertTrue(catalog.attire_phrase_ref("business_suit.navy_suit_tie", "nonsense"))

    def test_attire_phrase_ref_unknown_ref_falls_back(self):
        # A dropped/unknown ref must return a safe default, never raise.
        self.assertTrue(catalog.attire_phrase_ref("nope.nope", "male"))

    def test_background_phrase_ref(self):
        self.assertIn("beach", catalog.background_phrase_ref(
            "beach_sunset.golden_hour_beach").lower())
        self.assertTrue(catalog.background_phrase_ref("nope.nope"))  # safe default

    def test_lead_phrase_follows_background_category(self):
        # Style/lead is DERIVED from the background's category: a beach bg reads
        # lifestyle/candid, an office bg corporate — even if attire came from elsewhere.
        beach = catalog.lead_for_background_ref("beach_sunset.golden_hour_beach")
        office = catalog.lead_for_background_ref("business_suit.modern_office")
        self.assertNotEqual(beach, office)
        self.assertIn("photograph", beach.lower())

    def test_lighting_follows_background_category(self):
        default = ["neutral studio lighting"]
        # beach_sunset overrides lighting (golden/sunset) -> not the default.
        beach = catalog.lighting_for_background_ref(
            "beach_sunset.golden_hour_beach", default)
        self.assertNotEqual(beach, default)
        # A category with no lighting override returns the caller's default.
        self.assertEqual(
            catalog.lighting_for_background_ref("business_suit.light_gray_studio", default),
            default,
        )

    def test_ref_validation_helpers(self):
        self.assertTrue(catalog.valid_attire_ref("business_suit.navy_suit_tie"))
        self.assertFalse(catalog.valid_attire_ref("business_suit.NOPE"))
        self.assertTrue(catalog.valid_background_ref("beach_sunset.golden_hour_beach"))
        self.assertEqual(catalog.split_ref("a.b"), ("a", "b"))


@unittest.skipUnless(_HAS_PYFLAKES, "pyflakes not installed")
class NoUndefinedNamesInWorker(unittest.TestCase):
    """The run_inference `category` NameError class: main.py must have NO undefined
    names. pyflakes PARSES the file (does not import it), so torch/opencv/azure are
    not needed to run this."""

    def test_main_py_has_no_undefined_names(self):
        import io
        from pyflakes.api import checkPath
        from pyflakes.reporter import Reporter

        main_py = os.path.join(REPO_ROOT, "main.py")
        out, err = io.StringIO(), io.StringIO()
        checkPath(main_py, Reporter(out, err))
        undefined = [
            line for line in (out.getvalue() + err.getvalue()).splitlines()
            if "undefined name" in line
        ]
        self.assertEqual(
            undefined, [], "undefined name(s) in main.py:\n" + "\n".join(undefined)
        )


if __name__ == "__main__":
    unittest.main()
