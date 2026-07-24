# BetterSnap — Technical Decision Log

Companion to `ARCHITECTURE.md`. That document says **what the system is**; this one says
**what we tried, what the evidence said, and what is therefore settled.**

**Read this before re-opening any question below.** Several entries are marked REJECTED
*with measurements* — they are plausible-sounding ideas that were tested and did not work.
Re-running them costs GPU hours and produces the same answer.

Convention: ✅ Accepted · ❌ Rejected · 🚧 Planned · 🔵 Open.
Every entry cites the evidence. "Measured" means a number from `evaluation/benchmark.py`;
anything unmeasured is labelled a hypothesis.

---

## How claims here are measured

**The ruler:** `evaluation/benchmark.py` — ArcFace embedding cosine similarity between
generated images and the centroid of the subject's own training crops.

**Calibration is the whole trick.** Outputs are scored against the centroid, so the
reference bar must be too: **leave-one-out genuine-photo-vs-centroid**, never pairwise
photo-vs-photo. A centroid is an average and is always closer than any individual photo, so
a pairwise bar (0.719 on our first subject) flatters results badly — it reported outputs at
"96.8% of ceiling" when the honest figure was 84.3%.

**Two limits that bound every conclusion below:**

1. **Run-to-run noise ≈ 0.010.** Measured by running the *identical* config twice (v49 vs
   v50 baseline). Generation is seeded and deterministic, but the enhancement passes are
   not, so a whole run carries a shared random offset. The 30 images inside one run are
   therefore **pseudo-replicates, not 30 independent samples** — paired t-statistics
   overstate confidence for small deltas. **A single run per arm cannot resolve anything
   below ~0.02.**
2. **Absolute scores are not "percent of identity".** ArcFace was trained on photographs;
   synthetic images carry a domain-shift penalty even when identity is correct. The
   benchmark is trustworthy for **A-vs-B comparison**, not as a literal fidelity percentage.

**Licensing gate:** the InsightFace models used are **non-commercial research only**
(documented in `evaluation/embedder.py`). This is internal measurement tooling and is *not*
imported by the GPU image or the Functions app. Anything that would put ranking *into the
product* needs a commercially-licensed embedder first.

---

## ✅ Accepted

### A1. `IP_ADAPTER_SCALE = 0.2` — FROZEN
**Problem:** identity conditioning strength unknown.
**Experiment:** A/B on female then male subjects at 0.2 vs 0.6.
**Result:** 0.6 over-conditions (visible reference bleed, fights the attire/scene prompt);
0.2 follows the prompt while preserving likeness.
**Decision:** shipped at 0.2. **Do not re-tune.** Later work confirmed it indirectly — the
A/B was run on *enhanced* outputs, which is the shipping pipeline, so it remains valid.

### A2. Enhancement chain stays ON (realism + face-refine)
**Hypothesis (mine):** face-refine at strength 0.45 was *eroding* identity.
**Experiment:** 4-arm ablation, same LoRA/seeds/prompts, IP frozen.
**Result — hypothesis REFUTED, the opposite is true:**

| arm | identity | vs raw |
|---|---|---|
| raw (both off) | 0.6154 | — |
| realism only | 0.6574 | +0.042 |
| face-refine only | 0.6652 | +0.050 |
| **both (production)** | **0.6844** | **+0.069** |

Effects are **sub-additive** (+0.050 and +0.042 individually, +0.069 together) — both passes
re-condition the face via IP-Adapter, so the second has less left to fix.
**Decision:** keep both ON. Enhancement is load-bearing, not cosmetic. The worst *raw* image
scored 0.370 — near the threshold where ArcFace stops calling it the same person.

### A3. Exclude faceless / unusable crops from training — biggest measured win
**Problem:** a real customer's likeness was poor (0.6185, 76.1% of ceiling).
**Cause:** one training crop contained **no face at all** — Haar had fired on a university
crest (178 px) instead of the subject's face (~90 px), so a photo of masonry was captioned
"a photo of ohwx man" and trained as him.
**Experiment:** retrain on the 8 valid crops, `img6` excluded. Everything else frozen.
**Result:** **0.6185 → 0.6844 (+0.066, ~6× the noise floor)**, 76.1% → **84.2%** of ceiling —
statistically indistinguishable from a clean celebrity set (84.3%). All 4 matched pairs
improved; every new image beat the old *mean*; only 7 of the old 30 reached the new *worst*.
Average face size in output also rose 533 → 726 px (DreamBooth learns composition too, and
the faceless crop was dragging framing wider).
**Decision:** input validation is the highest-leverage quality lever found to date.

### A4. YuNet replaces Haar, plus `MIN_FACE_PX=200` and the eye-occlusion gate
Haar has no confidence score, so a false positive is indistinguishable from a real face and
`_largest()` picks the biggest box — which is how the crest won. YuNet (CNN, MIT licence,
vendored ONNX) returns per-detection confidence: 0.87–0.95 on genuine faces, and it does not
fire on the crest. **Verified** against the exact crop that poisoned A3: YuNet finds 0 faces
and would reject it. Deployed 2026-07-23 (the customer in A3 trained one day earlier).

### A5. EXIF orientation must be honoured at upload
**Problem:** a pristine 7.2 MP iPhone photo returned `FACE_NOT_FOUND`.
**Cause:** phones store a portrait shot as *landscape* pixels plus an orientation tag;
`Image.open` does not apply it, so the face arrived sideways and YuNet failed.
**Perverse failure mode:** messengers bake rotation in and strip the tag, so **degraded,
low-resolution copies passed while pristine originals were rejected** — the gate was
discarding exactly the input `MIN_FACE_PX` exists to obtain.
**Result:** 0 faces → 1 face at 643 px (`FACE_NOT_FOUND` → `OK`/ideal). No regression on
existing sets (`exif_transpose` is a no-op without the tag).

### A6. Fused `MODE=train_infer` for the first session
One-time buyers and monthly first sessions now train **and** generate in one container:
one cold start, one queue hop (~4 min of a ~45 min journey). The mode already existed in
`entrypoint.py`; only dispatch was missing.
**Safety:** the dispatcher **claims the parked job out of `waiting_lora`** in the same
transaction that claims the training — because `_finish_training` releases everything left
in that state, which would generate the job a **second time on one payment**. Claiming makes
it invisible to that query, so `_finish_training` is untouched. `processing` is reaper-visible,
so a fused run that dies is still recovered.
**Fallback:** any failure to claim → plain `MODE=train`, i.e. today's exact behaviour.
**Status:** committed, **not deployed** — wants one supervised production run first.

### A7. `USER_ID` lowercased at the inference entry
Training writes blobs under the lowercase Entra `oid`; SQL returns the GUID uppercase; blob
paths are case-sensitive. Without normalisation every generation fails "identity LoRA
missing". Confirmed load-bearing on a live run.

---

## ❌ Rejected — do not re-open without new evidence

### R1. Centroid-best IP-Adapter reference selection
**Hypothesis:** the reference is `img0` merely because it sorts first; picking the
centroid-closest crop should improve identity.
**Experiment:** 3 arms, randomised order, only `IP_ADAPTER_REF_INDEX` varied, reference
quality spread 0.179.

| reference | quality | identity |
|---|---|---|
| worst (img3) | 0.7545 | 0.5615 |
| **baseline (img0)** | 0.8350 | **0.6081** |
| "best" (img7) | 0.9337 | 0.5973 |

**Non-monotonic.** The mid-quality reference won.
**What IS true:** a *bad* reference costs ~0.047 (lost 30/30 paired comparisons) — reference
choice genuinely matters. **What is NOT true:** that centroid similarity ranks references
usefully. Identity-typical ≠ good conditioning image (it is blind to resolution — on one set
it ranked a 90 px face as "most representative").
**Decision:** the defensible change is a **quality floor** (avoid bad references), not
maximisation. Centroid-based auto-selection **not shipped** — no measured gain over `img0`,
and it would have required licensing a commercial embedder to deliver nothing.

### R2. Tuning `FACE_REFINE_STRENGTH` to recover skin texture
**Hypothesis:** enhancement buys identity by smoothing skin; a lower strength should trade
back some identity for texture.
**Experiment:** 0.25 / 0.35 / 0.45, everything else frozen.

| strength | identity | skin texture |
|---|---|---|
| 0.25 | 0.6622 | 527.9 |
| 0.35 | 0.6840 | 528.1 |
| 0.45 (prod) | 0.6844 | 525.6 |
| *raw (no img2img)* | *0.6154* | *624.3* |

**The knob does not trade texture for identity — it only costs identity.** Texture is
identical (~526–528) at every strength, while raw is 16% higher.
**Cause:** the loss tracks *how many img2img passes touch the face*, not how strongly —
realism at 0.18 alone costs 8%. Almost certainly the **VAE encode→decode round-trip**, which
cannot reconstruct pore-level detail and is strength-independent.
**Decision:** 0.35 is equivalent to 0.45 (−0.0004, far below noise) and has a better
worst-case, so it is a free reduction in processing — but **do not chase texture with this
knob.** Structural options only: composite the refined face back over the original
high-frequency layer, raise `GRAIN_AMOUNT`, or skip realism when face-refine already ran.

### R3. "The artificial look comes from base generation"
Refuted by R2's raw arm: **raw SDXL output has the most natural skin texture** of all arms
(624 vs 526). The processed look is introduced by the enhancement chain — which is also what
carries +0.069 identity. That is the real trade, and it is structural, not a parameter.

---

## 🚧 Planned

### P1. Duplicate detection at upload — *next*
Production has **none**. A real set of 8 files that was 4 photos each copied twice passed the
gate as "8 valid photos" and would have burned ~34 min of A100. Duplicates do not add
information; they double the weight on those frames and make overfitting worse.
Use a **perceptual hash** (byte-hashing misses re-saves and light crops);
`evaluation/identity_engine.py` already has a `_phash` helper.

### P2. Upload scorecard / photo-quality engine
`crops.assess()` already returns `face_px`, `confidence`, `face_area_pct`, `upscale`,
`eye_ratio` and a specific reason code per photo — its docstring literally says *"for any
future 'rate my upload set' UI"* — and **it is exposed by no endpoint.** Surfacing it lets a
user fix their set *before* training instead of discovering a weak likeness 40 minutes later.
Competitors show static good/bad examples; none of them can tell you *your* photo is 193 px
and needs 200.

### P3. Ship the PhotoTips copy fix
Written, uncommitted, not live. Targets the distant-upload problem the backend deliberately
won't reject (200–328 px passes as "usable"). One caveat found late: the drafted line
*"Selfies work best"* is half wrong — see O3.

### P4. The Realism Project
Not an experiment; a project. Why do competitors produce more photographic skin, eyes and
micro-texture? Is the limit the enhancement architecture or the base checkpoint? R2 and R3
have already narrowed it: the enhancement chain is where texture dies, and the loss is a VAE
property, not a tuning parameter.

---

## 🔵 Open / unresolved

### O1. The ~0.13 identity gap in upstream generation
Even with good training photos, generated images sit ~0.13 below a genuine photo of the same
person (celebrity, clean close-up set: 0.6956 vs 0.8250). Not post-processing (A2), not
reference ranking (R1), not training data (A3 closed that for one subject). It lives in the
upstream identity-generation stage as a whole — training images → LoRA training → how the
LoRA is applied → SDXL prior → conditioning → initial generation. **Not yet localised
further.**

### O2. Attire fidelity
Selected `navy_suit_tie` + `pinstripe`; nearly all jackets rendered **grey/charcoal**, zero
visible pinstripe, and 3 of 10 sampled images had **no jacket at all**. Prompts verified
correct ("sharp navy-blue business suit…"), so this is **not** payload or prompt
construction. **Hypothesis (untested):** IP-Adapter conditions on `crop_upperbody` crops that
contain the subject's original clothing, out-competing the attire text.

### O3. Selfie-lens distortion
Front cameras at arm's length enlarge the nose and narrow the cheeks and jaw. A
selfie-heavy training set would teach that distortion as the person's real face — which
matches the standing complaint that generated faces look "slimmer / narrower / leaner".
**Hypothesis, untested.** It also means "take selfies" is the wrong advice: the gate measures
*pixels*, not distance, so **"send the original file, don't send a messenger copy"** is the
higher-leverage instruction. Testable by comparing a selfie-trained LoRA against one trained
at portrait distance.

### O4. `jobs.credits_consumed` is always 0
Customers are billed correctly (`job_params.credit_cost` + the user balance), but the column
stays 0, so any usage/revenue analytics summing it reads zero. Harmless to customers, wrong
for reporting.

---

## Superseded beliefs (kept so they are not re-derived)

- *"Enhancement is making the face waxy and costing identity."* — **False.** It adds +0.069.
  Texture loss is real but is a VAE-roundtrip property (R2/R3).
- *"The celebrity result tells us what customers get."* — **False.** Celebrity training data
  is close, sharp and professionally lit, and SDXL likely already knows the face. It
  flattered the pipeline; a real customer scored 0.194 below their own ceiling vs 0.130.
- *"Bad photos explain the whole quality problem."* — **Partly.** Removing one corrupt crop
  recovered nearly all of one customer's deficit, but a ~0.13 gap survives on *good* data
  (O1).
- *"Only selfies can pass the gate."* — **False.** Same framing at full resolution passes
  easily; the earlier failures were compressed files (0.4–1.2 MP against a 12 MP phone).
