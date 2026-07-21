"""Typed pointers to artifacts.

Asset-ready seam (ARCHITECTURE.md §3.4): today a Ref is a thin {kind, location, version}
pointer; when the Asset registry lands (retention-driven), these become AssetIds without
changing call sites. Pure Python — no torch/PIL — so this is importable from the Azure
Functions control plane as well as the GPU worker.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RefKind(str, Enum):
    CROP = "crop"
    ADAPTER = "adapter"
    CANDIDATE = "candidate"
    OUTPUT = "output"
    EMBEDDING = "embedding"
    MANIFEST = "manifest"
    UPLOAD = "upload"


@dataclass(frozen=True)
class Ref:
    """A pointer to an artifact.

    `location` is a blob path or an in-context key (see runtime.PipelineContext); `kind`
    classifies it; `version` supports retrain / regenerate lineage. Frozen + hashable so
    refs can key dicts and sets.
    """
    kind: RefKind
    location: str
    version: int = 1


# Convenience constructors — intent-revealing call sites; all produce a plain Ref.
def crop_ref(location: str, version: int = 1) -> Ref:
    return Ref(RefKind.CROP, location, version)


def adapter_ref(location: str, version: int = 1) -> Ref:
    return Ref(RefKind.ADAPTER, location, version)


def output_ref(location: str, version: int = 1) -> Ref:
    return Ref(RefKind.OUTPUT, location, version)


def upload_ref(location: str, version: int = 1) -> Ref:
    return Ref(RefKind.UPLOAD, location, version)
