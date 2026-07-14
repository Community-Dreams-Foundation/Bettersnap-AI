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
import math
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

# Bundled Haar cascades (Apache-2.0, ship inside opencv — no download, no extra
# weights). Commercial-safe — do NOT swap to a non-commercial face detector.
#
# WHY MULTIPLE CASCADES + ROTATION
# --------------------------------
# The single frontal `default` cascade is blind to head ROLL (a leaning head, a
# hand-on-cheek pose) and to three-quarter turns, and rejected clearly-visible faces
# in those poses. We now try, in order: two frontal cascades upright, then the frontal
# cascades on a few rotated copies (mapping the box back to original coords), then the
# profile cascade and its mirror. A photo is only rejected once ALL of those fail — so
# a genuinely faceless photo is still caught, but a real face at an odd angle passes.
_HAAR = cv2.data.haarcascades
_FRONTAL = [
    cv2.CascadeClassifier(os.path.join(_HAAR, "haarcascade_frontalface_default.xml")),
    cv2.CascadeClassifier(os.path.join(_HAAR, "haarcascade_frontalface_alt2.xml")),
]
_PROFILE = cv2.CascadeClassifier(os.path.join(_HAAR, "haarcascade_profileface.xml"))

# Degrees to straighten the image by when an upright pass finds nothing. Covers the
# head-roll range of a typical "leaning" headshot. Only reached for photos the fast
# upright pass fails, so the common (frontal) case pays nothing for this.
_ROTATIONS = (12, -12, 25, -25)


class NoFaceError(Exception):
    """No face could be detected. The caller must reject the photo, never guess."""


class MultipleFacesError(Exception):
    """More than one distinct face — a group/couple photo. Training needs solo shots,
    or the adapter blends two identities into one face."""


def _detect(gray, cascade):
    return cascade.detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=4,
        minSize=(max(40, gray.shape[0] // 20), max(40, gray.shape[1] // 20)),
    )


def _detect_frontal(gray):
    """Boxes from the first frontal cascade that finds anything ([] if none do)."""
    for cascade in _FRONTAL:
        faces = _detect(gray, cascade)
        if len(faces):
            return faces
    return []


def _largest(faces):
    if len(faces) == 0:
        return None
    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
    return (int(x), int(y), int(w), int(h))


def _map_box(box, mat):
    """Apply 2x3 affine `mat` to a box's centre, keeping its w/h. Used to carry a face
    box found in a rotated frame back into the original image's coordinates."""
    x, y, bw, bh = box
    cx, cy = x + bw / 2.0, y + bh / 2.0
    nx = mat[0, 0] * cx + mat[0, 1] * cy + mat[0, 2]
    ny = mat[1, 0] * cx + mat[1, 1] * cy + mat[1, 2]
    return (int(round(nx - bw / 2.0)), int(round(ny - bh / 2.0)), int(bw), int(bh))


def detect_largest_face(bgr):
    """(x, y, w, h) of the largest detected face in ORIGINAL image coords, or None.
    The subject is almost always the largest/closest face. Tolerant of head roll and
    three-quarter turns (see cascade notes above)."""
    gray = cv2.equalizeHist(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY))
    h, w = gray.shape[:2]

    # 1) Upright frontal — the fast, overwhelmingly common case.
    box = _largest(_detect_frontal(gray))
    if box is not None:
        return box

    # 2) Rotated retries — straighten head roll, detect, map the box back to original.
    center = (w / 2.0, h / 2.0)
    for angle in _ROTATIONS:
        mat = cv2.getRotationMatrix2D(center, angle, 1.0)
        rot = cv2.warpAffine(gray, mat, (w, h), flags=cv2.INTER_LINEAR,
                             borderMode=cv2.BORDER_REPLICATE)
        box = _largest(_detect_frontal(rot))
        if box is not None:
            return _map_box(box, cv2.invertAffineTransform(mat))

    # 3) Profile, then its horizontal mirror — three-quarter turns either way.
    box = _largest(_detect(gray, _PROFILE))
    if box is not None:
        return box
    box = _largest(_detect(cv2.flip(gray, 1), _PROFILE))
    if box is not None:
        x, y, bw, bh = box
        return (w - x - bw, y, bw, bh)   # un-mirror the x coordinate

    return None


def count_faces(bgr):
    """Number of distinct, significant upright faces — for the group-photo check.
    Deliberately upright-frontal only (no rotation/profile passes) and area-filtered:
    the Haar cascade emits spurious small boxes, so counting everything would
    over-reject a valid solo photo. A box counts only if it is at least 45% of the
    largest box's area."""
    gray = cv2.equalizeHist(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY))
    faces = _detect_frontal(gray)
    if len(faces) <= 1:
        return len(faces)
    areas = [int(w) * int(h) for (_, _, w, h) in faces]
    biggest = max(areas)
    return sum(1 for a in areas if a >= 0.45 * biggest)


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
    if count_faces(bgr) > 1:
        raise MultipleFacesError("more than one face detected")
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
