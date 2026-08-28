"""BetterSnap domain model.

Pure Python — NO torch / PIL / CUDA — importable from BOTH the Azure Functions control plane
and the GPU worker. This is the stable contract the whole platform is organized around; see
ARCHITECTURE.md §3-§4. The runtime (torch/PIL) side lives in the separate `runtime/` package.
"""
from __future__ import annotations

from .candidate import (
    Candidate,
    FinalImage,
    Prompt,
    PromptSet,
    Scores,
    ScoredCandidate,
    Winner,
)
from .engines import (
    DeliveryEngine,
    EnhancementEngine,
    EvaluationEngine,
    GenerationEngine,
    IdentityEngine,
    PersonalizationEngine,
    PromptEngine,
    SelectionEngine,
)
from .events import Event, EventType, emit, set_event_sink
from .generation_job import GenerationJob, GenerationPlan, JobStatus, OutputSlot
from .identity_profile import CropQuality, IdentityProfile, IdentityStatus
from .plan import (
    QUALITY_PROFILES,
    CategoryRule,
    Plan,
    PlanType,
    QualityProfile,
    quality_profile_for,
)
from .refs import Ref, RefKind, adapter_ref, crop_ref, output_ref, upload_ref
from .request import GenerationRequest

__all__ = [
    # refs
    "Ref", "RefKind", "crop_ref", "adapter_ref", "output_ref", "upload_ref",
    # plan / quality
    "Plan", "PlanType", "CategoryRule", "QualityProfile", "QUALITY_PROFILES", "quality_profile_for",
    # three-stage flow
    "GenerationRequest", "GenerationPlan", "GenerationJob", "JobStatus", "OutputSlot",
    # identity
    "IdentityProfile", "IdentityStatus", "CropQuality",
    # generation artifacts
    "Prompt", "PromptSet", "Candidate", "Scores", "ScoredCandidate", "Winner", "FinalImage",
    # events
    "Event", "EventType", "emit", "set_event_sink",
    # engine contracts
    "IdentityEngine", "PersonalizationEngine", "PromptEngine", "GenerationEngine",
    "EvaluationEngine", "SelectionEngine", "EnhancementEngine", "DeliveryEngine",
]
