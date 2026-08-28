"""Reconcile a Container Apps execution outcome into a fail/refund decision.

PURE functions only — no Azure, no DB. The control-plane caller (the reaper in
function_app.py) fetches ACA's execution status plus optional preflight diagnostics and
passes them here; this decides the outcome class, whether delivery must be verified, and
whether a net-zero refund is owed. The refund itself is executed by
function_app._mark_failed, whose `status NOT IN ('failed','completed')` + rowcount guard
makes it idempotent.

WHY A SEPARATE CLASSIFIER
-------------------------
The reaper used to blindly `_mark_failed` any job stuck in 'processing'/'dispatching' — no
distinction between "GPU never attached" (infra, 2026-07-31), "image never pulled" (infra),
and "model raised after a healthy start" (application). This module makes that distinction
so infra failures are never mislabelled as customer/model failures, and so the diagnostic
reason survives into the jobs row.

The GPU preflight (gpu_preflight.py) exits with:
  42 -> infra_no_gpu | 43 -> infra_gpu_unusable | 44 -> infra_gpu_probe_error
"""
from shared import startup_stall

# preflight exit codes -> infra failure classes
INFRA_EXIT = {42: "infra_no_gpu", 43: "infra_gpu_unusable", 44: "infra_gpu_probe_error"}

# actions the caller must take
ACTION_REFUND = "refund"   # fail the job + refund exactly once (net-zero for the user)
ACTION_NONE = "none"       # still pending -> do nothing
ACTION_VERIFY_DELIVERY = "verify_delivery"  # ACA succeeded; caller must prove outputs exist
ACTION_RECOVER = "recover"  # caller recovered a delivered job to completed

# non-infra failure classes
CLASS_APPLICATION = "application"          # model/app raised AFTER a healthy container start
CLASS_OPERATOR_STOPPED = "operator_stopped"
CLASS_SUCCESS = "success"
CLASS_PENDING = "pending"
CLASS_DELIVERY_MISSING = "delivery_missing"

INFRA_CLASSES = frozenset(
    set(INFRA_EXIT.values())
    | {startup_stall.INFRA_IMAGE_PULL_STALL.lower(),
       startup_stall.INFRA_POST_PULL_START_STALL.lower()}
)


def _mk(action, failure_class, is_infra, reason, execution_id):
    return {"action": action, "failure_class": failure_class, "is_infra": is_infra,
            "reason": reason, "execution_id": execution_id}


def classify_execution(exec_data, now, *, start_threshold_s=None, post_pull_grace_s=None):
    """exec_data keys:
        execution_status  str | None   authoritative ACA job execution status
        preflight_exit_code int | None GPU probe result; never a container outcome
        reason            str | None   terminal reason ('ProcessExited'|'ManuallyStopped'|None)
        events            list         lifecycle events for startup_stall
        execution_id      str | None
        diagnostic_reason str | None   preserved reason from the preflight blob/log
    Returns dict(action, failure_class, is_infra, reason, execution_id)."""
    execution_status = (exec_data.get("execution_status") or "").lower()
    preflight_exit_code = exec_data.get("preflight_exit_code")
    reason_evt = exec_data.get("reason") or ""
    exec_id = exec_data.get("execution_id")
    diag = exec_data.get("diagnostic_reason")
    events = exec_data.get("events") or []

    # 1) GPU preflight infra exits (checked FIRST — these also occur after ContainerStarted,
    #    so they must win over the generic application-failure branch below).
    if execution_status in {"failed", "degraded", "cancelled", "canceled", "stopped"}:
        if preflight_exit_code in INFRA_EXIT:
            return _mk(ACTION_REFUND, INFRA_EXIT[preflight_exit_code], True,
                       diag or f"GPU preflight failed (exit {preflight_exit_code})", exec_id)
        if execution_status in {"cancelled", "canceled", "stopped"}:
            return _mk(ACTION_REFUND, CLASS_OPERATOR_STOPPED, False,
                       f"ACA execution ended with status {execution_status}", exec_id)
        return _mk(ACTION_REFUND, CLASS_APPLICATION, False,
                   diag or f"ACA execution ended with status {execution_status}", exec_id)

    # ACA success proves the container terminated cleanly, but not that result blobs landed.
    # The caller must verify outputs before recovering the stale SQL row to completed.
    if execution_status == "succeeded":
        return _mk(ACTION_VERIFY_DELIVERY, CLASS_SUCCESS, False,
                   "ACA execution succeeded; verifying delivered outputs", exec_id)

    # A passing preflight is NOT a terminal outcome. This is the C1 invariant: exit 0 in the
    # diagnostic blob only means the GPU probe passed before inference started.
    if preflight_exit_code in INFRA_EXIT:
        return _mk(ACTION_REFUND, INFRA_EXIT[preflight_exit_code], True,
                   diag or f"GPU preflight failed (exit {preflight_exit_code})", exec_id)

    # 3) legacy operator-stop event — user got nothing -> refund,
    #    but it is NOT an infra fault.
    if "ManuallyStopped" in reason_evt or exec_data.get("manually_stopped"):
        return _mk(ACTION_REFUND, CLASS_OPERATOR_STOPPED, False,
                   "execution manually stopped before delivery", exec_id)

    # 4) not terminated yet -> is it a startup stall?
    kw = {}
    if start_threshold_s is not None:
        kw["start_threshold_s"] = start_threshold_s
    if post_pull_grace_s is not None:
        kw["post_pull_grace_s"] = post_pull_grace_s
    verdict = startup_stall.classify_startup(events, now, **kw)
    if verdict["should_stop"]:
        return _mk(ACTION_REFUND, verdict["classification"].lower(), True,
                   verdict["reason"], exec_id)
    return _mk(ACTION_NONE, CLASS_PENDING, False, verdict["reason"], exec_id)
