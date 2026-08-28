#!/usr/bin/env python3
"""prepare_crops.py — local CLI for the head-and-shoulders training crops.

The crop LOGIC no longer lives here. It moved to
`Bettersnap-aI_Backend/shared/crops.py`, which the /train endpoint calls directly on
blob data — so production and this CLI run the exact same code and cannot diverge.
This file is now just a filesystem wrapper for inspecting/tuning crops by hand.

WHY HEAD-AND-SHOULDERS: a tight face crop teaches the LoRA an idealized, slimmed face
with no build context. Framing the face at ~1/3 of the crop height with headroom above
and shoulders below reproduces the subject's real face shape. See shared/crops.py.

NOTE ON FACELESS PHOTOS: production REJECTS them (/train returns 400 and asks the user
to replace the photo) — a faceless photo silently centre-cropped into the training set
poisons the adapter and you only find out ~51 minutes of A100 later. This CLI still
falls back to a centre crop so you can eyeball what happened, but it prints NO_FACE
loudly. Never ship a NO_FACE crop into a real training set.

USAGE
    python prepare_crops.py --in <dir-of-photos> --out <dir> [--size 1024]
                            [--face-frac 0.32] [--headroom 0.8]
"""
import argparse
import glob
import os
import sys

# shared/crops.py is the canonical implementation; import it from the backend package.
sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Bettersnap-aI_Backend"),
)
from shared.crops import (  # noqa: E402
    crop_head_and_shoulders, center_square_crop, NoFaceError, FaceTooSmallError,
    DEFAULT_SIZE, DEFAULT_FACE_FRAC, DEFAULT_HEADROOM,
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_dir", required=True)
    ap.add_argument("--out", dest="out_dir", required=True)
    ap.add_argument("--size", type=int, default=DEFAULT_SIZE)
    ap.add_argument("--face-frac", type=float, default=DEFAULT_FACE_FRAC,
                    help="face height as a fraction of the crop (smaller = wider/more body)")
    ap.add_argument("--headroom", type=float, default=DEFAULT_HEADROOM,
                    help="space above the face in face-heights")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    paths = sorted(
        p for p in glob.glob(os.path.join(args.in_dir, "*"))
        if p.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
    )
    print(f"Found {len(paths)} photos in {args.in_dir}")

    no_face, too_small = [], []
    for i, p in enumerate(paths):
        with open(p, "rb") as f:
            data = f.read()
        try:
            out = crop_head_and_shoulders(data, args.size, args.face_frac, args.headroom)
            status = "face"
        except NoFaceError:
            out = center_square_crop(data, args.size)
            status = "NO_FACE_center_crop"
            no_face.append(os.path.basename(p))
        except FaceTooSmallError as e:
            # Production REJECTS these (/train returns 400). The CLI still writes the
            # crop so you can eyeball how soft it is, but flags it loudly — a crop this
            # upscaled is what teaches the adapter blurry, waxy skin.
            out = crop_head_and_shoulders(data, args.size, args.face_frac,
                                          args.headroom, enforce_min_face=False)
            status = f"TOO_SMALL_{e.face_px}px"
            too_small.append(f"{os.path.basename(p)} ({e.face_px}px)")

        # img{i}.jpg matches the naming /train generates, so a hand-made set and a
        # production set are interchangeable.
        out_path = os.path.join(args.out_dir, f"img{i}.jpg")
        with open(out_path, "wb") as f:
            f.write(out)
        print(f"  {os.path.basename(p)} -> {os.path.basename(out_path)} [{status}]")

    print(f"Done -> {args.out_dir}")
    if no_face:
        print(f"\n!! {len(no_face)} photo(s) had NO DETECTABLE FACE: {', '.join(no_face)}")
    if too_small:
        print(f"\n!! {len(too_small)} photo(s) have a face too SMALL to crop cleanly "
              f"(production /train REJECTS these): {', '.join(too_small)}")
        print("   The subject is too far from the camera; the crop is heavily upscaled "
              "and will teach the adapter blurry skin. Use closer shots.")
        print("!! Production would REJECT these. Replace them before training.")


if __name__ == "__main__":
    main()
