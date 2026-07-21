"""Event timeline SEAM.

ARCHITECTURE.md §3.4. An append-only record of what happened, for debugging / audit / metrics
WITHOUT coupling engines to each other. Phase 1 ships only the seam: engines call `emit(...)`;
where events go is a pluggable sink the runtime sets. The durable sink (table/blob) and any
event-sourcing/replay are DEFERRED until a real need — build the seam, not the machinery.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class EventType(str, Enum):
    GENERATION_REQUESTED = "generation_requested"
    IDENTITY_PROFILE_CREATED = "identity_profile_created"
    TRAINING_STARTED = "training_started"
    TRAINING_FINISHED = "training_finished"
    CANDIDATE_GENERATED = "candidate_generated"
    CANDIDATE_REJECTED = "candidate_rejected"
    SLOT_FILLED = "slot_filled"
    ENHANCEMENT_STARTED = "enhancement_started"
    ENHANCEMENT_FINISHED = "enhancement_finished"
    DELIVERY_COMPLETED = "delivery_completed"
    JOB_FAILED = "job_failed"


@dataclass(frozen=True)
class Event:
    type: EventType
    job_id: str
    data: dict[str, Any] = field(default_factory=dict)


# The sink is pluggable and defaults to None (no-op). The runtime installs one (a logger, and
# later a durable table/blob writer). Engines never know where events go.
_sink: Callable[[Event], None] | None = None


def set_event_sink(sink: Callable[[Event], None] | None) -> None:
    global _sink
    _sink = sink


def emit(event: Event) -> None:
    if _sink is not None:
        _sink(event)
