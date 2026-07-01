import logging
from azure.identity import DefaultAzureCredential
from azure.mgmt.appcontainers import ContainerAppsAPIClient
from azure.mgmt.appcontainers.models import JobExecutionTemplate, Container, EnvironmentVar

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
    for ex in client.job_executions.list(RESOURCE_GROUP, JOB_NAME):
        status = (getattr(ex, "status", "") or "").lower()
        if status not in _TERMINAL_STATES:
            active += 1
    return active


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

    template = JobExecutionTemplate(
        containers=[
            Container(
                name=base.name,
                image=base.image,
                env=env,
                resources=base.resources,
                volume_mounts=base.volume_mounts,
            )
        ]
    )

    poller = client.jobs.begin_start(
        resource_group_name=RESOURCE_GROUP,
        job_name=JOB_NAME,
        template=template,
    )
    result = poller.result()
    # The execution id (name) is recorded against the job for dispatch
    # idempotency — a retried queue message for the same job_id must not start a
    # second A100. begin_start() only STARTS the job and returns; inference runs
    # in the separate container, so the caller's dispatch lease is NOT held
    # during inference.
    execution_id = getattr(result, "name", None)
    logging.info(
        f"Triggered Container Apps Job for job_id={job_id}, "
        f"image={base.image}, execution_id={execution_id}"
    )
    return execution_id