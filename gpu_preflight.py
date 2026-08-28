#!/usr/bin/env python3.11
"""Mandatory GPU preflight for the unified BetterSnap container.

WHY THIS EXISTS
---------------
On 2026-07-31 an execution started on a GPU node (the platform even emitted a `GpuInfo`
event) yet `torch.cuda.is_available()` was False INSIDE the container, so SDXL training
silently ran on CPU for ~75 minutes before anyone noticed. This preflight makes that
failure mode LOUD and CHEAP: it runs before model loading, training, inference, any
job-state update, or credit finalization, and if there is no usable CUDA device it exits
immediately with a distinct non-zero code instead of degrading to CPU.

CONTRACT
--------
- Never returns when CUDA is unavailable — it calls exit_fn(<code>) and stops the
  container. The container exit code propagates to ContainerAppSystemLogs
  (`ContainerTerminated ... exit code '<code>'`), which stays fresh even when the
  console-log pipeline is minutes behind (requirement 10).
- Never falls back to CPU: there is no CPU branch; failure == exit (requirements 6, 7).
- Blob persistence is BEST-EFFORT: a write failure (even a raised exception) is swallowed
  and MUST NOT change the exit code (requirement 5).
- The container never touches credits (proven: only the backend decrements, function_app
  .py:777). A non-zero exit routes the job to the backend 'failed' path, which refunds
  (function_app.py:1647/1691). This module only guarantees "no GPU work was started".

EXIT CODES (distinct, stable — do NOT renumber)
  0   OK             usable CUDA device present; run_preflight() returns normally
  42  NO_GPU         torch reports no CUDA AND no GPU hardware detected on the node
  43  GPU_UNUSABLE   GPU hardware present (nvidia-smi / /dev/nvidia*) but torch is blind
                     -> device-injection / runtime mismatch (the 2026-07-31 signature)
  44  PROBE_ERROR    the preflight itself could not probe (e.g. torch import failed)
"""
import json
import os
import subprocess
import sys

EXIT_OK = 0
EXIT_NO_GPU = 42
EXIT_GPU_UNUSABLE = 43
EXIT_PROBE_ERROR = 44

DIAG_CONTAINER = os.environ.get("PREFLIGHT_DIAG_CONTAINER", "diagnostics")


def _run(cmd):
    """Run a command, never raise; return (rc, stdout, stderr). rc is None if the binary
    is missing / timed out."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:  # FileNotFoundError (no nvidia-smi), TimeoutExpired, ...
        return None, "", f"{type(e).__name__}: {e}"


def _list_nvidia_devices():
    """Pure-python `ls -l /dev/nvidia*` (works without a shell). Returns a list of
    strings; empty when the node exposed no GPU device nodes to the container."""
    out = []
    try:
        for name in sorted(os.listdir("/dev")):
            if name.startswith("nvidia"):
                path = os.path.join("/dev", name)
                try:
                    st = os.stat(path)
                    out.append(f"{path} mode={oct(st.st_mode)} rdev={getattr(st, 'st_rdev', None)}")
                except Exception as e:
                    out.append(f"{path} (stat failed: {e})")
    except Exception as e:
        out.append(f"(/dev listing failed: {e})")
    return out


def probe():
    """Collect every diagnostic fact. Each sub-probe is isolated so one failure never
    aborts the rest. torch is imported HERE (lazily) so importing this module — and unit-
    testing classify()/run_preflight() — never requires torch or a GPU."""
    rec = {
        "execution_id": (os.environ.get("CONTAINER_APP_JOB_EXECUTION_NAME")
                         or os.environ.get("HOSTNAME")),
        "job_id": os.environ.get("JOB_ID"),
        "mode": os.environ.get("MODE"),
        "image_ref": os.environ.get("BUILD_IMAGE_REF"),
        "image_digest": os.environ.get("BUILD_IMAGE_DIGEST"),
        "git_sha": os.environ.get("BUILD_GIT_SHA"),
        "build_dirty": os.environ.get("BUILD_DIRTY"),      # "true" when built from an uncommitted tree
        "build_source_hash": os.environ.get("BUILD_SOURCE_HASH"),  # deterministic hash of baked source
        "dev_nvidia": _list_nvidia_devices(),
        "torch_version": None,
        "torch_cuda_version": None,
        "cuda_available": False,
        "device_count": 0,
        "device_names": [],
        "probe_error": None,
    }
    rc, out, err = _run(["nvidia-smi"])
    rec["nvidia_smi_rc"] = rc
    rec["nvidia_smi_stdout"] = out
    rec["nvidia_smi_stderr"] = err
    try:
        import torch
        rec["torch_version"] = getattr(torch, "__version__", None)
        rec["torch_cuda_version"] = getattr(getattr(torch, "version", None), "cuda", None)
        rec["cuda_available"] = bool(torch.cuda.is_available())
        try:
            rec["device_count"] = int(torch.cuda.device_count())
        except Exception:
            rec["device_count"] = 0
        names = []
        for i in range(rec["device_count"]):
            try:
                names.append(torch.cuda.get_device_name(i))
            except Exception as e:
                names.append(f"(device {i} name failed: {e})")
        rec["device_names"] = names
    except Exception as e:
        rec["probe_error"] = f"{type(e).__name__}: {e}"
    return rec


def _hardware_present(rec):
    """True if there is any evidence of a physical GPU on the node, independent of torch.
    This is what separates 'no GPU node' (42) from 'GPU present but torch-blind' (43)."""
    if rec.get("nvidia_smi_rc") == 0:
        return True
    devs = [d for d in (rec.get("dev_nvidia") or [])
            if "/dev/nvidia" in d and "failed" not in d]
    return len(devs) > 0


def classify(rec):
    """PURE decision function (no I/O). Returns (ok, exit_code, reason).

    The ONLY path to ok=True is a genuinely usable CUDA device — there is deliberately no
    branch that returns ok=True without CUDA, which is the formal guarantee that a CUDA
    failure can never be interpreted as 'proceed on CPU'."""
    if rec.get("probe_error"):
        return (False, EXIT_PROBE_ERROR,
                f"preflight could not probe the GPU stack: {rec['probe_error']}")
    if rec.get("cuda_available") and (rec.get("device_count") or 0) >= 1:
        return (True, EXIT_OK,
                f"{rec['device_count']} CUDA device(s): {rec.get('device_names')}")
    if _hardware_present(rec):
        return (False, EXIT_GPU_UNUSABLE,
                "GPU hardware present (nvidia-smi / /dev/nvidia*) but torch.cuda is "
                "unavailable -> device-injection or runtime mismatch")
    return (False, EXIT_NO_GPU,
            "no usable CUDA device and no GPU hardware detected on the node")


def build_record(rec, ok, exit_code, reason):
    out = dict(rec)
    out["required"] = {"min_cuda_devices": 1}
    out["result"] = {"ok": ok, "exit_code": exit_code, "reason": reason}
    return out


def write_diagnostic(record, *, container=None):
    """BEST-EFFORT persist to retention-exempt durable storage. Returns True on success,
    False on ANY failure — and NEVER raises, so a storage problem cannot change the
    process exit code (requirement 5)."""
    container = container or DIAG_CONTAINER
    try:
        from azure.storage.blob import BlobServiceClient
        conn = os.environ.get("STORAGE_CONNECTION_STRING")
        if not conn:
            return False
        svc = BlobServiceClient.from_connection_string(conn)
        try:
            svc.create_container(container)  # idempotent; the 'diagnostics' container is
        except Exception:                    # created without a retention lifecycle rule
            pass
        exec_id = (record.get("execution_id") or "unknown").replace("/", "_")
        blob = f"preflight/{exec_id}.json"
        svc.get_blob_client(container=container, blob=blob).upload_blob(
            json.dumps(record, indent=2, default=str).encode("utf-8"), overwrite=True)
        return True
    except Exception:
        return False


def run_preflight(*, exit_fn=sys.exit, probe_fn=probe, write_fn=write_diagnostic,
                  print_fn=print):
    """Orchestrate the preflight. Dependency-injected so tests never touch torch, Azure,
    or the real process exit.

    On success: prints the diagnostic and RETURNS None (the caller may proceed).
    On failure: prints the diagnostic, best-effort persists it, prints a distinct marker
    line, and calls exit_fn(code). It NEVER returns None on failure — even if an injected
    exit_fn fails to stop execution, the trailing `raise SystemExit` guarantees the caller
    cannot fall through into training/inference (requirement 7)."""
    rec = probe_fn()
    ok, code, reason = classify(rec)
    record = build_record(rec, ok, code, reason)

    # requirement 3: complete diagnostic to stdout
    print_fn("::PREFLIGHT_DIAGNOSTIC::" + json.dumps(record, default=str))

    # requirement 4 & 5: durable capture is best-effort and can NEVER affect the exit —
    # even a write_fn that raises is contained here.
    try:
        persisted = write_fn(record)
    except Exception:
        persisted = False
    print_fn(f"::PREFLIGHT_PERSIST::{'ok' if persisted else 'FAILED (non-fatal)'}")

    if ok:
        print_fn(f"::PREFLIGHT_RESULT::PASS::{reason}")
        return None

    # requirements 6, 7, 10: exit immediately with a distinct code; never CPU.
    print_fn(f"::PREFLIGHT_RESULT::FAIL::exit_code={code}::{reason}")
    exit_fn(code)
    raise SystemExit(code)  # defensive: unreachable when exit_fn == sys.exit


if __name__ == "__main__":
    run_preflight()
