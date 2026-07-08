"""
Harness 1 — local container-pipeline test (Windows, GPU mocked).

WHAT THIS DOES
--------------
Imports and runs the REAL inference code from ../main.py (parent folder
BETTERSNAP-AI_INFERENCE) — not a reconstruction. Real Azure SQL + real Blob
Storage; only the GPU model is mocked, so it runs instantly on a laptop with no
GPU and no 30GB model download.

  read JOB_ID / USER_ID  ->  real main.get_db_connection -> SELECT jobs row
  ->  real main.run_inference(job):
        real load_image_from_blob -> resize_for_sdxl -> MOCKED pipe()
        -> add_watermark -> real upload_image_to_blob (outputs/results/<job>/)

HOW THE GPU IS MOCKED (the documented swap, done properly)
----------------------------------------------------------
main.py does `import torch` and `from diffusers import StableDiffusionXLPipeline,
AutoencoderKL` at module top, `load_base_model()` uses `enable_model_cpu_offload()`
and CUDA helpers, and `run_inference` builds `torch.Generator("cuda")`. A laptop
has none of that.  So we:
  1. Inject stub `torch` / `diffusers` modules into sys.modules BEFORE importing
     main (so the heavy GPU libs need not even be installed).
  2. Replace `main.load_base_model` with a no-op and set `main.pipe` to a mock
     that returns a 1024×1024 PIL image instantly (mock of pipe(...).images[0]).

PROD-ONLY PLUMBING WE OVERRIDE SO IT RUNS OFF-AZURE
---------------------------------------------------
  * main.get_db_connection uses Authentication=ActiveDirectoryMsi (managed
    identity) — replaced with this repo's SQL-auth connection (password from .env).
  * main.blob_service uses DefaultAzureCredential — if STORAGE_CONNECTION_STRING
    is in .env we rebuild it from that; otherwise we keep main's credential client
    (needs `az login` + Storage Blob Data role).
  * main.update_job_status writes to the shared DB — stubbed to log-only unless
    you pass --commit (DB dry-run by default).

The inference business logic (run_inference, prompts, resize, watermark, the
results/<job>/ upload path, the SQL query strings) is the REAL main.py.

USAGE
-----
    pip install Pillow                 # torch/diffusers are stubbed, not needed
    python local_test.py --job-id test-alice-1 --user-id test-alice
    python local_test.py               # uses JOB_ID/USER_ID from .env
"""

import argparse
import json
import logging
import os
import sys
import traceback
import types

# ── logging ───────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("local_test")


# ── tiny .env loader (no python-dotenv dependency) ────────
def load_dotenv(path=".env"):
    if not os.path.exists(path):
        log.warning(".env not found at %s — relying on the real environment", path)
        return
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


# ── Key Vault shim (feeds shared/db.py the DB password from .env) ──
SECRET_ENV_MAP = {
    "Db-Password": "DB_PASSWORD",
    "storage-connection-string": "STORAGE_CONNECTION_STRING",
    "storage-account-key": "STORAGE_ACCOUNT_KEY",
}


def local_get_secret(name: str) -> str:
    env_key = SECRET_ENV_MAP.get(name)
    if env_key and os.environ.get(env_key):
        log.info("get_secret(%r) -> from .env (%s)", name, env_key)
        return os.environ[env_key]
    log.info("get_secret(%r) -> falling back to real Key Vault", name)
    from shared.keyvault import get_secret as real_get_secret
    return real_get_secret(name)


# ── GPU mock ──────────────────────────────────────────────
class _MockImages:
    """Mimics the diffusers return object: result.images[0] is a PIL.Image."""
    def __init__(self, image):
        self.images = [image]


def make_mock_pipe():
    """Stand-in for a loaded StableDiffusionXLPipeline. Returns an instant
    1024×1024 PIL image (SDXL text-to-image: no image= kwarg)."""
    from PIL import Image

    def _pipe(*args, **kwargs):
        log.info("MOCK pipe() called: kwargs=%s", list(kwargs.keys()))
        return _MockImages(Image.new("RGB", (1024, 1024), color=(120, 90, 200)))

    return _pipe


def install_gpu_stubs():
    """Inject fake torch/diffusers into sys.modules BEFORE `import main`, so the
    heavy GPU libraries are not required and `torch.Generator('cuda')` works."""
    torch_stub = types.ModuleType("torch")

    class _Generator:
        def manual_seed(self, _seed):
            return self

    # SDXL pipeline uses torch.float16
    torch_stub.float16 = "float16"
    torch_stub.bfloat16 = "bfloat16"
    torch_stub.Generator = lambda *a, **k: _Generator()

    # Stub cuda helpers used by main.py's VRAM probes
    cuda_stub = types.ModuleType("torch.cuda")

    class _DeviceProps:
        name = "MOCK-GPU"
        total_memory = 80 * 1024 ** 3

    cuda_stub.get_device_properties = lambda _idx: _DeviceProps()
    cuda_stub.max_memory_allocated = lambda _idx=0: 0
    torch_stub.cuda = cuda_stub
    sys.modules["torch"] = torch_stub
    sys.modules["torch.cuda"] = cuda_stub

    diffusers_stub = types.ModuleType("diffusers")

    class _FakePipeline:
        @classmethod
        def from_pretrained(cls, *a, **k):
            return make_mock_pipe()

        def enable_model_cpu_offload(self):
            pass

        @property
        def vae(self):
            class _VAE:
                def enable_tiling(self):
                    pass
            return _VAE()

    class _FakeVAE:
        @classmethod
        def from_pretrained(cls, *a, **k):
            return _FakeVAE()

    diffusers_stub.StableDiffusionXLPipeline = _FakePipeline
    diffusers_stub.AutoencoderKL = _FakeVAE
    sys.modules["diffusers"] = diffusers_stub


def import_real_main():
    """Import ../main.py (parent folder BETTERSNAP-AI_INFERENCE)."""
    parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if parent not in sys.path:
        sys.path.insert(0, parent)
    main_path = os.path.join(parent, "main.py")
    if not os.path.exists(main_path):
        log.error("Real main.py not found at %s", main_path)
        sys.exit(2)
    log.info("Importing real inference code from %s", main_path)
    import main  # noqa: E402  — stubs already installed
    return main


# ── the job flow (mirrors main.py's __main__ block) ───────
def run(job_id: str, user_id: str, commit: bool):
    # shim shared.db's get_secret so we get a working local SQL-auth connection
    import shared.db as db_mod
    db_mod.get_secret = local_get_secret
    from shared.db import get_db as shared_get_db

    install_gpu_stubs()
    main = import_real_main()

    # ── GPU mock: skip /models + cuda, install the instant mock pipe ──
    main.load_base_model = lambda: log.info("load_base_model: MOCKED no-op (GPU)")
    main.pipe = make_mock_pipe()
    # Email is best-effort in prod; skip entirely in local tests (no ACS creds).
    main.notify_user_email = lambda *a, **k: log.info("notify_user_email: MOCKED (local test)")

    # ── DB: replace MSI auth with local SQL auth; dry-run writes by default ──
    main.get_db_connection = lambda *a, **k: shared_get_db()
    if not commit:
        def _dry_update(jid, status, output_blob_paths=None):
            log.info("DRY RUN update_job_status: job=%s status=%s outputs=%s "
                     "(pass --commit to write)", jid, status, output_blob_paths)
        main.update_job_status = _dry_update

    # ── Blob: prefer .env connection string; else main's DefaultAzureCredential ──
    cs = os.environ.get("STORAGE_CONNECTION_STRING")
    if cs:
        from azure.storage.blob import BlobServiceClient
        main.blob_service = BlobServiceClient.from_connection_string(cs)
        log.info("blob_service: using STORAGE_CONNECTION_STRING from .env")
    else:
        log.info("blob_service: using main.py DefaultAzureCredential "
                 "(needs `az login` + Storage Blob Data role)")

    os.environ["JOB_ID"] = job_id
    os.environ["USER_ID"] = user_id
    log.info("=== local_test start: job_id=%s user_id=%s commit=%s ===",
             job_id, user_id, commit)

    try:
        # mirror main.__main__: read the job BEFORE the (mocked) model load
        conn = main.get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT job_params, input_blob_path FROM jobs WHERE job_id = ?", job_id
        )
        row = cursor.fetchone()
        conn.close()
        if not row:
            log.error("Job %s not found in DB (check id / firewall / user_id).", job_id)
            sys.exit(2)
        job_params_raw, input_blob_path = row[0], row[1]
        log.info("DB row: input_blob_path=%s", input_blob_path)
        log.info("DB row: job_params=%s", job_params_raw)

        job = {"job_id": job_id, "user_id": user_id, "job_params": job_params_raw}

        main.update_job_status(job_id, "processing")   # dry-run unless --commit
        main.load_base_model()                          # mocked no-op
        result = main.run_inference(job)                # REAL inference logic
        main.update_job_status(job_id, "completed", result)
    except Exception:
        log.error("JOB CRASHED — full traceback below:\n%s", traceback.format_exc())
        if commit:
            try:
                main.update_job_status(job_id, "failed")
            except Exception:
                log.error("Also failed to mark job failed:\n%s", traceback.format_exc())
        sys.exit(1)

    # ── expose the v14 silent-crash symptom ──
    expected = json.loads(job_params_raw or "{}").get("num_images", 4)
    if not result:
        log.error("INFERENCE PRODUCED 0 IMAGES. main.run_inference swallows each "
                  "variation's exception at main.py:360-361 (logs only str(e)) and "
                  "still returns — this is exactly the v14 silent crash. Re-run with a "
                  "real pipe, or read the per-variation log lines above.")
        sys.exit(1)
    if len(result) < expected:
        log.warning("Only %d/%d variations succeeded — the rest were silently "
                    "swallowed at main.py:360-361.", len(result), expected)
    log.info("Result blobs (outputs/...): %s", result)
    log.info("=== local_test done ===")


def main_cli():
    load_dotenv()
    ap = argparse.ArgumentParser(description="Local container-pipeline test (GPU mocked)")
    ap.add_argument("--job-id", default=os.environ.get("JOB_ID"),
                    help="job_id to process (default: JOB_ID from .env)")
    ap.add_argument("--user-id", default=os.environ.get("USER_ID"),
                    help="user_id that owns the job (default: USER_ID from .env)")
    ap.add_argument("--commit", action="store_true",
                    help="write status/output_blob_path back to the shared DB")
    args = ap.parse_args()

    if not args.job_id or not args.user_id:
        ap.error("job-id and user-id are required (set via flags or .env)")

    run(args.job_id, args.user_id, args.commit)


if __name__ == "__main__":
    main_cli()
