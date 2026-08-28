"""Validate the IP-Adapter reference crop actually contains a face (audit finding M5).

`main._get_ref_faces` decodes the user's crops and the pipeline feeds a SINGLE one
(`IP_ADAPTER_REF_INDEX`, default 0) to IP-Adapter Plus-Face. Training's Haar face gate can
false-positive and ship a wall / room crop as a "face", and nothing re-checks at generation
time — so the reference can condition identity on a non-face. The LoRA-missing and
ref-unavailable guards do NOT catch a present-but-faceless reference.

`pick_face_ref_index` detects a face in each fetched crop and picks a face-bearing one: keep the
configured index if it has a face, else fall back to the first crop that does. If NONE have a
detectable face it keeps the configured index and flags it — it does NOT block generation,
because the LoRA still carries identity and Haar has false-negatives on some valid (profile /
occluded) faces, so hard-failing on detector misses would be worse than proceeding.

Kept in its own module (no torch / diffusers / azure imports) so it is unit-testable without
importing the heavy `main` module. cv2 / numpy are imported lazily inside the function.
"""
from __future__ import annotations


def pick_face_ref_index(ref_faces, ref_ids, preferred, cascade, log, *, min_size=(64, 64)):
    """Return (chosen_index, meta). `cascade` is a cv2 CascadeClassifier (or None). `log` is a
    callable taking one string. `meta` records preferred/chosen/face_indices/note for the manifest."""
    meta = {"preferred": preferred, "face_indices": None, "chosen": preferred, "note": None}
    if cascade is None or not ref_faces:
        meta["note"] = "no-cascade" if cascade is None else "no-refs"
        return preferred, meta
    import cv2
    import numpy as np
    face_idx = []
    for i, im in enumerate(ref_faces):
        try:
            gray = cv2.cvtColor(np.array(im.convert("RGB")), cv2.COLOR_RGB2GRAY)
            found = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=min_size)
            if len(found) > 0:
                face_idx.append(i)
        except Exception as e:  # a decode/detect error on one crop must not sink the whole run
            log(f"IP-Adapter ref validation: detection failed on crop {i}: {e}")
    meta["face_indices"] = face_idx

    if not face_idx:
        meta["note"] = "no-face-in-any-ref"
        log(f"IP-Adapter ref validation: NO face detected in any of {len(ref_faces)} crop(s) "
            f"{ref_ids}; keeping index {preferred} (LoRA still carries identity)")
        return preferred, meta

    if preferred in face_idx:
        return preferred, meta

    chosen = face_idx[0]
    meta["chosen"] = chosen
    meta["note"] = "fell-back-to-face-crop"
    pid = ref_ids[preferred] if 0 <= preferred < len(ref_ids) else "?"
    log(f"IP-Adapter ref validation: configured index {preferred} ({pid}) is FACELESS; "
        f"falling back to index {chosen} ({ref_ids[chosen]})")
    return chosen, meta
