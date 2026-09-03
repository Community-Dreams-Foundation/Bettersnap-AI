"""Single source of truth for BetterSnap plans.

EVERY plan number lives here so a price/quota/limit tweak is a one-line edit. Nothing
else in the codebase should hardcode a plan quota or a per-plan limit — import from here.

Model (2026-07):
  • One-time (Basic/Pro/Expert) + Trial: an IMAGE PACK, no credit concept shown to the
    user. Internally credits_per_image = 1, so the credit counter == images remaining.
    Basic 30 / Pro 50 / Expert 70 images.
  • Monthly (Basic/Pro/Expert): CREDIT-based, credits_per_image = 5 (100/200/300 credits
    = 20/40/60 images/month); LoRA trained once, reused all month until credits run out.
  • Trial: free, the default at signup.

Plan KEYS are distinct per (tier, billing type) because Stripe prices them differently
(Basic one-time $10 vs Basic monthly $25/mo). The Stripe handlers set users.plan_name to
one of these keys via plan_key_for(); submit resolves the user's plan from plan_name.

Selection limits per plan:
  • max_attires / max_backgrounds — how many options a user may pick per generation.
  • category_rule: "single_type" (all picks from ONE of professional|personal) or
    "mixable" (may span both).

  ⚠️ NEEDS-DECISION (carried from the old config, not from Stripe): the per-tier
  attire/background limits and category_rule below are placeholders — confirm them.

Enforcement lives in function_app.submit_job; this module is pure data + helpers.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Plan:
    key: str                 # stable id stored on users.plan_name
    name: str                # display name
    price_usd: int
    image_count: int         # images per one-time purchase / per monthly session baseline
    max_attires: int
    max_backgrounds: int
    category_rule: str       # "single_type" | "mixable"
    plan_type: str           # "one_time" | "monthly"
    credits_per_image: int   # internal counter cost per generated image (5, Stripe-aligned)
    min_session_images: int = 1
    monthly_images: int = 0


# Credits granted at registration. One-time model = 1 credit/image, so this is the
# number of FREE trial images (4). MUST cover one generation on the DEFAULT (trial)
# plan — see the `affordable` guard test.  ⚠️ NEEDS-DECISION: free trial image count.
REGISTRATION_CREDITS = 4

# ── Retraining ───────────────────────────────────────────────────────────────
# First retrain FREE; each after costs credits. Flat (not plan-scaled): a retrain is
# ~32 min of A100 and, with MAX_ACTIVE_GPU_JOBS=1, blocks the queue for everyone.
# ⚠️ NEEDS-DECISION: with the mixed model a flat credit value means different image
# counts for one-time (1cr/img) vs monthly (5cr/img) — confirm the number.
FREE_RETRAINS = 1
RETRAIN_CREDITS = 10
MAX_TRAININGS_PER_DAY = 3


# ── The plans. Numbers below match the Stripe products/prices. ────────────────
PLANS = {
    # DEFAULT for every new signup. Free; REGISTRATION_CREDITS covers exactly one
    # 4-image trial session (20 credits). Limits mirror Basic so the trial teaches the
    # real rules.
    "trial": Plan(
        key="trial", name="Free Trial", price_usd=0, image_count=4,
        max_attires=2, max_backgrounds=2, category_rule="single_type",
        plan_type="one_time", credits_per_image=1,
    ),
    # ── One-time image packs (single payment; 1 credit == 1 image, no credits shown) ──
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
        key="expert", name="Expert", price_usd=65, image_count=70,
        max_attires=5, max_backgrounds=5, category_rule="mixable",
        plan_type="one_time", credits_per_image=1,
    ),
    # ── Teams (a purchased SEAT, not a personal purchase) ──
    # A seat is a Basic pack bought by an organization on a member's behalf: the Teams
    # contract is 30 headshots per seat at $35 for the first band -- the same price and the
    # same image count as Basic. Without this entry a team member fell through to
    # DEFAULT_PLAN_KEY ("trial"), so someone holding 30 paid seat credits was shown
    # "Free Trial" and CAPPED AT A 4-IMAGE SESSION. price_usd is 0 because the member
    # themselves pays nothing; the organization already did.
    "teams_basic": Plan(
        key="teams_basic", name="Team", price_usd=0, image_count=30,
        max_attires=2, max_backgrounds=2, category_rule="single_type",
        plan_type="one_time", credits_per_image=1,
    ),

    # ── Monthly (recurring) ──
    "monthly_basic": Plan(
        key="monthly_basic", name="Basic (Monthly)", price_usd=25, image_count=20,
        max_attires=2, max_backgrounds=2, category_rule="single_type",
        plan_type="monthly", credits_per_image=5, min_session_images=5, monthly_images=20,
    ),
    "monthly_pro": Plan(
        key="monthly_pro", name="Pro (Monthly)", price_usd=45, image_count=40,
        max_attires=3, max_backgrounds=3, category_rule="mixable",
        plan_type="monthly", credits_per_image=5, min_session_images=5, monthly_images=40,
    ),
    "monthly_expert": Plan(
        key="monthly_expert", name="Expert (Monthly)", price_usd=65, image_count=60,
        max_attires=5, max_backgrounds=5, category_rule="mixable",
        plan_type="monthly", credits_per_image=5, min_session_images=5, monthly_images=60,
    ),
}

DEFAULT_PLAN_KEY = "trial"


def plan_key_for(tier: str, subscription_type: str) -> str:
    """Map a Stripe (tier, type) pair to a PLANS key. one-time 'basic' -> 'basic';
    monthly 'basic' -> 'monthly_basic'. Falls back to the tier itself, then default.
    The Stripe webhook handlers use this to set users.plan_name so submit resolves the
    right plan."""
    tier = (tier or "").strip().lower()
    if (subscription_type or "").strip().lower() == "monthly":
        key = f"monthly_{tier}"
    else:
        key = tier
    return key if key in PLANS else (tier if tier in PLANS else DEFAULT_PLAN_KEY)


def affordable(plan: Plan, credits: int) -> bool:
    """Can `credits` pay for one full generation on this plan? The invariant that must
    hold for the default plan + REGISTRATION_CREDITS — see the guard test."""
    return credits >= credit_cost(plan, plan.image_count)


def get_plan(plan_key: str | None) -> Plan:
    """Resolve a plan by key, falling back to the default when unset/unknown so a
    missing users.plan_name never crashes submit.

    ⚠️ This FAILS OPEN at 1 credit/image (the trial rate). Never use it to price a
    MONTHLY account — use resolve_monthly_plan() instead, which fails closed."""
    return PLANS.get((plan_key or "").strip().lower(), PLANS[DEFAULT_PLAN_KEY])


def resolve_monthly_plan(plan_name: str | None,
                         subscription_plan: str | None = None):
    """The monthly Plan for an account whose subscription_type is 'monthly'.

    get_plan() maps a NULL, unknown, or BARE-TIER key to the trial plan at 1
    credit/image. For a monthly account that silently misprices everything by 5x:
    an add-on top-up grants a fifth of the credits the customer paid for, and a
    cancellation converts add-on credits to five times the images they own.
    users.subscription_plan holds the bare tier ('pro') for a monthly account, so
    it is the natural recovery source.

    Returns (plan, recovered). `plan` is None when no monthly plan can be
    resolved at all — callers MUST fail closed rather than price at a guess.
    """
    plan = get_plan(plan_name)
    if plan.plan_type == "monthly":
        return plan, False
    for candidate in (subscription_plan, plan_name):
        if not candidate:
            continue
        recovered = PLANS.get(plan_key_for(candidate, "monthly"))
        if recovered is not None and recovered.plan_type == "monthly":
            return recovered, True
    return None, True


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
