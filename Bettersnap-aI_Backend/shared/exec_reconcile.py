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
import os

from shared import execution_evidence, startup_stall

# preflight exit codes -> infra failure classes
INFRA_EXIT = {42: "infra_no_gpu", 43: "infra_gpu_unusable", 44: "infra_gpu_probe_error"}

# actions the caller must take
ACTION_REFUND = "refund"   # fail the job + refund exactly once (net-zero for the user)
ACTION_NONE = "none"       # still pending -> do nothing
ACTION_VERIFY_DELIVERY = "verify_delivery"  # ACA succeeded; caller must prove outputs exist
ACTION_RECOVER = "recover"  # caller recovered a delivered job to completed
# Bounded re-dispatch of the ONE class Azure can fix by trying again. The caller consumes an
# attempt and refunds only on exhaustion. Redispatch itself is NOT implemented in this phase.
ACTION_RETRY = "retry"

# non-infra failure classes
CLASS_APPLICATION = "application"          # model/app raised AFTER a healthy container start
CLASS_OPERATOR_STOPPED = "operator_stopped"
CLASS_SUCCESS = "success"
CLASS_PENDING = "pending"
CLASS_DELIVERY_MISSING = "delivery_missing"

# Azure never backed the replica: terminal BackoffLimitExceeded with NO ContainerStarted and no
# container exit. The one retryable class.
CLASS_PRE_CONTAINER_PROVISIONING = "infra_pre_container_provisioning_failed"

# Terminal, but the evidence never became good enough to attribute a cause. Refunded so the user
# is never charged for a run they did not get, but deliberately NOT called infra: we do not know
# that it was. Claiming infra here would be a fabricated attribution.
CLASS_UNCLASSIFIED_TERMINAL_FAILURE = "unclassified_terminal_failure"

INFRA_CLASSES = frozenset(
    set(INFRA_EXIT.values())
    | {startup_stall.INFRA_IMAGE_PULL_STALL.lower(),
       startup_stall.INFRA_POST_PULL_START_STALL.lower(),
       CLASS_PRE_CONTAINER_PROVISIONING}
)

# Telemetry windows, measured from terminal_observed_at (per execution ATTEMPT — migration 033).
# Below GRACE we have not waited long enough for Log Analytics; above MAX_OBSERVATION we stop
# waiting and refund as unclassified rather than leaving a row pending forever.
INGESTION_GRACE_S = int(os.environ.get("PROVISIONING_INGESTION_GRACE", "90"))
MAX_OBSERVATION_S = int(os.environ.get("PROVISIONING_MAX_OBSERVATION", "1800"))

_ARM_TERMINAL_FAILURE = {"failed", "degraded", "cancelled", "canceled", "stopped"}
_ARM_OPERATOR_STOPPED = {"cancelled", "canceled", "stopped"}


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
    Also accepts the richer execution_evidence structure (arm_status / container_exit_code /
    telemetry_ok / terminal_observed_at). Both shapes are normalised to evidence ONCE here, so
    the branches below read one vocabulary and legacy callers keep working unchanged.

    Returns dict(action, failure_class, is_infra, reason, execution_id)."""
    ev = execution_evidence.from_legacy(exec_data)
    execution_status = ev.get("arm_status") or ""
    preflight_exit_code = ev.get("preflight_exit_code")
    reason_evt = ev.get("_legacy_reason") or exec_data.get("reason") or ""
    exec_id = ev.get("execution_id")
    diag = ev.get("_diagnostic_reason") or exec_data.get("diagnostic_reason")
    events = list(ev.get("events") or ())

    # 1) GPU preflight infra exits (checked FIRST — these also occur after ContainerStarted,
    #    so they must win over the generic application-failure branch below).
    if execution_status in _ARM_TERMINAL_FAILURE:
        if preflight_exit_code in INFRA_EXIT:
            return _mk(ACTION_REFUND, INFRA_EXIT[preflight_exit_code], True,
                       diag or f"GPU preflight failed (exit {preflight_exit_code})", exec_id)

        # 1b) Operator stop is decided BEFORE any retry consideration: a human ended this run,
        #     so re-dispatching it would fight the operator.
        if execution_status in _ARM_OPERATOR_STOPPED:
            return _mk(ACTION_REFUND, CLASS_OPERATOR_STOPPED, False,
                       f"ACA execution ended with status {execution_status}", exec_id)

        age = execution_evidence.age_since_terminal(ev, now)
        started = ev.get("container_started")
        backoff = ev.get("backoff_limit_exceeded")
        c_exit = ev.get("container_exit_code")

        # 1c) POSITIVE proof of an application failure: the container demonstrably ran.
        #     Three independent proofs, any one is sufficient:
        #       * a container exit code exists (ContainerTerminated was observed);
        #       * ContainerStarted was observed;
        #       * a preflight exit code exists AT ALL. gpu_preflight.py runs INSIDE the
        #         container, so any recorded probe result — including 0 — proves the container
        #         started. (Infra probe exits 42/43/44 already returned at 1a, so reaching here
        #         with a non-None value means the probe passed and inference then failed.)
        #     Checked before the telemetry-wait branches so a well-evidenced failure resolves
        #     immediately instead of waiting out the observation window, and before 1d so a run
        #     that reached the probe can never be mistaken for a pre-container failure.
        if c_exit is not None or started is True or preflight_exit_code is not None:
            if c_exit is not None:
                why = f"container exited {c_exit}"
            elif started is True:
                why = "container started then failed"
            else:
                why = f"GPU preflight ran (exit {preflight_exit_code}); container had started"
            return _mk(ACTION_REFUND, CLASS_APPLICATION, False, diag or why, exec_id)

        # 1d) CONFIRMED pre-container provisioning failure. Every clause must be POSITIVELY
        #     observed: telemetry actually returned, BackoffLimitExceeded is present,
        #     ContainerStarted is absent, and there is no container or preflight exit. Absence
        #     of telemetry can never reach here.
        pre_container_shape = (
            ev.get("telemetry_ok") and backoff is True and started is False
            and c_exit is None and preflight_exit_code is None)
        if pre_container_shape:
            # The evidence SHAPE is not sufficient on its own. Log Analytics ingests
            # out-of-order and ~90s late, so a query run too early can legitimately return
            # BackoffLimitExceeded before the ContainerStarted row for the SAME execution has
            # landed — which is exactly the shape above. Retrying then would re-dispatch a run
            # whose container did start, double-spending an A100 on a genuine app failure.
            # So the age of the observation is a REQUIRED clause, not a heuristic:
            # terminal_observed_at must be known AND at least INGESTION_GRACE_S old.
            if age is None:
                return _mk(ACTION_NONE, CLASS_PENDING, False,
                           "pre-container shape observed but the terminal timestamp is unknown; "
                           "cannot prove the ingestion grace has elapsed", exec_id)
            if age < INGESTION_GRACE_S:
                return _mk(ACTION_NONE, CLASS_PENDING, False,
                           f"pre-container shape observed {age:.0f}s ago; within "
                           f"{INGESTION_GRACE_S}s ingestion grace, re-reading", exec_id)
            return _mk(ACTION_RETRY, CLASS_PRE_CONTAINER_PROVISIONING, True,
                       "replica failed pre-container: BackoffLimitExceeded with no "
                       "ContainerStarted and no container exit code", exec_id)

        # 1e) Not enough evidence yet. Keep observing while inside the window; the caller
        #     re-reads on the next pass. terminal_observed_at is stamped once per ATTEMPT, so
        #     age advances and this cannot loop forever.
        if age is not None and age < INGESTION_GRACE_S:
            return _mk(ACTION_NONE, CLASS_PENDING, False,
                       f"terminal {age:.0f}s ago; within {INGESTION_GRACE_S}s ingestion grace",
                       exec_id)
        if not ev.get("telemetry_ok") and (age is None or age < MAX_OBSERVATION_S):
            return _mk(ACTION_NONE, CLASS_PENDING, False,
                       "lifecycle telemetry unavailable (%s); observing"
                       % (ev.get("telemetry_error") or "unknown"), exec_id)
        if age is not None and age < MAX_OBSERVATION_S and backoff is not True:
            return _mk(ACTION_NONE, CLASS_PENDING, False,
                       "terminal but evidence is inconclusive; observing", exec_id)

        # 1f) Observation window exhausted, or evidence conflicts. Refund — the user got
        #     nothing — but do NOT attribute a cause we cannot support.
        return _mk(ACTION_REFUND, CLASS_UNCLASSIFIED_TERMINAL_FAILURE, False,
                   diag or ("terminal with insufficient or conflicting evidence after "
                            f"{MAX_OBSERVATION_S}s"), exec_id)

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
    if "ManuallyStopped" in reason_evt or ev.get("_legacy_manually_stopped"):
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
