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

        # Subject clause built ONCE from the user's REAL attributes (no beauty/idealization).
        subj = f"{cfg.IDENTITY_TRIGGER} {subject}" if cfg.IDENTITY_TRIGGER else f"a {subject}"
        if age_phrase:
            subj += f" {age_phrase}"
        subj += cfg.hair_phrase(hair_color)
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
                positive = f"{lead} {subj} {custom_prompt}. {lighting}, {_tail}"
            else:
                attire_ref, bg_ref = combos[i % len(combos)]
                attire = catalog.attire_phrase_ref(attire_ref, gkey)
                bg_phrase = catalog.background_phrase_ref(bg_ref)
                lead = catalog.lead_for_background_ref(bg_ref)
                _lighting = catalog.lighting_for_background_ref(bg_ref, cfg.LIGHTING)
                lighting = _lighting[(i // len(combos)) % len(_lighting)]
                combo_label = f"{bg_ref} | {attire_ref}"
                positive = f"{lead} {subj} wearing {attire}, {bg_phrase}. {lighting}, {_tail}"
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
        )
        return PromptSet(tuple(prompts))
