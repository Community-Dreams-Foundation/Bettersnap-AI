"""GenerationPlan (resolved business rules) and GenerationJob (execution state).

ARCHITECTURE.md §3.1 / §5. Second and third of the three-stage flow:
    GenerationRequest -> [control plane] -> GenerationPlan -> [GPU worker] -> GenerationJob

GenerationPlan is what the GPU worker receives: fully resolved, validated, plan-aware. The
worker NEVER computes credits or eligibility — `billable_count`, `credit_cost` and
`candidate_budget` are already decided upstream. `candidate_budget` is the COGS cap and is
NEVER billed to the customer (§5).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .candidate import FinalImage
from .plan import Plan
from .refs import Ref


@dataclass(frozen=True)
class OutputSlot:
    """One delivered image the customer paid for (Phase 8) — the billable unit. Renders one
    (attire, background) combo, or a custom scene. Count of slots == billable_count."""
    slot_id: int
    attire_ref: str | None = None
    background_ref: str | None = None
    custom_prompt: str = ""


@dataclass(frozen=True)
class GenerationPlan:
    """Resolved, validated, plan-aware instruction handed to the GPU worker."""
    user_id: str
    job_id: str
    plan: Plan
    billable_count: int          # = plan.image_count (monthly: resolved session size)
    credit_cost: int             # charged at reservation, in the control plane (§5)
    candidate_budget: int        # DERIVED from QualityProfile; COGS cap, NOT billed
    acceptance_threshold: float  # from the resolved QualityProfile
    retry_limit: int             # from the resolved QualityProfile
    slots: tuple[OutputSlot, ...] = ()
    # Resolved RENDER inputs (Phase-2 amendment, ARCHITECTURE.md §3.5): what to render, carried
    # forward from the (validated) GenerationRequest so the resolved Plan is a COMPLETE, self-
    # contained instruction the PromptEngine can build from — no need to reach back to the
    # Request or smuggle inputs through the runtime context.
    gender: str = ""
    age_range: str = ""
    hair_color: str = ""
    attire_refs: tuple[str, ...] = ()
    background_refs: tuple[str, ...] = ()
    custom_prompt: str = ""


class JobStatus(str, Enum):
    QUEUED = "queued"
    WAITING_LORA = "waiting_lora"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class GenerationJob:
    """Execution state — updated as the pipeline runs. Holds refs + scalars, never pixels.
    Promotes the old write-once run manifest (main.py) into the live workflow record."""
    job_id: str
    user_id: str
    status: JobStatus = JobStatus.QUEUED
    final_images: list[FinalImage] = field(default_factory=list)
    candidates_generated: int = 0     # COGS accounting (candidates != delivered, §5)
    generation_seconds: float = 0.0
    manifest: Ref | None = None

    @property
    def output_refs(self) -> list[Ref]:
        """Delivered blob pointers — DERIVED from final_images, so there is one source of
        truth for what was delivered (the two lists cannot drift apart)."""
        return [f.output_ref for f in self.final_images]
