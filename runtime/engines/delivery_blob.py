"""BlobDeliveryEngine — upload final images + write the run manifest.

CONTRACT
  Input:        FinalImage[], GenerationJob  (+ ctx.images, ctx.work["manifest_extra"])
  Output:       list[str] of delivered blob paths; mutates the GenerationJob (final_images)
  Side effects: BLOB UPLOADS (results/<job>/headshot_N.png) + MANIFEST WRITE (manifests/<job>.json)

Phase 2: code MOVED VERBATIM from main.py (upload_image_to_blob, write_run_manifest,
_sha256_file). Only mechanical changes: module globals -> self.* / self.cfg.*, write_debug ->
self.log. No path, format, or manifest-field change. Implements domain.DeliveryEngine.

Injected at construction:
  cfg           the main module (config constants: DEFAULT_LORA_WEIGHT, IDENTITY_TRIGGER, ...)
  blob_service  Azure BlobServiceClient (same client main.py used)
  stage_status  the STAGE_STATUS dict populated by the model loader (for the manifest)
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from datetime import datetime, timezone

from domain import FinalImage, GenerationJob, output_ref


class BlobDeliveryEngine:
    def __init__(self, ctx, cfg, blob_service, stage_status, log):
        self.ctx = ctx
        self.cfg = cfg
        self.blob_service = blob_service
        self.stage_status = stage_status
        self.log = log

    # ── public contract ──────────────────────────────────────────────────────
    def deliver(self, finals: list[FinalImage], job: GenerationJob) -> list[str]:
        blob_paths: list[str] = []
        for i, fin in enumerate(finals):
            img = self.ctx.images[fin.output_ref.location]
            blob_paths.append(self._upload_image_to_blob(img, job.job_id, i))
        # Record what was actually delivered on the workflow record (output_refs is derived).
        job.final_images = list(finals)
        # Run manifest — extra assembled by the orchestrator into ctx.work.
        self._write_run_manifest(job.job_id, job.user_id, self.ctx.work.get("manifest_extra", {}))
        return blob_paths

    # ── moved verbatim from main.py.upload_image_to_blob ─────────────────────
    def _upload_image_to_blob(self, img, job_id: str, index: int) -> str:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        blob_name   = f"results/{job_id}/headshot_{index + 1}.png"
        blob_client = self.blob_service.get_blob_client(
            container=os.environ.get("AZURE_BLOB_CONTAINER", "outputs"), blob=blob_name)
        blob_client.upload_blob(buf, overwrite=True)
        self.log(f"Uploaded: {blob_name}")
        return blob_name

    # ── moved verbatim from main.py._sha256_file ─────────────────────────────
    def _sha256_file(self, path):
        try:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            return h.hexdigest()
        except Exception:
            return None

    # ── moved verbatim from main.py.write_run_manifest ───────────────────────
    def _write_run_manifest(self, job_id, user_id, extra):
        try:
            try:
                import torch
                gpu = torch.cuda.get_device_properties(0).name
            except Exception:
                gpu = "unknown"
            manifest = {
                "schema": "bettersnap.run_manifest/v1",
                "job_id": job_id,
                "user_id_sha256": hashlib.sha256((user_id or "").encode()).hexdigest(),
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "mode": os.environ.get("MODE", "infer"),
                "container_image": os.environ.get("CONTAINER_IMAGE", "unset"),
                "container_image_digest": os.environ.get("CONTAINER_IMAGE_DIGEST", "unset"),
                "git_commit_sha": os.environ.get("GIT_COMMIT_SHA", "unset"),
                "lockfile_sha256": os.environ.get("LOCKFILE_SHA256", "unset"),
                "base_model": os.environ.get("BASE_MODEL", "/models/sdxl-base"),
                "vae_model": os.environ.get("VAE_MODEL", "/models/sdxl-vae"),
                "lora_adapter_sha256": self._sha256_file(f"/tmp/lora_identity_{user_id}.safetensors"),
                "lora_identity_weight": self.cfg.DEFAULT_LORA_WEIGHT,
                "identity_trigger": self.cfg.IDENTITY_TRIGGER,
                "ip_adapter": {"model": "ip-adapter-plus-face_sdxl_vit-h.safetensors",
                               **extra.get("ref_meta", {})},
                "scheduler": extra.get("scheduler", "unknown"),
                "guidance_scale": self.cfg.DEFAULT_CFG,
                "num_inference_steps": self.cfg.DEFAULT_STEPS,
                "gen_resolution": [self.cfg.DEFAULT_WIDTH, self.cfg.DEFAULT_HEIGHT],
                "delivered_resolution": extra.get("delivered_size"),
                "negative_prompt": extra.get("negative_prompt"),
                "seeds": extra.get("seeds", []),
                "image_count": extra.get("image_count"),
                "stages": dict(self.stage_status),
                "gpu": gpu,
                "generation_seconds": extra.get("generation_seconds"),
            }
            body = json.dumps(manifest, indent=2, default=str).encode()
            self.blob_service.get_blob_client(
                container="outputs", blob=f"manifests/{job_id}.json").upload_blob(
                body, overwrite=True)
            self.log(f"run manifest written: outputs/manifests/{job_id}.json")
        except Exception as e:
            self.log(f"manifest write FAILED (non-fatal): {e}")
