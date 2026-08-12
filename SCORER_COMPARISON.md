# Commercial-Safe Identity Scorer — Comparison & Recommendation

**Decision needed:** which face-identity embedder the Quality Gate (Phase 6) and the benchmark
should use in-product. The gate is already built scorer-agnostic (`runtime/engines/embedder.py`
→ `FaceEmbedder`); this picks the model that plugs in.

## Why we must change

The benchmark and all quality numbers today use **InsightFace `buffalo_l`**, whose weights are
**non-commercial (research only)**. It legally **cannot run inside the paid product**, and the
Quality Gate needs an in-product identity score to reject bad candidates. So we need a
**permissively-licensed (Apache-2.0 / MIT)** embedder. Swapping it also **re-baselines the
ruler** — every existing number (the ~0.05 gap, the 0.75 raw baseline) was measured with
`buffalo_l`, so we re-run the v49 baseline once on the new embedder to get the new thresholds.

## Options

| | **ArcFace ResNet100 ONNX** ⭐ | **FaceX (MobileFaceNet+ArcFace)** | **CompreFace** | buffalo_l (today) |
|---|---|---|---|---|
| License | **Apache-2.0** | **Apache-2.0** (code + weights) | Apache-2.0 | ❌ non-commercial |
| Architecture | ResNet100 + ArcFace | MobileFaceNet + ArcFace | ArcFace (service) | ResNet100 + ArcFace |
| Embedding | **512-dim** | 512-dim | 512-dim | 512-dim |
| Accuracy (LFW) | **~99.68%** | ~99.x% (lower) | ~99.x% | ~99.8% |
| Footprint | ~250 MB ONNX | ~5–25 MB | full Docker **service** | ~170 MB |
| Runtime | onnxruntime, **CPU or GPU** | onnxruntime / WASM, CPU | separate container + REST | onnxruntime |
| Integration effort | **Low** (drop-in ONNX session) | Low | High (stand up a service) | n/a |
| Score comparability to today | **Highest** (same ArcFace family) | High | Medium | — |

Source model: [onnxmodelzoo/arcfaceresnet100-8 (HF)](https://huggingface.co/onnxmodelzoo/arcfaceresnet100-8) ·
[OpenVINO model card](https://docs.openvino.ai/2023.3/omz_models_model_face_recognition_resnet100_arcface_onnx.html) ·
[FaceX (Apache-2.0)](https://github.com/facex-engine/facex) ·
[CompreFace](https://github.com/exadel-inc/CompreFace)

## Recommendation: **ArcFace ResNet100 ONNX**

- **Same ArcFace family as `buffalo_l`'s recogniser**, so the embedding space — and therefore
  the gap numbers — stay the closest to what you've already measured. Minimal re-baselining
  surprise.
- **Apache-2.0, ship-legal**, 512-dim, 99.68% LFW.
- **Drop-in**: an `onnxruntime` session, no torch, runs on CPU (~50–100 ms/face) or the A100.
- Highest accuracy of the permissive options → the gate's accept/reject decisions are trustworthy.

Pick **FaceX** instead only if container size / CPU latency is the priority and you'll accept a
small accuracy drop. **CompreFace** is a full recognition *service* — overkill here (you want a
library call, not a REST hop inside the GPU worker).

## How it plugs in (once you pick)

Implement `FaceEmbedder.embed(image)` in `runtime/engines/embedder.py`:
1. Detect + align the largest face — reuse the **YuNet** detector already in `shared/crops.py`.
2. Standard ArcFace preprocessing: align to 112×112, BGR, normalize.
3. Run the ONNX session → 512-dim vector → **L2-normalize** → return as plain floats.
4. Point `load_default_embedder()` at it (it currently raises by design so nothing ships
   unlicensed).

The identity centroid is then the mean of the user's reference-crop embeddings (already wired
in `runtime/quality_gate.py::run_quality_gate_for_job`).

## Then (GPU-gated)

Re-run the v49 baseline once with the chosen embedder to set `acceptance_threshold` and
`candidate_budget`, then validate the gate on ≥5 subjects. That's the first A100 step — it
needs your explicit go.
