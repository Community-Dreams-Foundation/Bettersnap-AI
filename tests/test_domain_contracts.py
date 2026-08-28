"""Phase 1 contract tests — ARCHITECTURE.md §3-§4.

Verifies the domain model is coherent AND torch-free (importable from the Functions control
plane). Plain asserts so it runs with or without pytest:  `python tests/test_domain_contracts.py`
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_domain_is_torch_free():
    """The whole point of domain/ vs runtime/: domain must not drag in torch/PIL/numpy, or the
    CPU Functions app can't import it (ARCHITECTURE.md §2.2)."""
    import domain  # noqa: F401
    for heavy in ("torch", "PIL", "numpy", "cv2"):
        assert heavy not in sys.modules, f"domain import pulled in {heavy!r} — breaks §2.2"


def test_plan_and_quality_are_decoupled():
    from domain import Plan, PlanType, CategoryRule, QUALITY_PROFILES, quality_profile_for
    basic = Plan("basic", PlanType.ONE_TIME, image_count=30, credits_per_image=1,
                 max_attires=2, max_backgrounds=2, category_rule=CategoryRule.SINGLE_TYPE)
    # Plan carries NO candidate budget / quality knobs.
    assert not hasattr(basic, "candidate_budget")
    assert not hasattr(basic, "acceptance_threshold")
    assert basic.credit_cost() == 30                       # 30 delivered x 1 credit
    # candidate_budget is DERIVED from the QualityProfile, never stored on the plan (§3.3).
    qp = quality_profile_for(basic)
    assert qp is QUALITY_PROFILES["standard"]
    assert qp.candidate_budget(30) == 45                   # ceil(30 * 1.5)
    assert QUALITY_PROFILES["premium"].candidate_budget(30) == 60   # ceil(30 * 2.0)


def test_billing_is_delivered_not_candidates():
    """Pricing invariant (§5): credit_cost tracks delivered image_count, candidate_budget does
    NOT enter the bill."""
    from domain import Plan, PlanType, CategoryRule, GenerationPlan, quality_profile_for
    expert = Plan("expert", PlanType.ONE_TIME, 70, 1, 5, 5, CategoryRule.MIXABLE)
    qp = quality_profile_for(expert)
    gp = GenerationPlan(
        user_id="u", job_id="j", plan=expert,
        billable_count=expert.image_count,
        credit_cost=expert.credit_cost(),
        candidate_budget=qp.candidate_budget(expert.image_count),
        acceptance_threshold=qp.acceptance_threshold,
        retry_limit=qp.retry_limit,
    )
    assert gp.billable_count == 70 and gp.credit_cost == 70   # customer pays for 70
    assert gp.candidate_budget == 105                         # may generate up to 105 (COGS)
    assert gp.candidate_budget > gp.billable_count            # over-generate, never over-bill


def test_three_stage_object_graph():
    """Request -> Plan -> Job, plus the candidate artifact chain, all compose (§3.1)."""
    from domain import (GenerationRequest, Plan, PlanType, CategoryRule, GenerationPlan,
                        GenerationJob, JobStatus, OutputSlot, Prompt, PromptSet, Candidate,
                        Scores, ScoredCandidate, Winner, FinalImage, IdentityProfile,
                        IdentityStatus, crop_ref, adapter_ref, output_ref)
    req = GenerationRequest("u", gender="female", attire_refs=("business_suit.navy_suit_tie",),
                            background_refs=("business_suit.studio_gray",))
    plan = Plan("basic", PlanType.ONE_TIME, 30, 1, 2, 2, CategoryRule.SINGLE_TYPE)
    gp = GenerationPlan("u", "j", plan, 30, 30, 45, 0.82, 1,
                        slots=(OutputSlot(0, "business_suit.navy_suit_tie", "business_suit.studio_gray"),))
    prof = IdentityProfile("u", reference_crops=[crop_ref("inputs/u/input/crop_upperbody/img0.jpg")],
                           identity_centroid=(0.1, 0.2, 0.3), adapter=adapter_ref("lora-weights/identity/u/adapter_model.safetensors"),
                           status=IdentityStatus.READY)
    p = Prompt("a photo of ohwx woman ...", "cartoon, ...", seed=1000, combo_label="c", slot_id=0)
    ps = PromptSet((p,))
    cand = Candidate("c0", p, crop_ref("ctx://c0"), slot_id=0)
    sc = ScoredCandidate(cand, Scores(identity=0.88, overall=0.85), accepted=True)
    win = Winner(sc, slot_id=0)
    fin = FinalImage(win, output_ref("outputs/j/0.png"))
    job = GenerationJob("j", "u", status=JobStatus.PROCESSING, final_images=[fin],
                        candidates_generated=42)
    assert ps.prompts[0].seed == 1000
    assert job.final_images[0].winner.scored.scores.identity == 0.88
    assert job.candidates_generated == 42 and req.user_id == prof.user_id == gp.user_id
    # output_refs is DERIVED from final_images (single source of truth), not a stored field.
    assert job.output_refs == [fin.output_ref]
    assert "output_refs" not in {f.name for f in job.__dataclass_fields__.values()}


def test_engine_protocols_are_structural():
    """A class with the right method shape satisfies the Protocol (runtime_checkable) — this is
    how runtime/ engines will plug in during Phase 2 without importing the contract's base."""
    from domain import GenerationEngine, PromptEngine

    class DummyGen:
        def generate(self, prompts, adapter):
            return []

    class NotAnEngine:
        pass

    assert isinstance(DummyGen(), GenerationEngine)
    assert not isinstance(NotAnEngine(), GenerationEngine)
    assert not isinstance(DummyGen(), PromptEngine)   # wrong method shape


def test_runtime_context_usable():
    from runtime import PipelineContext
    ctx = PipelineContext("j", "u")
    ctx.put_image("ctx://c0", object())
    assert ctx.has_image("ctx://c0") and ctx.get_image("ctx://c0") is not None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:
            failed += 1
            import traceback
            print(f"FAIL  {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
