"""
One-time script to download SDXL + IP-Adapter weights from HuggingFace.
Run this locally, then upload the output to the Azure file share.

Usage:
    pip install huggingface_hub
    python download_models.py

Then upload to Azure file share:
    az storage file upload-batch --source ./downloads/sdxl-base --destination "models/sdxl-base" --account-name bettersnapaistorage
    az storage file upload-batch --source ./downloads/ip-adapter --destination "models/ip-adapter" --account-name bettersnapaistorage
"""

import os
from huggingface_hub import snapshot_download, hf_hub_download

OUT = "./downloads"
os.makedirs(OUT, exist_ok=True)

# ── 1. SDXL Base ──────────────────────────────────────────
print("Downloading SDXL base (stabilityai/stable-diffusion-xl-base-1.0)...")
snapshot_download(
    repo_id="stabilityai/stable-diffusion-xl-base-1.0",
    local_dir=f"{OUT}/sdxl-base",
    ignore_patterns=["*.ckpt", "*.pt"],
)
print("✅ SDXL base done")

# ── 2. IP-Adapter image encoder (CLIP ViT-H) ─────────────
print("Downloading IP-Adapter image encoder...")
snapshot_download(
    repo_id="h94/IP-Adapter",
    local_dir=f"{OUT}/ip-adapter",
    allow_patterns=[
        "models/image_encoder/**",
        "sdxl_models/ip-adapter-plus-face_sdxl.bin",
    ],
)
print("✅ IP-Adapter done")

print("\nAll downloads complete. Files saved to ./downloads/")
print("\nNext: upload to Azure file share:")
print("  az storage file upload-batch --source ./downloads/sdxl-base --destination models/sdxl-base --account-name bettersnapaistorage")
print("  az storage file upload-batch --source ./downloads/ip-adapter --destination models/ip-adapter --account-name bettersnapaistorage")
