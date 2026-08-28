"""SdxlPromptEngine — build the per-image prompts (generation intent).

CONTRACT
  Input:        GenerationPlan  (+ cfg: main-module helpers/constants, catalog, prompt_control)
  Output:       PromptSet  (one Prompt per delivered image: positive, negative, seed, combo)
  Side effects: writes ctx.work["realism_prompt"] / ["face_prompt"] — the per-job enhancement
                prompts derived from the same subj+tail (read later by the EnhancementEngine).
                No GPU, no blob, no DB.

Phase 2: the prompt-assembly + per-image loop MOVED VERBATIM from main.py.run_inference
(~lines 1006-1191 + the realism/face prompt strings). ORDERING is preserved exactly:
  seed    = 1000 + i
  combo   = combos[i % len(combos)]
  menu    lighting = _lighting[(i // len(combos)) % len(_lighting)]
  custom  lighting = LIGHTING[i % len(LIGHTING)]
Only mechanical changes: reads render inputs from the resolved GenerationPlan (Phase-2
amendment) instead of a job_params dict; helpers/constants come via `cfg` (the main module).
Implements domain.PromptEngine.
"""
from __future__ import annotations

import os

from domain import GenerationPlan, Prompt, PromptSet


class SdxlPromptEngine:
    def __init__(self, ctx, cfg, log):
        self.ctx = ctx
        self.cfg = cfg      # main module: normalize_gender, age_to_phrase, hair_phrase,
        self.log = log      #              SUBJECT_NOUN, LIGHTING, IDENTITY_TRIGGER, ...

    def build(self, plan: GenerationPlan) -> PromptSet:
        import catalog
        from prompt_control import apply_composition_control
        cfg = self.cfg

        # ── per-user attributes (from the resolved plan; identical logic to main.py) ──
        gkey = cfg.normalize_gender(plan.gender)
        age_phrase = cfg.age_to_phrase(plan.age_range or "")
        hair_color = (plan.hair_color or "").strip().lower()

        attire_refs = list(plan.attire_refs or [])
        background_refs = list(plan.background_refs or [])
        custom_prompt = (plan.custom_prompt or "").strip()
        image_count = plan.billable_count           # resolved upstream (== the old image_count)

        subject = cfg.SUBJECT_NOUN[gkey]

        is_custom = bool(custom_prompt)
        combos = [] if is_custom else catalog.build_combos_global(attire_refs, background_refs)
        if not is_custom and not combos:
            raise ValueError(
                f"No attire/background combos "
                f"(attire_refs={attire_refs}, background_refs={background_refs})"
            )

        _negative = os.environ.get("NEGATIVE_PROMPT", cfg.NEGATIVE_PROMPT)

        # Expression control (experiments E1/E2). DEFAULT OFF ("" => baseline byte-identical).
        # EXPRESSION_CLAUSE appends to the base txt2img positive ONLY (generation-level, E1).
        # FACE_EXPRESSION_CLAUSE appends to the face-refine face_prompt ONLY (enhancement-level, E2).
        # Isolated on purpose so the two stages are tested one at a time; neither touches the
        # realism prompt or the negative.
        _expr = os.environ.get("EXPRESSION_CLAUSE", "").strip()
        _expr_suffix = f" {_expr}." if _expr else ""
        _face_expr = os.environ.get("FACE_EXPRESSION_CLAUSE", "").strip()

        # Subject clause built ONCE from the user's REAL attributes (no beauty/idealization).
        subj = f"{cfg.IDENTITY_TRIGGER} {subject}" if cfg.IDENTITY_TRIGGER else f"a {subject}"
        if age_phrase:
            subj += f" {age_phrase}"
        subj += cfg.hair_phrase(hair_color)
        # Body build (DEFAULT OFF — byte-identical to baseline until validated on GPU, same
        # discipline as EXPRESSION_CLAUSE). Two sources, in order:
        #   1. plan.body_type (slim|average|athletic|heavy) — PER-PERSON, when we can source a
        #      real build. Renders the actual physique, so a real gym-goer stays muscular and a
        #      slim person stays slim (NOT a global negative-prompt suppressor, which mis-sizes).
        #   2. BODY_BUILD_CLAUSE env — a global neutral nudge (e.g. "with a natural, realistic
        #      body build") for A/B testing the anti-"gym-bulk" effect before making it a default.
        # Neither set => no body clause => prompt unchanged.
        _body = (plan.body_type or "").strip().lower()
        if _body in ("slim", "average", "athletic", "heavy"):
            article = "an" if _body[0] in "aeiou" else "a"
            subj += f", with {article} {_body} build"
        else:
            _body_default = os.environ.get("BODY_BUILD_CLAUSE", "").strip()
            if _body_default:
                subj += f", {_body_default}"
        _tail = ("looking at the camera, sharp focus, high detail, realistic natural "
                 "skin texture, shot on a DSLR with an 85mm portrait lens.")

        # Phase-5 composition control (default off) — applied ONCE, used by all prompts.
        _tail, _negative = apply_composition_control(_tail, _negative, cfg.COMPOSITION_CONTROL)
        # Expose the FINAL (post-composition) negative so the orchestrator records it in the
        # manifest exactly as legacy run_inference did (which passed the post-control _negative).
        self.ctx.work["negative_prompt"] = _negative

        # ── per-image plan — EXACT ordering from main.py ─────────────────────────
        prompts: list[Prompt] = []
        for i in range(image_count):
            seed = 1000 + i
            if is_custom:
                combo_label = "custom_scene"
                lead = catalog.lead_phrase("custom_scene")
                lighting = cfg.LIGHTING[i % len(cfg.LIGHTING)]
                positive = f"{lead} {subj} {custom_prompt}. {lighting}, {_tail}{_expr_suffix}"
            else:
                attire_ref, bg_ref = combos[i % len(combos)]
                attire = catalog.attire_phrase_ref(attire_ref, gkey)
                bg_phrase = catalog.background_phrase_ref(bg_ref)
                lead = catalog.lead_for_background_ref(bg_ref)
                _lighting = catalog.lighting_for_background_ref(bg_ref, cfg.LIGHTING)
                lighting = _lighting[(i // len(combos)) % len(_lighting)]
                combo_label = f"{bg_ref} | {attire_ref}"
                positive = f"{lead} {subj} wearing {attire}, {bg_phrase}. {lighting}, {_tail}{_expr_suffix}"
            prompts.append(Prompt(positive=positive, negative=_negative, seed=seed,
                                  combo_label=combo_label, slot_id=i))

        # Enhancement prompts (per-job, from the SAME subj+tail) — stash for EnhancementEngine.
        self.ctx.work["realism_prompt"] = (
            f"candid photograph of {subj}, natural realistic skin texture with visible "
            f"pores, individual hair strands, fine fabric texture, {_tail}"
        )
        self.ctx.work["face_prompt"] = (
            f"close-up portrait photograph of {subj}, face in sharp focus, highly "
            f"detailed eyes, natural realistic skin texture with visible pores and fine "
            f"detail, individual hair strands, {_tail}"
            + (f" {_face_expr}." if _face_expr else "")
        )
        return PromptSet(tuple(prompts))
