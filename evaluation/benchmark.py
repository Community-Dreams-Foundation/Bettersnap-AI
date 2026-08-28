"""BetterSnap identity benchmark — the permanent regression test for output quality.

WHY THIS EXISTS
Every change to the identity stack (LoRA training, IP-Adapter scale, enhancement chain,
prompt, sampler) has until now been judged by eye. That produces "I think it looks better"
and an endless parameter loop. This module turns output quality into a NUMBER so a change
is either an improvement or it isn't.

WHAT IT MEASURES (per subject, per run)
  ceiling      source<->source mean similarity. The subject's own photos are not identical
               to each other; this is the realistic maximum. Scoring outputs without it is
               meaningless — a 0.70 output against a 0.74 ceiling is excellent, against a
               0.95 ceiling it is poor.
  similarity   each output vs the source CENTROID (mean of source embeddings).
  pct_ceiling  similarity / ceiling — the portable number to compare across subjects.
  det_score    detector confidence. A low identity score on a low det_score image is a
               detection/framing failure, not identity drift.
  n_faces      >1 means the frame has extra people/faces; identity score is unreliable.
  yaw/pitch/roll  head pose. Profile shots legitimately score lower — this separates
               "wrong face" from "same face, turned away".
  blur         variance of Laplacian. Separates "identity drift" from "soft/blurry output".

LICENSING — READ evaluation/embedder.py. The default InsightFace models are NON-COMMERCIAL
research only. This harness is internal measurement tooling; it is NOT imported by the GPU
image or the Functions app. Swap in a commercially-licensed embedder before shipping any
of this inside the product.

USAGE
  python -m evaluation.benchmark \
      --source-dir crops/ --output-dir results/ \
      --run-id v49-job0FD79268 --meta meta.json --out benchmark/v49.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from datetime import datetime, timezone

import numpy as np


# ── metrics that do not need a face model ────────────────────────────────────
def blur_score(image_bgr) -> float:
    """Variance of the Laplacian. Higher = sharper. Lets a poor identity score be
    attributed to softness rather than to the identity stack."""
    import cv2
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _load(path):
    import cv2
    img = cv2.imread(path)
    if img is None:
        raise ValueError(f"unreadable image: {path}")
    return img


def _faces(app, image_bgr):
    return app.get(image_bgr)


def _largest(faces):
    return max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))


def analyze(app, path: str) -> dict:
    """Every per-image metric in one pass, so a bad score is always explainable."""
    img = _load(path)
    h, w = img.shape[:2]
    rec = {"image": os.path.basename(path), "width": w, "height": h,
           "blur": round(blur_score(img), 1)}
    faces = _faces(app, img)
    rec["n_faces"] = len(faces)
    if not faces:
        rec["embedding"] = None
        rec["det_score"] = None
        return rec
    f = _largest(faces)
    x1, y1, x2, y2 = f.bbox
    rec["det_score"] = round(float(f.det_score), 4)
    rec["face_px"] = int(max(x2 - x1, y2 - y1))
    rec["face_frac"] = round(float((y2 - y1) / h), 4)   # how much of frame the face fills
    pose = getattr(f, "pose", None)
    if pose is not None:
        rec["pitch"], rec["yaw"], rec["roll"] = [round(float(v), 1) for v in pose]
    rec["embedding"] = np.asarray(f.normed_embedding, dtype=np.float64)
    return rec


def cosine(a, b) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def score(source_dir: str, output_dir: str, run_id: str, meta: dict) -> dict:
    """Build one benchmark record: calibrate the ceiling, then score every output."""
    from insightface.app import FaceAnalysis
    app = FaceAnalysis(name=meta.get("embedder_model", "buffalo_l"))
    app.prepare(ctx_id=-1, det_size=(640, 640))          # CPU

    exts = ("*.jpg", "*.jpeg", "*.png")
    src_paths = sorted(p for e in exts for p in glob.glob(os.path.join(source_dir, e)))
    out_paths = sorted((p for e in exts for p in glob.glob(os.path.join(output_dir, e))),
                       key=lambda p: (len(os.path.basename(p)), os.path.basename(p)))
    if not src_paths:
        raise SystemExit(f"no source images in {source_dir}")
    if not out_paths:
        raise SystemExit(f"no output images in {output_dir}")

    print(f"scoring {len(src_paths)} source + {len(out_paths)} output images ...")
    src = [analyze(app, p) for p in src_paths]
    out = [analyze(app, p) for p in out_paths]

    src_emb = [r for r in src if r["embedding"] is not None]
    if len(src_emb) < 2:
        raise SystemExit("need >=2 source images with a detectable face to calibrate")

    E = np.array([r["embedding"] for r in src_emb])
    centroid = E.mean(axis=0)

    # CEILING — must be APPLES-TO-APPLES with how outputs are scored.
    # Outputs are compared to the CENTROID, so the reference must also be
    # "<something> vs centroid", NOT pairwise photo-vs-photo. A centroid is an average and
    # is therefore always closer than any individual photo, so a pairwise ceiling (~0.72
    # here) understates the bar and makes generated images look far better than they are.
    # LEAVE-ONE-OUT: score each genuine photo against a centroid built WITHOUT it, so the
    # image cannot inflate its own reference. This is "what a real photo of this person
    # scores" — the honest bar for a generated image.
    loo = [cosine(E[i], np.delete(E, i, axis=0).mean(axis=0)) for i in range(len(E))]
    ceiling = float(np.mean(loo))
    # Kept for context only — NEVER use as the bar for outputs (see above).
    pair = [cosine(a["embedding"], b["embedding"])
            for i, a in enumerate(src_emb) for b in src_emb[i + 1:]]

    results = []
    for r in out:
        rec = {k: v for k, v in r.items() if k != "embedding"}
        if r["embedding"] is None:
            rec["similarity"] = None
            rec["pct_ceiling"] = None
            rec["note"] = "no face detected"
        else:
            s = cosine(r["embedding"], centroid)
            rec["similarity"] = round(s, 4)
            rec["pct_ceiling"] = round(s / ceiling * 100, 1) if ceiling else None
        results.append(rec)

    scored = [r for r in results if r["similarity"] is not None]
    sims = [r["similarity"] for r in scored]
    ranked = sorted(scored, key=lambda r: r["similarity"], reverse=True)

    return {
        "run_id": run_id,
        "scored_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "embedder": f"insightface/{meta.get('embedder_model', 'buffalo_l')} (NON-COMMERCIAL research models)",
        "config": meta,
        "calibration": {
            "method": "leave-one-out genuine-photo vs centroid (apples-to-apples with output scoring)",
            "source_images": len(src_paths),
            "source_with_face": len(src_emb),
            "ceiling": round(ceiling, 4),
            "ceiling_min": round(float(np.min(loo)), 4),
            "ceiling_max": round(float(np.max(loo)), 4),
            "pairwise_context_only": {
                "mean": round(float(np.mean(pair)), 4),
                "min": round(float(np.min(pair)), 4),
                "max": round(float(np.max(pair)), 4),
                "warning": "photo-vs-photo — do NOT use as the bar for centroid-scored outputs",
            },
            "source_detail": [{k: v for k, v in r.items() if k != "embedding"} for r in src],
        },
        "summary": {
            "outputs": len(results),
            "scored": len(scored),
            "no_face": len(results) - len(scored),
            "mean_similarity": round(float(np.mean(sims)), 4) if sims else None,
            "median_similarity": round(float(np.median(sims)), 4) if sims else None,
            "best": round(float(np.max(sims)), 4) if sims else None,
            "worst": round(float(np.min(sims)), 4) if sims else None,
            "mean_pct_ceiling": round(float(np.mean(sims)) / ceiling * 100, 1) if sims else None,
            "identity_gap": round(ceiling - float(np.mean(sims)), 4) if sims else None,
            # The decisive check: if the BEST generated image still scores below the WORST
            # genuine photo, the two populations are cleanly separable — a systematic
            # identity deficit, not noise. Overlap means some outputs are indistinguishable
            # from real photos by this metric.
            "overlaps_genuine": bool(np.max(sims) >= np.min(loo)) if sims else None,
            "best_image": ranked[0]["image"] if ranked else None,
            "worst_image": ranked[-1]["image"] if ranked else None,
            "mean_blur": round(float(np.mean([r["blur"] for r in results])), 1),
        },
        "results": ranked,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-dir", required=True, help="the subject's training crops")
    ap.add_argument("--output-dir", required=True, help="generated images to score")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--meta", help="JSON file of run config (lora, ip scale, enhancement, ...)")
    ap.add_argument("--out", required=True, help="where to write the benchmark record")
    a = ap.parse_args()

    meta = {}
    if a.meta and os.path.exists(a.meta):
        # utf-8-sig: Windows editors and PowerShell's Out-File -Encoding utf8 emit a BOM,
        # which plain utf-8 rejects. Handles both BOM and no-BOM.
        with open(a.meta, encoding="utf-8-sig") as f:
            meta = json.load(f)

    rec = score(a.source_dir, a.output_dir, a.run_id, meta)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=2)

    c, s = rec["calibration"], rec["summary"]
    print("\n" + "=" * 74)
    print(f"RUN {rec['run_id']}")
    print("=" * 74)
    print(f"CEILING (source<->source) : {c['ceiling']:.4f}   "
          f"[{c['ceiling_min']:.3f}-{c['ceiling_max']:.3f}] over {c['source_with_face']} images")
    print(f"OUTPUT mean similarity    : {s['mean_similarity']}   "
          f"= {s['mean_pct_ceiling']}% of ceiling")
    print(f"  best  {s['best']}  ({s['best_image']})")
    print(f"  worst {s['worst']}  ({s['worst_image']})")
    print(f"  no face detected: {s['no_face']} / {s['outputs']}")
    print("-" * 74)
    print(f"{'image':<22}{'sim':>7}{'%ceil':>8}{'det':>7}{'faces':>7}{'yaw':>7}{'blur':>9}")
    for r in rec["results"]:
        print(f"{r['image']:<22}{r['similarity']:>7.3f}{r['pct_ceiling']:>7.1f}%"
              f"{(r['det_score'] or 0):>7.2f}{r['n_faces']:>7}"
              f"{r.get('yaw', 0):>7.1f}{r['blur']:>9.1f}")
    print("=" * 74)
    print(f"written: {a.out}")


if __name__ == "__main__":
    main()
