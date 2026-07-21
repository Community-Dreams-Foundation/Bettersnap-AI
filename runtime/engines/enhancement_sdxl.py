"""SdxlEnhancementEngine — the winners-only enhancement chain.

CONTRACT
  Input:        Winner[]  (+ ctx.images, ctx.work[ref_face/realism_prompt/face_prompt])
  Output:       FinalImage[]  (enhanced PIL stored in ctx.images["final://<id>"])
  Side effects: GPU inference (img2img passes). No blob/DB writes, no business rules.

Phase 2: code MOVED VERBATIM from main.py (upscale_image, _realism_pass, _refine_face,
_add_film_grain, add_watermark). The ONLY changes are mechanical: module globals become
`self.*` / `self.cfg.*`, and `write_debug` becomes `self.log`. No prompt, strength, step, CFG,
resolution, ordering, or logic change. Implements domain.EnhancementEngine.

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

    # ── public contract ──────────────────────────────────────────────────────
    def enhance(self, winners: list[Winner]) -> list[FinalImage]:
        """Run the exact per-image chain from main.py — upscale -> realism -> face-refine ->
        grain -> watermark — on each winner (Phase 2 runs on ALL images, as today; winners-only
        selection arrives in Phase 6)."""
        ref_face = self.ctx.work.get("ref_face")
        realism_prompt = self.ctx.work.get("realism_prompt")
        face_prompt = self.ctx.work.get("face_prompt")
        finals: list[FinalImage] = []
        for win in winners:
            key = win.scored.candidate.image_ref.location
            output = self.ctx.images[key]

            # 2K post-process FIRST (Real-ESRGAN x4 -> fit to UPSCALE_TARGET).
            output = self._upscale_image(output)

            # Whole-image realism pass AFTER upscale, then face-refine — same order as main.py.
            _ref = ref_face if (self.ip_adapter_ok and ref_face is not None) else None
            if self.cfg.REALISM_PASS_ENABLE and self.img2img_pipe is not None:
                output = self._realism_pass(output, _ref, realism_prompt)
            if self.cfg.FACE_REFINE_ENABLE and self.img2img_pipe is not None:
                output = self._refine_face(output, _ref, face_prompt)

            output = self._add_film_grain(output)
            output = self._add_watermark(output)

            out_key = f"final://{win.scored.candidate.id}"
            self.ctx.images[out_key] = output
            finals.append(FinalImage(win, output_ref(out_key)))
        return finals

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
    def _realism_pass(self, image, ref_face, prompt):
        if self.img2img_pipe is None or self.cfg.REALISM_PASS_STRENGTH <= 0:
            return image
        try:
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
            if ref_face is not None and self.ip_adapter_ok:
                kwargs["ip_adapter_image"] = ref_face
            try:
                out = self.img2img_pipe(**kwargs).images[0]
            except Exception as e_ip:
                if "ip_adapter_image" in kwargs:
                    self.log(f"realism-pass: img2img+IP failed ({e_ip}); retrying LoRA-only")
                    kwargs.pop("ip_adapter_image")
                    out = self.img2img_pipe(**kwargs).images[0]
                else:
                    raise
            if out.size != image.size:
                from PIL import Image as _Image
                out = out.resize(image.size, _Image.LANCZOS)
            self.log(f"realism-pass OK: {image.size} strength={self.cfg.REALISM_PASS_STRENGTH} "
                     f"ip={'yes' if (ref_face is not None and self.ip_adapter_ok) else 'no'}")
            return out
        except Exception as e:
            self.log(f"realism-pass FAILED (using upscaled image): {e}")
            return image

    # ── moved verbatim from main.py._refine_face ─────────────────────────────
    def _refine_face(self, image, ref_face, face_prompt):
        if self.img2img_pipe is None or self._face_cascade is None:
            return image
        try:
            import cv2, numpy as np
            from PIL import Image as _Image, ImageFilter
            gray = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2GRAY)
            faces = self._face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                                        minSize=(96, 96))
            if len(faces) == 0:
                self.log("face-refine: no face detected, using base image")
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
            if ref_face is not None and self.ip_adapter_ok:
                kwargs["ip_adapter_image"] = ref_face
            try:
                _r = self.img2img_pipe(**kwargs).images[0]
            except Exception as e_ip:
                if "ip_adapter_image" in kwargs:
                    self.log(f"face-refine: img2img+IP failed ({e_ip}); retrying LoRA-only")
                    kwargs.pop("ip_adapter_image")
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
            return out
        except Exception as e:
            self.log(f"face-refine FAILED (using base image): {e}")
            return image

    # ── moved verbatim from main.py._add_film_grain ──────────────────────────
    def _add_film_grain(self, image):
        if self.cfg.FILM_GRAIN_AMOUNT <= 0:
            return image
        try:
            import numpy as _np
            from PIL import Image as _Image
            arr = _np.asarray(image.convert("RGB"), dtype=_np.float32)
            noise = _np.random.normal(0.0, self.cfg.FILM_GRAIN_AMOUNT, arr.shape[:2])[..., None]
            out = _np.clip(arr + noise, 0, 255).astype(_np.uint8)
            return _Image.fromarray(out)
        except Exception as e:
            self.log(f"film-grain FAILED (skipping): {e}")
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
