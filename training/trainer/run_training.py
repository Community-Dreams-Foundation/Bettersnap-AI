"""
Phase-2b SDXL identity-LoRA trainer — DreamBooth + prior preservation.

Supersedes the caption-based text-to-image trainer, which bound `ohwx` to the
WHOLE training image: at a scale high enough to render the subject's face it also
dragged in the training backgrounds and clothing, so the prompt lost control of
attire/scene. Prior preservation anchors the class term against generated class
images, forcing the rare token to carry only what distinguishes the subject from
"a woman" -- i.e. the face.

Flow inside a throwaway ACA GPU job (does NOT touch the inference image):
  1. download N pre-cropped instance photos from blob (FILES_JSON; captions unused)
  2. train UNet + text-encoder LoRA with diffusers train_dreambooth_lora_sdxl.py,
     --with_prior_preservation (class images generated in-container, first run)
  3. FORMAT GATE: reload base SDXL + load adapter (reproduces main.py load path);
     FAIL (no upload) on any key-mismatch warning or inactive adapter
  4. upload as lora-weights/identity/<USER_ID>/adapter_model.safetensors

No --random_flip: mirroring destroys asymmetric identity cues (nose stud, smile).

Env:
  STORAGE_CONNECTION_STRING, USER_ID, INPUT_CONTAINER=inputs,
  FILES_JSON = [{"blob":"<path-in-container>","caption":"<ignored>"}...],
  INSTANCE_PROMPT="a photo of ohwx woman"  CLASS_PROMPT="a photo of a woman"
  NUM_CLASS_IMAGES=200  PRIOR_LOSS_WEIGHT=1.0
  RANK=32  MAX_TRAIN_STEPS=1400  LEARNING_RATE=1e-4  TEXT_ENCODER_LR=5e-5
  LORA_CONTAINER=lora-weights
"""
import os, sys, json, glob, subprocess, logging, re, time, threading, datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("trainer")

def done(status, detail):
    print(f"::TRAINER_RESULT::{status}::{detail}", flush=True)
    sys.exit(0 if status == "SUCCESS" else 1)
def fail(msg):
    log.error(f"TRAINER FAIL: {msg}"); done("FAIL", msg)

try:
    CONN     = os.environ["STORAGE_CONNECTION_STRING"]
    USER_ID  = os.environ["USER_ID"]
    FILES    = json.loads(os.environ["FILES_JSON"])
except (KeyError, ValueError) as e:
    fail(f"bad/missing env: {e}")
IN_CONTAINER   = os.environ.get("INPUT_CONTAINER", "inputs")
# AZURE_LORA_CONTAINER is the CANONICAL name (see inference/main.py + job.yaml); LORA_CONTAINER
# is a DEPRECATED alias. Read canonical-first so the trainer and inference always resolve to the
# SAME container even if only one name is set on the job. (job.yaml sets AZURE_LORA_CONTAINER, so
# prod already resolves to canonical and no warning fires.)
LORA_CONTAINER = (os.environ.get("AZURE_LORA_CONTAINER")
                  or os.environ.get("LORA_CONTAINER") or "lora-weights")
if not os.environ.get("AZURE_LORA_CONTAINER") and os.environ.get("LORA_CONTAINER"):
    log.warning("LORA_CONTAINER is DEPRECATED; use AZURE_LORA_CONTAINER instead.")
RANK   = os.environ.get("RANK", "32")
STEPS  = os.environ.get("MAX_TRAIN_STEPS", "1400")
LR     = os.environ.get("LEARNING_RATE", "1e-4")
TE_LR  = os.environ.get("TEXT_ENCODER_LR", "5e-5")
INSTANCE_PROMPT  = os.environ.get("INSTANCE_PROMPT", "a photo of ohwx woman")
CLASS_PROMPT     = os.environ.get("CLASS_PROMPT", "a photo of a woman")
NUM_CLASS_IMAGES = os.environ.get("NUM_CLASS_IMAGES", "200")
PRIOR_W          = os.environ.get("PRIOR_LOSS_WEIGHT", "1.0")
# woman | man | person — the cache key for the class images (see below).
CLASS_WORD       = os.environ.get("CLASS_WORD", "woman").strip().lower()
# CLASS_WORD chooses the shared Blob prefix, while CLASS_PROMPT generates the images
# stored beneath it. A mismatch poisons every later cache hit (for example, male images
# uploaded under class/woman/). Validate before any filesystem, Blob, cache, or GPU work.
_VALID_CLASS_WORDS = {"woman", "man", "person"}
_norm_prompt = lambda value: " ".join((value or "").strip().lower().split())
_expected_class_prompt = f"a photo of a {CLASS_WORD}"
_expected_instance_prompt = f"a photo of ohwx {CLASS_WORD}"
if CLASS_WORD not in _VALID_CLASS_WORDS:
    fail(
        f"invalid CLASS_WORD={CLASS_WORD!r}; expected one of "
        f"{sorted(_VALID_CLASS_WORDS)}"
    )
if _norm_prompt(CLASS_PROMPT) != _expected_class_prompt:
    fail(
        f"CLASS_PROMPT/CLASS_WORD mismatch: CLASS_WORD={CLASS_WORD!r} requires "
        f"CLASS_PROMPT={_expected_class_prompt!r}, got {CLASS_PROMPT!r}; "
        "refusing to read or publish the shared class-image cache"
    )
if _norm_prompt(INSTANCE_PROMPT) != _expected_instance_prompt:
    fail(
        f"INSTANCE_PROMPT/CLASS_WORD mismatch: CLASS_WORD={CLASS_WORD!r} requires "
        f"INSTANCE_PROMPT={_expected_instance_prompt!r}, got {INSTANCE_PROMPT!r}"
    )
# Gradient checkpointing recomputes activations in the backward pass instead of
# storing them: ~30-40% SLOWER, much less VRAM. It is on by default because that is
# the safe assumption for SDXL@1024 — but we run on an 80GB A100 where inference peaks
# at 9GB, so there is very likely room to just keep the activations. Turning it off
# changes NO math (identical gradients, identical adapter) — only speed vs memory.
# Flip to 0 once PEAK TRAIN VRAM (logged below) shows the headroom.
GRAD_CKPT        = os.environ.get("GRADIENT_CHECKPOINTING", "1").strip() != "0"
# Base model + VAE. Default to the copies BAKED INTO THE IMAGE (the unified train+infer
# image), so training does NOT re-download SDXL from HuggingFace at runtime. The live
# download had no timeout and could silently hang the A100 for hours (stalled at 4/19
# files) — reading the baked weights from disk removes that failure mode entirely.
# Overridable via env for a standalone/dev run that wants to pull from the Hub.
BASE_MODEL    = os.environ.get("BASE_MODEL", "/models/sdxl-base")
VAE_MODEL     = os.environ.get("VAE_MODEL", "/models/sdxl-vae")
MODEL_VARIANT = os.environ.get("MODEL_VARIANT", "fp16")

# WORK holds the training scripts baked into the image (read-only is fine). The instance
# data, class images and LoRA output must go to a WRITABLE dir — in the unified train+infer
# image /workspace is NOT writable at runtime (it's a COPY target, not a WORKDIR), so the
# data/output dirs live under /tmp. Overridable via TRAIN_RUNDIR.
WORK   = "/workspace"
RUNDIR = os.environ.get("TRAIN_RUNDIR", "/tmp/bettersnap_train")
DATA   = f"{RUNDIR}/instance_data"
CLASS  = f"{RUNDIR}/class_data"
OUT    = f"{RUNDIR}/lora_out"
for d in (DATA, CLASS, OUT):
    os.makedirs(d, exist_ok=True)
log.info(f"CONFIG user={USER_ID} n_files={len(FILES)} rank={RANK} steps={STEPS} "
         f"lr={LR} te_lr={TE_LR} class_images={NUM_CLASS_IMAGES} prior_w={PRIOR_W}")
log.info(f"INSTANCE_PROMPT={INSTANCE_PROMPT!r} CLASS_PROMPT={CLASS_PROMPT!r}")

from azure.storage.blob import BlobServiceClient
bsc = BlobServiceClient.from_connection_string(CONN)

# Images are ALREADY face-cropped (square 1024, face-centred) and uploaded.
# DreamBooth keys identity off --instance_prompt, so per-image captions are unused;
# FILES_JSON's "caption" field is read but deliberately ignored.
for i, item in enumerate(FILES):
    blob = item["blob"]
    out = f"img{i}.jpg"
    try:
        data = bsc.get_blob_client(container=IN_CONTAINER, blob=blob).download_blob().readall()
        with open(f"{DATA}/{out}", "wb") as f:
            f.write(data)
    except Exception as e:
        fail(f"download failed for {blob}: {e}")
    log.info(f"[{i}] {blob} -> {out}")
log.info(f"downloaded {len(FILES)} instance images to {DATA}")

# ── CLASS-IMAGE CACHE ────────────────────────────────────────────────────────
# Prior preservation needs NUM_CLASS_IMAGES generic images of the class ("a photo of a
# woman"). They depend ONLY on CLASS_PROMPT — nothing about this user — yet the diffusers
# script regenerates them from scratch whenever class_data_dir is short, and this is a
# throwaway container, so EVERY user was paying to regenerate the SAME set: 17.6 minutes
# of A100, measured (50 batches x 21.15s), a third of the whole training run.
#
# So: cache them in blob per class word, download on start, and only generate (and then
# publish) on a cold cache. The diffusers script counts the files in class_data_dir and
# skips generation entirely once it finds enough, so a warm cache is a pure no-op there.
# These are the SAME images, not an approximation — identical prompt, identical model.
CLASS_PREFIX = f"class/{CLASS_WORD}/"
n_want = int(NUM_CLASS_IMAGES)


def _download_class_cache() -> int:
    try:
        container = bsc.get_container_client(LORA_CONTAINER)
        names = [b.name for b in container.list_blobs(name_starts_with=CLASS_PREFIX)
                 if b.name.lower().endswith(".jpg")]
    except Exception as e:
        log.warning(f"class cache: list failed ({e}); will generate")
        return 0
    if len(names) < n_want:
        log.info(f"class cache MISS for '{CLASS_WORD}': {len(names)}/{n_want} present")
        return 0
    for i, name in enumerate(sorted(names)[:n_want]):
        data = container.get_blob_client(name).download_blob().readall()
        with open(f"{CLASS}/{os.path.basename(name)}", "wb") as f:
            f.write(data)
    log.info(f"class cache HIT for '{CLASS_WORD}': downloaded {n_want} images -> {CLASS} "
             f"(skipping ~17 min of generation)")
    return n_want


def _upload_class_cache():
    """Publish what the trainer just generated so the NEXT user gets a cache hit."""
    imgs = [p for p in sorted(os.listdir(CLASS)) if p.lower().endswith((".jpg", ".png"))]
    if len(imgs) < n_want:
        log.warning(f"class cache: only {len(imgs)}/{n_want} generated; not publishing")
        return
    container = bsc.get_container_client(LORA_CONTAINER)
    for i, p in enumerate(imgs[:n_want]):
        with open(f"{CLASS}/{p}", "rb") as f:
            container.get_blob_client(f"{CLASS_PREFIX}img{i:04d}.jpg").upload_blob(
                f, overwrite=True)
    log.info(f"class cache PUBLISHED {n_want} images -> {LORA_CONTAINER}/{CLASS_PREFIX}")


cache_hit = _download_class_cache() >= n_want

cmd = [
    "accelerate", "launch", "--num_processes=1", "--num_machines=1",
    "--mixed_precision=bf16", "--dynamo_backend=no",
    f"{WORK}/train_dreambooth_lora_sdxl.py",
    f"--pretrained_model_name_or_path={BASE_MODEL}",
    f"--pretrained_vae_model_name_or_path={VAE_MODEL}",
    f"--variant={MODEL_VARIANT}",
    f"--instance_data_dir={DATA}",
    f"--instance_prompt={INSTANCE_PROMPT}",
    "--with_prior_preservation",
    f"--class_data_dir={CLASS}",
    f"--class_prompt={CLASS_PROMPT}",
    f"--num_class_images={NUM_CLASS_IMAGES}",
    f"--prior_loss_weight={PRIOR_W}",
    # NO --prior_generation_precision: it sets torch_dtype for the class-image
    # pipeline alone; bf16 there loads the text encoder mixed fp16/bf16 and dies
    # with "expected mat1 and mat2 to have the same dtype: Half != BFloat16".
    # Unset => fp16 on CUDA, which is self-consistent. Training precision is
    # controlled separately by --mixed_precision below.
    "--sample_batch_size=4",
    "--train_text_encoder",
    f"--text_encoder_lr={TE_LR}",
    "--resolution=1024",
    "--train_batch_size=1", "--gradient_accumulation_steps=1",
    f"--max_train_steps={STEPS}",
    f"--learning_rate={LR}", "--lr_scheduler=constant", "--lr_warmup_steps=0",
    "--mixed_precision=bf16", f"--rank={RANK}", "--seed=42",
    f"--output_dir={OUT}",
]
if GRAD_CKPT:
    cmd.append("--gradient_checkpointing")

log.info(f"class_cache_hit={cache_hit} gradient_checkpointing={GRAD_CKPT}")
log.info("TRAIN CMD: " + " ".join(cmd))

# PEAK TRAIN VRAM — the measurement that decides whether GRADIENT_CHECKPOINTING can be
# turned off (~30% faster at identical quality, since checkpointing changes no math).
# Training runs in a SUBPROCESS, so torch.cuda.max_memory_allocated() in THIS process
# would report the format gate's usage, not training's — a falsely low number that would
# talk us into disabling checkpointing on bad evidence. Sample the device instead, from a
# thread, while the child trains.
vram = {"peak_mb": 0, "total_mb": 0, "sampling": True}


def _sample_vram():
    while vram["sampling"]:
        try:
            line = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip().splitlines()[0]
            used, total = (int(x.strip()) for x in line.split(","))
            vram["peak_mb"] = max(vram["peak_mb"], used)
            vram["total_mb"] = total
        except Exception:
            pass
        time.sleep(5)


sampler = threading.Thread(target=_sample_vram, daemon=True)
sampler.start()
rc = subprocess.run(cmd).returncode
vram["sampling"] = False
sampler.join(timeout=8)

log.info(
    f"PEAK TRAIN VRAM: {vram['peak_mb']/1024:.1f} GB of {vram['total_mb']/1024:.1f} GB "
    f"(gradient_checkpointing={GRAD_CKPT}) — if this is well under total, rerun with "
    f"GRADIENT_CHECKPOINTING=0 for ~30% faster training at identical quality"
)

# Publish the class images BEFORE the training-success check.
#
# They are complete and valid the moment they are generated — which happens at the START
# of the training subprocess, before a single training step runs. Whether the training loop
# later succeeds says NOTHING about whether these 200 pictures of a generic woman are good.
# Gating the publish on training success (the original design) meant an OOM at step 900
# threw away 17.6 minutes of finished GPU work and made the next user regenerate it.
# _upload_class_cache still refuses a PARTIAL set (< NUM_CLASS_IMAGES), which is the actual
# risk that gating was meant to guard against.
if not cache_hit:
    try:
        _upload_class_cache()
    except Exception as e:
        log.warning(f"class cache publish failed (non-fatal): {e}")

if rc != 0:
    fail("training subprocess exited non-zero (see log above)")

cands = sorted(glob.glob(f"{OUT}/*.safetensors"))
if not cands:
    fail(f"no .safetensors in {OUT}: {os.listdir(OUT)}")
produced = cands[0]
log.info(f"Produced adapter: {produced} ({os.path.getsize(produced)} bytes)")

# ── FORMAT GATE (reproduce main.py load path, strict) ─────
import torch, logging as _logging
from diffusers import StableDiffusionXLImg2ImgPipeline
log.info("FORMAT GATE: loading base SDXL fp16 + adapter with warning capture ...")
pipe = StableDiffusionXLImg2ImgPipeline.from_pretrained(
    BASE_MODEL,
    torch_dtype=torch.float16, variant=MODEL_VARIANT, use_safetensors=True).to("cuda")
caught = []
class Catch(_logging.Handler):
    def emit(self, rec):
        if rec.levelno >= _logging.WARNING:
            caught.append(rec.getMessage())
root = _logging.getLogger(); h = Catch(); root.addHandler(h)
try:
    pipe.load_lora_weights(produced, adapter_name="identity_lora")
    pipe.set_adapters(["identity_lora"], adapter_weights=[1.0])
finally:
    root.removeHandler(h)
for w in caught:
    log.info(f"[load warning] {w}")
active = pipe.get_active_adapters()
log.info(f"FORMAT GATE: active adapters = {active}")
MISMATCH = re.compile(r"unexpected key|no lora keys|not used|could not be found|"
                      r"cannot load|led to unexpected", re.IGNORECASE)
bad = [w for w in caught if MISMATCH.search(w)]
if bad:
    fail(f"FORMAT GATE FAILED — key-mismatch warnings (would render BASE SDXL): {bad}")
if "identity_lora" not in active:
    fail(f"FORMAT GATE FAILED — adapter not active: {active}")
log.info("FORMAT GATE PASSED — diffusers-native, no key-mismatch, adapter active.")

target = f"identity/{USER_ID}/adapter_model.safetensors"

# ── BACK UP THE EXISTING ADAPTER BEFORE OVERWRITING IT ───────────────────────
# On a RETRAIN we are about to destroy a model the user may already be happy with. A
# retrain can easily come out WORSE (different photos, different luck), and without this
# there is no way back — the good adapter is simply gone. Copy it aside first; the upload
# below is the point of no return.
# Server-side copy (start_copy_from_url), so no 237 MB round-trip through this container.
try:
    src = bsc.get_blob_client(container=LORA_CONTAINER, blob=target)
    if src.exists():
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup = f"identity/{USER_ID}/backup_{stamp}/adapter_model.safetensors"
        bsc.get_blob_client(container=LORA_CONTAINER, blob=backup).start_copy_from_url(src.url)
        log.info(f"Backed up previous adapter -> {LORA_CONTAINER}/{backup}")
    else:
        log.info("No previous adapter to back up (first training for this user).")
except Exception as e:
    # Non-fatal: losing the backup is bad, but refusing to deliver a freshly-trained model
    # over it would be worse. Loud, so it is visible when it happens.
    log.warning(f"BACKUP FAILED (continuing with upload): {e}")

log.info(f"Uploading {produced} -> {LORA_CONTAINER}/{target}")
try:
    with open(produced, "rb") as f:
        bsc.get_blob_client(container=LORA_CONTAINER, blob=target).upload_blob(f, overwrite=True)
except Exception as e:
    fail(f"upload failed: {e}")
done("SUCCESS", f"{LORA_CONTAINER}/{target} bytes={os.path.getsize(produced)}")
