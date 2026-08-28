"""Reconcile a Container Apps execution outcome into a fail/refund decision.

PURE functions only — no Azure, no DB. The control-plane caller (the reaper in
function_app.py) fetches the execution's exit code + lifecycle events and passes them here;
this decides the outcome CLASS, whether a net-zero refund is owed, and preserves the
diagnostic reason + execution id. The refund itself is executed by function_app._mark_failed,
whose `status NOT IN ('failed','completed')` + rowcount guard makes it idempotent.

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
ACTION_NONE = "none"       # success, or still pending -> do nothing

# non-infra failure classes
CLASS_APPLICATION = "application"          # model/app raised AFTER a healthy container start
CLASS_OPERATOR_STOPPED = "operator_stopped"
CLASS_SUCCESS = "success"
CLASS_PENDING = "pending"

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
        exit_code         int | None   container exit code (None if not terminated)
        reason            str | None   terminal reason ('ProcessExited'|'ManuallyStopped'|None)
        events            list         lifecycle events for startup_stall
        execution_id      str | None
        diagnostic_reason str | None   preserved reason from the preflight blob/log
    Returns dict(action, failure_class, is_infra, reason, execution_id)."""
    exit_code = exec_data.get("exit_code")
    reason_evt = exec_data.get("reason") or ""
    exec_id = exec_data.get("execution_id")
    diag = exec_data.get("diagnostic_reason")
    events = exec_data.get("events") or []

    # 1) GPU preflight infra exits (checked FIRST — these also occur after ContainerStarted,
    #    so they must win over the generic application-failure branch below).
    if exit_code in INFRA_EXIT:
        return _mk(ACTION_REFUND, INFRA_EXIT[exit_code], True,
                   diag or f"GPU preflight failed (exit {exit_code})", exec_id)

    # 2) clean success — the container delivered and wrote its own terminal state.
    if exit_code == 0:
        return _mk(ACTION_NONE, CLASS_SUCCESS, False, "process exited 0", exec_id)

    # 3) operator stop (empty/absent exit code + ManuallyStopped) — user got nothing -> refund,
    #    but it is NOT an infra fault.
    if "ManuallyStopped" in reason_evt or exec_data.get("manually_stopped"):
        return _mk(ACTION_REFUND, CLASS_OPERATOR_STOPPED, False,
                   "execution manually stopped before delivery", exec_id)

    started = any(e.get("reason") == "ContainerStarted" for e in events)

    # 4) other non-zero exit AFTER a healthy container start -> application/model failure
    #    (explicitly NOT infra).
    if isinstance(exit_code, int) and exit_code != 0 and started:
        return _mk(ACTION_REFUND, CLASS_APPLICATION, False,
                   f"application failure (exit {exit_code}) after container start", exec_id)

    # 5) not terminated yet -> is it a startup stall?
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
