"""Startup-stall classifier for `bettersnapai-if` executions.

Distinguishes an IMAGE-PULL stall from a POST-PULL container-start stall using the
ContainerAppSystemLogs lifecycle events (AssigningReplica, PulledImage, ContainerStarted,
ContainerTerminated) — which stay fresh even when the console-log pipeline lags. These are
PURE functions (no Azure calls): the ops watcher / dispatcher supplies the events and the
current time, and this decides whether a deadline has passed and how to classify.

WHY (the 2026-07-31 incident)
-----------------------------
Run #2 sat in image-pull for 17.3 min (PulledImage at t+1042s), never reached
ContainerStarted, and was stopped by an operator at t+1160s. A naive "stop at 8 min" rule
would have mislabelled it, and stopping at 1160s was in fact PREMATURE — the pull had only
just finished 118s earlier. This classifier encodes the correct policy:

  * Do NOT blindly stop at 8 minutes.
  * If the image is still pulling at the 8-min mark, that alone is the pull-stall signal.
  * If the image JUST finished pulling, give the container a post-pull grace window to
    start before calling it a stall.
  * Only past the final calculated deadline is it an infrastructure stall, and the two
    causes are labelled separately so pull-latency is never conflated with a device/start
    problem.

Never consume credits for either infra-failure class — the caller routes both to the
backend 'failed' path, which refunds (function_app.py:1647/1691).
"""

START_THRESHOLD_S = 480   # replica-assignment -> first ContainerStarted budget (8 min)
POST_PULL_GRACE_S = 300   # extra budget to reach ContainerStarted AFTER pull completes (5 min)

# classifications
RUNNING = "RUNNING_OR_STARTED"            # ContainerStarted seen -> not a startup stall
TERMINATED = "TERMINATED"                 # ContainerTerminated seen
PENDING = "PENDING"                       # within deadline -> keep waiting, do NOT stop
INFRA_IMAGE_PULL_STALL = "INFRA_IMAGE_PULL_STALL"
INFRA_POST_PULL_START_STALL = "INFRA_POST_PULL_START_STALL"

# both infra classes are credit-safe: the caller must refund, never charge, for these
INFRA_STALL_CLASSES = frozenset({INFRA_IMAGE_PULL_STALL, INFRA_POST_PULL_START_STALL})


def _first(events, reason):
    """Earliest timestamp for a lifecycle reason, or None. `time` is any comparable number
    (epoch seconds recommended)."""
    times = [e["time"] for e in events
             if e.get("reason") == reason and e.get("time") is not None]
    return min(times) if times else None


def classify_startup(events, now, *, start_threshold_s=START_THRESHOLD_S,
                     post_pull_grace_s=POST_PULL_GRACE_S):
    """events: [{'reason': <lifecycle reason>, 'time': <number>}, ...]
    now: current time, same unit as `time`.

    Returns dict(classification, should_stop, deadline, reason). `should_stop` is True ONLY
    for a confirmed infrastructure stall past its deadline."""
    # Explicit None checks — a valid timestamp of 0 is falsy, so `a or b` would wrongly
    # discard it (caught by test_startup_stall's relative-second fixtures).
    t_assign = _first(events, "AssigningReplica")
    if t_assign is None:
        t_assign = _first(events, "SuccessfulCreate")
    t_pull = _first(events, "PulledImage")
    t_start = _first(events, "ContainerStarted")
    t_term = _first(events, "ContainerTerminated")

    if t_term is not None:
        return {"classification": TERMINATED, "should_stop": False,
                "deadline": None, "reason": "execution already terminated"}
    if t_start is not None:
        return {"classification": RUNNING, "should_stop": False,
                "deadline": None, "reason": "container started; not a startup stall"}
    if t_assign is None:
        return {"classification": PENDING, "should_stop": False,
                "deadline": None, "reason": "no replica-assignment event yet"}

    if t_pull is not None:
        # Pull finished but no start yet: measure the grace window FROM pull completion, so
        # a pull that finished near/after the 8-min mark still gets its full start budget.
        deadline = t_pull + post_pull_grace_s
        stall_class = INFRA_POST_PULL_START_STALL
        stall_reason = ("image pulled but container did not reach ContainerStarted within "
                        "the post-pull grace window")
    else:
        # Still pulling: the pull itself is the thing that overran.
        deadline = t_assign + start_threshold_s
        stall_class = INFRA_IMAGE_PULL_STALL
        stall_reason = "image did not finish pulling within the pull threshold"

    if now < deadline:
        return {"classification": PENDING, "should_stop": False, "deadline": deadline,
                "reason": f"within deadline; do not stop before {deadline}"}
    return {"classification": stall_class, "should_stop": True, "deadline": deadline,
            "reason": stall_reason}
