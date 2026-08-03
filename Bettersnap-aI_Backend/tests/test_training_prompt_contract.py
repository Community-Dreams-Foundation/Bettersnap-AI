"""The trainer must fail before Blob/cache access when its class fields disagree."""
import os
import subprocess
import sys
import unittest
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]
TRAINER = BACKEND.parent / "training" / "trainer" / "run_training.py"


class TrainingPromptContractTests(unittest.TestCase):
    def _run(self, **overrides):
        env = os.environ.copy()
        env.update({
            "STORAGE_CONNECTION_STRING": "must-not-be-used",
            "USER_ID": "contract-test-user",
            "FILES_JSON": "[]",
            "CLASS_WORD": "woman",
            "CLASS_PROMPT": "a photo of a woman",
            "INSTANCE_PROMPT": "a photo of ohwx woman",
        })
        env.update(overrides)
        return subprocess.run(
            [sys.executable, str(TRAINER)],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )

    def test_male_prompt_cannot_poison_woman_cache(self):
        result = self._run(CLASS_WORD="woman", CLASS_PROMPT="a photo of a man")
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1)
        self.assertIn("CLASS_PROMPT/CLASS_WORD mismatch", output)
        self.assertIn("refusing to read or publish", output)
        self.assertNotIn("connection string", output.lower())

    def test_instance_prompt_must_use_same_class_word(self):
        result = self._run(
            CLASS_WORD="man",
            CLASS_PROMPT="a photo of a man",
            INSTANCE_PROMPT="a photo of ohwx woman",
        )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1)
        self.assertIn("INSTANCE_PROMPT/CLASS_WORD mismatch", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
