"""BetterSnap runtime — GPU-worker-only execution concepts (imports torch/PIL).

Kept separate from `domain/` so the domain stays importable in the CPU control plane (Azure
Functions). See ARCHITECTURE.md §2.2.
"""
from __future__ import annotations

from .pipeline_context import PipelineContext

__all__ = ["PipelineContext"]
