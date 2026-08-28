"""Composition scoring for a generated headshot — geometry only, no identity.

Reuses the SAME YuNet detector as the input-crop gate (shared/crops.py), so 'is this a
headshot' is judged consistently across the pipeline. Reports raw measurements; the
harness applies thresholds (Phase 5 defines the headshot bands: min ~28%, prefer 35-50%,
upper ~60%).

Requires the backend package on sys.path (for shared.crops). Imported lazily so this module
loads without it and tests can arrange the path.
"""


def _crops():
    from shared.crops import detect_faces_landmarks, eye_visibility_ratio, count_faces
    return detect_faces_landmarks, eye_visibility_ratio, count_faces


def composition_scores(image_bgr):
    """Geometry of the dominant face in a BGR image. Keys:
        face_count           distinct faces (solo headshots want exactly 1)
        face_height_pct      face box height as % of image height
        face_area_pct        face box area as % of image area
        center_offset        |face-center-x - frame-center-x| / width   (0 = centered)
        headroom_frac        space above the face top / height          (>0 wanted)
        top_clipped          face top at/above frame edge (head cut off)
        chin_clipped         face bottom at/below frame edge (chin cut off)
        yaw_offset           signed nose offset from eye midpoint / eye span (0 = frontal)
        eye_visibility       eye/cheek brightness ratio (low => sunglasses/closed)
    Returns {face_count: 0} when no face is found.
    """
    detect_faces_landmarks, eye_visibility_ratio, count_faces = _crops()
    h, w = image_bgr.shape[:2]
    faces = detect_faces_landmarks(image_bgr)
    if not faces:
        return {"face_count": 0}

    (fx, fy, fw, fh), score, pts = faces[0]
    out = {
        "face_count": count_faces(image_bgr),
        "confidence": round(float(score), 3),
        "face_height_pct": round(100.0 * fh / h, 2),
        "face_area_pct": round(100.0 * fw * fh / (w * h), 2),
        "center_offset": round(abs((fx + fw / 2.0) - w / 2.0) / w, 3),
        "headroom_frac": round(max(0.0, fy) / h, 3),
        "top_clipped": fy <= 1,
        "chin_clipped": (fy + fh) >= (h - 1),
    }

    # yaw from the 5 YuNet landmarks: right eye, left eye, nose, right mouth, left mouth.
    (rex, rey), (lex, ley), (nx, _) = pts[0], pts[1], pts[2]
    eye_span = abs(lex - rex)
    if eye_span > 1e-6:
        out["yaw_offset"] = round((nx - (rex + lex) / 2.0) / eye_span, 3)

    # eye-line: vertical position of the eyes in the frame (0=top, 1=bottom). Corporate
    # headshots place the eyes in the upper third (~0.30-0.45).
    out["eye_line_frac"] = round(((rey + ley) / 2.0) / h, 3)

    # shoulder room (PROXY, not true shoulder detection): fraction of frame below the face
    # box, where head-and-shoulders framing puts the shoulders/upper chest. Low => the face
    # runs to the bottom edge (passport-style), high => room for shoulders.
    out["below_face_frac"] = round(max(0.0, h - (fy + fh)) / h, 3)

    ratio = eye_visibility_ratio(image_bgr)
    if ratio is not None:
        out["eye_visibility"] = round(float(ratio), 3)
    return out


# Headshot face-size bands (% of frame height). Training crops stay at 32% (validated); these
# govern OUTPUT composition. hard_min/hard_max are accept/reject; the preferred band drives
# ranking. Calibrate if the product's framing target changes.
HEADSHOT_BANDS = {"hard_min": 28.0, "pref_lo": 35.0, "pref_hi": 50.0, "hard_max": 60.0}


def classify_face_size(face_height_pct, bands=None):
    """Bucket a face height % against the headshot bands (descriptive, for reports/gates)."""
    b = bands or HEADSHOT_BANDS
    if face_height_pct is None:
        return "unknown"
    if face_height_pct < b["hard_min"]:
        return "too_small"
    if face_height_pct < b["pref_lo"]:
        return "below_preferred"
    if face_height_pct <= b["pref_hi"]:
        return "preferred"
    if face_height_pct <= b["hard_max"]:
        return "above_preferred"
    return "too_large"


def sharpness(image_bgr):
    """Variance of the Laplacian over the whole frame — a blur/artifact proxy. Higher =
    sharper. NOTE (measured earlier): on tiny faces this rewards pixel noise, so it is a
    visual-quality signal, NOT a face-size gate."""
    import cv2
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())
