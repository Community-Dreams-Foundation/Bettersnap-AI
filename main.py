import os
import io
import json
import time
import logging
import faulthandler

faulthandler.enable()   # dumps C++ stack to stderr on SIGSEGV / SIGABRT / SIGFPE / SIGBUS
from datetime import datetime, timezone

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
# Real-ESRGAN generator (vendored, BSD-3-Clause — COMMERCIAL-SAFE). 2K upscale
# post-process. do NOT swap to a non-commercial upscaler.
from rrdbnet import RRDBNet
from stage_runtime import run_stage
from prompt_control import apply_composition_control
from azure.keyvault.secrets import SecretClient

from azure.storage.blob import BlobServiceClient
from azure.identity import DefaultAzureCredential
from azure.communication.email import EmailClient

# ── Logging ───────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Environment Variables ─────────────────────────────────
AZURE_STORAGE_ACCOUNT   = os.environ.get("AZURE_STORAGE_ACCOUNT", "bettersnapaistorage")
AZURE_BLOB_CONTAINER    = os.environ.get("AZURE_BLOB_CONTAINER", "outputs")
# LoRA-weights container. AZURE_LORA_CONTAINER is CANONICAL (matches AZURE_BLOB_CONTAINER /
# AZURE_STORAGE_ACCOUNT and the ACA job manifest job.yaml). LORA_CONTAINER is a DEPRECATED
# alias kept for the trainer's older convention: read both (canonical first) so an operator can
# set EITHER and the trainer + inference always agree on one container. Remove the alias once
# deploy manifests + docs use AZURE_LORA_CONTAINER everywhere (see run_training.py for the twin).
AZURE_LORA_CONTAINER    = (os.environ.get("AZURE_LORA_CONTAINER")
                           or os.environ.get("LORA_CONTAINER") or "lora-weights")
if not os.environ.get("AZURE_LORA_CONTAINER") and os.environ.get("LORA_CONTAINER"):
    log.warning("LORA_CONTAINER is DEPRECATED; use AZURE_LORA_CONTAINER instead.")
SQL_SERVER              = os.environ.get("SQL_SERVER", "bettersnap-srv.database.windows.net")
SQL_DATABASE            = os.environ.get("SQL_DATABASE", "bettersnap-db")
KEY_VAULT_URL           = "https://bettersnapkeyvault.vault.azure.net/"

# ── Azure Clients ─────────────────────────────────────────
credential = DefaultAzureCredential()

blob_service = BlobServiceClient(
    account_url=f"https://{AZURE_STORAGE_ACCOUNT}.blob.core.windows.net",
    credential=credential
)

# ── Key Vault helper ──────────────────────────────────────
# Cache the SecretClient + fetched values in-process (mirrors shared/keyvault.py): rebuilding
# the client and re-reading Key Vault on every call is a wasteful round-trip, and secrets change
# rarely, so a short TTL is safe. The inference container runs ONE job single-threaded, so no
# locking is needed (unlike the multi-threaded Functions host the backend guards).
_kv_client = None
_secret_cache: "dict[str, tuple[float, str]]" = {}
_SECRET_TTL = int(os.environ.get("KEYVAULT_CACHE_TTL_SECONDS", "600"))


def get_secret(name: str) -> str:
    global _kv_client
    now = time.time()
    cached = _secret_cache.get(name)
    if cached and (now - cached[0]) < _SECRET_TTL:
        return cached[1]
    if _kv_client is None:
        _kv_client = SecretClient(vault_url=KEY_VAULT_URL, credential=credential)
    value = _kv_client.get_secret(name).value
    _secret_cache[name] = (now, value)
    return value

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
        blob_client = blob_service.get_blob_client(container=AZURE_BLOB_CONTAINER, blob=blob_name)
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
        # VAE is env-configurable (default = baked fp16-fix VAE), mirroring BASE_MODEL
        # below — so the actual load matches what the run manifest records (which already
        # reads VAE_MODEL). Without this, setting VAE_MODEL changed ONLY the manifest.
        _vae_path = os.environ.get("VAE_MODEL", "/models/sdxl-vae")
        vae = AutoencoderKL.from_pretrained(
            _vae_path,
            torch_dtype=torch.float16,
        )
        write_debug(f"vae model: {_vae_path}")
        # Pure txt2img: identity comes from the per-user LoRA loaded per job, the
        # scene from the prompt. No source photo, no ControlNet at inference.
        # Base checkpoint is env-configurable (default = baked SDXL 1.0). Lets an
        # experimental image point at an alternate baked base (e.g. /models/realvis)
        # WITHOUT changing default/production behavior. Also makes the actual load match
        # what the run manifest records (which already read BASE_MODEL).
        _base = os.environ.get("BASE_MODEL", "/models/sdxl-base")
        pipe = StableDiffusionXLPipeline.from_pretrained(
            _base,
            vae=vae,
            torch_dtype=torch.float16,
            variant="fp16",
            use_safetensors=True,
        )
        write_debug(f"base model: {_base}")
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
                # non-terminal (e.g. 'processing') — do NOT stamp completed_at, and GUARD it
                # like the terminal paths: a job the reaper already drove to 'failed' (and
                # refunded) must NOT be flipped back to 'processing' — that reopens the
                # double-credit race (user keeps the refund AND the delivered images). If the
                # row is already terminal this is a no-op (rowcount 0). (Audit FF2)
                cursor.execute("""
                    UPDATE jobs
                    SET status = ?, output_blob_path = ?
                    WHERE job_id = ? AND status NOT IN ('failed', 'completed')
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
        blob_name = f"{user_id}/{catalog.CROP_SUBDIR}/img{i}.jpg"
        try:
            data = blob_service.get_blob_client(container="inputs", blob=blob_name).download_blob().readall()
            refs.append(Image.open(io.BytesIO(data)).convert("RGB"))
            ids.append(f"img{i}.jpg")
        except Exception as e:
            write_debug(f"IP-Adapter ref img{i} unavailable: {e}")
    return refs, ids


# ─────────────────────────────────────────────────────────
# Core inference
# ─────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────
# Phase 2 orchestrator (engine-based) — ARCHITECTURE.md §4/§7
# ─────────────────────────────────────────────────────────

def run_inference(job: dict) -> list:
    """Phase 2 orchestrator — owns the workflow; engines are leaves (none calls another).

    Behavior matches the pre-refactor implementation on the DETERMINISTIC subset (verified
    pixel-identical, txt2img + upscale): the
    per-image prompt/generation/enhancement/delivery code was MOVED into the engines, not
    changed. Model setup (identity LoRA, adapters, IP-Adapter, reference faces) runs in the SAME
    order as legacy, BEFORE any engine. Enhancement stays non-deterministic exactly as before."""
    import sys
    from domain import (GenerationPlan, Plan, PlanType, CategoryRule, GenerationJob,
                        ScoredCandidate, Winner, Scores, Ref, RefKind)
    from runtime import PipelineContext
    from runtime.engines.prompt_sdxl import SdxlPromptEngine
    from runtime.engines.generation_sdxl import SdxlGenerationEngine
    from runtime.engines.enhancement_sdxl import SdxlEnhancementEngine
    from runtime.engines.delivery_blob import BlobDeliveryEngine

    cfg = sys.modules[__name__]
    job_id     = job["job_id"]
    user_id    = job["user_id"]
    job_params = json.loads(job.get("job_params", "{}"))

    attire_refs     = job_params.get("attire_ids") or []
    background_refs = job_params.get("background_ids") or []
    custom_prompt   = (job_params.get("custom_prompt") or "").strip()
    image_count     = int(job_params.get("image_count") or DEFAULT_NUM_OUTPUTS)
    _test_cap = int(os.environ.get("IMAGE_COUNT_OVERRIDE", "0") or "0")
    if _test_cap > 0:
        image_count = min(image_count, _test_cap)
    image_count = max(1, min(image_count, 100))
    write_debug(f"[orchestrator] job={job_id} image_count={image_count} "
                f"custom={'yes' if custom_prompt else 'no'}")

    # ── model setup — SAME ORDER as legacy (identity LoRA -> adapters -> IP) ──
    active, weights = [], []
    identity_ok = load_identity_lora(user_id)
    if identity_ok:
        active.append("identity_lora"); weights.append(DEFAULT_LORA_WEIGHT)
    if not identity_ok:
        raise RuntimeError(
            f"identity LoRA missing for user_id={user_id} "
            f"(lora-weights/identity/{user_id}/adapter_model.safetensors) — refusing to "
            f"render base SDXL and pass off a generic face as this user's headshots"
        )
    pipe.set_adapters(active, adapter_weights=weights)
    write_debug(f"LoRA adapters ACTIVE: {active} weights={weights}")

    use_ip_adapter = False
    ref_faces = []
    ref_idx = 0
    ref_meta = {"fetched": [], "used": None, "scale": IP_ADAPTER_SCALE}
    if ip_adapter_ok and IP_ADAPTER_SCALE > 0:
        ref_faces, ref_ids = _get_ref_faces(user_id)
        if not ref_faces:
            raise IpAdapterReferenceUnavailable(
                f"IP-Adapter enabled (scale={IP_ADAPTER_SCALE}) but NO reference face crops "
                f"exist for user_id={user_id} at inputs/{user_id}/{catalog.CROP_SUBDIR}/ "
                f"(retention may have deleted them). Refusing to silently fall back to "
                f"LoRA-only. code={IpAdapterReferenceUnavailable.code}"
            )
        pipe.set_ip_adapter_scale(IP_ADAPTER_SCALE)
        use_ip_adapter = True
        ref_idx = IP_ADAPTER_REF_INDEX
        if ref_idx < 0 or ref_idx >= len(ref_faces):
            write_debug(f"IP_ADAPTER_REF_INDEX={ref_idx} out of range for "
                        f"{len(ref_faces)} crops -> using 0")
            ref_idx = 0
        ref_meta["fetched"] = ref_ids
        ref_meta["used"] = ref_ids[ref_idx]
        ref_meta["used_index"] = ref_idx
        ref_meta["strategy"] = "A_first" if ref_idx == 0 else "B_selected"
        write_debug(
            f"IP-Adapter ACTIVE: fetched {len(ref_faces)} crop(s) {ref_ids}; "
            f"USING 1 (index {ref_idx} = {ref_ids[ref_idx]}); scale={IP_ADAPTER_SCALE}"
        )

    # ── resolved plan + runtime context ──────────────────────────────────────
    _plan = Plan(   # worker-side reconstruction; billing fields UNUSED here (control plane owns billing)
        key=(job_params.get("plan_name") or "unknown"),
        plan_type=PlanType.ONE_TIME, image_count=image_count, credits_per_image=1,
        max_attires=99, max_backgrounds=99, category_rule=CategoryRule.MIXABLE,
    )
    plan = GenerationPlan(
        user_id=user_id, job_id=job_id, plan=_plan,
        billable_count=image_count, credit_cost=0, candidate_budget=image_count,
        acceptance_threshold=0.0, retry_limit=0,
        gender=(job_params.get("gender") or ""),
        age_range=(job_params.get("age_range") or ""),
        hair_color=(job_params.get("hair_color") or ""),
        attire_refs=tuple(attire_refs), background_refs=tuple(background_refs),
        custom_prompt=custom_prompt,
    )

    ctx = PipelineContext(job_id, user_id)
    ctx.models["pipe"] = pipe
    ctx.models["upscaler"] = upscaler
    ctx.models["img2img"] = img2img_pipe
    ctx.models["face_cascade"] = _face_cascade
    _ref = ref_faces[ref_idx] if (use_ip_adapter and ref_faces) else None
    ctx.work["ip_adapter_ok"] = ip_adapter_ok
    ctx.work["use_ip_adapter"] = use_ip_adapter
    ctx.work["ip_adapter_image"] = _ref
    ctx.work["ref_face"] = _ref

    # ── run: prompt -> generate -> [Phase-6 wrap] -> enhance -> deliver ───────
    _gen_t0 = time.time()
    prompt_engine = SdxlPromptEngine(ctx, cfg, write_debug)
    gen_engine    = SdxlGenerationEngine(ctx, cfg, write_debug)
    enh_engine    = SdxlEnhancementEngine(ctx, cfg, write_debug)
    del_engine    = BlobDeliveryEngine(ctx, cfg, blob_service, STAGE_STATUS, write_debug)

    prompts    = prompt_engine.build(plan)
    adapter    = Ref(RefKind.ADAPTER, f"/tmp/lora_identity_{user_id}.safetensors")
    candidates = gen_engine.generate(prompts, adapter)

    # Phase-2 pass-through: no Quality Gate yet — every candidate becomes a winner (accepted).
    winners = [
        Winner(ScoredCandidate(c, Scores(identity=0.0), accepted=True),
               slot_id=(c.slot_id if c.slot_id is not None else i))
        for i, c in enumerate(candidates)
    ]
    finals = enh_engine.enhance(winners)

    # delivered_size read from the actual last final image (parity with legacy).
    _delivered_size = None
    if finals:
        _last = ctx.images[finals[-1].output_ref.location]
        _delivered_size = list(_last.size)

    # manifest extra — SAME fields the legacy write_run_manifest call passed.
    ctx.work["manifest_extra"] = {
        "ref_meta": ref_meta,
        "scheduler": type(pipe.scheduler).__name__,
        "negative_prompt": ctx.work.get("negative_prompt"),
        "seeds": [1000 + i for i in range(image_count)],
        "image_count": image_count,
        "delivered_size": _delivered_size,
        "generation_seconds": round(time.time() - _gen_t0, 1),
    }
    gjob = GenerationJob(job_id=job_id, user_id=user_id)
    return del_engine.deliver(finals, gjob)


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