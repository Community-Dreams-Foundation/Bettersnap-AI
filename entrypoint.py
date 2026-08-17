#!/usr/bin/env python3.11
"""Mode switch for the unified BetterSnap image (one image, three modes).

MODE=train           -> train the per-user LoRA only       (/workspace/run_training.py)
MODE=train_infer     -> train, THEN generate in ONE run    (trainer, then /app/main.py)
MODE=infer (default) -> generate only                      (/app/main.py)

Each script assumes it runs from its own directory (main.py does `import catalog` from
/app; run_training.py runs from /workspace but writes its data and LoRA output under
/tmp, which is writable at runtime), so we chdir before exec.

WHY train_infer EXISTS
----------------------
Product shape, not a convenience:
  * one-time plans        -> the user buys a pack; we train and deliver it in ONE run.
                             Every repeat purchase retrains from scratch.
  * monthly, 1st session  -> train + generate together, so the first session delivers.
  * monthly, later        -> MODE=infer against the adapter from that first session.
Splitting train and generate into two dispatches made a one-time buyer wait through two
cold starts and two queue hops for what is, to them, a single purchase.

WHY subprocess AND NOT execvp FOR THE TRAINER
---------------------------------------------
os.execvp REPLACES this process, so nothing can run after it — fine for the single-phase
modes, fatal for the fused one. The trainer therefore runs as a CHILD in train_infer, and
only the FINAL phase execs (so the container's exit code is still the real work's).

WHY THE FUSED MODE IS MATCHED FIRST
-----------------------------------
"train_infer".startswith("train") is True. A startswith("train") check ordered before the
fused case silently degrades train_infer into train-only: you pay for the training, no
images are produced, and nothing errors. Exact-match the fused mode first.

WHY JOB_ID IS VALIDATED UP FRONT
--------------------------------
main.py hard-requires JOB_ID and USER_ID and exits fatally without them. In a fused run
that check would otherwise happen AFTER ~30 minutes of A100 training — so fail in the
first second instead, while the mistake is still cheap.
"""
import logging
import os
import subprocess
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("entrypoint")

TRAIN_DIR, TRAIN_SCRIPT = "/workspace", "run_training.py"
INFER_DIR, INFER_SCRIPT = "/app", "main.py"

# Accepted spellings of the fused mode. Exact membership, never a prefix test.
FUSED = {"train_infer", "train+infer", "train_and_infer", "fused"}

mode = os.environ.get("MODE", "infer").strip().lower()

# ── MANDATORY GPU PREFLIGHT ──────────────────────────────────────────────────────────
# Runs for EVERY mode, before ANY dispatch, model load, job-state update, or credit
# finalization. On a GPU/CUDA failure it prints a full diagnostic, best-effort persists it
# to durable storage, and exits with a distinct non-zero code (42/43/44) — so the container
# NEVER falls back to CPU and never reaches run_training.py / main.py. The exit code is
# visible via `ContainerTerminated` in ContainerAppSystemLogs even when console logs lag.
# (gpu_preflight.py is COPYed next to this script in Dockerfile.unified, so it is importable
# from the script directory.)
import gpu_preflight  # noqa: E402
gpu_preflight.run_preflight()   # never returns when CUDA is unavailable

# ── PREFLIGHT-ONLY DIAGNOSTIC MODE ───────────────────────────────────────────────────
# Infra-validation switch: prove the GPU preflight runs correctly inside Azure Container Apps
# WITHOUT loading any model, training, generating, reserving work, or consuming credits. On a
# GPU FAILURE run_preflight() already exited (42/43/44) above and wrote its diagnostic blob;
# on a GPU SUCCESS it returned, and here we exit 0 BEFORE any dispatch. Completes in seconds.
if os.environ.get("PREFLIGHT_ONLY", "").strip().lower() in ("1", "true", "yes"):
    log.info("PREFLIGHT_ONLY set -> GPU preflight PASSED; exiting 0 without dispatch "
             "(no model load, no training, no inference, no credits).")
    sys.exit(0)


def _exec(cwd, script):
    """Replace this process with `script`, so its exit code is the container's."""
    os.chdir(cwd)
    os.execvp("python3.11", ["python3.11", script])


if mode in FUSED:
    missing = [v for v in ("JOB_ID", "USER_ID") if not os.environ.get(v)]
    if missing:
        log.error(
            "MODE=%s needs %s for the generation phase, but they are unset. Refusing to "
            "start: training would burn ~30 min of A100 and then fail at the handoff.",
            mode, "/".join(missing),
        )
        sys.exit(2)

    log.info("MODE=%s -> phase 1/2: training", mode)
    rc = subprocess.run(["python3.11", TRAIN_SCRIPT], cwd=TRAIN_DIR).returncode
    if rc != 0:
        # The adapter is missing or failed its format gate. Generating now would either
        # fail on a missing blob or silently render base SDXL and hand the user a
        # photogenic stranger. main.py guards that too — do not rely on it twice.
        log.error("training failed (rc=%s) — NOT generating", rc)
        sys.exit(rc)

    log.info("MODE=%s -> phase 2/2: generation", mode)
    _exec(INFER_DIR, INFER_SCRIPT)

elif mode.startswith("train"):
    _exec(TRAIN_DIR, TRAIN_SCRIPT)

elif mode in ("infer", ""):
    _exec(INFER_DIR, INFER_SCRIPT)

else:
    # A typo like MODE=inferr used to silently run inference — so a mis-set training job would
    # generate against a missing adapter instead of failing loudly. Refuse unknown modes. (Audit A7)
    log.error("Unknown MODE=%r — expected one of train | train_infer | infer. Refusing to guess.", mode)
    sys.exit(2)
