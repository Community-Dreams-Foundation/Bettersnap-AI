"""Head-and-shoulders training crops — bytes in, bytes out.

The canonical crop step for EVERY user's identity-LoRA training set. Previously this
logic lived only in the local CLI `training/prepare_crops.py`; it now lives here so
the /train endpoint can call it directly against blob data, and the CLI imports it
from here. One implementation, so a laptop run and a production run cannot diverge.

WHY HEAD-AND-SHOULDERS (not a tight face crop)
----------------------------------------------
A tight face crop (face fills the frame) teaches the LoRA an idealized, slimmed face
with no shoulder/build context, and generated headshots come out slimmer than the real
person. Framing the face at ~1/3 of the crop height, with headroom above the hair and
shoulders below, gives the adapter the face at natural scale PLUS the subject's real
build — so it reproduces their actual face shape.

WHY NO SILENT FALLBACK
----------------------
`crop_head_and_shoulders` RAISES NoFaceError when it cannot find a face. It deliberately
does NOT fall back to a centre crop: a faceless photo silently centre-cropped into the
training set poisons the LoRA, and the cost of finding out is ~51 minutes of A100 plus a
bad adapter the user sees. /train rejects the upload in milliseconds instead. The centre
crop remains available as `center_square_crop` for callers that explicitly want it.
"""
import io
import os

import cv2
import numpy as np
from PIL import Image

# SDXL training resolution.
DEFAULT_SIZE = 1024
# Face height as a fraction of the crop height. Smaller = wider framing / more body.
DEFAULT_FACE_FRAC = 0.32
# Space above the face, in face-heights, for hair.
DEFAULT_HEADROOM = 0.8

# Bundled Haar frontal-face cascade (Apache-2.0, ships inside opencv — no download,
# no extra weights). Commercial-safe — do NOT swap to a non-commercial face detector.
_CASCADE = cv2.CascadeClassifier(
    os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
)


class NoFaceError(Exception):
    """No face could be detected. The caller must reject the photo, never guess."""


def detect_largest_face(bgr):
    """(x, y, w, h) of the largest detected face, or None. The subject is almost
    always the largest/closest face."""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)
    faces = _CASCADE.detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=5,
        minSize=(max(40, gray.shape[0] // 20), max(40, gray.shape[1] // 20)),
    )
    if len(faces) == 0:
        return None
    return max(faces, key=lambda f: f[2] * f[3])


def head_and_shoulders_box(face, img_w, img_h,
                           face_frac=DEFAULT_FACE_FRAC, headroom=DEFAULT_HEADROOM):
    """Square head-and-shoulders crop box: the face occupies ~face_frac of the crop
    height, with `headroom` * face-height above it for hair and the remainder below for
    shoulders/upper chest. Clamped inside the image."""
    x, y, w, h = face
    cx = x + w / 2.0
    side = h / face_frac
    side = min(side, img_w, img_h)             # can't exceed the image
    top = y - headroom * h
    left = cx - side / 2.0
    left = min(max(0.0, left), img_w - side)   # keep the crop inside the image
    top = min(max(0.0, top), img_h - side)
    return int(round(left)), int(round(top)), int(round(side))


def _encode(pil: Image.Image, size: int) -> bytes:
    buf = io.BytesIO()
    pil.resize((size, size), Image.LANCZOS).save(buf, "JPEG", quality=95)
    return buf.getvalue()


def crop_head_and_shoulders(image_bytes: bytes, size: int = DEFAULT_SIZE,
                            face_frac: float = DEFAULT_FACE_FRAC,
                            headroom: float = DEFAULT_HEADROOM) -> bytes:
    """Square head-and-shoulders JPEG at `size`x`size`.

    Raises NoFaceError if no face is found — callers must surface that to the user
    rather than training on a guess. Raises ValueError if the bytes aren't an image.
    """
    try:
        pil = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception as e:
        raise ValueError(f"not a readable image: {e}") from e

    bgr = cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR)
    h, w = bgr.shape[:2]
    face = detect_largest_face(bgr)
    if face is None:
        raise NoFaceError("no face detected")

    left, top, side = head_and_shoulders_box(face, w, h, face_frac, headroom)
    return _encode(pil.crop((left, top, left + side, top + side)), size)


def center_square_crop(image_bytes: bytes, size: int = DEFAULT_SIZE) -> bytes:
    """Centred square crop. NOT used by /train — a faceless photo must be rejected,
    not guessed at. Kept for the CLI, which flags such photos explicitly."""
    pil = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = pil.size
    side = min(w, h)
    left, top = (w - side) // 2, (h - side) // 2
    return _encode(pil.crop((left, top, left + side, top + side)), size)
