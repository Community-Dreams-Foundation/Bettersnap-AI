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
import time
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
        # Upload each image with retry (see _upload_bytes_with_retry). If an image STILL
        # fails after retries, DO NOT abort the whole batch: that raises out of run_inference
        # and marks the job 'failed' + refunds it — discarding a ~40-min run whose other
        # images ARE delivered, and stranding those uploaded blobs (the terminal-state guard
        # then blocks any re-run). Instead we collect the failures and complete with what
        # landed. Only a delivery that produced ZERO images is a genuine total failure and
        # is raised (so the caller correctly fails + refunds).
        blob_paths: list[str] = []
        delivered_finals: list[FinalImage] = []
        failed_indices: list[int] = []
        for i, fin in enumerate(finals):
            img = self.ctx.images[fin.output_ref.location]
            try:
                blob_paths.append(self._upload_image_to_blob(img, job.job_id, i))
                delivered_finals.append(fin)
            except Exception as e:
                failed_indices.append(i)
                self.log(f"DELIVERY: headshot_{i + 1} failed after retries ({e}); "
                         f"continuing with the remaining images")

        if not blob_paths:
            raise RuntimeError(
                f"delivery produced 0 images for job {job.job_id}: all "
                f"{len(finals)} upload(s) failed after retries")
        if failed_indices:
            self.log(f"DELIVERY PARTIAL: {len(blob_paths)}/{len(finals)} delivered; "
                     f"{len(failed_indices)} failed (indices {failed_indices}). "
                     f"Completing with the delivered set.")

        # Record what was actually delivered on the workflow record (output_refs is derived).
        job.final_images = delivered_finals
        # Run manifest — extra assembled by the orchestrator into ctx.work; annotate delivery.
        extra = dict(self.ctx.work.get("manifest_extra", {}))
        extra["delivered_count"] = len(blob_paths)
        extra["failed_count"] = len(failed_indices)
        if failed_indices:
            extra["failed_indices"] = failed_indices
        self._write_run_manifest(job.job_id, job.user_id, extra)
        return blob_paths

    # ── blob upload with retry (WRITE-path mirror of main._read_blob) ────────
    def _upload_bytes_with_retry(self, blob_name: str, data: bytes, *,
                                 retries: int = 4, base_delay: float = 0.5) -> None:
        """Upload bytes to a blob, retrying TRANSIENT failures with exponential backoff.
        Delivery writes previously had NO retry (reads did), so a single transient 500/timeout
        mid-batch discarded a completed ~40-min run. `data` is bytes (not a stream) so it is
        safely re-sent across attempts. Raises the last error after `retries` attempts."""
        last = None
        for attempt in range(retries):
            try:
                self.blob_service.get_blob_client(
                    container=self.cfg.AZURE_BLOB_CONTAINER, blob=blob_name
                ).upload_blob(data, overwrite=True)
                return
            except Exception as e:
                last = e
                if attempt < retries - 1:
                    delay = base_delay * (2 ** attempt)
                    self.log(f"blob upload {blob_name} failed "
                             f"(attempt {attempt + 1}/{retries}: {e}); retrying in {delay:.1f}s")
                    time.sleep(delay)
        raise last

    # ── from main.py.upload_image_to_blob (now retried) ──────────────────────
    def _upload_image_to_blob(self, img, job_id: str, index: int) -> str:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        blob_name = f"results/{job_id}/headshot_{index + 1}.png"
        self._upload_bytes_with_retry(blob_name, buf.getvalue())
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
                # H7: per-stage enhancement tally (applied/failed/skipped) so a degraded batch
                # is distinguishable from a good one; None on legacy/pre-enhancement runs.
                "enhancement": extra.get("enhancement"),
                # M4: face-present check on delivered images (checked / no_face / rate).
                "face_gate": extra.get("face_gate"),
                # C3: how many images actually landed vs failed after upload retries.
                "delivery": {
                    "delivered_count": extra.get("delivered_count"),
                    "failed_count": extra.get("failed_count"),
                    "failed_indices": extra.get("failed_indices"),
                },
            }
            body = json.dumps(manifest, indent=2, default=str).encode()
            self._upload_bytes_with_retry(f"manifests/{job_id}.json", body)
            self.log(f"run manifest written: {self.cfg.AZURE_BLOB_CONTAINER}/manifests/{job_id}.json")
        except Exception as e:
            self.log(f"manifest write FAILED (non-fatal): {e}")
