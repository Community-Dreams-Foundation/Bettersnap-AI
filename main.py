import os
import io
import json
import time
import hashlib
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
from stage_runtime import run_stage
from prompt_control import apply_composition_control
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
ip_adapter_ok = False   # IP-Adapter Plus-Face loaded successfully?
img2img_pipe = None     # SDXL img2img (shares pipe's UNet/LoRA/IP-Adapter) for face refine
_face_cascade = None    # cv2 Haar cascade for locating the face to refine

# Per-stage init outcome for the run manifest: name -> {enabled, initialized, reason[, error]}.
# Populated by load_base_model via stage_runtime.run_stage. SINGLE-JOB INVARIANT: the GPU
# worker serves ONE job per container execution (one JOB_ID, no threads), so a module-global
# is safe. If the worker is ever changed to serve concurrent jobs in one process, this must
# become per-job state (pass a fresh dict into run_stage per job) — see stage_runtime.py.
STAGE_STATUS = {}


class IpAdapterReferenceUnavailable(RuntimeError):
    """IP-Adapter is enabled but the user's reference face crops are unavailable at
    generation time (retention deletes the input crops after N days). We FAIL the job
    clearly rather than silently dropping to LoRA-only — that would change output quality
    with no error and no signal to anyone. The __main__ handler catches this like any
    other generation error and marks the job 'failed' (which refunds)."""
    code = "IP_ADAPTER_REFERENCE_UNAVAILABLE"

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
# IP-Adapter Plus-Face conditioning strength (0 disables). 0.5-0.7 adds identity from the
# user's own face crop without fighting the prompt/attire. Env-tunable for A/B testing.
IP_ADAPTER_SCALE      = float(os.environ.get("IP_ADAPTER_SCALE", "0.6"))
# Which fetched reference crop feeds IP-Adapter (Phase-3 ablation). DEFAULT 0 = the first
# crop = current behavior (strategy A), so the baseline / Phase-2 control is unchanged. The
# harness (evaluation.reference_selection) picks a best-quality index for strategy B and it
# is passed in via this env — the GPU worker does no detection of its own. Clamped to the
# available crops at use time.
IP_ADAPTER_REF_INDEX  = int(os.environ.get("IP_ADAPTER_REF_INDEX", "0"))
# Phase-5 composition control: append explicit head-and-shoulders framing + composition
# negatives to steer away from full-body/averted output. DEFAULT 0 so the Phase-2 baseline
# (current prompts) is unchanged; the Phase-5 experiment sets it to 1.
COMPOSITION_CONTROL   = os.environ.get("COMPOSITION_CONTROL", "0").strip() != "0"
# Face-inpaint ("ADetailer") pass: after the base image, re-render JUST the face crop at
# 1024 (with LoRA + IP-Adapter) so eyes/skin get real detail instead of upscaler-guessed
# mush. STRENGTH controls how much is rewritten (0.35-0.5); too high drifts the face.
FACE_REFINE_ENABLE    = os.environ.get("FACE_REFINE", "1").strip() != "0"
FACE_REFINE_STRENGTH  = float(os.environ.get("FACE_REFINE_STRENGTH", "0.45"))
# WHOLE-IMAGE realism pass: a light img2img over the FULL upscaled (2048) image. The plastic
# hair + painterly look the user flagged comes mostly from Real-ESRGAN over-smoothing the
# ENTIRE frame (not just the face) — a face-only refine can't reach the hair top or fabric.
# Running img2img AFTER the upscale is the only ordering where re-injected texture survives
# (ESRGAN already ran and can't re-smooth it). Low strength keeps identity/composition; it
# just rebuilds fine detail (hair strands, skin pores, fabric weave). Shares pipe's UNet so
# the identity LoRA + IP-Adapter stay active. Env-tunable so strength is A/B'd without a rebuild.
REALISM_PASS_ENABLE   = os.environ.get("REALISM_PASS", "1").strip() != "0"
REALISM_PASS_STRENGTH = float(os.environ.get("REALISM_STRENGTH", "0.18"))
# Film grain: real camera sensors add luminance noise; a perfectly clean frame reads as "AI".
# A subtle grain at the very end (post-refine, pre-watermark) is the cheapest realism win.
# GRAIN_AMOUNT is the noise sigma in 0-255 space (~4-8 is subtle). 0 disables.
FILM_GRAIN_AMOUNT     = float(os.environ.get("GRAIN_AMOUNT", "5.0"))
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
    # upscaler / img2img_pipe / _face_cascade are assigned inside the nested _init_*
    # functions below, which declare their own `global`; only pipe and ip_adapter_ok are
    # assigned directly in this scope.
    global pipe, ip_adapter_ok
    if pipe is not None:
        return

    write_debug("START: load_base_model called")
    # Fresh status each real load (the pipe!=None guard above means this runs once per
    # process, but clearing keeps STAGE_STATUS honest if that guard ever changes).
    STAGE_STATUS.clear()
    ip_adapter_ok = False

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

        # ── Optional pipeline stages (Phase-0 stage runner) ──────────────────────
        # Each stage initializes independently and records {enabled, initialized,
        # reason[, error]} in STAGE_STATUS for the run manifest. Policy: a DISABLED stage
        # is skipped and recorded; an ENABLED stage that fails to initialize is FATAL —
        # no more silently continuing with a required quality stage missing (the bug that
        # made face-refine + realism never run in prod when cv2 was absent). EXCEPTION:
        # 'upscale' degrades to 1024 and records reason=degraded_to_1024 rather than
        # failing a ~30-min job over a resolution downgrade. See stage_runtime.py.
        def _init_ip_adapter():
            # IP-Adapter Plus-Face (CLIP ViT-H, commercial-safe): conditions on the user's
            # own face crop (applied per job), alongside the identity LoRA.
            global ip_adapter_ok
            pipe.load_ip_adapter(
                "/models/ip-adapter",
                subfolder="sdxl_models",
                weight_name="ip-adapter-plus-face_sdxl_vit-h.safetensors",
                image_encoder_folder="models/image_encoder",
            )
            ip_adapter_ok = True
            write_debug("IP-Adapter Plus-Face loaded (CLIP ViT-H)")

        def _init_upscaler():
            # Real-ESRGAN x4 (RRDBNet, BSD-3-Clause — COMMERCIAL-SAFE). Weight baked at
            # /models/realesrgan (params_ema key). do NOT swap to a non-commercial upscaler.
            global upscaler
            up = RRDBNet(num_in_ch=3, num_out_ch=3, scale=4,
                         num_feat=64, num_block=23, num_grow_ch=32)
            _sd = torch.load("/models/realesrgan/RealESRGAN_x4plus.pth", map_location="cpu")
            _sd = _sd.get("params_ema", _sd.get("params", _sd))
            up.load_state_dict(_sd, strict=True)
            up = up.to("cuda").eval()
            if UPSCALE_HALF:
                up = up.half()
            upscaler = up
            write_debug(f"Real-ESRGAN upscaler loaded (target={UPSCALE_TARGET}, half={UPSCALE_HALF})")

        def _init_shared_img2img():
            # The ONE img2img pipe shared by realism + face-refine. Shares pipe.components
            # (same UNet/LoRA/IP-Adapter — no second SDXL load). Lazy + idempotent, so
            # realism no longer depends on face-refine being enabled.
            global img2img_pipe
            if img2img_pipe is None:
                from diffusers import StableDiffusionXLImg2ImgPipeline
                img2img_pipe = StableDiffusionXLImg2ImgPipeline(**pipe.components)

        def _init_face_refine():
            # Requires BOTH the shared img2img pipe AND the Haar detector; either failing
            # must leave the stage un-initialized (so run_stage records init_failed).
            global _face_cascade
            import cv2
            _init_shared_img2img()
            casc = cv2.CascadeClassifier(
                cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
            if casc.empty():
                raise RuntimeError("Haar face cascade failed to load (empty classifier)")
            _face_cascade = casc
            write_debug("Face-inpaint img2img pipe + detector ready")

        def _init_realism():
            # Realism's only init-time prerequisite is the shared img2img pipe (ref_face is
            # per-image/optional; prompts + steps/CFG are module constants). Named so any
            # future realism-only prereq lives here and is required BEFORE 'initialized'.
            _init_shared_img2img()
            write_debug("Realism img2img pipe ready")

        def _stage(name, enabled, init_fn, **kw):
            return run_stage(name, enabled, init_fn, STAGE_STATUS, log=write_debug, **kw)

        _stage("ip_adapter",  IP_ADAPTER_SCALE > 0, _init_ip_adapter)
        _stage("upscale",     UPSCALE_TARGET > 0,   _init_upscaler,
               fatal=False, degraded_reason="degraded_to_1024")
        _stage("face_refine", FACE_REFINE_ENABLE,   _init_face_refine)
        _stage("realism",     REALISM_PASS_ENABLE,  _init_realism)

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
                # GUARDED like the 'failed' path above: only complete a job that is still
                # in-flight. Without this, a job the reaper already marked 'failed' (and
                # REFUNDED) could be overwritten to 'completed' when the GPU finishes late —
                # the user would keep BOTH the refund AND the delivered images. If the row is
                # already terminal, this is a no-op (rowcount 0) and we leave it 'failed'.
                cursor.execute("""
                    UPDATE jobs
                    SET status = ?, output_blob_path = ?, completed_at = GETUTCDATE()
                    WHERE job_id = ? AND status NOT IN ('failed', 'completed')
                """, status, output_json, job_id)
                transitioned = cursor.rowcount == 1
                conn.commit()
                if transitioned:
                    log.info(f"✅ Job {job_id} status updated to 'completed'")
                else:
                    log.warning(
                        f"Job {job_id} completion IGNORED — row already terminal (likely reaped "
                        f"to 'failed' and refunded). Not overwriting, to avoid a double credit "
                        f"(refund + delivered images)."
                    )
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

        # Link the user to their DASHBOARD (not a single-image download). The results
        # live there, it's retention/plan-aware, and one link covers the whole batch.
        # APP_BASE_URL is env-driven so staging/prod point at the right frontend host.
        app_base = os.environ.get("APP_BASE_URL", "https://bettersnap.ai").rstrip("/")
        dashboard_url = f"{app_base}/dashboard"
        count = len(result_blob_paths or [])
        noun = "headshots" if count != 1 else "headshot"

        acs_conn_str = get_secret("acs-connection-string")
        client = EmailClient.from_connection_string(acs_conn_str)
        client.begin_send({
            "senderAddress": "noreply@bettersnap.ai",
            "recipients": {"to": [{"address": to_email}]},
            "content": {
                "subject": "Your BetterSnap AI headshots are ready!",
                "plainText": (
                    f"Great news — your {count} AI {noun} are ready. "
                    f"View and download them in your dashboard: {dashboard_url}"
                ),
                "html": (
                    f"<h2>Your headshots are ready! 🎉</h2>"
                    f"<p>Your {count} BetterSnap AI {noun} have finished generating.</p>"
                    f"<p><a href=\"{dashboard_url}\">Open your dashboard to view and download</a></p>"
                ),
            },
        })
        log.info(f"✅ Completion email (dashboard) sent to {to_email} for job_id={job_id}")
    except Exception as e:
        log.warning(f"⚠️ Completion email FAILED for job_id={job_id} (non-fatal): {e}")


def _get_ref_faces(user_id: str, n: int = 3):
    """Download up to `n` of the user's face crops (from the `inputs` container) to use as
    IP-Adapter Plus-Face reference images. These are the SAME crops the LoRA trained on, so
    the two identity signals agree.

    IMPORTANT: these crops live under the user's `inputs` prefix, which retention deletes —
    so at generation time they may be gone. The caller decides what to do with an empty
    result (see the IP_ADAPTER_REFERENCE_UNAVAILABLE path); this function only reports.

    Returns (refs, ids): a list of PIL RGB images and the parallel list of blob basenames
    that were actually fetched, so callers can log/record EXACTLY which references exist and
    which one is used (the pipeline currently uses index 0 only)."""
    import io
    from PIL import Image
    refs, ids = [], []
    for i in range(n):
        blob_name = f"{user_id}/input/crop_upperbody/img{i}.jpg"
        try:
            data = blob_service.get_blob_client(container="inputs", blob=blob_name).download_blob().readall()
            refs.append(Image.open(io.BytesIO(data)).convert("RGB"))
            ids.append(f"img{i}.jpg")
        except Exception as e:
            write_debug(f"IP-Adapter ref img{i} unavailable: {e}")
    return refs, ids


def _refine_face(image, ref_face, face_prompt):
    """ADetailer-style face fix: locate the largest face, re-render JUST that crop at 1024
    (LoRA + IP-Adapter still active via the shared UNet), and blend it back with a feathered
    mask. This is what turns upscaler-mushy eyes/skin into sharp, exact detail. Graceful:
    returns the ORIGINAL image on any miss so it can never break a generation."""
    if img2img_pipe is None or _face_cascade is None:
        return image
    try:
        import cv2, numpy as np
        from PIL import Image as _Image, ImageFilter
        gray = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2GRAY)
        faces = _face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                                minSize=(96, 96))
        if len(faces) == 0:
            write_debug("face-refine: no face detected, using base image")
            return image
        x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
        # Asymmetric padding: reach further UP to capture the HAIR above the face box (Haar
        # only boxes eyes-to-chin), so the re-render produces real hair strands, not the
        # upscaler's plastic mush. Wider sides/bottom keep the blend seam off the face.
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
            # Push hard AGAINST the "AI plastic" look the user flagged on face + hair.
            negative_prompt=(os.environ.get("NEGATIVE_PROMPT", NEGATIVE_PROMPT)
                             + ", plastic skin, airbrushed, smooth waxy skin, cgi, 3d render, "
                               "doll, overprocessed, blurry hair"),
            image=crop_1024,
            strength=FACE_REFINE_STRENGTH,
            num_inference_steps=DEFAULT_STEPS,
            guidance_scale=DEFAULT_CFG,
        )
        if ref_face is not None and ip_adapter_ok:
            kwargs["ip_adapter_image"] = ref_face
        try:
            _r = img2img_pipe(**kwargs).images[0]
        except Exception as e_ip:
            # If IP-Adapter on the img2img path is unhappy, still do a LoRA-only face refine
            # (which alone fixes eye/skin detail) rather than skipping the whole thing.
            if "ip_adapter_image" in kwargs:
                write_debug(f"face-refine: img2img+IP failed ({e_ip}); retrying LoRA-only")
                kwargs.pop("ip_adapter_image")
                _r = img2img_pipe(**kwargs).images[0]
            else:
                raise
        refined = _r.resize((cw, ch), _Image.LANCZOS)
        # Feathered blend so the paste seam is invisible.
        b = max(1, int(0.12 * min(cw, ch)))
        m = np.zeros((ch, cw), dtype=np.uint8)
        m[b:ch - b, b:cw - b] = 255
        mask = _Image.fromarray(m).filter(ImageFilter.GaussianBlur(max(1, b // 2)))
        out = image.copy()
        out.paste(refined, (x0, y0), mask)
        write_debug(f"face-refine OK: crop ({x0},{y0})-({x1},{y1}) "
                    f"strength={FACE_REFINE_STRENGTH} ip={'yes' if (ref_face is not None and ip_adapter_ok) else 'no'}")
        return out
    except Exception as e:
        write_debug(f"face-refine FAILED (using base image): {e}")
        return image


def _realism_pass(image, ref_face, prompt):
    """Light WHOLE-IMAGE img2img over the upscaled frame to de-plasticize hair/skin/fabric
    that Real-ESRGAN over-smoothed. Runs at the 2048 resolution (A100 80GB has ample VRAM;
    the base pass peaked ~11GB). Low strength => identity, pose, attire, and background are
    preserved; only fine texture is rebuilt. Identity LoRA + IP-Adapter ride along on the
    shared UNet. Graceful: returns the input image on any miss so it can't break a job."""
    if img2img_pipe is None or REALISM_PASS_STRENGTH <= 0:
        return image
    try:
        kwargs = dict(
            prompt=prompt,
            negative_prompt=(os.environ.get("NEGATIVE_PROMPT", NEGATIVE_PROMPT)
                             + ", plastic skin, airbrushed, smooth waxy skin, cgi, 3d render, "
                               "digital painting, illustration, overprocessed, blurry hair, "
                               "smooth hair"),
            image=image,
            strength=REALISM_PASS_STRENGTH,
            num_inference_steps=DEFAULT_STEPS,
            guidance_scale=DEFAULT_CFG,
        )
        if ref_face is not None and ip_adapter_ok:
            kwargs["ip_adapter_image"] = ref_face
        try:
            out = img2img_pipe(**kwargs).images[0]
        except Exception as e_ip:
            if "ip_adapter_image" in kwargs:
                write_debug(f"realism-pass: img2img+IP failed ({e_ip}); retrying LoRA-only")
                kwargs.pop("ip_adapter_image")
                out = img2img_pipe(**kwargs).images[0]
            else:
                raise
        # img2img may return the native training size (1024); restore the 2K frame.
        if out.size != image.size:
            from PIL import Image as _Image
            out = out.resize(image.size, _Image.LANCZOS)
        write_debug(f"realism-pass OK: {image.size} strength={REALISM_PASS_STRENGTH} "
                    f"ip={'yes' if (ref_face is not None and ip_adapter_ok) else 'no'}")
        return out
    except Exception as e:
        write_debug(f"realism-pass FAILED (using upscaled image): {e}")
        return image


def _add_film_grain(image):
    """Add subtle monochrome sensor grain so the frame reads as a photo, not a clean render.
    Luminance-only noise (added equally to R/G/B) avoids color speckle. No-op if disabled."""
    if FILM_GRAIN_AMOUNT <= 0:
        return image
    try:
        import numpy as _np
        from PIL import Image as _Image
        arr = _np.asarray(image.convert("RGB"), dtype=_np.float32)
        noise = _np.random.normal(0.0, FILM_GRAIN_AMOUNT, arr.shape[:2])[..., None]
        out = _np.clip(arr + noise, 0, 255).astype(_np.uint8)
        return _Image.fromarray(out)
    except Exception as e:
        write_debug(f"film-grain FAILED (skipping): {e}")
        return image


# ─────────────────────────────────────────────────────────
# Core inference
# ─────────────────────────────────────────────────────────

def _sha256_file(path):
    """SHA-256 of a file, or None if unreadable. Used for the LoRA-adapter fingerprint
    in the run manifest so a run can be tied to the EXACT adapter it used."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def write_run_manifest(job_id, user_id, extra):
    """Emit a reproducibility manifest to outputs/manifests/<job_id>.json (0.5).

    Records what is needed to reproduce or audit a run: the exact image/commit, model
    versions + adapter checksum, per-stage init status (STAGE_STATUS), the IP-Adapter
    references actually used, sampling config, seeds, GPU, durations, and the DELIVERED
    resolution (read from the real output, not inferred). Fields that only build/deploy
    knows (image digest, git SHA, lockfile hash) are read from env and recorded as 'unset'
    when not wired, so a gap is visible rather than hidden. Best-effort: never fails a job."""
    try:
        try:
            gpu = torch.cuda.get_device_properties(0).name
        except Exception:
            gpu = "unknown"
        manifest = {
            "schema": "bettersnap.run_manifest/v1",
            "job_id": job_id,
            "user_id_sha256": hashlib.sha256((user_id or "").encode()).hexdigest(),
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "mode": os.environ.get("MODE", "infer"),
            # reproducibility — injected by build/deploy; 'unset' until wired (follow-up)
            "container_image": os.environ.get("CONTAINER_IMAGE", "unset"),
            "container_image_digest": os.environ.get("CONTAINER_IMAGE_DIGEST", "unset"),
            "git_commit_sha": os.environ.get("GIT_COMMIT_SHA", "unset"),
            "lockfile_sha256": os.environ.get("LOCKFILE_SHA256", "unset"),
            # models
            "base_model": os.environ.get("BASE_MODEL", "/models/sdxl-base"),
            "vae_model": os.environ.get("VAE_MODEL", "/models/sdxl-vae"),
            "lora_adapter_sha256": _sha256_file(f"/tmp/lora_identity_{user_id}.safetensors"),
            "lora_identity_weight": DEFAULT_LORA_WEIGHT,
            "identity_trigger": IDENTITY_TRIGGER,
            # ip-adapter (model + which refs were fetched vs used, from 0.3/0.4)
            "ip_adapter": {"model": "ip-adapter-plus-face_sdxl_vit-h.safetensors",
                           **extra.get("ref_meta", {})},
            # sampling
            "scheduler": extra.get("scheduler", "unknown"),
            "guidance_scale": DEFAULT_CFG,
            "num_inference_steps": DEFAULT_STEPS,
            "gen_resolution": [DEFAULT_WIDTH, DEFAULT_HEIGHT],
            "delivered_resolution": extra.get("delivered_size"),
            "negative_prompt": extra.get("negative_prompt"),
            "seeds": extra.get("seeds", []),
            "image_count": extra.get("image_count"),
            # stages (0.2) — enabled/initialized/reason per optional stage
            "stages": dict(STAGE_STATUS),
            # perf
            "gpu": gpu,
            "generation_seconds": extra.get("generation_seconds"),
        }
        body = json.dumps(manifest, indent=2, default=str).encode()
        blob_service.get_blob_client(
            container="outputs", blob=f"manifests/{job_id}.json").upload_blob(
            body, overwrite=True)
        write_debug(f"run manifest written: outputs/manifests/{job_id}.json")
    except Exception as e:
        write_debug(f"manifest write FAILED (non-fatal): {e}")


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
    # NOTE: category-style LoRAs are NOT deployed (no category/<x>/adapter_model.safetensors
    # blobs exist), and selections now span MULTIPLE categories (attire_refs/background_refs),
    # so there is no single `category` to load — the old `load_category_lora(category)` call
    # referenced an undefined name and crashed every generation. Identity LoRA below is the
    # only adapter carrying identity; attire/background are driven by the prompt.
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

    # Phase-5: optionally steer composition toward head-and-shoulders framing (default off,
    # so the baseline prompts are unchanged). Applied to BOTH the shared tail and negatives.
    _tail, _negative = apply_composition_control(_tail, _negative, COMPOSITION_CONTROL)
    if COMPOSITION_CONTROL:
        write_debug("Composition control ON: framing phrase + composition negatives appended")

    # ── IP-Adapter Plus-Face: condition on the user's OWN face crops (commercial-safe) ──
    # Applied to every image in this job (the reference face is constant per user). Scale
    # is env-tunable; on any miss we fall back to LoRA-only so generation never breaks.
    use_ip_adapter = False
    ref_faces = []
    ref_idx = 0    # which fetched crop feeds IP-Adapter (Phase-3); set below when active
    ref_meta = {"fetched": [], "used": None, "scale": IP_ADAPTER_SCALE}   # for the manifest
    if ip_adapter_ok and IP_ADAPTER_SCALE > 0:
        ref_faces, ref_ids = _get_ref_faces(user_id)
        if not ref_faces:
            # 0.3: IP-Adapter is enabled but its reference crops are gone (retention deletes
            # the input prefix). FAIL CLEARLY — the old code silently became LoRA-only here,
            # changing output quality with no signal. __main__ catches this and marks the job
            # failed (refunds). NOTE: the DB has no error-reason column yet, so the reason is
            # recorded to the debug blob + run manifest; adding jobs.error_reason (migration)
            # is a tracked follow-up.
            raise IpAdapterReferenceUnavailable(
                f"IP-Adapter enabled (scale={IP_ADAPTER_SCALE}) but NO reference face crops "
                f"exist for user_id={user_id} at inputs/{user_id}/input/crop_upperbody/ "
                f"(retention may have deleted them). Refusing to silently fall back to "
                f"LoRA-only. code={IpAdapterReferenceUnavailable.code}"
            )
        pipe.set_ip_adapter_scale(IP_ADAPTER_SCALE)
        use_ip_adapter = True
        # Phase-3: pick which fetched crop to use. Default 0 (strategy A). Clamp so an
        # out-of-range index can never IndexError — fall back to the first crop.
        ref_idx = IP_ADAPTER_REF_INDEX
        if ref_idx < 0 or ref_idx >= len(ref_faces):
            write_debug(f"IP_ADAPTER_REF_INDEX={ref_idx} out of range for "
                        f"{len(ref_faces)} crops -> using 0")
            ref_idx = 0
        ref_meta["fetched"] = ref_ids
        ref_meta["used"] = ref_ids[ref_idx]
        ref_meta["used_index"] = ref_idx
        ref_meta["strategy"] = "A_first" if ref_idx == 0 else "B_selected"
        # 0.4 + Phase-3: HONEST log. We fetch up to 3 crops but pass exactly ONE to the
        # pipeline. The old log said "3 ref faces", implying all 3 were used. Multi-image
        # averaging remains a separate Phase-3 arm.
        write_debug(
            f"IP-Adapter ACTIVE: fetched {len(ref_faces)} crop(s) {ref_ids}; "
            f"USING 1 (index {ref_idx} = {ref_ids[ref_idx]}); scale={IP_ADAPTER_SCALE}"
        )

    result_blob_paths = []
    _gen_t0 = time.time()          # generation wall-clock, for the run manifest (0.5)
    _delivered_size = None         # actual delivered resolution, read from real output
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
            _pipe_kwargs = dict(
                prompt=prompt,
                negative_prompt=_negative,
                guidance_scale=DEFAULT_CFG,
                num_inference_steps=DEFAULT_STEPS,
                width=DEFAULT_WIDTH,
                height=DEFAULT_HEIGHT,
                generator=torch.Generator("cuda").manual_seed(seed),
            )
            if use_ip_adapter:
                # Single reference face (the clearest crop) — safest API path; multi-image
                # averaging is a later enhancement.
                _pipe_kwargs["ip_adapter_image"] = ref_faces[ref_idx]
            output = pipe(**_pipe_kwargs).images[0]

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

            # 2K post-process FIRST (Real-ESRGAN x4 → fit to UPSCALE_TARGET).
            _pre = output.size
            output    = upscale_image(output)
            if i == 0:
                write_debug(f"Upscaled {_pre} -> {output.size} (target={UPSCALE_TARGET})")

            # ── Whole-image realism pass AFTER upscale: de-plasticize the WHOLE frame (hair
            # top, skin, fabric) that ESRGAN over-smoothed. Runs before the face-refine so the
            # face gets the final, sharpest word. Low strength => identity/pose preserved.
            _ref = ref_faces[ref_idx] if (use_ip_adapter and ref_faces) else None
            if REALISM_PASS_ENABLE and img2img_pipe is not None:
                _realism_prompt = (
                    f"candid photograph of {subj}, natural realistic skin texture with visible "
                    f"pores, individual hair strands, fine fabric texture, {_tail}"
                )
                output = _realism_pass(output, _ref, _realism_prompt)

            # ── Face-inpaint AFTER upscale: on the 2048 image the face crop is ~1000px (vs
            # ~500 pre-upscale) so the eyes get REAL detail, and the ESRGAN — which already
            # ran — can no longer re-smooth the refined face (the bug that made v43 only
            # marginally better). Graceful: falls back to the un-refined image on any miss.
            if FACE_REFINE_ENABLE and img2img_pipe is not None:
                _face_prompt = (
                    f"close-up portrait photograph of {subj}, face in sharp focus, highly "
                    f"detailed eyes, natural realistic skin texture with visible pores and fine "
                    f"detail, individual hair strands, {_tail}"
                )
                output = _refine_face(output, _ref, _face_prompt)

            # Subtle film grain so the final frame reads as a photo, not a clean render.
            output    = _add_film_grain(output)
            output    = add_watermark(output)
            _delivered_size = list(output.size)          # actual, not inferred (0.5)
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

    # 0.5: reproducibility manifest for this run (best-effort; never fails the job).
    write_run_manifest(job_id, user_id, {
        "ref_meta": ref_meta,
        "scheduler": type(pipe.scheduler).__name__,
        "negative_prompt": _negative,
        "seeds": [1000 + i for i in range(image_count)],
        "image_count": image_count,
        "delivered_size": _delivered_size,
        "generation_seconds": round(time.time() - _gen_t0, 1),
    })
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