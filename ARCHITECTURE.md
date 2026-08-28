# BetterSnap — Architecture Constitution

**Status: FROZEN.** This document is the authoritative design contract for the BetterSnap
generation platform. Any implementation must comply with it, or explicitly amend it in the
same change. It exists so decisions are pointed to, not re-argued, during implementation.

> Scope: the AI generation platform (identity → personalization → generation → delivery).
> It does **not** redesign billing, the HTTP API, plans, or the frontend — those are the
> stable control plane the platform runs inside.

---

## 1. Principles

1. **Advanced monolith, not microservices.** One codebase, one deployment per plane, one
   database. Engines communicate through in-process interfaces, never HTTP.
2. **Identity-first, not model-first.** The platform revolves around the lifecycle of a
   customer's identity. The generation model (SDXL / RealVis / Flux / DreamBooth /
   PhotoMaker) is a replaceable implementation detail *inside* an engine.
3. **Quality-driven.** Architecture exists to make outputs measurably better, not to satisfy
   engineering aesthetics. Every quality phase must answer a measurable KPI (§8).

---

## 2. Runtime boundaries

### 2.1 Control plane vs GPU worker

| Concern | Home | Why |
|---|---|---|
| HTTP API, auth, uploads | **Azure Functions** | already correct |
| Plans, credits, reservation, refunds, daily caps | **Azure Functions** | billing must never live on the GPU |
| Selection-limit + category-rule validation | **Azure Functions** | plan rules resolved before dispatch |
| Dispatch (start ACA job via ARM SDK) | **Azure Functions** | `queue_trigger.py` / `training_trigger.py` |
| Identity understanding (embed, centroid, quality) | **Functions (CPU)** | light; runs before any model |
| Personalization (train adapter) | **ACA GPU job, MODE=train** | heavy GPU |
| Generation, evaluation, selection, enhancement | **ACA GPU job, MODE=infer** | heavy GPU |
| Delivery (SAS, notifications) | **Azure Functions** | already correct |

**The GPU worker never computes credits, plan rules, or eligibility.** It receives a
already-resolved `GenerationPlan` and executes it.

### 2.2 Domain vs Runtime (a hard code boundary)

- **`domain/`** — pure Python. **No torch, no PIL, no CUDA.** Must be importable from BOTH
  Azure Functions (CPU, no ML deps) and the GPU worker. Holds business concepts.
- **`runtime/`** — imports torch/PIL/CUDA. GPU worker only. Holds execution concepts
  (loaded models, tensors, the pipeline context, GPU state).

This split is a constraint, not a preference: if a domain object imports torch, the Functions
control plane can no longer use it.

---

## 3. Domain model

### 3.1 The three-stage object (customer intent → business rules → execution state)

```
GenerationRequest   customer intent      (gender, age, hair, attire_refs, background_refs, custom_prompt)
        │  resolved by the CONTROL PLANE (plan lookup, validation, credit reservation)
        ▼
GenerationPlan      resolved rules       (billable_count, credit_cost, plan_ref,
                                          quality_profile, candidate_budget, retry_policy,
                                          resolved attire×background caps, category_rule)
        │  handed to the GPU worker
        ▼
GenerationJob       execution state      (status, output_slots[], scores, artifact refs, timings)
```

### 3.2 IdentityProfile (per user — outlives every model)

The single source of truth for "who this person is." Built once per training, **before** any
model runs. Consumed by Personalization, Evaluation, and Selection.

```
IdentityProfile {
  user_id
  reference_crops[]        ← from crops.py (head-and-shoulders, gated)
  identity_centroid        ← embedding centroid (evaluation/centroid.py), outlier-removed
  quality metadata         ← per-crop: face_px, blur, eye-visibility, pose
  adapter_ref              ← pointer to the personalization asset (blob)
  version, status
}
```

### 3.3 Plan and QualityProfile (pricing decoupled from quality)

`Plan` mirrors `shared/plans.py` and is the **source of truth for pricing**. It does NOT
carry quality knobs — only a *name* pointing at a `QualityProfile`.

```
Plan {                                   QualityProfile {              ← separate registry
  plan_type (one_time|monthly)             acceptance_threshold          (quality strategy,
  image_count                              retry_limit                    NOT pricing)
  credits_per_image                        candidate_multiplier
  max_attires, max_backgrounds           }
  category_rule (single_type|mixable)
  monthly_images, min_session_images     STANDARD  = {0.82, 1, 1.5}   ← default for all plans
  quality_profile = "standard"           PREMIUM   = {0.90, 3, 2.0}   ← a future lever, not sold yet
}
```

`candidate_budget` is **derived**, never stored on the plan:
`candidate_budget = ceil(image_count × QualityProfile.candidate_multiplier)`.

Pricing changes and quality changes never touch each other.

### 3.4 Asset & Event seams (designed, not built)

- **Asset** — every artifact (crop, adapter, candidate, final image, embedding, manifest)
  is conceptually an Asset `{id, type, location, version, lifecycle/expiry}`. For now, refs
  are **typed pointers** (`adapter_ref`, `output_ref`) shaped so they can become `AssetId`
  later. Real driver = retention lifecycle. Do not build the registry until retention needs it.
- **Event** — an `emit(event)` seam on the engine base logs an append-only timeline
  (`GenerationRequested`, `TrainingFinished`, `CandidateRejected`, `DeliveryCompleted`, …).
  Build the seam early (debugging pain is real); do not build event-sourcing/replay.

### 3.5 Frozen sub-decisions (Phase 1 review)

Settled during the Phase 1 review; do not revisit without a genuine contradiction.

- **`Candidate → ScoredCandidate → Winner → FinalImage` is transient execution state, kept
  typed.** These are NOT persisted business entities (BetterSnap sells neither a
  "ScoredCandidate" nor a "Winner"); they exist only within one GPU execution and collapse
  into `GenerationJob.final_images` + the manifest. They model four *guarantee-states*
  (generated → judged → assigned-to-a-delivered-slot → enhanced), **not** four pipeline
  stages — new pipeline steps (face repair, lighting correction) are Enhancement sub-steps in
  `runtime`, and add **no** classes here. Kept typed (not one mutable `Candidate` with
  optional fields) so engine contracts catch wrong-state data at author time — worth it for
  paid output.
- **`GenerationPlan` carries a flattened snapshot** (`acceptance_threshold`, `retry_limit`,
  `candidate_budget`), NOT a reference to the `QualityProfile`. It is the *resolved execution
  plan* (compiler analogy): the GPU worker uses the values and does not care where they came
  from. One-source-of-truth lives upstream in the control plane, not on the plan.
- **`GenerationJob.output_refs` is derived** from `final_images` (a `@property`), never a
  stored field — one source of truth for what was delivered.
- **`IdentityProfile`: `crop_quality` = all analyzed crops; `reference_crops` = the selected
  subset** (accepted crops chosen for training/IP reference). Documented relationship, no code
  change.
- **AMENDMENT (Phase 2):** `GenerationPlan` additively carries the resolved RENDER inputs
  (`gender, age_range, hair_color, attire_refs, background_refs, custom_prompt`). Discovered
  while extracting the PromptEngine: the resolved Plan could not say WHAT to render, so
  `PromptEngine.build(plan)` had nothing to build from. Adding them makes the Plan a complete,
  self-contained instruction (the "compiler output") and keeps the engine contract honest
  (input = `GenerationPlan`), rather than smuggling inputs through the runtime context. Purely
  additive (new fields default to empty) — Phase 1 contracts/tests unaffected.

---

## 4. Engine contracts

Eight interfaces, each with a default implementation. Model names appear ONLY inside
Personalization and Generation.

```
IdentityEngine.analyze(uploads)                  -> IdentityProfile      # raw uploads, not crops
PersonalizationEngine.fit(IdentityProfile)       -> adapter_ref          # DreamBooth | PhotoMaker | Flux
PromptEngine.build(GenerationPlan)               -> PromptSet
GenerationEngine.generate(PromptSet, adapter_ref)-> Candidate[]          # SDXL | RealVis | Flux
EvaluationEngine.score(Candidate[], IdentityProfile) -> ScoredCandidate[]
SelectionEngine.select(ScoredCandidate[], GenerationPlan) -> Winner[]
EnhancementEngine.enhance(Winner[])              -> FinalImage[]         # ESRGAN + realism + face-refine + grain
DeliveryEngine.deliver(FinalImage[], GenerationJob) -> urls
```

**Runtime (`runtime/`):** `PipelineContext` carries the actual PIL images / tensors /
embeddings within a single GPU execution; hydrated from `GenerationJob` + `IdentityProfile`
at entry, flushed to blob + `GenerationJob` at exit.

---

## 5. Pricing boundaries (non-negotiable)

1. **Bill delivered slots, never candidates.** Billable unit = delivered image =
   `plan.image_count`. Credits charged = `image_count × credits_per_image`, at reservation.
   Unchanged from today (`job_reservation.py`).
2. **Candidates are internal COGS.** The Quality Gate may generate more than
   `image_count` (score + retry), bounded by the derived `candidate_budget`. The customer
   never knows and is never billed for it.
3. **Never short a paid slot.** If a slot can't clear threshold within `candidate_budget`,
   deliver the best candidate — never under-deliver what was paid for.
4. **Billing stays in the control plane.** Engines assume validated, plan-aware input and
   never re-implement credits, caps, or eligibility.
5. **Monthly vs one-time is a control-plane concern.** One-time = fused train+generate,
   retrain to repeat. Monthly = train once, generate many sessions against the saved adapter.
   `PersonalizationEngine` just emits an adapter; reuse-vs-retrain is decided upstream
   (as `entrypoint.py` MODE already does). Contracts bake in no reuse assumption.

### Plan reference (from `shared/plans.py`)

| Plan | type | delivered | credits/img | credits | attire/bg | category |
|---|---|---|---|---|---|---|
| trial | one_time | 4 | 1 | 4 | — | — |
| basic | one_time | 30 | 1 | 30 | 2 / 2 | single_type |
| pro | one_time | 50 | 1 | 50 | 3 / 3 | mixable |
| expert | one_time | 70 | 1 | 70 | 5 / 5 | mixable |
| monthly_basic | monthly | 20 | 5 | 100 | — | — |
| monthly_pro | monthly | 40 | 5 | 200 | — | — |
| monthly_expert | monthly | 60 | 5 | 300 | — | — |

Retrain: FREE_RETRAINS=1, RETRAIN_CREDITS=10, MAX_TRAININGS_PER_DAY=3. Registration grants 4 credits (trial).

---

## 6. Pipeline (the business process, model-agnostic)

```
Identity Acquisition → Identity Intelligence → Personalization → Prompt Intelligence →
Candidate Generation → Evaluation → Selection → Enhancement (winners only) → Delivery
```

Key rule: **generate candidates cheaply at base resolution; score them; then spend the
expensive enhancement (upscale/realism/face-refine/grain) ONLY on selected winners.**
Identity scoring works on the raw generation — it does not need the 2048 upscale. The
enhancement *order* is fixed and deliberate (realism + face-refine run AFTER upscale; see
`main.py` — reversing it lets ESRGAN re-smooth the refined face). The change is winners-only,
not reordering.

---

## 7. Phase roadmap

Every phase leaves BetterSnap in a working, deployable state. Phases 1–2 are zero-behavior-
change. Base-model swaps come AFTER the Quality Gate so they are measured, not eyeballed.

| # | Phase | Purpose | Risk |
|---|---|---|---|
| 1 | Domain Foundation | contracts (`domain/` + `runtime/`); no wiring | very low |
| 2 | Engine Extraction | main.py → orchestrator; code moved behind engines; output byte-identical | low |
| 3 | Identity Intelligence | quality analysis + IdentityProfile; better training data | medium |
| 4 | Personalization | wrap trainer behind PersonalizationEngine | medium |
| 5 | Generation Tuning | prompts/scheduler/CFG/steps/IP/negatives — **no base swap** | medium |
| 6 | Quality Gate | per-slot generate→score→accept/retry; winners-only enhance | medium |
| 7 | Base-Model Experiments | RealVis/Flux/Juggernaut, compared on the same metrics | medium |
| 8 | Plan-aware Output | `OutputSlot` per delivered image; enforces plan caps + category_rule | medium |
| 9 | Enhancement | improve ESRGAN/realism/face-refine (winners only) | medium |
| 10 | Performance | split train/infer jobs; warm loading; batching; caching | medium |
| 11 | Enterprise | events, metrics, versioning, monitoring | low |

Critical path for image quality: **1 → 2 → 3 → 6 → 5/7**. Phases 1, 2, 10, 11 are plumbing.

---

## 8. KPIs (every quality phase answers a measurable question)

| Phase | KPI | Measured with |
|---|---|---|
| 3 | reject-rate, face-px, blur, crop-size distribution | `crops.py::assess()` |
| 4 | identity similarity (cosine to centroid) ↑ | `evaluation/centroid.py` + commercial-safe embedder |
| 5 | prompt-adherence + realism ↑ (same model) | eval harness + human review |
| 6 | % low-quality delivered ↓ | Quality-Gate scores |
| 7 | RealVis/Flux/SDXL on identical metrics | same scorer as 4/6 |
| 9 | sharpness / perceived realism ↑ | eval harness |

**Embedder licensing:** the identity embedder must be commercial-safe. `evaluation/embedder.py`
uses InsightFace (research-only) — do NOT ship it. Use the CLIP ViT-H encoder already loaded
for IP-Adapter for v1; upgrade to a permissive ArcFace later.

---

## 9. Amendment rule

This is a constitution. During implementation, do not reopen these decisions for preference or
aesthetics. Amend only when implementation reveals a **genuine contradiction** — and when you
do, update this document in the same change, with the reason. Otherwise: comply, and build.
