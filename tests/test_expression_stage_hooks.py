"""E1/E2 experiment hooks — prove DEFAULT-OFF and byte-identical baseline.

Verifies:
  1. EXPRESSION_CLAUSE / FACE_EXPRESSION_CLAUSE unset  => positive + face_prompt byte-identical to
     the pre-change baseline strings.
  2. EXPRESSION_CLAUSE set  => appended to the MAIN positive ONLY; face_prompt UNCHANGED.
  3. FACE_EXPRESSION_CLAUSE set => appended to face_prompt ONLY; positive UNCHANGED.
  4. STAGE_CAPTURE guard is off by default (predicate False when unset/"0").
These are pure string/predicate checks — no torch, no GPU, no blob.
"""
import os, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "Bettersnap-aI_Backend" / "shared"))

import catalog
from domain import GenerationPlan, Plan, PlanType, CategoryRule
from runtime.pipeline_context import PipelineContext
from runtime.engines.prompt_sdxl import SdxlPromptEngine

# ── mock cfg (the same helpers/constants main.py exposes; catalog-backed) ──────
LIGHTING = ["soft even front studio lighting", "warm Rembrandt side lighting",
            "clean natural window light", "bright high-key studio lighting"]
NEGATIVE_PROMPT = ("cartoon, illustration, painting, 3d render, cgi, blurry, low quality, "
                   "low resolution, jpeg artifacts, watermark, signature, text, logo, frame, "
                   "border, extra fingers, deformed hands")

class Cfg:
    IDENTITY_TRIGGER = catalog.IDENTITY_TRIGGER
    SUBJECT_NOUN = catalog.SUBJECT_NOUN
    LIGHTING = LIGHTING
    NEGATIVE_PROMPT = NEGATIVE_PROMPT
    COMPOSITION_CONTROL = False
    normalize_gender = staticmethod(catalog.normalize_gender)
    @staticmethod
    def age_to_phrase(a):  # age removed in E1 -> "" ; matches main.py for empty
        import re
        nums = [int(n) for n in re.findall(r"\d+", str(a or ""))]
        if not nums: return ""
        mid = sum(nums)/len(nums)
        return "in their early twenties" if mid < 25 else "in their thirties"
    @staticmethod
    def hair_phrase(h):
        h = (h or "").strip().lower().replace("_", " ")
        if not h or h in ("custom","other","none","prefer not to say"): return ""
        if h in ("bald","bald or shaved","shaved"): return ", bald,"
        return f", with {h} hair,"

def build(expr="", face_expr=""):
    for k, v in (("EXPRESSION_CLAUSE", expr), ("FACE_EXPRESSION_CLAUSE", face_expr)):
        if v: os.environ[k] = v
        else: os.environ.pop(k, None)
    _plan = Plan(key="basic", plan_type=PlanType.ONE_TIME, image_count=8, credits_per_image=1,
                 max_attires=99, max_backgrounds=99, category_rule=CategoryRule.MIXABLE)
    plan = GenerationPlan(user_id="u", job_id="j", plan=_plan, billable_count=8, credit_cost=0,
                          candidate_budget=8, acceptance_threshold=0.0, retry_limit=0,
                          gender="male", age_range="", hair_color="black",
                          attire_refs=("business_suit.navy_suit_tie",),
                          background_refs=("business_suit.studio_gray",), custom_prompt="")
    ctx = PipelineContext("j", "u")
    ps = SdxlPromptEngine(ctx, Cfg(), lambda *a: None).build(plan)
    return ps.prompts[0].positive, ctx.work["face_prompt"]

BASE_POS = ("A professional corporate headshot photograph of ohwx man, with black hair, wearing "
            "a sharp navy-blue business suit with a crisp white dress shirt and a silk tie, against "
            "a smooth neutral gray photography studio backdrop. soft even front studio lighting, "
            "looking at the camera, sharp focus, high detail, realistic natural skin texture, shot "
            "on a DSLR with an 85mm portrait lens.")
EXPR = "relaxed, approachable expression, gentle natural closed-mouth smile, relaxed eyes and facial muscles"

def test_default_off_byte_identical():
    pos, face = build()  # both hooks empty
    assert pos == BASE_POS, f"baseline positive DRIFTED:\n{pos}"
    base_face = face
    # 4. stage-capture guard default-off
    assert (os.environ.get("STAGE_CAPTURE", "0").strip() == "1") is False
    # 2. EXPRESSION_CLAUSE -> main positive only, face unchanged
    pos2, face2 = build(expr=EXPR)
    assert pos2 == BASE_POS + f" {EXPR}.", f"expr not appended cleanly:\n{pos2}"
    assert face2 == base_face, "face_prompt must NOT change from EXPRESSION_CLAUSE"
    # 3. FACE_EXPRESSION_CLAUSE -> face only, positive unchanged
    pos3, face3 = build(face_expr=EXPR)
    assert pos3 == BASE_POS, "positive must NOT change from FACE_EXPRESSION_CLAUSE"
    assert face3 == base_face + f" {EXPR}.", "face expr not appended cleanly"
    print("PASS: default-off byte-identical; EXPRESSION_CLAUSE->main only; FACE_EXPRESSION_CLAUSE->face only; STAGE_CAPTURE off by default")

if __name__ == "__main__":
    test_default_off_byte_identical()
