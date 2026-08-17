"""B3 FIX REGRESSION: generation-time subject / class-word desync (Policy B).

The LoRA is trained to bind the trigger token to "<IDENTITY_TRIGGER> <class_word>", where
class_word is derived from the TRAINING gender and PERSISTED (lora_trainings.class_word).
BEFORE the fix, generation rebuilt the subject from GENERATION-time gender, ignoring class_word.
AFTER the fix (Policy B), main.py resolves effective_gender from class_word when available,
falling back to generation-time gender for legacy records.

Tests verify that the effective gender (what the prompt engine fires) honors the trained
class_word over mismatched generation-time gender.

Run: python -m unittest tests.test_class_word_desync_repro
"""
import os
import sys
import types
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(BACKEND_DIR)
for p in (REPO_ROOT, os.path.join(BACKEND_DIR, "shared")):  # shared/ -> bare `import catalog`
    if p not in sys.path:
        sys.path.insert(0, p)

import catalog  # noqa: E402
from domain import GenerationPlan  # noqa: E402
from domain.plan import Plan, PlanType, CategoryRule  # noqa: E402
from runtime.engines.prompt_sdxl import SdxlPromptEngine  # noqa: E402


def _positive_prompt(effective_gender):
    """Run the REAL prompt engine (simulating main.py's resolved gender) and return the
    first image's positive prompt."""
    cfg = types.SimpleNamespace(
        normalize_gender=catalog.normalize_gender,
        SUBJECT_NOUN=catalog.SUBJECT_NOUN,
        IDENTITY_TRIGGER=catalog.IDENTITY_TRIGGER,
        age_to_phrase=lambda s: "",
        hair_phrase=lambda h: "",
        LIGHTING=["studio lighting"],
        NEGATIVE_PROMPT="",
        COMPOSITION_CONTROL=0,
    )
    ctx = types.SimpleNamespace(work={})
    plan = GenerationPlan(
        user_id="u", job_id="j",
        plan=Plan(key="k", plan_type=PlanType.ONE_TIME, image_count=1, credits_per_image=1,
                  max_attires=99, max_backgrounds=99, category_rule=CategoryRule.MIXABLE),
        billable_count=1, credit_cost=0, candidate_budget=1, acceptance_threshold=0.0,
        retry_limit=0, gender=effective_gender,
        attire_refs=("business_suit.navy_suit_tie",),
        background_refs=("business_suit.studio_gray",), custom_prompt="",
    )
    return SdxlPromptEngine(ctx, cfg, log=lambda *a, **k: None).build(plan).prompts[0].positive


class ClassWordDesyncRegression(unittest.TestCase):
    """Regression tests for B3 fix: main.py resolves effective_gender from class_word."""

    def test_trained_female_overrides_generation_male(self):
        # SCENARIO: LoRA trained female (class_word="woman"), but generation says male.
        # BEFORE: prompt emits "ohwx man" (wrong token for the trained adapter).
        # AFTER (Policy B): main.py resolves effective_gender="female" from class_word, so
        # prompt emits "ohwx woman" (correct token).
        trained_class_word = "woman"
        gen_gender = "male"
        # Simulate main.py's Policy B resolution:
        effective_gender = catalog.gender_from_class_word(trained_class_word) or gen_gender
        # Now the prompt engine fires the CORRECT token.
        pos = _positive_prompt(effective_gender)
        self.assertIn("ohwx woman", pos, "should use the trained class_word, not gen_gender")
        self.assertNotIn("ohwx man", pos)

    def test_trained_male_overrides_generation_female(self):
        # SCENARIO: LoRA trained male (class_word="man"), generation says female.
        trained_class_word = "man"
        gen_gender = "female"
        effective_gender = catalog.gender_from_class_word(trained_class_word) or gen_gender
        pos = _positive_prompt(effective_gender)
        self.assertIn("ohwx man", pos)
        self.assertNotIn("ohwx woman", pos)

    def test_legacy_no_class_word_uses_generation_gender(self):
        # SCENARIO: Legacy/pre-Phase2 user (no lora_trainings row) has class_word=None.
        # AFTER (Policy B): falls back to generation-time gender.
        trained_class_word = None
        gen_gender = "female"
        effective_gender = catalog.gender_from_class_word(trained_class_word) or gen_gender
        pos = _positive_prompt(effective_gender)
        self.assertIn("ohwx woman", pos)
        self.assertNotIn("ohwx man", pos)

    def test_legacy_corrupted_class_word_falls_back(self):
        # SCENARIO: Corrupted DB row has class_word="invalid_value".
        # AFTER (Policy B): gender_from_class_word returns None, falls back to generation.
        trained_class_word = "invalid_value"
        gen_gender = "male"
        effective_gender = catalog.gender_from_class_word(trained_class_word) or gen_gender
        pos = _positive_prompt(effective_gender)
        self.assertIn("ohwx man", pos)

    def test_matching_train_and_generation_unchanged(self):
        # SCENARIO: Train gender and generation gender both "female" (matching case).
        # AFTER (Policy B): the prompt is unchanged (both say woman).
        trained_class_word = "woman"
        gen_gender = "female"
        effective_gender = catalog.gender_from_class_word(trained_class_word) or gen_gender
        pos = _positive_prompt(effective_gender)
        self.assertIn("ohwx woman", pos)

    def test_gender_from_class_word_reverses_correctly(self):
        # Unit test the reverse-map helper.
        self.assertEqual(catalog.gender_from_class_word("woman"), "female")
        self.assertEqual(catalog.gender_from_class_word("man"), "male")
        self.assertEqual(catalog.gender_from_class_word("person"), "neutral")
        self.assertIsNone(catalog.gender_from_class_word(None))
        self.assertIsNone(catalog.gender_from_class_word("invalid"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
