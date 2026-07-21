"""Plan (pricing) and QualityProfile (quality) — deliberately decoupled.

ARCHITECTURE.md §3.3 / §5. `Plan` is the pricing + entitlement source of truth (mirrors
shared/plans.py). It carries NO quality knobs — only the *name* of a QualityProfile. The
candidate budget (how many candidates the Quality Gate may generate as COGS) is DERIVED from
the QualityProfile, never stored on the plan — so pricing and quality never touch each other.
Pure Python: importable from Functions and the GPU worker.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


class PlanType(str, Enum):
    ONE_TIME = "one_time"
    MONTHLY = "monthly"


class CategoryRule(str, Enum):
    SINGLE_TYPE = "single_type"   # trial/basic: professional OR personal, not both
    MIXABLE = "mixable"           # pro/expert: may mix professional + personal


@dataclass(frozen=True)
class Plan:
    """Pricing + entitlement. Source of truth is shared/plans.py; this mirrors it into the
    domain so engines can read plan rules without importing the Functions app. Billing math
    itself stays in the control plane (§5)."""
    key: str
    plan_type: PlanType
    image_count: int
    credits_per_image: int
    max_attires: int
    max_backgrounds: int
    category_rule: CategoryRule
    monthly_images: int = 0
    min_session_images: int = 0
    quality_profile: str = "standard"

    def credit_cost(self, image_count: int | None = None) -> int:
        n = self.image_count if image_count is None else image_count
        return n * self.credits_per_image


@dataclass(frozen=True)
class QualityProfile:
    """Quality strategy, independent of pricing. Many plans may share one; a plan can move to
    a stricter profile with NO price change (§3.3). `candidate_multiplier` sets the COGS cap."""
    name: str
    acceptance_threshold: float
    retry_limit: int
    candidate_multiplier: float

    def candidate_budget(self, image_count: int) -> int:
        """Derived COGS cap: the max candidates the Quality Gate may generate for a job of
        `image_count` delivered slots. Never billed to the customer (§5)."""
        return math.ceil(image_count * self.candidate_multiplier)


# Default registry. STANDARD applies to every plan today. PREMIUM is a SEAM (§3.3): the fields
# exist so tier-differentiated quality can become a pricing lever later, but it is not sold yet.
QUALITY_PROFILES: dict[str, QualityProfile] = {
    "standard": QualityProfile("standard", acceptance_threshold=0.82, retry_limit=1, candidate_multiplier=1.5),
    "premium":  QualityProfile("premium",  acceptance_threshold=0.90, retry_limit=3, candidate_multiplier=2.0),
}


def quality_profile_for(plan: Plan) -> QualityProfile:
    """The QualityProfile a plan resolves to, defaulting to STANDARD for any unknown name."""
    return QUALITY_PROFILES.get(plan.quality_profile, QUALITY_PROFILES["standard"])
