"""The artifacts that flow through the generation engines.

ARCHITECTURE.md §4 / §6. Everything here is METADATA + refs — the actual pixels live in the
runtime PipelineContext, keyed by `image_ref.location`. Keeping pixels OUT of the domain is
what lets these types stay torch/PIL-free and importable in the control plane.

Flow: Prompt/PromptSet -> Candidate -> ScoredCandidate -> Winner -> FinalImage.

TRANSIENT EXECUTION STATE, NOT PERSISTED BUSINESS ENTITIES. BetterSnap does not sell or store
a "ScoredCandidate" or a "Winner" — these are typed value objects that exist only within one
GPU execution and collapse into GenerationJob.final_images + the run manifest at the end. They
are FOUR GUARANTEE-STATES (generated -> judged -> assigned-to-a-delivered-slot -> enhanced),
NOT four pipeline stages: adding pipeline stages (face repair, lighting correction, ...) does
NOT add classes here — those are Enhancement sub-steps that live in runtime (PipelineContext),
invisible to the domain. The chain is kept typed (rather than one mutable Candidate with
optional fields) so the engine contracts catch wrong-state data at author time — worth it for
paid output. This is a FROZEN decision (ARCHITECTURE.md amendment rule).
"""
from __future__ import annotations

from dataclasses import dataclass

from .refs import Ref


@dataclass(frozen=True)
class Prompt:
    positive: str
    negative: str
    seed: int
    combo_label: str = ""          # "background_ref | attire_ref" or "custom_scene"
    slot_id: int | None = None     # which OutputSlot this targets (Phase 8)


@dataclass(frozen=True)
class PromptSet:
    prompts: tuple[Prompt, ...]


@dataclass(frozen=True)
class Candidate:
    """One generated image at base resolution (pre-enhancement). `image_ref` points into the
    PipelineContext image store (or blob) — no pixels here."""
    id: str
    prompt: Prompt
    image_ref: Ref
    slot_id: int | None = None


@dataclass(frozen=True)
class Scores:
    """Quality-Gate scores (§8). `identity` = cosine similarity to IdentityProfile.centroid
    from a commercial-safe embedder. All 0..1. `overall` is the gate's combined score."""
    identity: float
    aesthetic: float = 0.0
    prompt_match: float = 0.0
    technical: float = 0.0
    overall: float = 0.0


@dataclass(frozen=True)
class ScoredCandidate:
    candidate: Candidate
    scores: Scores
    accepted: bool = False          # cleared QualityProfile.acceptance_threshold


@dataclass(frozen=True)
class Winner:
    """A selected candidate assigned to a delivered OutputSlot."""
    scored: ScoredCandidate
    slot_id: int


@dataclass(frozen=True)
class FinalImage:
    """A winner after winners-only enhancement (upscale/realism/face-refine/grain), ready for
    delivery. `output_ref` is the delivered blob."""
    winner: Winner
    output_ref: Ref
