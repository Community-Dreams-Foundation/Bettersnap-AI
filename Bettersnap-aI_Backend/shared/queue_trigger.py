import logging
from azure.identity import DefaultAzureCredential
from azure.mgmt.appcontainers import ContainerAppsAPIClient
from azure.mgmt.appcontainers.models import (
    JobExecutionTemplate, Container, EnvironmentVar, ContainerResources,
)

SUBSCRIPTION_ID = "cf197124-2e9a-48d5-af4b-de22fbbd683e"
RESOURCE_GROUP = "bettersnap-ai-rg"
JOB_NAME = "bettersnapai-if"
# EVERY job that consumes the A100 workload profile. There is now exactly ONE such job
# (bettersnapai-if on profile bettersnapaiWPn, Consumption-GPU-NC24-A100, 24 CPU/220Gi):
# a training execution and an inference execution both land on it, so counting its live
# executions IS the cap. (Listing a retired job's executions would raise, so the trainer
# name is intentionally NOT here.)
GPU_JOB_NAMES = (JOB_NAME,)

# Execution states that mean an A100 replica is (or may be) consuming GPU.
# Anything NOT terminal counts as active — conservative on purpose: when a
# state is ambiguous we treat it as active so we don't start another job over
# the cap. replicaTimeout guarantees a stuck execution clears, so this
# can't deadlock the queue permanently.
_TERMINAL_STATES = {"succeeded", "failed", "stopped", "degraded", "cancelled"}


def count_active_job_executions() -> int:
    """Source of truth for how many A100 jobs are live, read from the Azure
    Container Apps job-executions API (NOT the DB, which can be stale or race
    the container's own status write). Counts inference AND training executions —
    they share one GPU profile, so they must share one cap."""
    credential = DefaultAzureCredential()
    client = ContainerAppsAPIClient(credential, SUBSCRIPTION_ID)
    active = 0
    for job_name in GPU_JOB_NAMES:
        for ex in client.jobs_executions.list(RESOURCE_GROUP, job_name):
            status = (getattr(ex, "status", "") or "").lower()
            if status not in _TERMINAL_STATES:
                active += 1
    return active


def _newest_execution_name(client, job_name: str = JOB_NAME) -> str:
    """Name of the most-recently-started execution for `job_name`. Used to recover the
    execution id when the begin_start LRO does not resolve cleanly (slow GPU start, or
    a fast container failure makes poller.result() raise)."""
    newest, newest_t = None, None
    for ex in client.jobs_executions.list(RESOURCE_GROUP, job_name):
        t = getattr(getattr(ex, "properties", None), "start_time", None) or getattr(ex, "start_time", None)
        if t is not None and (newest_t is None or t > newest_t):
            newest_t, newest = t, getattr(ex, "name", None)
    return newest


def find_execution_for_job(job_id: str, job_name: str = JOB_NAME) -> str:
    """Find the ACA execution carrying ``JOB_ID=job_id``.

    This closes the unavoidable gap between ACA accepting ``begin_start`` and SQL
    recording the returned execution name.  The queue retry/reaper can reconcile
    that execution instead of either starting a duplicate or refunding a live run.
    """
    credential = DefaultAzureCredential()
    client = ContainerAppsAPIClient(credential, SUBSCRIPTION_ID)
    for summary in client.jobs_executions.list(RESOURCE_GROUP, job_name):
        execution = summary
        # List responses are not guaranteed to contain the execution template.
        if not getattr(getattr(execution, "template", None), "containers", None):
            name = getattr(summary, "name", None)
            if not name:
                continue
            try:
                execution = client.jobs_executions.get(RESOURCE_GROUP, job_name, name)
            except Exception:
                logging.exception(f"could not inspect ACA execution={name}")
                continue
        for container in (getattr(getattr(execution, "template", None), "containers", None) or []):
            for env in (getattr(container, "env", None) or []):
                if getattr(env, "name", None) == "JOB_ID" and str(getattr(env, "value", "")) == str(job_id):
                    return getattr(execution, "name", None) or getattr(summary, "name", None)
    return None


def _start_execution(owned_env_keys, env_overrides, job_name: str = JOB_NAME) -> str:
    """Start ONE execution of `job_name`. Extracted verbatim from the two dispatchers
    (inference + training) — NO behavior change; only the per-run env policy differs and
    that is supplied by the caller (owned_env_keys to strip + env_overrides to set).

    ACA REPLACES (does not merge) the container spec when a start-time execution template
    is supplied, so the override must echo back EVERYTHING the live job template defines:
      - name + image: without these the env override is dropped (vars never reach the container).
      - resources: rebuilt EXPLICITLY — passing base.resources back through begin_start does
        NOT round-trip; ACA silently drops it to the platform default 0.5 CPU / 1Gi, which
        SIGKILLs the container (exit 137) during peft's LoRA load vs the 220Gi it needs. A
        freshly-constructed ContainerResources IS honored (== `job start --cpu 24 --memory 220Gi`).
      - volume_mounts: the /models AzureFile mount; omitting it launches with no model mount,
        so from_pretrained('/models') fails. (JobExecutionTemplate has no `volumes` field —
        volumes stay at the job-template level, referenced here only via volume_mounts.)
    Reading every field from the live job keeps this correct across image/profile bumps.

    begin_start SUBMITTING is the point of no return: once it returns the execution exists
    server-side. poller.result() can block on a slow GPU start or RAISE on a fast failure —
    that raise used to lose the execution id, so recover it by listing.
    """
    credential = DefaultAzureCredential()
    client = ContainerAppsAPIClient(credential, SUBSCRIPTION_ID)

    job = client.jobs.get(RESOURCE_GROUP, job_name)
    base = job.template.containers[0]

    env = [e for e in (base.env or []) if e.name not in owned_env_keys]
    env.extend(env_overrides)

    resources = ContainerResources(
        cpu=float(base.resources.cpu),
        memory=str(base.resources.memory),
    )
    template = JobExecutionTemplate(
        containers=[
            Container(
                name=base.name,
                image=base.image,
                env=env,
                resources=resources,
                volume_mounts=base.volume_mounts,
            )
        ]
    )
    logging.info(
        f"ACA start: job={job_name} image={base.image} "
        f"cpu={resources.cpu} memory={resources.memory}"
    )

    poller = client.jobs.begin_start(
        resource_group_name=RESOURCE_GROUP,
        job_name=job_name,
        template=template,
    )
    try:
        execution_id = getattr(poller.result(), "name", None)
    except Exception as e:
        logging.warning(
            f"begin_start LRO did not resolve cleanly for job={job_name} ({e}); "
            f"recovering execution id from the executions list"
        )
        execution_id = None
    if not execution_id:
        execution_id = _newest_execution_name(client, job_name)
    return execution_id


def execution_status(execution_name: str, job_name: str = JOB_NAME) -> str:
    """Return the ACA status of ONE execution (e.g. 'Running', 'Succeeded', 'Failed', 'Stopped'),
    or '' if it can't be read. Lets the reaper tell a dead container from a healthy long-running
    one WITHOUT waiting out the full REAPER_STUCK_MINUTES window."""
    if not execution_name:
        return ""
    credential = DefaultAzureCredential()
    client = ContainerAppsAPIClient(credential, SUBSCRIPTION_ID)
    try:
        for ex in client.jobs_executions.list(RESOURCE_GROUP, job_name):
            if getattr(ex, "name", None) == execution_name:
                return (getattr(ex, "status", "") or "")
    except Exception as e:
        logging.warning(f"execution_status: could not read {execution_name}: {e}")
    return ""


def stop_execution(execution_name: str, job_name: str = JOB_NAME) -> bool:
    """Best-effort stop of ONE ACA job execution. Used by cancel to free the single A100 slot
    immediately, instead of letting a doomed run hold the GPU until it exits on its own. Safe to
    call with a blank name (no-op). Returns True if the stop was accepted by ACA."""
    if not execution_name:
        return False
    credential = DefaultAzureCredential()
    client = ContainerAppsAPIClient(credential, SUBSCRIPTION_ID)
    try:
        client.jobs.begin_stop_execution(RESOURCE_GROUP, job_name, execution_name).result()
        logging.info(f"stopped ACA execution={execution_name} (job={job_name})")
        return True
    except Exception as e:
        logging.warning(f"stop_execution: could not stop {execution_name}: {e}")
        return False


def trigger_container_job(job_id: str, user_id: str):
    # MODE=infer is set EXPLICITLY: this one job also runs training (MODE=train), so an
    # inference run must never inherit a stale/baked MODE and boot into the trainer.
    execution_id = _start_execution(
        owned_env_keys=("JOB_ID", "USER_ID", "MODE"),
        env_overrides=[
            EnvironmentVar(name="JOB_ID", value=job_id),
            EnvironmentVar(name="USER_ID", value=user_id),
            EnvironmentVar(name="MODE", value="infer"),
        ],
    )
    logging.info(f"Triggered Container Apps Job for job_id={job_id}, execution_id={execution_id}")
    return execution_id
