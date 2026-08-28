"""PipelineContext — the transient, in-memory object that flows through the engines within a
single GPU execution.

ARCHITECTURE.md §2.2 / §4. This is the runtime side of the domain-vs-runtime boundary: domain
objects carry only refs + metadata, while the actual pixels / tensors / loaded models live
HERE, keyed by the `Ref.location` an engine put in a Candidate/Winner. Hydrated from a
GenerationJob + IdentityProfile at entry, flushed to blob + the GenerationJob at exit.

This module is GPU-worker-only. It is intentionally NOT in `domain/` because it deals in
torch/PIL objects. PIL is imported lazily so the module can at least be imported for typing in
a CPU/test context; real use requires the GPU deps.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

try:  # pragma: no cover - the GPU image always has PIL; the guard is for CPU test hosts
    from PIL import Image as _PILImage  # noqa: F401
except Exception:
    _PILImage = None  # type: ignore


@dataclass
class PipelineContext:
    """One GPU execution's working set.

    `images` maps a Ref.location key -> PIL image; `embeddings` maps a candidate id -> vector;
    `models` is a bag of loaded runtime objects (pipe, upscaler, embedder). Nothing here is
    billed or persisted directly — it is flushed to blob and the GenerationJob at the end.
    """
    job_id: str
    user_id: str
    images: dict[str, Any] = field(default_factory=dict)       # Ref.location -> PIL.Image
    embeddings: dict[str, Any] = field(default_factory=dict)   # candidate id -> vector
    models: dict[str, Any] = field(default_factory=dict)       # "pipe" / "upscaler" / "img2img" / "face_cascade" / ...
    # Per-job RUNTIME scratch (NOT business rules — Rule 2): the IP reference face image, the
    # derived realism/face-refine prompts, ip_adapter_ok flag, etc. Set by the orchestrator.
    work: dict[str, Any] = field(default_factory=dict)

    def put_image(self, key: str, image: Any) -> None:
        self.images[key] = image

    def get_image(self, key: str) -> Any:
        return self.images[key]

    def has_image(self, key: str) -> bool:
        return key in self.images
