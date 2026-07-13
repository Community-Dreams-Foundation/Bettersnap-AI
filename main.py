import os
import io
import json
import time
import logging
import traceback
import faulthandler
import requests

faulthandler.enable()   # dumps C++ stack to stderr on SIGSEGV / SIGABRT / SIGFPE / SIGBUS
from datetime import datetime, timedelta, timezone
from PIL import Image, ImageDraw, ImageFont

import torch
import pyodbc
# Canonical category/attire/background catalog. The Dockerfile COPYs
# Bettersnap-aI_Backend/shared/catalog.py to /app/catalog.py so this import
# resolves in the container; it is the SAME file the Functions app imports as
# shared.catalog, so prompt phrases + option ids never drift between the two.
import catalog
# ── PHASE 1: per-user LoRA GENERATION (txt2img) ──────────────────────────────
# The product is an Aragon/BetterPic-style headshot generator: the per-user
# identity LoRA (trained on their 8-12 uploads) carries identity, and we GENERATE
# fresh professional headshots across a fixed background/attire menu. Uploaded
# photos are training-only — never fed at inference. This is why the pipeline is
# txt2img, NOT img2img (img2img re-painted one source photo = "same picture" bug).
#
# The depth-ControlNet body-structure stack (xinsir depth ControlNet + MiDaS,
# both Apache-2.0) is still BAKED in the image but NOT loaded here — it is a
# Phase 3 fallback for build preservation, off by default because it constrains
# pose and reduces the scene variety the product needs. commercial-safe only —
# do NOT swap to Depth-Anything-V2-Large or CMU OpenPose (both non-commercial).
from diffusers import StableDiffusionXLPipeline, AutoencoderKL
import numpy as np
# Real-ESRGAN generator (vendored, BSD-3-Clause — COMMERCIAL-SAFE). 2K upscale
# post-process. do NOT swap to a non-commercial upscaler.
from rrdbnet import RRDBNet
from azure.keyvault.secrets import SecretClient

from azure.storage.queue import QueueClient, TextBase64DecodePolicy
from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions
from azure.identity import DefaultAzureCredential
from azure.communication.email import EmailClient

# ── Logging ───────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Environment Variables ─────────────────────────────────
AZURE_STORAGE_ACCOUNT   = os.environ.get("AZURE_STORAGE_ACCOUNT", "bettersnapaistorage")
AZURE_QUEUE_NAME        = os.environ.get("AZURE_QUEUE_NAME", "inference-jobs")
AZURE_BLOB_CONTAINER    = os.environ.get("AZURE_BLOB_CONTAINER", "outputs")
AZURE_LORA_CONTAINER    = os.environ.get("AZURE_LORA_CONTAINER", "lora-weights")
AZURE_STORAGE_KEY       = os.environ.get("AZURE_STORAGE_KEY")
SQL_SERVER              = os.environ.get("SQL_SERVER", "bettersnap-srv.database.windows.net")
SQL_DATABASE            = os.environ.get("SQL_DATABASE", "bettersnap-db")
KEY_VAULT_URL           = "https://bettersnapkeyvault.vault.azure.net/"

# ── Azure Clients ─────────────────────────────────────────
credential = DefaultAzureCredential()

blob_service = BlobServiceClient(
    account_url=f"https://{AZURE_STORAGE_ACCOUNT}.blob.core.windows.net",
    credential=credential
)
queue_client = QueueClient(
    account_url=f"https://{AZURE_STORAGE_ACCOUNT}.queue.core.windows.net",
    queue_name=AZURE_QUEUE_NAME,
    credential=credential,
    # Messages are enqueued base64-encoded (to match the Functions queue
    # extension default); decode them symmetrically on receive.
    message_decode_policy=TextBase64DecodePolicy(),
)

# ── Key Vault helper ──────────────────────────────────────
def get_secret(name: str) -> str:
    kv_client = SecretClient(vault_url=KEY_VAULT_URL, credential=credential)
    return kv_client.get_secret(name).value

# ── Debug logger to blob ──────────────────────────────────
def write_debug(msg: str):
    try:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        line = f"[{timestamp}] {msg}\n"
        # Per-job log blob. The old code appended EVERY run to one shared
        # outputs/debug/log.txt with overwrite=True, so two concurrent A100 runs
        # clobbered each other's logs (read-modify-write race) and the blob grew
        # unbounded. Namespacing by JOB_ID isolates each run's log.
        job_id = os.environ.get("JOB_ID", "unknown")
        blob_name = f"debug/{job_id}.txt"
        blob_client = blob_service.get_blob_client(container="outputs", blob=blob_name)
        try:
            existing = blob_client.download_blob().readall().decode()
        except:
            existing = ""
        blob_client.upload_blob(existing + line, overwrite=True)
    except Exception as e:
        log.error(f"write_debug failed: {e}")

# ── SQL Connection ────────────────────────────────────────
def get_db_connection(max_attempts: int = 5, base_delay: float = 3.0):
    conn_str = (
        f"DRIVER={{ODBC Driver 18 for SQL Server}};"
        f"SERVER={SQL_SERVER},1433;"
        f"DATABASE={SQL_DATABASE};"
        "Authentication=ActiveDirectoryMsi;"
        "Encrypt=yes;"
        "TrustServerCertificate=no;"
        "Connection Timeout=60;"
    )
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            return pyodbc.connect(conn_str)
        except Exception as e:
            last_err = e
            log.warning(f"DB connect attempt {attempt}/{max_attempts} failed: {e}")
            if attempt < max_attempts:
                time.sleep(base_delay * attempt)   # linear backoff: 3s, 6s, 9s, 12s
    raise last_err

# ── Global pipeline (SDXL txt2img) + Real-ESRGAN upscaler ─
pipe     = None
upscaler = None   # Real-ESRGAN x4 (RRDBNet, BSD-3-Clause) — 2K post-process

# ─────────────────────────────────────────────────────────
# PRODUCT-LEVEL MENU + PROMPT CONFIG (general — identical for every user)
# NOTHING here is per-user or per-demographic. `gender` selects which attire set
# is used; the user's identity (face, skin tone, features, build) comes ENTIRELY
# from their trained LoRA, never from anything hardcoded below.
# ─────────────────────────────────────────────────────────

# Phase-1 STARTER background menu (3). The full canonical 13-category menu
# (6 professional + 7 personal) lands after Phase 1 proves txt2img works.
BACKGROUND_MENU = [
    ("studio_white", "against a clean, pure white photography studio backdrop"),
    ("studio_gray",  "against a smooth neutral gray photography studio backdrop"),
    ("modern_office","in a modern corporate office with softly blurred glass walls behind"),
]

# Phase-1 STARTER attire menu, keyed by gender (2 each). 'neutral' is the
# fallback when gender is absent/unknown — we NEVER assume male or female.
ATTIRE_MENU = {
    "female":  [
        "a tailored navy blazer over a white blouse",
        "a charcoal grey business suit",
    ],
    "male":    [
        "a navy blue business suit with a white shirt and tie",
        "a charcoal grey business suit",
    ],
    "neutral": [
        "professional business attire",
        "a charcoal grey business suit",
    ],
}

# Subject noun driven by gender — fixes the "gender is a dead read" bug (it was
# read then never used, and defaulted to male). No default is assumed here.
# Sourced from catalog — the SAME module the Functions app uses to build the trainer's
# INSTANCE_PROMPT. The class word baked into the prompt here ("ohwx woman") must be the
# one the adapter was trained against, or the trigger token fires against a word the
# LoRA never saw. Defining it twice is how that drifts, so it is defined once.
SUBJECT_NOUN = catalog.SUBJECT_NOUN

# Lighting styles rotated across outputs for extra variety (product-level, general).
LIGHTING = [
    "soft even front studio lighting",
    "warm Rembrandt side lighting",
    "clean natural window light",
    "bright high-key studio lighting",
]

# General SDXL seeds — enough distinct values to span a full menu without repeats.
SEEDS = [42, 1337, 9999, 77777, 271828, 161803, 314159, 112358]

# GENERAL anti-idealization defaults (NOT tuned to any person). Env-overridable
# for GLOBAL tuning only — never set per-user.
#  • CFG ~6: enough for the scene/attire prompt to land WITHOUT over-idealizing
#    the face. (Stage-B's 4.5 was too weak; the fix is "right", not "lowest".)
#  • LoRA identity weight ~1.0: let the user's own trained identity dominate.
DEFAULT_CFG           = float(os.environ.get("GUIDANCE_SCALE", "6.0"))
DEFAULT_LORA_WEIGHT   = float(os.environ.get("LORA_IDENTITY_WEIGHT", "1.0"))
DEFAULT_STEPS         = int(os.environ.get("NUM_INFERENCE_STEPS", "30"))
DEFAULT_WIDTH         = int(os.environ.get("GEN_WIDTH", "1024"))
DEFAULT_HEIGHT        = int(os.environ.get("GEN_HEIGHT", "1024"))
# How many (background, attire) tuples one job spans (product wants ~4-6 for range).
DEFAULT_NUM_OUTPUTS   = int(os.environ.get("NUM_OUTPUTS", "6"))

# DreamBooth trigger token baked into EVERY prompt as "ohwx <class>" so the
# per-user identity LoRA actually FIRES. Every per-user LoRA is trained with this
# token in its captions (see the training pipeline); without it in the prompt the
# adapter loads but is never invoked → generic strangers, not the user. General,
# env-overridable ('' disables). The <class> word is gender-driven (woman/man/person).
IDENTITY_TRIGGER      = os.environ.get("IDENTITY_TRIGGER", catalog.IDENTITY_TRIGGER).strip()

# 2K deliverable: generate at 1024, then Real-ESRGAN x4 → fit long edge to this.
# Set UPSCALE_TARGET=0 to disable the upscale pass entirely (ships raw 1024).
UPSCALE_TARGET        = int(os.environ.get("UPSCALE_TARGET", "2048"))
# Real-ESRGAN weight is fp32 by default (best quality; fp16 can artifact). A100
# 80GB has ample room. Set UPSCALE_HALF=1 to run the upscaler in fp16.
UPSCALE_HALF          = os.environ.get("UPSCALE_HALF", "0").strip() == "1"

# DEMOGRAPHIC-NEUTRAL negatives: environment + encoding-quality ONLY. Deliberately
# NO anatomy/beauty terms ("deformed", "ugly", "bad anatomy") — those steer SDXL
# toward an idealized, slimmed, lighter-skinned face and would erase real features
# of ANY user (this bias hits darker-skinned users hardest). Nothing here mentions
# skin, age, or ethnicity in any direction.
NEGATIVE_PROMPT = (
    "cartoon, illustration, painting, 3d render, cgi, "
    "blurry, low quality, low resolution, jpeg artifacts, "
    "watermark, signature, text, logo, frame, border, "
    "extra fingers, deformed hands"
)


def normalize_gender(g: str) -> str:
    """Map any gender input to one of {'female','male','neutral'}. Never assumes a
    default — absent/unknown → 'neutral'. Delegates to catalog so training and
    generation classify a user identically."""
    return catalog.normalize_gender(g)


def hair_phrase(hair_color: str) -> str:
    """Hair clause for the subject, or '' when there is nothing sensible to say.

    Naively interpolating the picker's value produced nonsense for two of its own options:
    the frontend hair list includes 'bald' and 'custom', so `f", with {hair} hair,"` yielded
    "with bald hair" and "with custom hair". 'bald' needs its own phrasing; 'custom' (the
    'Other' escape hatch) carries no information and must be dropped, not repeated at SDXL.
    """
    h = (hair_color or "").strip().lower().replace("_", " ")
    if not h or h in ("custom", "other", "none", "prefer not to say"):
        return ""
    if h in ("bald", "bald or shaved", "shaved"):
        return ", bald,"
    return f", with {h} hair,"


def age_to_phrase(age_range: str) -> str:
    """Robustly map ANY age-range string ('24-26', '51-65', '65+', '18-20') to a
    life-stage phrase, from the RANGE'S midpoint — no exact-key lookup that
    silently drops unrecognized values (the old AGE_MAP bug). Returns '' only when
    no number can be parsed. Phrasing is gender-neutral ('in their ...')."""
    import re
    nums = [int(n) for n in re.findall(r"\d+", str(age_range or ""))]
    if not nums:
        return ""
    mid = sum(nums) / len(nums)
    if mid < 21:   return "in their late teens"
    if mid < 25:   return "in their early twenties"
    if mid < 30:   return "in their late twenties"
    if mid < 40:   return "in their thirties"
    if mid < 50:   return "in their forties"
    if mid < 60:   return "in their fifties"
    if mid < 65:   return "in their early sixties"
    return "in their late sixties or older"


# ─────────────────────────────────────────────────────────
# Model loading
# ─────────────────────────────────────────────────────────

def load_base_model():
    global pipe, upscaler
    if pipe is not None:
        return

    write_debug("START: load_base_model called")

    try:
        files = os.listdir("/models")
        write_debug(f"/models contents: {files}")
    except Exception as e:
        write_debug(f"/models listdir ERROR: {e}")

    try:
        write_debug("Loading SDXL txt2img + fp16-fix VAE from baked /models ...")
        vae = AutoencoderKL.from_pretrained(
            "/models/sdxl-vae",
            torch_dtype=torch.float16,
        )
        # Pure txt2img: identity comes from the per-user LoRA loaded per job, the
        # scene from the prompt. No source photo, no ControlNet at inference.
        pipe = StableDiffusionXLPipeline.from_pretrained(
            "/models/sdxl-base",
            vae=vae,
            torch_dtype=torch.float16,
            variant="fp16",
            use_safetensors=True,
        )
        pipe = pipe.to("cuda")

        # Real-ESRGAN x4 upscaler (RRDBNet, BSD-3-Clause — COMMERCIAL-SAFE) for the
        # 2K post-process. Weight baked at /models/realesrgan (params_ema key). Kept
        # resident on the A100 (its ~67MB weights + a single 1024→4096 pass fit
        # easily in 80GB alongside the fp16 pipe). do NOT swap to a non-commercial
        # upscaler. UPSCALE_TARGET=0 skips loading it.
        if UPSCALE_TARGET > 0:
            try:
                upscaler = RRDBNet(num_in_ch=3, num_out_ch=3, scale=4,
                                   num_feat=64, num_block=23, num_grow_ch=32)
                _sd = torch.load("/models/realesrgan/RealESRGAN_x4plus.pth",
                                 map_location="cpu")
                _sd = _sd.get("params_ema", _sd.get("params", _sd))
                upscaler.load_state_dict(_sd, strict=True)
                upscaler = upscaler.to("cuda").eval()
                if UPSCALE_HALF:
                    upscaler = upscaler.half()
                write_debug(f"Real-ESRGAN upscaler loaded (target={UPSCALE_TARGET}, half={UPSCALE_HALF})")
            except Exception as e:
                # Non-fatal: fall back to raw 1024 rather than failing the whole job.
                upscaler = None
                write_debug(f"Upscaler load FAILED (shipping 1024): {e}")

        try:
            props = torch.cuda.get_device_properties(0)
            msg = (f"GPU={props.name} total_memory={props.total_memory} "
                   f"({props.total_memory/1024**3:.1f} GB), "
                   f"max_memory_allocated={torch.cuda.max_memory_allocated(0)} "
                   f"({torch.cuda.max_memory_allocated(0)/1024**3:.1f} GB)")
            write_debug(msg)
        except Exception as e:
            write_debug(f"VRAM check failed: {e}")
        write_debug("SUCCESS: SDXL txt2img base model loaded")
        log.info("✅ SDXL txt2img base model loaded")
    except Exception as e:
        write_debug(f"from_pretrained ERROR: {e}")
        raise


def load_category_lora(category: str) -> bool:
    """Download + register the category LoRA. Returns True if the adapter was
    loaded, False otherwise. Caller decides set_adapters from what loaded."""
    lora_path = f"/tmp/lora_category_{category}.safetensors"
    blob_name  = f"category/{category}/adapter_model.safetensors"
    try:
        blob_client = blob_service.get_blob_client(container=AZURE_LORA_CONTAINER, blob=blob_name)
        with open(lora_path, "wb") as f:
            f.write(blob_client.download_blob().readall())
        pipe.load_lora_weights(lora_path, adapter_name="category_lora")
        log.info(f"✅ Category LoRA loaded: {category}")
        return True
    except Exception as e:
        log.warning(f"⚠️ Category LoRA not found for '{category}': {e}")
        return False


def load_identity_lora(user_id: str) -> bool:
    """Download + register the identity LoRA from
    lora-weights/identity/<user_id>/adapter_model.safetensors. Returns True if the
    adapter was loaded, False otherwise. Does NOT call set_adapters; the caller
    activates only the adapters that actually loaded.

    A key-format mismatch (kohya vs diffusers) does NOT raise — diffusers WARNS
    about unexpected/unmatched keys and silently loads nothing, which then renders
    as a generic (no-effect) image. We capture those WARNING logs and write them to
    the debug blob so a silent no-op LoRA is diagnosable instead of invisible."""
    lora_path = f"/tmp/lora_identity_{user_id}.safetensors"
    blob_name  = f"identity/{user_id}/adapter_model.safetensors"
    try:
        blob_client = blob_service.get_blob_client(container=AZURE_LORA_CONTAINER, blob=blob_name)
        with open(lora_path, "wb") as f:
            f.write(blob_client.download_blob().readall())

        import logging as _logging
        caught = []
        class _Catch(_logging.Handler):
            def emit(self, rec):
                if rec.levelno >= _logging.WARNING:
                    caught.append(rec.getMessage())
        root = _logging.getLogger()
        handler = _Catch()
        root.addHandler(handler)
        try:
            pipe.load_lora_weights(lora_path, adapter_name="identity_lora")
        finally:
            root.removeHandler(handler)

        size = os.path.getsize(lora_path)
        write_debug(
            f"Identity LoRA loaded: user={user_id} bytes={size} "
            f"load_warnings={caught if caught else 'none'}"
        )
        log.info(f"✅ Identity LoRA loaded: {user_id} (warnings={len(caught)})")
        return True
    except Exception as e:
        write_debug(f"Identity LoRA load FAILED for '{user_id}': {e}")
        log.warning(f"⚠️ Identity LoRA not found/failed for '{user_id}': {e}")
        return False


def unload_loras():
    try:
        pipe.unload_lora_weights()
        log.info("✅ LoRAs unloaded")
    except Exception:
        pass


# ─────────────────────────────────────────────────────────
# Image utilities
# ─────────────────────────────────────────────────────────

def generate_sas_url(container: str, blob_name: str, expiry_hours: int = 24) -> str:
    sas_token = generate_blob_sas(
        account_name=AZURE_STORAGE_ACCOUNT,
        container_name=container,
        blob_name=blob_name,
        account_key=AZURE_STORAGE_KEY,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.now(timezone.utc) + timedelta(hours=expiry_hours)
    )
    return f"https://{AZURE_STORAGE_ACCOUNT}.blob.core.windows.net/{container}/{blob_name}?{sas_token}"


def load_image_from_blob(container: str, blob_name: str) -> Image.Image:
    blob_client = blob_service.get_blob_client(container=container, blob=blob_name)
    data = blob_client.download_blob().readall()
    return Image.open(io.BytesIO(data)).convert("RGB")


def resize_for_sdxl(img: Image.Image) -> Image.Image:
    # SDXL is trained at ~1024². Cap the long edge at 1024 and snap to a multiple
    # of 8 (SDXL's latent stride). Today's render is text-to-image so this is only
    # used to validate the input-blob read path; it becomes load-bearing when the
    # img2img / identity path lands.
    target = 1024
    w, h   = img.size
    ratio  = min(target / w, target / h)
    new_w  = (int(w * ratio) // 8) * 8
    new_h  = (int(h * ratio) // 8) * 8
    return img.resize((new_w, new_h), Image.LANCZOS)


def upscale_image(img: Image.Image) -> Image.Image:
    """Real-ESRGAN x4 (BSD-3-Clause, commercial-safe) upscale, then fit the long
    edge to UPSCALE_TARGET (2K). POST-PROCESS ONLY — no regeneration, identity is
    untouched. Runs on the A100 right after generation. Falls back to the input
    image if the upscaler failed to load.

    commercial-safe license — do NOT swap to a non-commercial upscaler."""
    if upscaler is None or UPSCALE_TARGET <= 0:
        return img
    dtype = torch.float16 if UPSCALE_HALF else torch.float32
    arr = np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0
    ten = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0).to("cuda", dtype=dtype)
    with torch.no_grad():
        out = upscaler(ten).clamp(0, 1)
    out = (out.squeeze(0).permute(1, 2, 0).float().cpu().numpy() * 255.0)
    up = Image.fromarray(out.round().astype(np.uint8))   # 4096² for a 1024² input
    # Fit the long edge to the 2K target (high-quality Lanczos downscale from x4).
    w, h = up.size
    if max(w, h) != UPSCALE_TARGET:
        ratio = UPSCALE_TARGET / max(w, h)
        up = up.resize((round(w * ratio), round(h * ratio)), Image.LANCZOS)
    return up


def add_watermark(img: Image.Image) -> Image.Image:
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


def upload_image_to_blob(img: Image.Image, job_id: str, index: int) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    blob_name   = f"results/{job_id}/headshot_{index + 1}.png"
    blob_client = blob_service.get_blob_client(container=AZURE_BLOB_CONTAINER, blob=blob_name)
    blob_client.upload_blob(buf, overwrite=True)
    log.info(f"✅ Uploaded: {blob_name}")
    return blob_name


# ─────────────────────────────────────────────────────────
# SQL update
# ─────────────────────────────────────────────────────────

def update_job_status(job_id: str, status: str, output_blob_paths: list = None,
                      max_attempts: int = 3):
    """Write a job's status, retrying on transient DB errors and RAISING if it
    ultimately fails. The old version swallowed every exception, so a failed
    'completed'/'failed' write silently left the row stuck in 'processing'.

    - 'failed': refund the FULL amount the user was charged, tied to the ACTUAL
      transition (WHERE status NOT IN ('failed','completed') + rowcount) so retries /
      the backend ALSO failing the job can never double-refund.
    - completed_at is set ONLY for terminal states; the old code stamped it even
      on 'processing'. (#9)
    """
    output_json = json.dumps(output_blob_paths) if output_blob_paths else None
    last_err = None
    for attempt in range(1, max_attempts + 1):
        conn = None
        try:
            conn   = get_db_connection()
            cursor = conn.cursor()
            if status == "failed":
                cursor.execute("""
                    UPDATE jobs
                    SET status = ?, output_blob_path = ?, completed_at = GETUTCDATE()
                    WHERE job_id = ? AND status NOT IN ('failed', 'completed')
                """, status, output_json, job_id)
                transitioned = cursor.rowcount == 1
                refund = 0
                if transitioned:
                    # Refund what was actually SPENT at submit: image_count *
                    # credits_per_image, stored as credit_cost in job_params. This used
                    # to be hardcoded +1, so a failed 30-image job refunded 1 credit and
                    # silently ate the other 29 — the backend's _mark_failed already
                    # refunds the full amount, and these two paths must agree. Falls back
                    # to 1 for legacy rows written before per-image charging.
                    cursor.execute("SELECT job_params FROM jobs WHERE job_id = ?", job_id)
                    r = cursor.fetchone()
                    refund = 1
                    if r and r[0]:
                        try:
                            refund = max(1, int(json.loads(r[0]).get("credit_cost", 1)))
                        except (TypeError, ValueError):
                            refund = 1
                    cursor.execute("""
                        UPDATE users
                        SET credits_remaining = credits_remaining + ?
                        WHERE user_id = (SELECT user_id FROM jobs WHERE job_id = ?)
                    """, refund, job_id)
                conn.commit()
                log.info(
                    f"✅ Job {job_id} -> 'failed' "
                    f"(transitioned={transitioned}, credits_refunded={refund if transitioned else 0})"
                )
            elif status == "completed":
                cursor.execute("""
                    UPDATE jobs
                    SET status = ?, output_blob_path = ?, completed_at = GETUTCDATE()
                    WHERE job_id = ?
                """, status, output_json, job_id)
                conn.commit()
                log.info(f"✅ Job {job_id} status updated to 'completed'")
            else:
                # non-terminal (e.g. 'processing') — do NOT stamp completed_at
                cursor.execute("""
                    UPDATE jobs
                    SET status = ?, output_blob_path = ?
                    WHERE job_id = ?
                """, status, output_json, job_id)
                conn.commit()
                log.info(f"✅ Job {job_id} status updated to '{status}'")
            conn.close()
            return
        except Exception as e:
            last_err = e
            log.warning(f"update_job_status '{status}' attempt {attempt}/{max_attempts} failed: {e}")
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            if attempt < max_attempts:
                time.sleep(2 * attempt)
    log.error(f"❌ Failed to set job {job_id} -> '{status}' after {max_attempts} attempts: {last_err}")
    raise last_err


# ─────────────────────────────────────────────────────────
# Completion email
# ─────────────────────────────────────────────────────────

def notify_user_email(job_id: str, user_id: str, result_blob_paths: list):
    """Best-effort completion email. Never raises — a failure here must NOT mark
    the job failed (images are already uploaded and the DB row is 'completed')."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT email FROM users WHERE user_id = ?", user_id)
        row = cursor.fetchone()
        conn.close()
        to_email = row[0] if row else None
        if not to_email:
            log.info(f"No email on file for user_id={user_id}; skipping completion email")
            return

        # Build a SAS URL for the first headshot (24 h expiry).
        storage_key = AZURE_STORAGE_KEY or get_secret("storage-account-key")
        first_blob = result_blob_paths[0] if result_blob_paths else None
        if not first_blob:
            return
        sas_token = generate_blob_sas(
            account_name=AZURE_STORAGE_ACCOUNT,
            container_name=AZURE_BLOB_CONTAINER,
            blob_name=first_blob,
            account_key=storage_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.now(timezone.utc) + timedelta(hours=24),
        )
        url = (f"https://{AZURE_STORAGE_ACCOUNT}.blob.core.windows.net/"
               f"{AZURE_BLOB_CONTAINER}/{first_blob}?{sas_token}")

        acs_conn_str = get_secret("acs-connection-string")
        client = EmailClient.from_connection_string(acs_conn_str)
        client.begin_send({
            "senderAddress": "noreply@bettersnap.ai",
            "recipients": {"to": [{"address": to_email}]},
            "content": {
                "subject": "Your BetterSnap AI headshot is ready!",
                "plainText": (
                    f"Your headshot (Job ID: {job_id}) is ready. "
                    f"Download it here: {url}"
                ),
                "html": (
                    f"<h2>Your headshot is ready!</h2>"
                    f"<p>Job ID: {job_id}</p>"
                    f"<p><a href=\"{url}\">Click here to download your headshot</a></p>"
                ),
            },
        })
        log.info(f"✅ Completion email sent to {to_email} for job_id={job_id}")
    except Exception as e:
        log.warning(f"⚠️ Completion email FAILED for job_id={job_id} (non-fatal): {e}")


# ─────────────────────────────────────────────────────────
# Core inference
# ─────────────────────────────────────────────────────────

def run_inference(job: dict) -> list:
    job_id     = job["job_id"]
    user_id    = job["user_id"]
    job_params = json.loads(job.get("job_params", "{}"))

    # ── Per-user attributes (from THIS user's job_params; NO assumed defaults) ──
    # These come from the user's own onboarding data; the pipeline is identical for
    # every user — only the values (and their LoRA) differ.
    gkey       = normalize_gender(job_params.get("gender"))         # dead-read bug FIXED
    age_phrase = age_to_phrase(job_params.get("age_range", ""))     # silent-drop bug FIXED
    hair_color = (job_params.get("hair_color") or "").strip().lower()

    # ── User's GLOBAL cross-category selections + plan-driven count ────────────
    # attire_ids / background_ids are category-qualified refs ("business_suit.navy_
    # suit_tie") the user picked in the UI — they may span professional AND personal
    # categories (Pro/Expert). The catalog turns each ref into a gender-aware phrase.
    # image_count comes from the user's PLAN (resolved in function_app.submit_job) —
    # NOT a fixed constant. custom_prompt is set only for the custom_scene mode
    # (user-typed scene), which we WRAP with identity + quality below. IMAGE_COUNT_
    # OVERRIDE (env, off by default) caps the count for cheap wiring tests.
    attire_refs      = job_params.get("attire_ids") or []
    background_refs  = job_params.get("background_ids") or []
    custom_prompt    = (job_params.get("custom_prompt") or "").strip()
    image_count      = int(job_params.get("image_count") or DEFAULT_NUM_OUTPUTS)
    _test_cap = int(os.environ.get("IMAGE_COUNT_OVERRIDE", "0") or "0")
    if _test_cap > 0:
        image_count = min(image_count, _test_cap)
    image_count      = max(1, min(image_count, 100))                # hard safety clamp

    subject = SUBJECT_NOUN[gkey]
    write_debug(
        f"User attrs: gender={job_params.get('gender')!r}->{gkey} subject='{subject}' "
        f"age_range={job_params.get('age_range')!r}->age_phrase={age_phrase!r} "
        f"hair={hair_color!r} attire_refs={attire_refs} background_refs={background_refs} "
        f"image_count={image_count} custom={'yes' if custom_prompt else 'no'} "
        f"test_cap={_test_cap or 'off'}"
    )

    # ── Per-user identity LoRA — the ONLY thing carrying identity in txt2img ──
    # Base SDXL is baked; the LoRA is the per-job fetch from
    # lora-weights/identity/<user_id>/adapter_model.safetensors. Activated via
    # set_adapters (compatible with cpu offload). active/weights built from what
    # actually loaded, so a missing category adapter can't name an unloaded one.
    active, weights = [], []
    if load_category_lora(category):
        active.append("category_lora"); weights.append(0.8)
    identity_ok = load_identity_lora(user_id)
    if identity_ok:
        active.append("identity_lora"); weights.append(DEFAULT_LORA_WEIGHT)

    # HARD FAIL on a missing identity adapter. In txt2img the LoRA is the ONLY thing
    # carrying the user's face — with no adapter, base SDXL renders a photogenic
    # STRANGER and, before this check, happily uploaded it as the user's headshots.
    # A loud failure (which refunds their credits via _mark_failed) is the only
    # acceptable outcome; delivering someone else's face is not. /jobs/submit already
    # gates on lora_status, so reaching here means the adapter blob is missing or
    # unreadable — a real fault, not a user error.
    if not identity_ok:
        raise RuntimeError(
            f"identity LoRA missing for user_id={user_id} "
            f"(lora-weights/identity/{user_id}/adapter_model.safetensors) — refusing to "
            f"render base SDXL and pass off a generic face as this user's headshots"
        )

    pipe.set_adapters(active, adapter_weights=weights)
    write_debug(f"LoRA adapters ACTIVE: {active} weights={weights}")

    try:
        free_vram, total_vram = torch.cuda.mem_get_info(0)
        write_debug(
            f"Pre-inference free VRAM: {free_vram/1024**3:.1f} GB "
            f"of {total_vram/1024**3:.1f} GB total"
        )
    except Exception as e:
        write_debug(f"mem_get_info failed: {e}")

    # ── Build this job's per-image plan ───────────────────────────────────────
    # combos = cartesian product of the user's SELECTED attire refs × background
    # refs, spanning ANY categories (Pro/Expert may mix professional + personal;
    # the catalog drops unknown refs). The plan's image_count is distributed across
    # combos by cycling (i % len), each image with a distinct seed so a repeated
    # combo still varies. The custom_scene mode has NO menu — every image wraps the
    # user's typed scene. The lead phrase + lighting are DERIVED PER-IMAGE from the
    # BACKGROUND's category (a beach bg reads 'lifestyle portrait', golden lighting),
    # even when the attire came from a different category.
    is_custom = bool(custom_prompt)
    combos = [] if is_custom else catalog.build_combos_global(attire_refs, background_refs)
    if not is_custom and not combos:
        raise ValueError(
            f"No attire/background combos "
            f"(attire_refs={attire_refs}, background_refs={background_refs})"
        )
    write_debug(
        f"Plan: image_count={image_count} combos={len(combos)} custom={is_custom} "
        f"trigger={IDENTITY_TRIGGER!r} CFG={DEFAULT_CFG} lora_w={DEFAULT_LORA_WEIGHT} "
        f"{DEFAULT_WIDTH}x{DEFAULT_HEIGHT} steps={DEFAULT_STEPS}"
    )

    _negative = os.environ.get("NEGATIVE_PROMPT", NEGATIVE_PROMPT)

    # Subject clause built ONCE from the user's REAL attributes — no beauty/
    # idealization language anywhere (that lightened + slimmed the face). Bake the
    # DreamBooth trigger so the per-user LoRA FIRES: "ohwx <woman|man|person>";
    # without it the adapter loads but is never invoked → generic strangers.
    subj = f"{IDENTITY_TRIGGER} {subject}" if IDENTITY_TRIGGER else f"a {subject}"
    if age_phrase:
        subj += f" {age_phrase}"
    subj += hair_phrase(hair_color)
    _tail = ("looking at the camera, sharp focus, high detail, realistic natural "
             "skin texture, shot on a DSLR with an 85mm portrait lens.")

    result_blob_paths = []
    for i in range(image_count):
        seed = 1000 + i                                   # distinct per image
        if is_custom:
            combo_label = "custom_scene"
            lead = catalog.lead_phrase("custom_scene")
            lighting = LIGHTING[i % len(LIGHTING)]
            # Scene-only wrap: keep identity subj + quality tail + negatives around
            # whatever the user typed (they supply only the scene/outfit).
            prompt = f"{lead} {subj} {custom_prompt}. {lighting}, {_tail}"
        else:
            attire_ref, bg_ref = combos[i % len(combos)]
            attire    = catalog.attire_phrase_ref(attire_ref, gkey)
            bg_phrase = catalog.background_phrase_ref(bg_ref)
            lead      = catalog.lead_for_background_ref(bg_ref)        # style follows bg
            _lighting = catalog.lighting_for_background_ref(bg_ref, LIGHTING)
            # Advance lighting once per FULL CYCLE of combos, not per image.
            # Indexing both by `i` aliases them whenever len(combos) and len(_lighting)
            # share a factor — and the common case (2 attires x 2 backgrounds = 4 combos,
            # 4 default lighting options) is the worst one: combo k would get lighting k
            # every single time, so all 8 images of a combo shared a byte-identical prompt
            # and differed only by seed. Dividing decorrelates the two cycles, so each
            # combo walks through every lighting setup.
            lighting  = _lighting[(i // len(combos)) % len(_lighting)]
            combo_label = f"{bg_ref} | {attire_ref}"
            prompt = f"{lead} {subj} wearing {attire}, {bg_phrase}. {lighting}, {_tail}"
        log.info(f"📣 Output {i+1}/{image_count} [{combo_label}]: {prompt}")
        try:
            write_debug(
                f"Output {i+1}: txt2img pipe() [{combo_label}] cfg={DEFAULT_CFG} seed={seed}"
            )
            output = pipe(
                prompt=prompt,
                negative_prompt=_negative,
                guidance_scale=DEFAULT_CFG,
                num_inference_steps=DEFAULT_STEPS,
                width=DEFAULT_WIDTH,
                height=DEFAULT_HEIGHT,
                generator=torch.Generator("cuda").manual_seed(seed),
            ).images[0]

            # Real inference VRAM peak on the first output — surfaces any OOM headroom.
            if i == 0:
                try:
                    total = torch.cuda.get_device_properties(0).total_memory
                    peak  = torch.cuda.max_memory_allocated(0)
                    msg = (f"INFERENCE VRAM PEAK: max_memory_allocated={peak} "
                           f"({peak / 1024**3:.1f} GB) of total_memory={total} "
                           f"({total / 1024**3:.1f} GB)")
                    log.info(f"🔎 {msg}")
                    write_debug(msg)
                except Exception as e:
                    write_debug(f"inference VRAM probe failed: {e}")

            # 2K post-process (Real-ESRGAN x4 → fit to UPSCALE_TARGET), then
            # watermark at the final resolution (the bar scales with image height).
            _pre = output.size
            output    = upscale_image(output)
            if i == 0:
                write_debug(f"Upscaled {_pre} -> {output.size} (target={UPSCALE_TARGET})")
            output    = add_watermark(output)
            blob_path = upload_image_to_blob(output, job_id, i)
            result_blob_paths.append(blob_path)
            log.info(f"✅ Output {i+1} complete ({output.size[0]}x{output.size[1]})")
        except Exception:
            # Do NOT swallow: log the FULL traceback and re-raise so __main__ marks
            # the job FAILED (and refunds), instead of a silent short/empty result.
            log.error(
                f"❌ Output {i+1} FAILED — full traceback:\n{traceback.format_exc()}"
            )
            raise

    return result_blob_paths


# ─────────────────────────────────────────────────────────
# Queue polling (legacy)
# ─────────────────────────────────────────────────────────

def process_queue():
    log.info(f"📭 Polling queue: {AZURE_QUEUE_NAME}")
    while True:
        messages = queue_client.receive_messages(max_messages=1, visibility_timeout=600)
        message  = next(messages, None)

        if message is None:
            log.info("Queue empty. Waiting 10s...")
            time.sleep(10)
            continue

        job_id = None
        try:
            job    = json.loads(message.content)
            job_id = job.get("job_id")
            log.info(f"📦 Job received: {job_id}")
            update_job_status(job_id, "processing")
            result_blob_paths = run_inference(job)
            update_job_status(job_id, "completed", result_blob_paths)
            queue_client.delete_message(message)
            log.info(f"✅ Job {job_id} complete")
        except Exception as e:
            log.error(f"❌ Job failed: {e}")
            if job_id:
                update_job_status(job_id, "failed")


# ─────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    write_debug("=== CONTAINER STARTED ===")
    log.info("🚀 BetterSnap AI Inference Container Starting...")

    job_id  = os.environ.get("JOB_ID")
    user_id = os.environ.get("USER_ID")

    write_debug(f"JOB_ID={job_id}, USER_ID={user_id}")

    if job_id and user_id:
        log.info(f"📦 Container Apps Job mode: job_id={job_id}")
        write_debug(f"Starting job mode for job_id={job_id}")

        result_blob_paths = None
        try:
            # Read the job from the DB BEFORE loading the 30GB model, so a
            # missing job or a flaky DB connect fails in seconds instead of
            # burning a full ~5-min GPU model load first.
            write_debug("Connecting to DB...")
            conn   = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT job_params, status FROM jobs WHERE job_id = ?", job_id)
            row = cursor.fetchone()
            conn.close()

            if not row:
                write_debug(f"ERROR: Job {job_id} not found in DB")
                log.error(f"❌ Job {job_id} not found in DB")
                exit(1)

            # If a prior run already drove this job to a terminal state, re-running
            # is wasteful (a fresh ~7-min A100 cold start) and unsafe (it would flip
            # the row back to 'processing' and could trigger a second refund). Treat
            # the re-run as a no-op success. (replicaRetryLimit is 0, but a stray
            # manual re-trigger or a duplicate dispatch could still land here.)
            existing_status = row[1]
            if existing_status in ("failed", "completed"):
                write_debug(
                    f"Job {job_id} already terminal ('{existing_status}'); "
                    f"skipping re-run (no-op)."
                )
                log.info(f"⏭️ Job {job_id} already '{existing_status}', nothing to do")
                exit(0)

            job = {
                "job_id":     job_id,
                "user_id":    user_id,
                "job_params": row[0],
            }

            update_job_status(job_id, "processing")

            write_debug("Loading base model...")
            load_base_model()

            write_debug("Job found in DB, starting inference...")
            result_blob_paths = run_inference(job)

        except Exception as e:
            # Generation/setup failed and was catchable. Record 'failed' (which
            # also refunds the credit). NOTE: an OOM SIGKILL / exit 137 cannot
            # reach here — the process is killed outright — so a row killed that
            # way stays 'processing' and needs the external reaper / admin tool.
            write_debug(f"FATAL ERROR during generation: {e}")
            log.error(f"❌ Job {job_id} failed: {e}")
            try:
                update_job_status(job_id, "failed")
            except Exception as se:
                write_debug(f"ALSO failed to write 'failed' status for {job_id}: {se}")
                log.error(f"❌ Could not record 'failed' for {job_id}: {se}")
            exit(1)

        # Generation succeeded — images are already in blob storage. A failure to
        # write 'completed' here must NOT mark the job 'failed' (that would wrongly
        # refund a job the user actually received). Log loudly for reconciliation.
        try:
            update_job_status(job_id, "completed", result_blob_paths)
            write_debug(f"SUCCESS: Job {job_id} complete. Output: {result_blob_paths}")
            log.info(f"✅ Job {job_id} complete")
            notify_user_email(job_id, user_id, result_blob_paths)
        except Exception as se:
            write_debug(
                f"CRITICAL: Job {job_id} generation succeeded and images uploaded "
                f"({result_blob_paths}) but writing 'completed' failed: {se}. "
                f"Row left in 'processing' — reconcile, do NOT refund."
            )
            log.error(f"❌ Job {job_id} completed but status write failed: {se}")
            exit(1)
    else:
        write_debug("FATAL: JOB_ID/USER_ID not set — env overrides did not reach the container. Exiting.")
        log.error("❌ JOB_ID/USER_ID not set; this job must be started with env overrides. Exiting.")
        exit(1)