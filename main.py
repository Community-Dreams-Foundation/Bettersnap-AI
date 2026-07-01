import os
import io
import json
import time
import logging
import traceback
import requests
from datetime import datetime, timedelta, timezone
from PIL import Image, ImageDraw, ImageFont

import torch
import pyodbc
from diffusers import FluxKontextPipeline
from azure.keyvault.secrets import SecretClient

from azure.storage.queue import QueueClient, TextBase64DecodePolicy
from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions
from azure.identity import DefaultAzureCredential

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

# ── Global pipeline ───────────────────────────────────────
pipe = None

# ── Backgrounds ───────────────────────────────────────────
BACKGROUNDS = {
    "white":      "pure white studio background",
    "gray":       "neutral gray studio background",
    "light_grey": "soft light grey studio background",
    "office":     "modern corporate office background with glass walls",
    "black":      "dark black studio background",
    "Clean White":      "pure white studio background",
    "Soft Light Gray":  "soft light grey studio background",
    "Neutral Gray":     "neutral gray studio background",
    "Dark Charcoal":    "dark charcoal studio background",
    "Black Studio":     "dark black studio background",
    "Modern Office":    "modern corporate office background with glass walls",
    "Corporate Lobby":  "polished corporate lobby background",
    "Co-working Space": "modern co-working space background",
    "Executive Office": "executive office background",
    "Library or Academic": "library or academic background",
    "University Campus":   "university campus background",
    "Outdoor Professional": "outdoor professional background with natural light",
    "City Background":  "city skyline background",
    "Warm Studio":      "warm toned studio background",
    "Soft Gradient":    "soft gradient background",
}

# ── Age map ───────────────────────────────────────────────
AGE_MAP = {
    "18-20": "young adult",
    "21-24": "young professional in their early twenties",
    "25-29": "professional in their late twenties",
    "30-40": "professional in their thirties",
    "41-50": "experienced professional in their forties",
    "51-65": "senior professional in their fifties to mid-sixties",
    "65+":   "distinguished senior professional",
}

# ── Prompt templates ──────────────────────────────────────
PROMPT_TEMPLATES = [
    "Change the background to {bg}. Dress the person in a {attire}. Keep the person's exact face, skin tone{descriptor}. Soft front-facing studio lighting, classic professional headshot.",
    "Change the background to {bg}. Dress the person in a {attire}. Keep the person's exact face, skin tone{descriptor}. Warm Rembrandt lighting from the left side, slight body turn, rich and dramatic portrait.",
    "Change the background to {bg}. Dress the person in a {attire}. Keep the person's exact face, skin tone{descriptor}. Cool natural window light from the right, slightly wider framing, clean and modern headshot.",
    "Change the background to {bg}. Dress the person in a {attire}. Keep the person's exact face, skin tone{descriptor}. High contrast dramatic studio lighting, strong shadows, bold executive portrait style.",
]

SEEDS           = [42, 1337, 9999, 77777]
GUIDANCE_SCALES = [1.5, 2.5, 3.0, 3.5]


# ─────────────────────────────────────────────────────────
# Model loading
# ─────────────────────────────────────────────────────────

def load_base_model():
    global pipe
    if pipe is not None:
        return

    write_debug("START: load_base_model called")

    try:
        files = os.listdir("/models")
        write_debug(f"/models contents: {files}")
    except Exception as e:
        write_debug(f"/models listdir ERROR: {e}")

    try:
        write_debug("Calling FluxKontextPipeline.from_pretrained('/models')...")
        # NOTE: do NOT call .to("cuda") here. enable_model_cpu_offload() takes over
        # device placement: each component (transformer, T5, CLIP, VAE) lives on CPU
        # and is paged to GPU only while it runs, then offloaded. This keeps the ~10GB
        # T5+CLIP+VAE out of VRAM during the transformer denoising loop (the v15 OOM was
        # all components co-resident on GPU at pipe() start). vae.enable_tiling() caps the
        # VAE encode/decode peak. Calling .to("cuda") would conflict with the offload hooks.
        pipe = FluxKontextPipeline.from_pretrained(
            "/models",
            torch_dtype=torch.bfloat16,
            local_files_only=True,
        )
        pipe.enable_model_cpu_offload()
        pipe.vae.enable_tiling()

        # One-time VRAM sanity check: confirm the container actually sees the full A100
        # 80GB. A single 1024 Kontext image (~34GB weights) OOMing an 80GB card would mean
        # usable VRAM is lower than nominal — something beyond co-residency.
        try:
            props = torch.cuda.get_device_properties(0)
            total_gb = props.total_memory / 1024**3
            peak_gb  = torch.cuda.max_memory_allocated(0) / 1024**3
            msg = (f"GPU={props.name} total_memory={props.total_memory} "
                   f"({total_gb:.1f} GB), max_memory_allocated={torch.cuda.max_memory_allocated(0)} "
                   f"({peak_gb:.1f} GB)")
            log.info(f"🔎 {msg}")
            write_debug(msg)
        except Exception as e:
            write_debug(f"VRAM sanity check failed: {e}")

        write_debug("SUCCESS: Base model loaded")
        log.info("✅ Base model loaded")
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
    """Download + register the identity LoRA. Returns True if the adapter was
    loaded, False otherwise. Does NOT call set_adapters (the old code hardcoded
    ['category_lora', 'identity_lora'] here and crashed when category was absent);
    the caller activates only the adapters that actually loaded."""
    lora_path = f"/tmp/lora_identity_{user_id}.safetensors"
    blob_name  = f"identity/{user_id}/adapter_model.safetensors"
    try:
        blob_client = blob_service.get_blob_client(container=AZURE_LORA_CONTAINER, blob=blob_name)
        with open(lora_path, "wb") as f:
            f.write(blob_client.download_blob().readall())
        pipe.load_lora_weights(lora_path, adapter_name="identity_lora")
        log.info(f"✅ Identity LoRA loaded: {user_id}")
        return True
    except Exception as e:
        log.warning(f"⚠️ Identity LoRA not found for '{user_id}': {e}")
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


def resize_for_kontext(img: Image.Image) -> Image.Image:
    target = 1024
    w, h   = img.size
    ratio  = min(target / w, target / h)
    new_w  = (int(w * ratio) // 16) * 16
    new_h  = (int(h * ratio) // 16) * 16
    return img.resize((new_w, new_h), Image.LANCZOS)


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

    - 'failed': refund one credit, tied to the ACTUAL transition (WHERE status
      NOT IN ('failed','completed') + rowcount) so retries / the backend ALSO
      failing the job can never double-refund.
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
                if transitioned:
                    cursor.execute("""
                        UPDATE users
                        SET credits_remaining = credits_remaining + 1
                        WHERE user_id = (SELECT user_id FROM jobs WHERE job_id = ?)
                    """, job_id)
                conn.commit()
                log.info(
                    f"✅ Job {job_id} -> 'failed' "
                    f"(transitioned={transitioned}, credit_refunded={transitioned})"
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
# Core inference
# ─────────────────────────────────────────────────────────

def run_inference(job: dict) -> list:
    job_id       = job["job_id"]
    user_id      = job["user_id"]
    job_params   = json.loads(job.get("job_params", "{}"))

    category     = job_params.get("purpose", "linkedin")
    gender       = job_params.get("gender", "male")
    age_range    = job_params.get("age_range", "")
    hair_color   = job_params.get("hair_color", "")
    background   = job_params.get("background", "light_grey")
    attire       = job_params.get("attire", "navy blue professional business suit with a white shirt")
    input_blob   = job_params.get("input_blob_path", "")
    num_images   = job_params.get("num_images", 4)

    # Canonical convention: input_blob_path is "<container>/<blob>" (e.g.
    # "inputs/<user_id>/input/<file>"), matching what /upload returns.
    if not input_blob or "/" not in input_blob:
        raise ValueError(
            f"input_blob_path must be '<container>/<blob>', got: {input_blob!r}"
        )

    descriptors = []
    if hair_color:
        descriptors.append(f"{hair_color.strip().lower()} hair")
    if age_range and age_range in AGE_MAP:
        descriptors.append(AGE_MAP[age_range])
    descriptor = (", " + ", ".join(descriptors)) if descriptors else ""

    bg = BACKGROUNDS.get(background, "soft light grey studio background")

    container_name, blob_name = input_blob.split("/", 1)
    user_image = load_image_from_blob(container_name, blob_name)
    user_image = resize_for_kontext(user_image)
    log.info(f"🖼️ Image size: {user_image.size}")

    # ── Per-user/category LoRA: DISABLED (inference-only, no LoRA weights yet).
    # Base FLUX runs with no adapters. Re-enable the block below when LoRA
    # weights exist. set_adapters is built from what actually loaded, so a
    # missing/partial load can never reference an unloaded adapter name.
    # active, weights = [], []
    # if load_category_lora(category):
    #     active.append("category_lora"); weights.append(0.8)
    # if load_identity_lora(user_id):
    #     active.append("identity_lora"); weights.append(0.6)
    # if active:
    #     pipe.set_adapters(active, adapter_weights=weights)

    result_blob_paths = []
    for i in range(num_images):
        prompt = PROMPT_TEMPLATES[i % len(PROMPT_TEMPLATES)].format(
            bg=bg, attire=attire, descriptor=descriptor
        )
        log.info(f"📣 Variation {i+1} prompt: {prompt}")
        try:
            output = pipe(
                prompt=prompt,
                image=user_image,
                guidance_scale=GUIDANCE_SCALES[i % len(GUIDANCE_SCALES)],
                num_inference_steps=20,
                generator=torch.Generator("cuda").manual_seed(SEEDS[i % len(SEEDS)]),
            ).images[0]

            # Real inference VRAM peak: after the first pipe() completes, max_memory_allocated
            # reflects the actual denoising peak (post-load it was ~0 under cpu_offload). If this
            # is well under total_memory the OOM was pure co-residency; if it's near the ceiling,
            # usable VRAM is the real limit (consider 768 res or sequential offload).
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

            output    = add_watermark(output)
            blob_path = upload_image_to_blob(output, job_id, i)
            result_blob_paths.append(blob_path)
            log.info(f"✅ Variation {i+1} complete")
        except Exception:
            # Do NOT swallow this. The old code logged only str(e) and let the
            # loop continue, so run_inference returned a short/empty list that
            # __main__ then marked 'completed' — the v14 silent crash. Log the
            # FULL traceback and re-raise so __main__ marks the job FAILED and
            # the next GPU run finally surfaces the real inference error.
            log.error(
                f"❌ Variation {i+1} FAILED — full traceback:\n{traceback.format_exc()}"
            )
            raise

    # unload_loras()  # DISABLED with the LoRA loading above; no adapters to unload.
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