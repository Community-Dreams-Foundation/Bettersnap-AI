import logging
from azure.identity import DefaultAzureCredential
from azure.mgmt.appcontainers import ContainerAppsAPIClient
from azure.mgmt.appcontainers.models import (
    JobExecutionTemplate, Container, EnvironmentVar, ContainerResources,
)

SUBSCRIPTION_ID = "cf197124-2e9a-48d5-af4b-de22fbbd683e"
RESOURCE_GROUP = "bettersnap-ai-rg"
JOB_NAME = "bettersnapai-if"

# Execution states that mean an A100 replica is (or may be) consuming GPU.
# Anything NOT terminal counts as active — conservative on purpose: when a
# state is ambiguous we treat it as active so we don't start another job over
# the cap. replicaTimeout (1800s) guarantees a stuck execution clears, so this
# can't deadlock the queue permanently.
_TERMINAL_STATES = {"succeeded", "failed", "stopped", "degraded", "cancelled"}


def count_active_job_executions() -> int:
    """Source of truth for how many A100 jobs are live, read from the Azure
    Container Apps job-executions API (NOT the DB, which can be stale or race
    the container's own status write)."""
    credential = DefaultAzureCredential()
    client = ContainerAppsAPIClient(credential, SUBSCRIPTION_ID)
    active = 0
    for ex in client.jobs_executions.list(RESOURCE_GROUP, JOB_NAME):
        status = (getattr(ex, "status", "") or "").lower()
        if status not in _TERMINAL_STATES:
            active += 1
    return active


def _newest_execution_name(client) -> str:
    """Name of the most-recently-started execution for this job. Used to recover the
    execution id when the begin_start LRO does not resolve cleanly (slow GPU start, or
    a fast container failure makes poller.result() raise)."""
    newest, newest_t = None, None
    for ex in client.jobs_executions.list(RESOURCE_GROUP, JOB_NAME):
        t = getattr(getattr(ex, "properties", None), "start_time", None) or getattr(ex, "start_time", None)
        if t is not None and (newest_t is None or t > newest_t):
            newest_t, newest = t, getattr(ex, "name", None)
    return newest


def trigger_container_job(job_id: str, user_id: str):
    credential = DefaultAzureCredential()
    client = ContainerAppsAPIClient(credential, SUBSCRIPTION_ID)

    # ACA REPLACES (does not merge) the container spec when a start-time
    # execution template is supplied, so the override must echo back EVERYTHING
    # the job template defines for the container — not just name+image+env:
    #   - name + image: without these the env override is dropped (JOB_ID/USER_ID
    #     never reach the container).
    #   - resources: the GPU profile's fixed cpu/memory alloc.
    #   - volume_mounts: the /models AzureFile mount. Omitting it is a latent
    #     production bug — the manual `az containerapp job start` path preserves
    #     the full template, but THIS dispatch path would launch the container
    #     with no model mount, so from_pretrained('/models') fails.
    # (JobExecutionTemplate has no `volumes` field — volumes stay defined at the
    # job-template level and are referenced here only via volume_mounts.)
    # Reading every field from the live job keeps this correct across image and
    # profile bumps without hardcoding.
    job = client.jobs.get(RESOURCE_GROUP, JOB_NAME)
    base = job.template.containers[0]

    env = [e for e in (base.env or []) if e.name not in ("JOB_ID", "USER_ID")]
    env.append(EnvironmentVar(name="JOB_ID", value=job_id))
    env.append(EnvironmentVar(name="USER_ID", value=user_id))

    # CRITICAL: rebuild resources EXPLICITLY. Passing base.resources (the
    # ContainerResources object read back from jobs.get()) does NOT round-trip through
    # begin_start — ACA silently drops it and the execution falls back to the platform
    # default 0.5 CPU / 1Gi. That 1Gi caused a host-RAM SIGKILL (exit 137, ProcessExited)
    # during peft's LoRA load, while the job template says 220Gi. A freshly-constructed
    # ContainerResources with explicit cpu/memory IS honored — this is exactly what
    # `az containerapp job start --cpu 24 --memory 220Gi` sends and it launches at 220Gi.
    resources = ContainerResources(
        cpu=float(base.resources.cpu),
        memory=str(base.resources.memory),
    )
    logging.info(
        f"dispatch resources for job_id={job_id}: cpu={resources.cpu} memory={resources.memory}"
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

    # begin_start() SUBMITTING the start is the point of no return: if this raises the
    # execution was never created (caller reverts the claim + retries). Once it returns,
    # the execution EXISTS server-side. poller.result() can then either block on a slow
    # GPU start or RAISE if the container fails fast — in the old code that raise lost the
    # execution id (external_execution_id/last_dispatch_at stayed NULL). Recover the id by
    # listing so dispatch idempotency + observability survive a slow/failed start.
    poller = client.jobs.begin_start(
        resource_group_name=RESOURCE_GROUP,
        job_name=JOB_NAME,
        template=template,
    )
    try:
        result = poller.result()
        execution_id = getattr(result, "name", None)
    except Exception as e:
        logging.warning(
            f"begin_start LRO did not resolve cleanly for job_id={job_id} ({e}); "
            f"recovering execution id from the executions list"
        )
        execution_id = None
    if not execution_id:
        execution_id = _newest_execution_name(client)

    logging.info(
        f"Triggered Container Apps Job for job_id={job_id}, "
        f"image={base.image}, execution_id={execution_id}"
    )
    return execution_id