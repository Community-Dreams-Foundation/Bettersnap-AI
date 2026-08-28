"""The eight engine contracts (Protocols).

ARCHITECTURE.md §4. Structural interfaces ONLY — implementations live in runtime/ (Phase 2+),
where they touch torch/PIL. Model names (SDXL, RealVis, Flux, DreamBooth, PhotoMaker) appear
only inside the Personalization and Generation *implementations*, never in these signatures.

All signatures are domain-typed. Engines that touch pixels receive their runtime dependencies
(the loaded pipeline, the PipelineContext) at CONSTRUCTION — not through these methods — so the
contracts stay torch-free. Pixels flow via `image_ref` into the runtime context, not through
these interfaces.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from .candidate import Candidate, FinalImage, PromptSet, ScoredCandidate, Winner
from .generation_job import GenerationJob, GenerationPlan
from .identity_profile import IdentityProfile
from .refs import Ref


@runtime_checkable
class IdentityEngine(Protocol):
    """Identity Acquisition + Intelligence: raw uploads -> understood identity. Takes uploads
    (not crops) so quality analysis, dedup and cropping can all evolve inside it (§7 Phase 3)."""
    def analyze(self, uploads: list[Ref]) -> IdentityProfile: ...


@runtime_checkable
class PersonalizationEngine(Protocol):
    """IdentityProfile -> personalization asset. Impl is DreamBooth today; PhotoMaker / Flux
    adapter tomorrow (§7 Phase 4). Returns the adapter Ref."""
    def fit(self, profile: IdentityProfile) -> Ref: ...


@runtime_checkable
class PromptEngine(Protocol):
    """Resolved plan -> prompts. Business/prompt logic only; no model calls."""
    def build(self, plan: GenerationPlan) -> PromptSet: ...


@runtime_checkable
class GenerationEngine(Protocol):
    """Prompts + adapter -> CANDIDATES (not final images) at base resolution. Impl is SDXL
    today; RealVis / Flux is a swapped impl (§7 Phase 7)."""
    def generate(self, prompts: PromptSet, adapter: Ref) -> list[Candidate]: ...


@runtime_checkable
class EvaluationEngine(Protocol):
    """Candidates -> scored candidates, measured against the IdentityProfile centroid and
    quality (§7 Phase 6). Commercial-safe embedder only (§8)."""
    def score(self, candidates: list[Candidate], profile: IdentityProfile) -> list[ScoredCandidate]: ...


@runtime_checkable
class SelectionEngine(Protocol):
    """Scored candidates + plan -> winners (exactly billable_count, per-slot, diverse).
    Retries only failed slots, within candidate_budget; never shorts a paid slot (§5)."""
    def select(self, scored: list[ScoredCandidate], plan: GenerationPlan) -> list[Winner]: ...


@runtime_checkable
class EnhancementEngine(Protocol):
    """Winners -> final images. The expensive chain (upscale/realism/face-refine/grain) runs
    HERE, on winners ONLY (§6). Order is fixed and deliberate — do not reorder."""
    def enhance(self, winners: list[Winner]) -> list[FinalImage]: ...


@runtime_checkable
class DeliveryEngine(Protocol):
    """Final images + job -> delivered URLs. Uploads, manifest, notifications."""
    def deliver(self, finals: list[FinalImage], job: GenerationJob) -> list[str]: ...
