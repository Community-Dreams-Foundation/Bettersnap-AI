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

WHY YUNET, NOT HAAR (2026-07-20)
--------------------------------
Haar has no confidence score, so a spurious detection is indistinguishable from a real
face — and `_largest()` picks the BIGGEST box. Measured on a real user's upload set:
on `img7` Haar fired on a university crest at 178px while the actual face was 68px, so
the crest won and a photo of MASONRY was captioned "a photo of ohwx man" and trained on.
The multi-cascade + rotation + profile retries below made this worse: every extra pass is
another chance to false-positive. YuNet (CNN, MIT licence, bundled with OpenCV >= 4.5.4
as cv2.FaceDetectorYN) returns a per-detection confidence; on the same set it scored
every genuine face 0.93-0.95 and did not fire on the crest. Haar remains reachable via
FACE_DETECTOR=haar purely as an emergency rollback — do not use it by default.

WHY A MINIMUM FACE SIZE
-----------------------
Detection being correct is not enough. The crop is `face_h / face_frac` px wide and is
resized to `size`, so a small face is UPSCALED: at DEFAULT_FACE_FRAC the face renders at
size*0.32 = 328px, and a source face below that is interpolated. In the same real set,
6 of 9 photos had faces of 78-86px (0.3% of frame vs the ~4% guideline) = a ~4x upscale,
so the adapter learned blurry, waxy skin and a soft likeness. MIN_FACE_PX rejects those
before the GPU ever sees them. NOTE: sharpness metrics do NOT substitute for this —
variance-of-Laplacian on the face region RANKED THE BEST PHOTO WORST in that set (a tiny
face is full of high-frequency pixel noise), so it would reject exactly the wrong photos.
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

# ── Face-size gate ───────────────────────────────────────────────────────────
# Source face height below which the crop would be upscaled unacceptably. 328px
# (= DEFAULT_SIZE * DEFAULT_FACE_FRAC) means no upscaling at all, but that rejects
# almost every ordinary phone photo, so MIN_FACE_PX caps upscaling at ~1.6x instead
# and WARN_FACE_PX flags the 200-328px band as "usable but not ideal".
MIN_FACE_PX = int(os.environ.get("MIN_FACE_PX", "200"))
WARN_FACE_PX = int(round(DEFAULT_SIZE * DEFAULT_FACE_FRAC))    # 328

# ── Eye-occlusion (sunglasses) gate ──────────────────────────────────────────
# Eyes carry more identity than any other feature, and a training set where most usable
# photos are in sunglasses teaches the adapter a face with no eyes to speak of. YuNet
# predicts eye LANDMARK POSITIONS even through dark lenses, so landmark presence proves
# nothing; what changes is the eye REGION's brightness. Measured as a RATIO against the
# cheek (not an absolute threshold) so it is invariant to exposure and to skin tone —
# an absolute cut-off would reject darker-skinned users' eyes as "occluded".
#
# Measured on a real upload set (2 sunglasses photos, 7 with visible eyes):
#     sunglasses     0.37, 0.55
#     eyes visible   0.91 - 1.04
# 0.70 sits in the gap with margin either side. CAVEAT: that is one person under two
# lighting conditions — widen the sample before tightening this. Deliberately lenient:
# a false reject costs a confused user, a false accept costs one weak photo out of 8-12.
EYE_VISIBILITY_RATIO = float(os.environ.get("EYE_VISIBILITY_RATIO", "0.70"))

# YuNet detection confidence. Genuine faces measured 0.93-0.95 on real uploads and the
# false positive that caused the masonry bug was a Haar artefact YuNet never produced,
# so 0.6 sits well clear of both. Raise to tighten, lower if real faces get missed.
YUNET_CONF = float(os.environ.get("YUNET_CONF", "0.6"))
YUNET_NMS = float(os.environ.get("YUNET_NMS", "0.3"))

# MIT-licensed, vendored next to this file (232 KB) so there is no runtime download and
# no dependency on a gated/rate-limited host — the trainer already lost 2h to a stalled
# model download once. Commercial-safe; do NOT swap to a non-commercial detector.
_YUNET_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "models", "face_detection_yunet_2023mar.onnx")

# Emergency rollback only: FACE_DETECTOR=haar restores the pre-2026-07-20 cascade path,
# including its false-positive behaviour. Default is YuNet.
_USE_HAAR = os.environ.get("FACE_DETECTOR", "yunet").strip().lower() == "haar"

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


class FaceTooSmallError(Exception):
    """A face was found but is too few pixels to crop without heavy upscaling. Carries
    the measured height so the caller can tell the user how far off the photo was."""

    def __init__(self, face_px: int, required_px: int = None):
        self.face_px = int(face_px)
        self.required_px = int(required_px if required_px is not None else MIN_FACE_PX)
        super().__init__(
            f"face is {self.face_px}px tall, need >= {self.required_px}px "
            f"(stand closer / crop tighter)"
        )


class EyesOccludedError(Exception):
    """A face was found but the eyes are covered (sunglasses) or too dark to read."""

    def __init__(self, ratio: float):
        self.ratio = round(float(ratio), 3)
        super().__init__(
            f"eyes appear covered (brightness ratio {self.ratio:.2f} vs cheek, "
            f"need >= {EYE_VISIBILITY_RATIO})"
        )


def _yunet(w: int, h: int):
    """A YuNet detector sized for this image. Cheap to construct (the ONNX graph is
    cached by OpenCV), and constructing per-call keeps this module thread-safe — the
    Functions host serves concurrent requests and setInputSize mutates the detector."""
    det = cv2.FaceDetectorYN.create(_YUNET_PATH, "", (w, h), YUNET_CONF, YUNET_NMS, 5000)
    det.setInputSize((w, h))
    return det


def detect_faces(bgr):
    """[((x, y, w, h), score), ...] above YUNET_CONF, highest score first."""
    if _USE_HAAR:
        return [(b, 1.0) for b in ([_largest(_detect_frontal(
            cv2.equalizeHist(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY))))] or []) if b]
    h, w = bgr.shape[:2]
    _, faces = _yunet(w, h).detect(bgr)
    if faces is None:
        return []
    out = [((int(f[0]), int(f[1]), int(f[2]), int(f[3])), float(f[-1])) for f in faces]
    return sorted(out, key=lambda t: -t[1])


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


def detect_faces_landmarks(bgr):
    """[(box, score, [(x, y) x5]), ...] — YuNet's 5 landmarks are right eye, left eye,
    nose, right mouth corner, left mouth corner. Empty under FACE_DETECTOR=haar, which
    has no landmarks at all."""
    if _USE_HAAR:
        return []
    h, w = bgr.shape[:2]
    _, faces = _yunet(w, h).detect(bgr)
    if faces is None:
        return []
    out = []
    for f in faces:
        box = (int(f[0]), int(f[1]), int(f[2]), int(f[3]))
        pts = [(int(f[4 + 2 * i]), int(f[5 + 2 * i])) for i in range(5)]
        out.append((box, float(f[-1]), pts))
    return sorted(out, key=lambda t: -t[1])


def _patch(bgr, cx, cy, r):
    h, w = bgr.shape[:2]
    x0, x1 = max(0, cx - r), min(w, cx + r)
    y0, y1 = max(0, cy - r), min(h, cy + r)
    return bgr[y0:y1, x0:x1] if x1 > x0 and y1 > y0 else None


def eye_visibility_ratio(bgr):
    """Mean brightness of the two eye regions over the mean of two cheek regions, or
    None when it cannot be measured (no face, no landmarks, degenerate patches).

    Cheeks are sampled midway between the nose and each mouth corner — reliably skin on
    any face, and close enough to the eyes to share their lighting."""
    faces = detect_faces_landmarks(bgr)
    if not faces:
        return None
    (_, _, fw, _), _, pts = faces[0]
    r = max(4, fw // 8)
    eyes = [_patch(bgr, x, y, r) for (x, y) in pts[:2]]
    nose, mr, ml = pts[2], pts[3], pts[4]
    cheeks = [_patch(bgr, (nose[0] + m[0]) // 2, (nose[1] + m[1]) // 2, r)
              for m in (mr, ml)]
    eyes = [p for p in eyes if p is not None and p.size]
    cheeks = [p for p in cheeks if p is not None and p.size]
    if not eyes or not cheeks:
        return None

    def _v(ps):
        return float(np.mean(
            [cv2.cvtColor(p, cv2.COLOR_BGR2HSV)[..., 2].mean() for p in ps]))

    cheek_v = _v(cheeks)
    if cheek_v < 1e-6:
        return None
    return _v(eyes) / cheek_v


def detect_largest_face(bgr):
    """(x, y, w, h) of the subject's face in ORIGINAL image coords, or None.

    Picks the HIGHEST-CONFIDENCE detection, not the largest box. That ordering is the
    fix for the masonry bug: the spurious crest box was bigger than the real face, so
    "largest" chose the wall. YuNet handles head roll, three-quarter turns and profiles
    natively, so none of the rotation/mirror retries the cascade needed are required.
    """
    faces = detect_faces(bgr)
    if not faces:
        return _detect_largest_face_haar(bgr) if _USE_HAAR else None
    return faces[0][0]


def _detect_largest_face_haar(bgr):
    """Pre-2026-07-20 cascade path. Retained ONLY for FACE_DETECTOR=haar rollback; it
    has no confidence signal and can return a non-face box (see module docstring)."""
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
    """Number of distinct, significant faces — for the group-photo check.

    Area-filtered against the largest detection (>= 45%) so a bystander far behind the
    subject doesn't trip the solo-photo rule. With YuNet the old "spurious small boxes"
    caveat is largely gone (every box already cleared YUNET_CONF), but the area filter
    still earns its place on genuine background faces."""
    faces = detect_faces(bgr)
    if len(faces) <= 1:
        return len(faces)
    areas = [w * h for ((_, _, w, h), _) in faces]
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
                            headroom: float = DEFAULT_HEADROOM,
                            enforce_min_face: bool = True) -> bytes:
    """Square head-and-shoulders JPEG at `size`x`size`.

    Raises NoFaceError if no face is found — callers must surface that to the user
    rather than training on a guess. Raises FaceTooSmallError when the face is present
    but too few pixels to crop without heavy upscaling. Raises ValueError if the bytes
    aren't an image.

    `enforce_min_face=False` skips ONLY the size gate, for inspection tooling that wants
    to see how soft an undersized crop actually is. Never pass it on the /train path.
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

    # Size gate BEFORE cropping: a correctly-detected but tiny face still yields an
    # upscaled, mushy crop, and the adapter learns that mush as the user's skin.
    if enforce_min_face and face[3] < MIN_FACE_PX:
        raise FaceTooSmallError(face[3])

    # Eye gate. `ratio is None` means "could not measure" (no landmarks, degenerate
    # patches) — never treat unmeasurable as occluded, or a detector quirk silently
    # rejects a perfectly good photo.
    if enforce_min_face:
        ratio = eye_visibility_ratio(bgr)
        if ratio is not None and ratio < EYE_VISIBILITY_RATIO:
            raise EyesOccludedError(ratio)

    left, top, side = head_and_shoulders_box(face, w, h, face_frac, headroom)
    return _encode(pil.crop((left, top, left + side, top + side)), size)


def assess(image_bytes: bytes):
    """Non-raising inspection of one photo — for diagnostics, tests and any future
    'rate my upload set' UI. Returns a dict; never raises for image content."""
    try:
        pil = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception as e:
        return {"ok": False, "code": "NOT_AN_IMAGE", "detail": str(e)}

    bgr = cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR)
    h, w = bgr.shape[:2]
    faces = detect_faces(bgr)
    out = {"width": w, "height": h, "faces": len(faces)}
    if not faces:
        return {**out, "ok": False, "code": "FACE_NOT_FOUND"}

    (fx, fy, fw, fh), score = faces[0]
    out.update({"face_px": fh, "confidence": round(score, 3),
                "face_area_pct": round(100.0 * fw * fh / (w * h), 2),
                "upscale": round(WARN_FACE_PX / max(fh, 1), 1)})
    ratio = eye_visibility_ratio(bgr)
    if ratio is not None:
        out["eye_ratio"] = round(ratio, 3)
    if count_faces(bgr) > 1:
        return {**out, "ok": False, "code": "MULTIPLE_FACES"}
    if fh < MIN_FACE_PX:
        return {**out, "ok": False, "code": "FACE_TOO_SMALL"}
    if ratio is not None and ratio < EYE_VISIBILITY_RATIO:
        return {**out, "ok": False, "code": "EYES_OCCLUDED"}
    if fh < WARN_FACE_PX:
        return {**out, "ok": True, "code": "OK_LOW_RES", "warn": True}
    return {**out, "ok": True, "code": "OK"}


def center_square_crop(image_bytes: bytes, size: int = DEFAULT_SIZE) -> bytes:
    """Centred square crop. NOT used by /train — a faceless photo must be rejected,
    not guessed at. Kept for the CLI, which flags such photos explicitly."""
    pil = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = pil.size
    side = min(w, h)
    left, top = (w - side) // 2, (h - side) // 2
    return _encode(pil.crop((left, top, left + side, top + side)), size)
