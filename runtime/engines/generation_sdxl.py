"""SdxlGenerationEngine — base txt2img candidate generation.

CONTRACT
  Input:        PromptSet, adapter Ref  (+ ctx.models["pipe"], ctx.work[use_ip_adapter/ip_adapter_image])
  Output:       Candidate[]  (base-resolution PIL stored in ctx.images["gen://<id>"])
  Side effects: GPU inference. No blob/DB writes, no business rules.

Phase 2: the per-image `pipe(**kwargs)` call MOVED VERBATIM from main.py.run_inference. Same
kwargs, same explicit `torch.Generator("cuda").manual_seed(seed)` (this is the DETERMINISTIC
part of the pipeline), same IP-Adapter image path. Implements domain.GenerationEngine.

The identity LoRA + IP-Adapter scale are loaded/activated by the ORCHESTRATOR before this runs
(model setup); the `adapter` Ref is received for the contract but the pipe is already configured.

Injected at construction:
  cfg   the main module (DEFAULT_CFG, DEFAULT_STEPS, DEFAULT_WIDTH, DEFAULT_HEIGHT)
"""
from __future__ import annotations

from domain import PromptSet, Candidate, Ref, RefKind


class SdxlGenerationEngine:
    def __init__(self, ctx, cfg, log):
        self.ctx = ctx
        self.cfg = cfg
        self.log = log
        self.pipe = ctx.models["pipe"]

    def generate(self, prompts: PromptSet, adapter: Ref) -> list[Candidate]:
        import torch
        use_ip_adapter = bool(self.ctx.work.get("use_ip_adapter", False))
        ip_adapter_image = self.ctx.work.get("ip_adapter_image")
        candidates: list[Candidate] = []
        for i, p in enumerate(prompts.prompts):
            _pipe_kwargs = dict(
                prompt=p.positive,
                negative_prompt=p.negative,
                guidance_scale=self.cfg.DEFAULT_CFG,
                num_inference_steps=self.cfg.DEFAULT_STEPS,
                width=self.cfg.DEFAULT_WIDTH,
                height=self.cfg.DEFAULT_HEIGHT,
                generator=torch.Generator("cuda").manual_seed(p.seed),
            )
            if use_ip_adapter:
                _pipe_kwargs["ip_adapter_image"] = ip_adapter_image
            output = self.pipe(**_pipe_kwargs).images[0]

            # Real inference VRAM peak on the first output — verbatim metric side-effect.
            if i == 0:
                try:
                    total = torch.cuda.get_device_properties(0).total_memory
                    peak  = torch.cuda.max_memory_allocated(0)
                    self.log(f"INFERENCE VRAM PEAK: max_memory_allocated={peak} "
                             f"({peak / 1024**3:.1f} GB) of total_memory={total} "
                             f"({total / 1024**3:.1f} GB)")
                except Exception as e:
                    self.log(f"inference VRAM probe failed: {e}")

            cid = f"cand{i}"
            key = f"gen://{cid}"
            self.ctx.images[key] = output
            candidates.append(Candidate(cid, p, Ref(RefKind.CANDIDATE, key), slot_id=p.slot_id))
        return candidates
