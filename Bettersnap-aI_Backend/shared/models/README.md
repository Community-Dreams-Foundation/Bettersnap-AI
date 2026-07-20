# Vendored models

## `face_detection_yunet_2023mar.onnx`

Face detector used by `shared/crops.py` to build identity-LoRA training crops.

| | |
|---|---|
| Source | [opencv_zoo / face_detection_yunet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet) |
| Licence | **MIT** (see `LICENSE`) — commercial use permitted, no revenue cap |
| Size | 232 KB |
| Loaded via | `cv2.FaceDetectorYN` (OpenCV >= 4.5.4; we pin `opencv-python-headless==4.11.0.86`) |

### Why it is vendored rather than downloaded

Two reasons, both learned the hard way:

1. **No runtime download.** The trainer once hung ~2h on a stalled HuggingFace fetch with
   no timeout. Nothing on a request path should depend on a third-party host being up.
2. **Reproducible builds.** A pinned file cannot silently change under us.

At 232 KB it costs nothing to ship inside the Functions package. There is no `.funcignore`,
so it is included in the deployment automatically — if one is ever added, it MUST NOT
exclude `shared/models/`, or `/train` will fail to load the detector at runtime.

### Why YuNet and not the Haar cascades

Haar returns no confidence score, so a spurious detection is indistinguishable from a real
face — and the old code picked the *largest* box. Measured on a real upload set, Haar fired
on a university crest at 178px while the actual face was 68px, so the crest won and a photo
of masonry was captioned "a photo of ohwx man" and trained on. YuNet scored every genuine
face in that set 0.93–0.95 and never produced that detection.

Licence compatibility: MIT here, Apache-2.0 for opencv_zoo, BSD-3-Clause for Real-ESRGAN —
all commercial-safe, matching the rule stated in the root `Dockerfile`. Do **not** swap in a
non-commercial detector (e.g. anything under a research-only or CC-BY-NC licence).
