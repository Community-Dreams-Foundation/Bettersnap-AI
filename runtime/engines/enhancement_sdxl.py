"""SdxlEnhancementEngine — the winners-only enhancement chain.

CONTRACT
  Input:        Winner[]  (+ ctx.images, ctx.work[ref_face/realism_prompt/face_prompt])
  Output:       FinalImage[]  (enhanced PIL stored in ctx.images["final://<id>"])
  Side effects: GPU inference (img2img passes). No blob/DB writes, no business rules.

Phase 2: code MOVED VERBATIM from main.py (upscale_image, _realism_pass, _refine_face,
_add_film_grain, add_watermark). The ONLY changes are mechanical: module globals become
`self.*` / `self.cfg.*`, and `write_debug` becomes `self.log`. No prompt, strength, step, CFG,
resolution, ordering, or logic change. Implements domain.EnhancementEngine.

L3 (post-Phase-2, the ONLY non-mechanical change to the moved code): every stochastic stage is
now SEEDED from the candidate's own generation seed, so the run manifest's `seeds` field can
reproduce delivered pixels. Previously the img2img passes took no `generator` and the grain used
the global `np.random`, which made the delivered image unreproducible even with the seed in hand.
Set ENHANCEMENT_DETERMINISTIC=0 to restore the legacy unseeded behaviour.

Config is NOT extracted in Phase 2 (Rule #1: move, don't improve). `cfg` is injected at
construction — in production it is the `main` module itself, so the engine reads main.py's
live constants (DEFAULT_CFG, DEFAULT_STEPS, UPSCALE_TARGET, FACE_REFINE_STRENGTH, ...) with no
duplication and no circular import.

Runtime deps injected via the PipelineContext:
  models["upscaler"]     Real-ESRGAN RRDBNet (or None)
  models["img2img"]      shared SDXL img2img pipe (or None)
  models["face_cascade"] Haar cascade for face-refine (or None)
  work["ip_adapter_ok"]  bool
  work["ref_face"]       PIL reference face for IP-Adapter, or None
  work["realism_prompt"] / work["face_prompt"]  the per-job prompts (built by the orchestrator)
"""
from __future__ import annotations

import os

from domain import Winner, FinalImage, output_ref


class SdxlEnhancementEngine:
    def __init__(self, ctx, cfg, log):
        self.ctx = ctx
        self.cfg = cfg      # main module (or any object exposing the same constants)
        self.log = log
        self.upscaler = ctx.models.get("upscaler")
        self.img2img_pipe = ctx.models.get("img2img")
        self._face_cascade = ctx.models.get("face_cascade")
        self.ip_adapter_ok = bool(ctx.work.get("ip_adapter_ok", False))
        # Per-stage outcome tally (H7): the enhancement stages swallow exceptions and return
        # the un-enhanced image, so a systematic failure (img2img OOM at 2048, version mismatch)
        # would silently ship raw frames as 'completed' with nothing recorded. We count
        # applied / failed / skipped per stage, surface it in the run manifest, and fail the
        # job when a stage failed on EVERY attempted image (see enhance()).
        self.stats = {}
        # L3: enhancement used to be wholly unseeded, so `seeds` in the manifest could not
        # reproduce a delivered image (undercutting pixel-level debugging and any
        # "regenerate exactly this" claim). Seeded by default; set ENHANCEMENT_DETERMINISTIC=0
        # to restore the legacy behaviour.
        self._deterministic = (os.environ.get("ENHANCEMENT_DETERMINISTIC", "1").strip().lower()
                               not in ("0", "false", "no"))

    def _record(self, stage: str, outcome: str):
        """outcome in {'applied','failed','skipped'}. 'skipped' = the stage was a legit no-op
        (disabled, strength 0, no face detected) and is NOT counted against the fail rate."""
        s = self.stats.setdefault(stage, {"applied": 0, "failed": 0, "skipped": 0})
        s[outcome] = s.get(outcome, 0) + 1

    # ── determinism (L3) ─────────────────────────────────────────────────────
    # Generation seeds are CONSECUTIVE (1000 + i), so a small per-stage offset would collide
    # with the next image's seed and correlate their noise. Use widely separated offsets.
    _STAGE_SEED_OFFSET = {"realism": 10_000_000, "face_refine": 20_000_000, "grain": 30_000_000}

    def _sub_seed(self, seed, stage: str):
        """Per-stage sub-seed derived from the candidate's generation seed. None means "leave
        this stage unseeded" (ENHANCEMENT_DETERMINISTIC=0, or no seed available)."""
        if not self._deterministic or seed is None:
            return None
        return int(seed) + self._STAGE_SEED_OFFSET[stage]

    def _generator(self, sub_seed):
        """Torch RNG for an img2img pass; None = pipeline default (global RNG). A Generator's
        state ADVANCES once used, so callers must build a fresh one per pipe call INCLUDING the
        LoRA-only retry — reusing it there would silently produce a different image."""
        if sub_seed is None:
            return None
        import torch
        try:
            return torch.Generator("cuda").manual_seed(sub_seed)
        except Exception:   # CPU-only test path
            return torch.Generator().manual_seed(sub_seed)

    # ── CUDA memory hygiene (H8) ─────────────────────────────────────────────
    # The upscale (fp32 Real-ESRGAN 1024->4096) and the realism/face-refine img2img passes
    # (SDXL at 2048) are the two largest VRAM spikes and run back-to-back per image with the
    # caching allocator holding freed blocks. Without releasing them between stages, the cached
    # fp32 upscale tensor plus a fresh 2048 img2img allocation can fragment into an OOM — a
    # SIGKILL (exit 137) that can't reach the failed-status handler. These helpers are no-ops
    # when CUDA (or torch) is absent, so the CPU test path is unaffected.
    def _cuda_reclaim(self):
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    def _cuda_reset_peak(self):
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.reset_peak_memory_stats()
        except Exception:
            pass

    def _cuda_log_peak(self):
        """Emit the ENHANCEMENT-stage VRAM peak so a real run captures the realism-at-2048
        headroom the audit wants (generation logs its own peak separately)."""
        try:
            import torch
            if torch.cuda.is_available():
                peak = torch.cuda.max_memory_allocated()
                total = torch.cuda.get_device_properties(0).total_memory
                self.log(f"ENHANCEMENT VRAM PEAK: max_memory_allocated={peak} "
                         f"({peak / 1024**3:.1f} GB) of {total / 1024**3:.1f} GB")
        except Exception:
            pass

    # ── public contract ──────────────────────────────────────────────────────
    def enhance(self, winners: list[Winner]) -> list[FinalImage]:
        """Run the exact per-image chain from main.py — upscale -> realism -> face-refine ->
        grain -> watermark — on each winner (Phase 2 runs on ALL images, as today; winners-only
        selection arrives in Phase 6)."""
        ref_face = self.ctx.work.get("ref_face")
        realism_prompt = self.ctx.work.get("realism_prompt")
        face_prompt = self.ctx.work.get("face_prompt")
        finals: list[FinalImage] = []
        self._cuda_reset_peak()   # H8: measure the enhancement-stage VRAM peak across the batch
        for win in winners:
            key = win.scored.candidate.image_ref.location
            output = self.ctx.images[key]
            # L3: the seed this candidate was GENERATED with — also seeds its enhancement, so
            # the manifest's `seeds` reproduces the delivered pixels, not just the base frame.
            seed = getattr(getattr(win.scored.candidate, "prompt", None), "seed", None)

            # 2K post-process FIRST (Real-ESRGAN x4 -> fit to UPSCALE_TARGET).
            output = self._upscale_image(output)
            self._cuda_reclaim()   # H8: free the fp32 4096 upscale tensor BEFORE the 2048 img2img

            # Whole-image realism pass AFTER upscale, then face-refine — same order as main.py.
            _ref = ref_face if (self.ip_adapter_ok and ref_face is not None) else None
            if self.cfg.REALISM_PASS_ENABLE and self.img2img_pipe is not None:
                output = self._realism_pass(output, _ref, realism_prompt, seed)
                self._cuda_reclaim()
            if self.cfg.FACE_REFINE_ENABLE and self.img2img_pipe is not None:
                output = self._refine_face(output, _ref, face_prompt, seed)
                self._cuda_reclaim()

            output = self._add_film_grain(output, seed)
            output = self._add_watermark(output)

            out_key = f"final://{win.scored.candidate.id}"
            self.ctx.images[out_key] = output
            finals.append(FinalImage(win, output_ref(out_key)))

        self._cuda_log_peak()   # H8: emit the enhancement VRAM peak for the headroom trace

        # H7: surface the per-stage tally in the manifest, and fail the job if any enhancement
        # stage failed on EVERY attempted image (a systemic failure that would otherwise ship a
        # wholly un-enhanced batch as 'completed'). Isolated per-image failures ship + are flagged.
        # Threshold is the fail-fraction that trips a hard fail; default 1.0 (100% = all attempts
        # failed). Set ENHANCEMENT_FAIL_THRESHOLD > 1 to always ship + flag instead of failing.
        self.ctx.work["enhancement_stats"] = {**self.stats, "deterministic": self._deterministic}
        threshold = float(os.environ.get("ENHANCEMENT_FAIL_THRESHOLD", "1.0"))
        systemic = []
        for stage, s in self.stats.items():
            attempted = s["applied"] + s["failed"]
            if attempted and s["failed"]:
                rate = s["failed"] / attempted
                self.log(f"enhancement '{stage}': {s['applied']} ok / {s['failed']} FAILED "
                         f"/ {s['skipped']} skipped (fail rate {rate:.0%})")
                if rate >= threshold:
                    systemic.append(f"{stage} {s['failed']}/{attempted} ({rate:.0%})")
        if systemic:
            raise RuntimeError(
                "enhancement systemically failed; refusing to ship un-enhanced images as "
                f"'completed': {', '.join(systemic)}. (Set ENHANCEMENT_FAIL_THRESHOLD>1 to "
                "ship + flag instead of failing.)")
        return finals

    # ── minimum inline quality gate (M4) ─────────────────────────────────────
    def face_present_gate(self, finals: list[FinalImage]) -> dict:
        """Every DELIVERED image must contain a detectable face. Prod shipped ZERO automated
        verification, so a faceless / abstract render could be billed as success. This is the
        audit's "at minimum a face-present check" (the full ArcFace identity gate stays behind
        QUALITY_GATE=1 until its threshold is A100-validated). Uses the Haar cascade already
        loaded for face-refine. Faceless images are flagged in the manifest; the job is FAILED
        only when EVERY delivered image is faceless — Haar has false-negatives on some valid
        (profile / occluded) faces, so failing on a single miss would be too aggressive. The
        trip rate is env-tunable via FACE_GATE_MAX_NOFACE_RATE (default 1.0 = all-faceless)."""
        stats = {"checked": 0, "no_face": 0, "rate": 0.0}
        if self._face_cascade is None or not finals:
            stats["note"] = "cascade-unavailable" if self._face_cascade is None else "no-finals"
            return stats
        import cv2
        import numpy as np
        no_face = 0
        for fin in finals:
            img = self.ctx.images[fin.output_ref.location].convert("RGB")
            gray = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY)
            faces = self._face_cascade.detectMultiScale(gray, scaleFactor=1.1,
                                                        minNeighbors=5, minSize=(96, 96))
            if len(faces) == 0:
                no_face += 1
        checked = len(finals)
        rate = (no_face / checked) if checked else 0.0
        stats = {"checked": checked, "no_face": no_face, "rate": round(rate, 3)}
        if no_face:
            self.log(f"face-present gate: {checked - no_face}/{checked} have a face; "
                     f"{no_face} faceless (rate {rate:.0%})")
        threshold = float(os.environ.get("FACE_GATE_MAX_NOFACE_RATE", "1.0"))
        if checked and rate >= threshold:
            raise RuntimeError(
                f"face-present gate: {no_face}/{checked} delivered images are faceless "
                f"(>= {threshold:.0%} threshold); refusing to ship a faceless batch as 'completed'.")
        return stats

    # ── moved verbatim from main.py.upscale_image ────────────────────────────
    def _upscale_image(self, img):
        import torch
        import numpy as np
        from PIL import Image
        if self.upscaler is None or self.cfg.UPSCALE_TARGET <= 0:
            return img
        dtype = torch.float16 if self.cfg.UPSCALE_HALF else torch.float32
        arr = np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0
        ten = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0).to("cuda", dtype=dtype)
        with torch.no_grad():
            out = self.upscaler(ten).clamp(0, 1)
        out = (out.squeeze(0).permute(1, 2, 0).float().cpu().numpy() * 255.0)
        up = Image.fromarray(out.round().astype(np.uint8))
        w, h = up.size
        if max(w, h) != self.cfg.UPSCALE_TARGET:
            ratio = self.cfg.UPSCALE_TARGET / max(w, h)
            up = up.resize((round(w * ratio), round(h * ratio)), Image.LANCZOS)
        return up

    # ── moved verbatim from main.py._realism_pass ────────────────────────────
    def _realism_pass(self, image, ref_face, prompt, seed=None):
        if self.img2img_pipe is None or self.cfg.REALISM_PASS_STRENGTH <= 0:
            self._record("realism", "skipped")
            return image
        try:
            _sub = self._sub_seed(seed, "realism")   # L3: seed the pass for reproducibility
            kwargs = dict(
                prompt=prompt,
                negative_prompt=(os.environ.get("NEGATIVE_PROMPT", self.cfg.NEGATIVE_PROMPT)
                                 + ", plastic skin, airbrushed, smooth waxy skin, cgi, 3d render, "
                                   "digital painting, illustration, overprocessed, blurry hair, "
                                   "smooth hair"),
                image=image,
                strength=self.cfg.REALISM_PASS_STRENGTH,
                num_inference_steps=self.cfg.DEFAULT_STEPS,
                guidance_scale=self.cfg.DEFAULT_CFG,
            )
            kwargs["generator"] = self._generator(_sub)   # L3: None unless deterministic
            if ref_face is not None and self.ip_adapter_ok:
                kwargs["ip_adapter_image"] = ref_face
            try:
                out = self.img2img_pipe(**kwargs).images[0]
            except Exception as e_ip:
                if "ip_adapter_image" in kwargs:
                    self.log(f"realism-pass: img2img+IP failed ({e_ip}); retrying LoRA-only")
                    kwargs.pop("ip_adapter_image")
                    kwargs["generator"] = self._generator(_sub)   # fresh: the first was consumed
                    out = self.img2img_pipe(**kwargs).images[0]
                else:
                    raise
            if out.size != image.size:
                from PIL import Image as _Image
                out = out.resize(image.size, _Image.LANCZOS)
            self.log(f"realism-pass OK: {image.size} strength={self.cfg.REALISM_PASS_STRENGTH} "
                     f"ip={'yes' if (ref_face is not None and self.ip_adapter_ok) else 'no'}")
            self._record("realism", "applied")
            return out
        except Exception as e:
            self.log(f"realism-pass FAILED (using upscaled image): {e}")
            self._record("realism", "failed")
            return image

    # ── moved verbatim from main.py._refine_face ─────────────────────────────
    def _refine_face(self, image, ref_face, face_prompt, seed=None):
        if self.img2img_pipe is None or self._face_cascade is None:
            self._record("face_refine", "skipped")
            return image
        try:
            _sub = self._sub_seed(seed, "face_refine")   # L3: seed the pass for reproducibility
            import cv2, numpy as np
            from PIL import Image as _Image, ImageFilter
            gray = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2GRAY)
            faces = self._face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                                        minSize=(96, 96))
            if len(faces) == 0:
                self.log("face-refine: no face detected, using base image")
                self._record("face_refine", "skipped")
                return image
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
            pad_x   = int(0.40 * w)
            pad_top = int(0.85 * h)
            pad_bot = int(0.45 * h)
            x0 = max(0, x - pad_x)
            y0 = max(0, y - pad_top)
            x1 = min(image.width,  x + w + pad_x)
            y1 = min(image.height, y + h + pad_bot)
            crop = image.crop((x0, y0, x1, y1))
            cw, ch = crop.size
            crop_1024 = crop.resize((1024, 1024), _Image.LANCZOS)
            kwargs = dict(
                prompt=face_prompt,
                negative_prompt=(os.environ.get("NEGATIVE_PROMPT", self.cfg.NEGATIVE_PROMPT)
                                 + ", plastic skin, airbrushed, smooth waxy skin, cgi, 3d render, "
                                   "doll, overprocessed, blurry hair"),
                image=crop_1024,
                strength=self.cfg.FACE_REFINE_STRENGTH,
                num_inference_steps=self.cfg.DEFAULT_STEPS,
                guidance_scale=self.cfg.DEFAULT_CFG,
            )
            kwargs["generator"] = self._generator(_sub)   # L3: None unless deterministic
            if ref_face is not None and self.ip_adapter_ok:
                kwargs["ip_adapter_image"] = ref_face
            try:
                _r = self.img2img_pipe(**kwargs).images[0]
            except Exception as e_ip:
                if "ip_adapter_image" in kwargs:
                    self.log(f"face-refine: img2img+IP failed ({e_ip}); retrying LoRA-only")
                    kwargs.pop("ip_adapter_image")
                    kwargs["generator"] = self._generator(_sub)   # fresh: the first was consumed
                    _r = self.img2img_pipe(**kwargs).images[0]
                else:
                    raise
            refined = _r.resize((cw, ch), _Image.LANCZOS)
            b = max(1, int(0.12 * min(cw, ch)))
            m = np.zeros((ch, cw), dtype=np.uint8)
            m[b:ch - b, b:cw - b] = 255
            mask = _Image.fromarray(m).filter(ImageFilter.GaussianBlur(max(1, b // 2)))
            out = image.copy()
            out.paste(refined, (x0, y0), mask)
            self.log(f"face-refine OK: crop ({x0},{y0})-({x1},{y1}) "
                     f"strength={self.cfg.FACE_REFINE_STRENGTH} ip={'yes' if (ref_face is not None and self.ip_adapter_ok) else 'no'}")
            self._record("face_refine", "applied")
            return out
        except Exception as e:
            self.log(f"face-refine FAILED (using base image): {e}")
            self._record("face_refine", "failed")
            return image

    # ── moved verbatim from main.py._add_film_grain ──────────────────────────
    def _add_film_grain(self, image, seed=None):
        if self.cfg.FILM_GRAIN_AMOUNT <= 0:
            self._record("grain", "skipped")
            return image
        try:
            import numpy as _np
            from PIL import Image as _Image
            arr = _np.asarray(image.convert("RGB"), dtype=_np.float32)
            # L3: seed the grain RNG so the delivered pixels reproduce from the manifest seed.
            # _sub is None (-> global np.random, legacy) when ENHANCEMENT_DETERMINISTIC=0.
            _sub = self._sub_seed(seed, "grain")
            _rng = _np.random.default_rng(_sub) if _sub is not None else _np.random
            noise = _rng.normal(0.0, self.cfg.FILM_GRAIN_AMOUNT, arr.shape[:2])[..., None]
            out = _np.clip(arr + noise, 0, 255).astype(_np.uint8)
            self._record("grain", "applied")
            return _Image.fromarray(out)
        except Exception as e:
            self.log(f"film-grain FAILED (skipping): {e}")
            self._record("grain", "failed")
            return image

    # ── moved verbatim from main.py.add_watermark ────────────────────────────
    def _add_watermark(self, img):
        from PIL import Image, ImageDraw, ImageFont
        img        = img.convert("RGBA")
        w, h       = img.size
        bar_height = int(h * 0.07)
        overlay    = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw       = ImageDraw.Draw(overlay)
        draw.rectangle([(0, h - bar_height), (w, h)], fill=(0, 0, 0, 160))
        text      = "BetterSnap AI"
        font_size = int(bar_height * 0.55)
        font_paths = [
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]
        font = None
        for path in font_paths:
            if os.path.exists(path):
                font = ImageFont.truetype(path, font_size)
                break
        if font is None:
            font = ImageFont.load_default()
        bbox   = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        x      = (w - text_w) // 2
        y      = h - bar_height + (bar_height - text_h) // 2
        draw.text((x + 2, y + 2), text, font=font, fill=(0, 0, 0, 180))
        draw.text((x, y),         text, font=font, fill=(255, 255, 255, 230))
        return Image.alpha_composite(img, overlay).convert("RGB")
