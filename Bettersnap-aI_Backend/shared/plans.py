"""Single source of truth for BetterSnap plans.

EVERY plan number lives here so a price/quota/limit tweak is a one-line edit
(e.g. Basic 30 -> 35 images, or $35 -> $30). Nothing else in the codebase should
hardcode a plan quota or a per-plan limit — import from here.

Model (see milestone1-spec):
  • One-time plans (Basic/Pro/Expert): buy once, get image_count images. Internally
    a "credit" == one image (credits_per_image = 1), so credits_remaining counts
    down per generated image exactly like the monthly model will.
  • Monthly (structure wired, not fully enabled): credit-based, credits_per_image ~4-5,
    so ~20 images from a larger credit grant; LoRA trained once and reused all month.

Selection limits per plan:
  • max_attires / max_backgrounds — how many attire/background options a user may
    pick in one generation.
  • category_rule:
      "single_type" — all selections must come from ONE type (professional OR
                      personal); user cannot mix. (Basic)
      "mixable"     — selections may span professional AND personal. (Pro/Expert)

Enforcement lives in function_app.submit_job; this module is pure data + helpers.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Plan:
    key: str                 # stable id stored on users.plan_name
    name: str                # display name
    price_usd: int
    image_count: int         # images generated per one-time purchase / per session baseline
    max_attires: int
    max_backgrounds: int
    category_rule: str       # "single_type" | "mixable"
    plan_type: str           # "one_time" | "monthly"
    credits_per_image: int   # internal counter cost per generated image
    # Monthly-only extras (ignored for one_time). min_session_images bounds how few
    # images a single monthly session may request; monthly_images is the ~quota/month.
    min_session_images: int = 1
    monthly_images: int = 0


# Credits granted at registration (/users/register). This MUST cover at least one
# generation on the DEFAULT plan, or every new user is created unable to generate
# anything: the grant used to be 20 while the default plan (Basic) cost 30, so every
# single signup hit "402 Insufficient credits" on their first job, permanently, with no
# billing flow to top up. The trial plan below is what the grant is sized against.
REGISTRATION_CREDITS = 20

# ── Retraining ───────────────────────────────────────────────────────────────
# The first retrain is FREE (it covers the legitimate "my photos were bad" / "it doesn't
# look like me" case). Every one after costs credits.
#
# This is a FLAT cost, deliberately not scaled by plan: a retrain is ~32 minutes of A100
# whatever plan you are on, and with MAX_ACTIVE_GPU_JOBS=1 it also blocks the queue for
# every other user for that whole time. It is the single most expensive action a user can
# take, so it is metered rather than plan-priced.
FREE_RETRAINS = 1
RETRAIN_CREDITS = 10          # == one trial session
# Backstop so a well-funded account still cannot monopolise the single GPU.
MAX_TRAININGS_PER_DAY = 3


# ── The plans. Tweak numbers freely; structure is final. ──────────────────────
PLANS = {
    # The DEFAULT for every new signup. Free, no billing, and deliberately cheap enough
    # that REGISTRATION_CREDITS buys a real session (10 images = 10 credits, so the 20
    # granted covers two). Paid plans are entered by purchase, which grants credits.
    # Limits mirror Basic (single_type) so the trial teaches the real product's rules.
    "trial": Plan(
        key="trial", name="Free Trial", price_usd=0, image_count=10,
        max_attires=2, max_backgrounds=2, category_rule="single_type",
        plan_type="one_time", credits_per_image=1,
    ),
    "basic": Plan(
        key="basic", name="Basic", price_usd=35, image_count=30,
        max_attires=2, max_backgrounds=2, category_rule="single_type",
        plan_type="one_time", credits_per_image=1,
    ),
    "pro": Plan(
        key="pro", name="Pro", price_usd=45, image_count=50,
        max_attires=3, max_backgrounds=3, category_rule="mixable",
        plan_type="one_time", credits_per_image=1,
    ),
    "expert": Plan(
        key="expert", name="Expert", price_usd=60, image_count=65,
        max_attires=5, max_backgrounds=5, category_rule="mixable",
        plan_type="one_time", credits_per_image=1,
    ),
    # Structure wired so it's easy to enable later. credits_per_image=4 → a ~80-credit
    # grant yields ~20 images/month. Enforcement can read this exactly like one_time.
    "monthly": Plan(
        key="monthly", name="Monthly", price_usd=25, image_count=20,
        max_attires=3, max_backgrounds=3, category_rule="mixable",
        plan_type="monthly", credits_per_image=4,
        min_session_images=5, monthly_images=20,
    ),
}

DEFAULT_PLAN_KEY = "trial"


def affordable(plan: Plan, credits: int) -> bool:
    """Can `credits` pay for one full generation on this plan? The invariant that must
    hold for the default plan + REGISTRATION_CREDITS — see the guard test."""
    return credits >= credit_cost(plan, plan.image_count)


def get_plan(plan_key: str | None) -> Plan:
    """Resolve a plan by key, falling back to the default when unset/unknown so a
    missing users.plan_name never crashes submit (it just gets Basic limits)."""
    return PLANS.get((plan_key or "").strip().lower(), PLANS[DEFAULT_PLAN_KEY])


def credit_cost(plan: Plan, image_count: int) -> int:
    """Internal counter cost for generating `image_count` images on this plan."""
    return image_count * plan.credits_per_image


def public_plans() -> list[dict]:
    """Serializable plan list for GET /plans (frontend pricing + limits)."""
    return [
        {
            "key": p.key, "name": p.name, "price_usd": p.price_usd,
            "image_count": p.image_count, "max_attires": p.max_attires,
            "max_backgrounds": p.max_backgrounds, "category_rule": p.category_rule,
            "plan_type": p.plan_type, "credits_per_image": p.credits_per_image,
            "min_session_images": p.min_session_images,
            "monthly_images": p.monthly_images,
        }
        for p in PLANS.values()
    ]
