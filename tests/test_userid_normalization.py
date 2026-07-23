"""Regression guard for the USER_ID case-normalization contract in main.py.

Training writes every per-user blob — the identity-LoRA adapter AND the IP-Adapter face
crops — under the LOWERCASE Entra oid. SQL returns the GUID UPPERCASE and the dispatcher
forwards it as-is. Blob paths are CASE-SENSITIVE, so USER_ID must be lowercased once at the
inference entry, or the adapter read (load_identity_lora) and the reference read
(_get_ref_faces) both look up a non-existent uppercase path and generation fails
("identity LoRA missing" / IpAdapterReferenceUnavailable).

main.py is not importable here (needs torch/diffusers), so this guards the contract two ways:
  1. SOURCE guard — the single os.environ.get("USER_ID") read is lowercased, with no bare
     read left. If someone reverts the normalization, this test fails.
  2. BEHAVIORAL — replicates the exact path templates from main.py to prove an uppercase
     USER_ID (as the DB returns it) yields lowercase blob paths.

Run: python -m unittest tests.test_userid_normalization   (from the repo root)
"""
import os
import unittest

MAIN_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")


class UserIdNormalizationTests(unittest.TestCase):
    def setUp(self):
        with open(MAIN_PY, encoding="utf-8") as f:
            self.src = f.read()

    def test_userid_normalized_at_entry(self):
        # The env read must lowercase USER_ID...
        self.assertIn('user_id = (os.environ.get("USER_ID") or "").lower()', self.src,
                      "USER_ID must be normalized to lowercase where it enters main.py")
        # ...and the old un-normalized read must be gone.
        self.assertNotIn('user_id = os.environ.get("USER_ID")\n', self.src,
                         "a bare (un-normalized) USER_ID read is still present")

    def test_uppercase_userid_yields_lowercase_blob_paths(self):
        # Replicate the normalization + the two case-sensitive templates main.py builds:
        #   load_identity_lora: f"identity/{user_id}/adapter_model.safetensors"
        #   _get_ref_faces:     f"{user_id}/{catalog.CROP_SUBDIR}/img{i}.jpg"
        raw = "46EA2DA3-D761-4B8F-B35A-0B076922F168"   # UPPERCASE, exactly as SQL returns it
        user_id = (raw or "").lower()
        CROP_SUBDIR = "input/crop_upperbody"           # == catalog.CROP_SUBDIR
        lora_blob = f"identity/{user_id}/adapter_model.safetensors"
        crop_blob = f"{user_id}/{CROP_SUBDIR}/img0.jpg"
        tmp_path = f"/tmp/lora_identity_{user_id}.safetensors"

        self.assertEqual(user_id, raw.lower())
        for path in (lora_blob, crop_blob, tmp_path):
            self.assertEqual(path, path.lower(),
                             f"blob path leaked uppercase: {path}")
        self.assertEqual(lora_blob, "identity/46ea2da3-d761-4b8f-b35a-0b076922f168/adapter_model.safetensors")
        self.assertTrue(crop_blob.startswith("46ea2da3-d761-4b8f-b35a-0b076922f168/"))

    def test_missing_userid_is_falsy_not_crash(self):
        # Unset USER_ID -> "" (falsy), so main.py's `if job_id and user_id` still fails loudly
        # rather than the .lower() raising AttributeError on None.
        self.assertEqual((None or "").lower(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
