import datetime
import json
import logging
import os
import re
import urllib.request

from shared import execution_evidence
from azure.identity import DefaultAzureCredential
from azure.mgmt.appcontainers import ContainerAppsAPIClient
from azure.mgmt.appcontainers.models import (
    JobExecutionTemplate, Container, EnvironmentVar, ContainerResources,
)

# Which ACA job the GPU work runs on. Overridable by app setting so the region can be
# changed without a code edit — East US stopped allocating A100s on 2026-08-31 (every
# execution died at BackoffLimitExceeded +1s, before any image pull, quota confirmed
# clear at 0/2) and the only fix was to move regions.
#
# The DEFAULT is the West US 3 job, deliberately. It was East US, on the reasoning that a
# default should preserve existing behaviour — but on 2026-09-01 this setting was cleared
# by something outside this repo and production silently began routing generation into the
# region that cannot allocate a GPU, discoverable only from failed jobs minutes later. A
# default that fails into a known-broken region is not a safe default. Setting
# ACA_JOB_NAME=bettersnapai-if is now the explicit opt-in for going back.
#
# These three are read by BOTH dispatch (queue_trigger, training_trigger) and
# reconciliation (reaper, training_watcher, the GPU cap). They MUST move together: a
# dispatcher pointing at one region while the reconciler reads another makes every job
# look dead the moment it starts, and the reaper fails and refunds a healthy run.
SUBSCRIPTION_ID = os.environ.get(
    "ACA_SUBSCRIPTION_ID", "cf197124-2e9a-48d5-af4b-de22fbbd683e")
RESOURCE_GROUP = os.environ.get("ACA_RESOURCE_GROUP", "bettersnap-ai-rg")
JOB_NAME = os.environ.get("ACA_JOB_NAME", "bettersnapai-if-wus3-canary")
# EVERY job that consumes the A100 workload profile. There is exactly ONE such job at a
# time (Consumption-GPU-NC24-A100, 24 CPU/220Gi): a training execution and an inference
# execution both land on it, so counting its live executions IS the cap. Derived from
# JOB_NAME so it follows the region override automatically — hardcoding it would leave
# the cap counting a job nothing dispatches to, i.e. no cap at all.
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


_EPOCH_MIN = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


def _start_time_key(started):
    """Sort key for an execution's start_time. Executions with NO start_time sort OLDEST
    (they are never chosen over one that demonstrably started), and naive values are read as
    UTC so a mixed list cannot raise on comparison."""
    if started is None:
        return (0, _EPOCH_MIN)
    try:
        if getattr(started, "tzinfo", None) is None:
            started = started.replace(tzinfo=datetime.timezone.utc)
        return (1, started)
    except (AttributeError, TypeError, ValueError):
        return (0, _EPOCH_MIN)


def list_executions_for_job(job_id: str, job_name: str = JOB_NAME, client=None):
    """EVERY ACA execution carrying ``JOB_ID=job_id``, as (name, start_time) pairs.

    A job used to have at most one execution ever, so "the first match" was "the only match".
    Bounded provisioning retries break that: after one retry ACA holds two executions for the
    same JOB_ID, and after two, three. Callers must therefore choose deliberately rather than
    take whatever the list yields first.
    """
    if client is None:
        credential = DefaultAzureCredential()
        client = ContainerAppsAPIClient(credential, SUBSCRIPTION_ID)
    found = []
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
        matched = False
        for container in (getattr(getattr(execution, "template", None), "containers", None) or []):
            for env in (getattr(container, "env", None) or []):
                # EXACT match: 'J1' must never match 'J12'.
                if (getattr(env, "name", None) == "JOB_ID"
                        and str(getattr(env, "value", "")) == str(job_id)):
                    matched = True
                    break
            if matched:
                break
        if not matched:
            continue
        name = getattr(execution, "name", None) or getattr(summary, "name", None)
        if not name:
            continue
        started = (getattr(getattr(execution, "properties", None), "start_time", None)
                   or getattr(execution, "start_time", None)
                   or getattr(summary, "start_time", None))
        found.append((name, started))
    return found


def find_execution_for_job(job_id: str, job_name: str = JOB_NAME, *,
                           exclude=(), not_before=None, client=None) -> str:
    """The NEWEST execution of `job_id` that has not already been reconciled.

    This closes the unavoidable gap between ACA accepting ``begin_start`` and SQL recording
    the returned execution name — the reaper and the dispatcher both use it to recover an
    execution id after a crash in that window.

    ORDERING IS NOW LOAD-BEARING. Returning an arbitrary match was safe only while one job
    had one execution. With retries, recovering a STALE terminal execution would pin the row
    to an outcome that was already reconciled, and the row could never reach a new verdict.
    So:

      * sort by start_time DESCENDING, execution name descending as the tie-break, so equal
        (or missing) start times still produce ONE deterministic answer;
      * an execution with no start_time sorts oldest — never preferred over one that started;
      * `exclude` drops every id already recorded in provisioning_execution_ids;
      * `not_before` (optional) drops anything that started before the current dispatch
        attempt, e.g. the row's dispatched_at;
      * return None when every match was already handled — the caller must then treat the row
        as an orphan rather than re-adopting a spent execution.
    """
    excluded = {str(e) for e in (exclude or ())}
    nb_key = _start_time_key(not_before) if not_before is not None else None
    eligible = []
    for name, started in list_executions_for_job(job_id, job_name, client=client):
        if str(name) in excluded:
            continue
        key = _start_time_key(started)
        if nb_key is not None and key < nb_key:
            continue
        eligible.append((key, str(name)))
    if not eligible:
        return None
    eligible.sort(reverse=True)
    return eligible[0][1]


def get_job_execution_outcome(execution_id: str, job_name: str = JOB_NAME) -> dict:
    """Return ACA's authoritative status for one job execution.

    The preflight diagnostic blob describes only the GPU probe subprocess. A successful
    preflight therefore cannot prove the inference container later completed. ACA's job
    execution resource is the control-plane source of truth for the whole container run.
    """
    credential = DefaultAzureCredential()
    client = ContainerAppsAPIClient(credential, SUBSCRIPTION_ID)
    execution = client.job_execution(RESOURCE_GROUP, job_name, execution_id)
    return {
        "execution_id": getattr(execution, "name", None) or execution_id,
        "execution_status": (getattr(execution, "status", "") or "").lower(),
        "start_time": getattr(execution, "start_time", None),
        "end_time": getattr(execution, "end_time", None),
    }


def _epoch(ts):
    """Parse a Log Analytics TimeGenerated value into epoch seconds, or None.

    startup_stall compares event times numerically (`_first` does min() over `time`), so an ISO
    string would raise or silently mis-order. Returns None rather than guessing on an
    unparseable value; an event with time=None is skipped by startup_stall's filters."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    s = str(ts).strip().replace("Z", "+00:00")
    # Log Analytics emits up to 7 fractional digits; datetime accepts at most 6.
    m = re.match(r"^(.*\.\d{6})\d*(\+\d{2}:\d{2}|-\d{2}:\d{2})?$", s)
    if m:
        s = m.group(1) + (m.group(2) or "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except Exception:                            # noqa: BLE001
        return None


def _row_belongs_to(execution_name, exec_name_col, replica_col, log_col):
    """Does this Log Analytics row belong to THIS execution?

    ACA does not populate ExecutionName_s on every row — controller/execution-level rows
    (observed in production: EventSource_s='ContainerAppController') carry it blank. Three
    correlations, each anchored on the EXACT execution name so a similarly-named execution can
    never cross-match:

      1. ExecutionName_s == '<exec>'         exact, when populated
      2. ReplicaName_s startswith '<exec>-'  replicas are '<exec>-<suffix>'; the trailing
                                             hyphen stops '...-abc' matching '...-abcd'
      3. Log_s mentions '<exec>' DELIMITED   execution-level rows with no replica

    (3) must not be a bare substring test: execution names share prefixes, so 'e-1' would
    otherwise absorb a row reading "container of e-12 started". The occurrence must be the
    WHOLE identifier — hyphen counts as a name character on BOTH sides, so 'e-1' rejects
    'e-12', 'xe-1', 'x-e-1' and 'e-1-other' alike. Suffix matching is the replica rule's job
    (2) and belongs to ReplicaName_s only; a log naming 'e-1-other' names a DIFFERENT
    execution, not a replica of this one. Punctuation, whitespace, quotes or end-of-string
    may delimit it: "execution e-1 failed", "Job Execution 'e-1'", "(e-1)".
    """
    if (exec_name_col or "").strip() == execution_name:
        return True
    if (replica_col or "").startswith(execution_name + "-"):
        return True
    return re.search(r"(?<![A-Za-z0-9-])%s(?![A-Za-z0-9-])" % re.escape(execution_name),
                     log_col or "") is not None


def fetch_lifecycle_events(execution_name: str, workspace_id: str = None, http=None):
    """Fetch the REAL ACA lifecycle events for one execution from Log Analytics.

    Returns (events, container_exit_code, error):
      events              list[{"reason", "time", "at", "log"}] in time order, or None when the
                          query failed, returned nothing, or returned nothing FOR THIS
                          execution. `time` is epoch seconds and is authoritative for
                          classification; `at` preserves the raw ISO value for diagnostics.
      container_exit_code int parsed from the ContainerTerminated message, else None.
      error               short string describing why events is None, else None.

    (None, None, "<reason>") means UNKNOWN, never "nothing happened". Log Analytics ingestion
    lags ~90s, so a freshly-terminal execution legitimately has no rows yet, and rows belonging
    only to OTHER executions must not be reported as a successful empty observation.

    `http` is an injectable query runner (query_str -> response dict) for offline tests.
    """
    try:
        workspace_id = workspace_id or os.environ.get("LOG_ANALYTICS_WORKSPACE_ID")
        if not workspace_id:
            return None, None, "no LOG_ANALYTICS_WORKSPACE_ID configured"
        # The execution name is interpolated into KQL, so restrict it to the shape ACA emits.
        if not re.match(r"^[A-Za-z0-9\-]{1,128}$", execution_name or ""):
            return None, None, "invalid execution name"
        # Broad server-side filter, then an exact post-filter in Python. `has`/`contains` are
        # substring-ish, so the query alone could pull in a similarly-named execution.
        query = (
            "ContainerAppSystemLogs_CL "
            f"| where ExecutionName_s == '{execution_name}' "
            f"   or ReplicaName_s startswith '{execution_name}-' "
            f"   or Log_s contains '{execution_name}' "
            "| project ExecutionName_s, ReplicaName_s, TimeGenerated, Reason_s, Log_s "
            "| order by TimeGenerated asc")
        if http is not None:
            body = http(query)
        else:
            token = DefaultAzureCredential().get_token(
                "https://api.loganalytics.io/.default").token
            req = urllib.request.Request(
                f"https://api.loganalytics.io/v1/workspaces/{workspace_id}/query",
                data=json.dumps({"query": query}).encode(),
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                method="POST")
            with urllib.request.urlopen(req, timeout=20) as r:
                body = json.loads(r.read())
        tables = (body or {}).get("tables") or []
        if not tables or not tables[0].get("rows"):
            return None, None, "no rows ingested yet"
        cols = [c["name"] for c in tables[0]["columns"]]
        try:
            ei = cols.index("ExecutionName_s")
            pi = cols.index("ReplicaName_s")
            ti = cols.index("TimeGenerated")
            ri = cols.index("Reason_s")
            li = cols.index("Log_s")
        except ValueError as e:
            # A schema we do not recognise is UNKNOWN evidence, never an empty observation.
            return None, None, "unexpected columns: %s" % e
        events, container_exit_code = [], None
        for row in tables[0]["rows"]:
            if not _row_belongs_to(execution_name, row[ei], row[pi], row[li]):
                continue
            reason = row[ri]
            if not reason:
                continue
            log = row[li] or ""
            events.append({"reason": reason, "time": _epoch(row[ti]), "at": row[ti], "log": log})
            if reason == execution_evidence.EV_CONTAINER_TERMINATED:
                m = re.search(r"exit code '?(\d+)'?", log)
                if m:
                    container_exit_code = int(m.group(1))
        if not events:
            return None, None, "no rows for this execution"
        return events, container_exit_code, None
    except Exception as e:                       # noqa: BLE001 - any failure is UNKNOWN
        logging.warning("fetch_lifecycle_events(%s) failed: %s", execution_name, type(e).__name__)
        return None, None, "%s: %s" % (type(e).__name__, str(e)[:120])


def get_execution_evidence(execution_id: str, *, job_name: str = JOB_NAME,
                           preflight_exit_code=None, terminal_observed_at=None,
                           http=None, get_outcome=None, get_events=None) -> dict:
    """Assemble the full evidence structure for one execution.

    Two independent sources, kept separate on purpose:
      * ARM/ACA control plane  -> arm_status (authoritative verdict on the whole execution)
      * Log Analytics          -> lifecycle events + container exit code

    A failure of either source degrades that source to UNKNOWN; it never invents the other.
    `preflight_exit_code` is supplied by the caller (it comes from a blob, not from here) and is
    passed through untouched so a probe exit is never confused with a container exit.
    """
    get_outcome = get_outcome or get_job_execution_outcome
    get_events = get_events or fetch_lifecycle_events
    arm_status = None
    try:
        arm_status = (get_outcome(execution_id, job_name) or {}).get("execution_status")
    except Exception as e:                       # noqa: BLE001
        logging.warning("ARM status unavailable for %s: %s", execution_id, type(e).__name__)
    events, container_exit_code, err = get_events(execution_id, http=http)
    return execution_evidence.make_evidence(
        execution_id=execution_id,
        arm_status=arm_status,
        preflight_exit_code=preflight_exit_code,
        container_exit_code=container_exit_code,
        events=events,
        telemetry_ok=events is not None,
        telemetry_error=err,
        terminal_observed_at=terminal_observed_at,
    )


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
        # DO NOT block on .result(). begin_stop_execution issues the stop request and returns
        # a poller; .result() then waited for the whole long-running operation to finish,
        # unbounded, INSIDE the caller's HTTP request. cancel_job therefore hung on Azure
        # while the browser gave up at its own 30s timeout, showing "Couldn't cancel" for a
        # cancel the server went on to complete — the row said cancelled, the customer was
        # told it failed.
        #
        # The stop is ACCEPTED once this call returns without raising; waiting for the
        # container to actually exit buys the caller nothing.
        client.jobs.begin_stop_execution(RESOURCE_GROUP, job_name, execution_name)
        logging.info(f"stop requested for ACA execution={execution_name} (job={job_name})")
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
