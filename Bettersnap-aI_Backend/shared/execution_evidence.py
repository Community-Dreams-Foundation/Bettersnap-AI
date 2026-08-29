"""ONE explicit structure describing what we actually know about an ACA job execution.

WHY THIS EXISTS
---------------
Classification used to read a single `exit_code` key that meant different things depending on
who filled it in — sometimes the GPU preflight probe's exit, sometimes the inference container's
exit, sometimes nothing at all. Those are three different facts with three different
consequences, and collapsing them lets a passing preflight (exit 0) look like a successful
container run.

Worse, ABSENCE was being read as evidence. A missing preflight blob is not proof the container
never started — the blob write is best-effort and can simply have failed. Treating "no blob" as
"pre-container failure" would retry genuine application failures forever.

So every field here is TRI-STATE:

    True / False / int   -> we observed this
    None                 -> we do NOT know

and `telemetry_ok` records whether the evidence fetch itself succeeded. A caller must never
infer a lifecycle event from the absence of another signal.

FIELDS
------
execution_id          str | None   ACA execution name.
arm_status            str | None   Lowercased ARM/ACA execution status ('succeeded', 'failed',
                                   'running', ...). The control plane's own verdict on the whole
                                   execution. None when unknown.
preflight_exit_code   int | None   Exit code of the GPU probe SUBPROCESS (gpu_preflight.py:
                                   42/43/44 = infra). NEVER a container outcome; exit 0 here
                                   means only that the probe passed before inference began.
container_exit_code   int | None   Exit code of the INFERENCE CONTAINER, parsed from the
                                   ContainerTerminated lifecycle event. None when the container
                                   never terminated, or when telemetry is unavailable.
events                tuple        Lifecycle events in time order, each {"reason", "at"}.
                                   EMPTY when telemetry_ok is False — never fabricated.
container_started     bool | None  ContainerStarted observed. None when telemetry unavailable.
backoff_limit_exceeded bool | None BackoffLimitExceeded observed (terminal give-up by the
                                   scheduler). None when telemetry unavailable.
pod_deletion          str | None   PodDeletion / SuccessfulDelete REASON name, when present.
pod_deletion_detail   str | None   The PodDeletion message itself, e.g. "...has exited with
                                   status Succeeded". The reason name alone cannot tell a clean
                                   teardown from a failed one, so both are kept.
telemetry_ok          bool         True only if the lifecycle query actually returned rows.
                                   False => events/container_started/backoff are UNKNOWN.
telemetry_error       str | None   Why the fetch failed, for the diagnostic trail.
terminal_observed_at  float | None Epoch seconds when this execution attempt was FIRST observed
                                   terminal. Per ATTEMPT, not per row: a retry begins a new
                                   attempt and must reset it (see migration 033).

This module is PURE: no Azure, no DB, no network. Collection lives in queue_trigger.
"""

# Lifecycle reasons we care about, as ACA emits them.
EV_CONTAINER_STARTED = "ContainerStarted"
EV_CONTAINER_TERMINATED = "ContainerTerminated"
EV_BACKOFF_LIMIT_EXCEEDED = "BackoffLimitExceeded"
EV_POD_DELETION = "PodDeletion"
EV_PULLING_IMAGE = "PullingImage"
EV_PULLED_IMAGE = "PulledImage"
EV_ASSIGNING_REPLICA = "AssigningReplica"

FIELDS = (
    "execution_id", "arm_status", "preflight_exit_code", "container_exit_code",
    "events", "container_started", "backoff_limit_exceeded", "pod_deletion",
    "pod_deletion_detail",
    "telemetry_ok", "telemetry_error", "terminal_observed_at",
)


def make_evidence(*, execution_id=None, arm_status=None, preflight_exit_code=None,
                  container_exit_code=None, events=None, telemetry_ok=False,
                  telemetry_error=None, terminal_observed_at=None):
    """Build the evidence structure, deriving observations ONLY from real events.

    When `telemetry_ok` is False the derived observations are None (unknown) rather than
    False — "we did not see ContainerStarted" and "we could not look" are different facts and
    only the first may support a pre-container verdict."""
    evs = tuple(events or ())
    if telemetry_ok:
        reasons = {e.get("reason") for e in evs}
        container_started = EV_CONTAINER_STARTED in reasons
        backoff = EV_BACKOFF_LIMIT_EXCEEDED in reasons
        pod_deletion = None
        pod_deletion_detail = None
        for e in evs:
            if e.get("reason") in (EV_POD_DELETION, "SuccessfulDelete"):
                pod_deletion = e.get("reason")
                # Keep the MESSAGE, not just the reason name: PodDeletion rows carry the
                # outcome ("...has exited with status Succeeded" / "...status Failed"), which
                # is the part that actually distinguishes a clean teardown from a failed one.
                pod_deletion_detail = e.get("log") or None
                break
    else:
        evs = ()                       # never surface partial/fabricated events
        container_started = None
        backoff = None
        pod_deletion = None
        pod_deletion_detail = None
    return {
        "execution_id": execution_id,
        "arm_status": (arm_status or "").lower() or None,
        "preflight_exit_code": preflight_exit_code,
        "container_exit_code": container_exit_code,
        "events": evs,
        "container_started": container_started,
        "backoff_limit_exceeded": backoff,
        "pod_deletion": pod_deletion,
        "pod_deletion_detail": pod_deletion_detail,
        "telemetry_ok": bool(telemetry_ok),
        "telemetry_error": telemetry_error,
        "terminal_observed_at": terminal_observed_at,
    }


def from_legacy(exec_data):
    """Adapt the pre-existing exec_data dict to this structure without changing its meaning.

    The legacy shape carried `execution_status`, `preflight_exit_code`, `reason`, `events` and
    an overloaded `exit_code`. Callers and tests still pass it, so the classifier accepts both.

    IMPORTANT: legacy `exit_code` is NOT mapped to container_exit_code. In the legacy shape it
    was the value startup_stall reasoned about and was frequently None-meaning-unknown; silently
    promoting it to a container outcome would recreate the exact conflation this module removes.
    Legacy callers therefore get container_exit_code=None and are classified on ARM status and
    events, which is what they did before."""
    if exec_data is None:
        return make_evidence()
    if _is_evidence(exec_data):
        return exec_data
    events = tuple(exec_data.get("events") or ())
    # Legacy callers supplied events directly; their presence is the only telemetry signal we
    # have. An explicitly-supplied empty list is still "looked, saw nothing" for those callers.
    telemetry_ok = "events" in exec_data
    ev = make_evidence(
        execution_id=exec_data.get("execution_id"),
        arm_status=exec_data.get("execution_status"),
        preflight_exit_code=exec_data.get("preflight_exit_code"),
        container_exit_code=None,
        events=events,
        telemetry_ok=telemetry_ok,
        terminal_observed_at=exec_data.get("terminal_observed_at"),
    )
    ev["_legacy_reason"] = exec_data.get("reason") or ""
    ev["_legacy_manually_stopped"] = bool(exec_data.get("manually_stopped"))
    ev["_diagnostic_reason"] = exec_data.get("diagnostic_reason")
    return ev


def _is_evidence(d):
    return isinstance(d, dict) and "telemetry_ok" in d and "arm_status" in d


def has_event(evidence, reason):
    """True only when the event was actually observed. Unknown telemetry returns False here;
    callers that must distinguish unknown from absent read telemetry_ok directly."""
    return any(e.get("reason") == reason for e in (evidence.get("events") or ()))


def age_since_terminal(evidence, now):
    """Seconds since this attempt was first observed terminal, or None when unknown."""
    at = evidence.get("terminal_observed_at")
    if at is None or now is None:
        return None
    return max(0.0, float(now) - float(at))
