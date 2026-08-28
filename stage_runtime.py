"""Stage-initialization runner for the GPU worker's optional pipeline stages.

Pure orchestration — NO torch/diffusers/cv2 imports — so the enable/fatal/degrade policy
is unit-testable without the GPU stack. `main.py` owns the actual init_fn bodies and the
STAGE_STATUS dict; this module only decides what happens on success/failure and records a
machine-readable status entry. Tests import THIS `run_stage`, not a copy.

Policy (per the Phase-0 review):
  disabled              -> {enabled:False, initialized:False, reason:"disabled"}
  enabled + ok          -> {enabled:True,  initialized:True,  reason:"ok"}
  enabled + fail + fatal -> record reason:"init_failed" + error, then RAISE
  enabled + fail + !fatal -> record reason:<degraded_reason> + error, continue

`error` keeps "TypeName: message" so the manifest stays machine-readable AND debuggable.
`status` is passed in (not a module global) so the caller owns its lifetime — a worker
that ever serves concurrent jobs must give each job its own dict rather than sharing one.
"""


def run_stage(name, enabled, init_fn, status, *, fatal=True,
              degraded_reason="degraded", log=lambda _m: None):
    """Run init_fn() for one optional stage and record the outcome in status[name].

    Returns True if the stage is initialized and usable, False otherwise. On a fatal
    failure it raises RuntimeError (chained to the original) after recording status.
    """
    if not enabled:
        status[name] = {"enabled": False, "initialized": False, "reason": "disabled"}
        log(f"stage {name}: disabled")
        return False
    try:
        init_fn()
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        if fatal:
            status[name] = {"enabled": True, "initialized": False,
                            "reason": "init_failed", "error": err}
            log(f"stage {name}: FATAL init failure: {err}")
            raise RuntimeError(
                f"required stage '{name}' failed to initialize: {err}") from e
        status[name] = {"enabled": True, "initialized": False,
                        "reason": degraded_reason, "error": err}
        log(f"stage {name}: degraded ({degraded_reason}): {err}")
        return False
    status[name] = {"enabled": True, "initialized": True, "reason": "ok"}
    log(f"stage {name}: initialized")
    return True
