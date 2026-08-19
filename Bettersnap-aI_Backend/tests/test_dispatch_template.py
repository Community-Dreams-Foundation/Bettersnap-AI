"""H2 regression: the extracted _start_execution helper must build the SAME
JobExecutionTemplate the two dispatchers built before the dedup.

Unit tests elsewhere mock shared.queue_trigger, so the ACA template-building code was
never exercised. Here we stub the Azure SDK at the boundary, capture the template passed
to begin_start, and assert — for BOTH the inference and training dispatchers — that:
  - owned env keys are stripped and the caller's overrides appended (order preserved)
  - non-owned baked env is carried through untouched
  - ContainerResources is rebuilt explicitly (cpu float, memory str) — the SIGKILL workaround
  - volume_mounts are preserved verbatim (the /models mount)
  - name/image are echoed back
  - the execution id is returned (poller happy path AND the list-recovery fallback)

Run: python -m unittest tests.test_dispatch_template   (from the backend dir)
"""
import os
import sys
import json
import types
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

CAPTURED = {}
VOLUME_MOUNTS = ["<models-azurefile-mount>"]   # module const so identity (assertIs) is checkable


# ── Fake ARM SDK models (record their kwargs) ────────────────────────────────
class FakeEnvVar:
    def __init__(self, name=None, value=None): self.name, self.value = name, value


class FakeResources:
    def __init__(self, cpu=None, memory=None): self.cpu, self.memory = cpu, memory


class FakeContainer:
    def __init__(self, name=None, image=None, env=None, resources=None, volume_mounts=None):
        self.name, self.image, self.env = name, image, env
        self.resources, self.volume_mounts = resources, volume_mounts


class FakeTemplate:
    def __init__(self, containers=None): self.containers = containers


def _base_container():
    """What the live job template 'returns' — includes stale owned vars (to prove they are
    stripped) and non-owned vars (to prove they are carried through)."""
    return FakeContainer(
        name="unified-container",
        image="registry/inference@sha256:deadbeef",
        env=[
            FakeEnvVar("KEEP_ME", "keep"), FakeEnvVar("RANK", "32"),
            FakeEnvVar("MODE", "STALE"), FakeEnvVar("USER_ID", "STALE-USER"),
            FakeEnvVar("JOB_ID", "STALE-JOB"), FakeEnvVar("FILES_JSON", "STALE-FILES"),
        ],
        resources=FakeResources(cpu=24.0, memory="220Gi"),
        volume_mounts=VOLUME_MOUNTS,
    )


class _RaisingResult:
    def result(self):  # simulate a fast container failure: poller.result() raises
        raise RuntimeError("container failed fast")


class _OkResult:
    def result(self):
        return types.SimpleNamespace(name="exec-happy")


class FakeJobs:
    def get(self, rg, job_name):
        return types.SimpleNamespace(
            template=types.SimpleNamespace(containers=[_base_container()]))

    def begin_start(self, resource_group_name, job_name, template):
        CAPTURED["template"] = template
        CAPTURED["job_name"] = job_name
        return _RaisingResult() if CAPTURED.get("raise_result") else _OkResult()


class FakeJobsExecutions:
    def list(self, rg, job_name):
        # newest-by-start_time recovery target
        return [types.SimpleNamespace(name="exec-recovered", start_time=1,
                                      properties=types.SimpleNamespace(start_time=1))]


class FakeClient:
    def __init__(self, *a, **k):
        self.jobs = FakeJobs()
        self.jobs_executions = FakeJobsExecutions()

    def job_execution(self, rg, job_name, execution_id):
        CAPTURED["outcome_request"] = (rg, job_name, execution_id)
        return types.SimpleNamespace(
            name=execution_id,
            status="Failed",
            start_time="start",
            end_time="end",
        )


def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


# Stub the Azure SDK BEFORE importing the real dispatchers.
_mod("azure")
_mod("azure.identity", DefaultAzureCredential=lambda *a, **k: object())
_mod("azure.mgmt")
_mod("azure.mgmt.appcontainers", ContainerAppsAPIClient=FakeClient)
_mod("azure.mgmt.appcontainers.models",
     JobExecutionTemplate=FakeTemplate, Container=FakeContainer,
     EnvironmentVar=FakeEnvVar, ContainerResources=FakeResources)

# Import the REAL dispatchers WITHOUT disturbing a sibling test module's stubs: when the whole
# suite runs in one process, test_dispatch_logic replaces sys.modules["shared.queue_trigger"]
# with a Mock. Pop any such stub, import the real modules into local references, then restore
# the stub — so this is order-independent and leaves the other module's tests untouched.
import importlib  # noqa: E402
_saved = {n: sys.modules.get(n) for n in ("shared.queue_trigger", "shared.training_trigger")}
for _n in _saved:
    sys.modules.pop(_n, None)
try:
    queue_trigger = importlib.import_module("shared.queue_trigger")
    training_trigger = importlib.import_module("shared.training_trigger")
finally:
    for _n, _m in _saved.items():
        if _m is not None:
            sys.modules[_n] = _m
        else:
            sys.modules.pop(_n, None)


class DispatchTemplateTests(unittest.TestCase):
    def setUp(self):
        CAPTURED.clear()

    def _container(self):
        return CAPTURED["template"].containers[0]

    def _assert_common(self, c):
        # ContainerResources rebuilt EXACTLY (the SIGKILL workaround)
        self.assertIsInstance(c.resources, FakeResources)
        self.assertEqual(c.resources.cpu, 24.0)
        self.assertEqual(c.resources.memory, "220Gi")
        # volume mounts preserved verbatim (same object)
        self.assertIs(c.volume_mounts, VOLUME_MOUNTS)
        # name + image echoed back
        self.assertEqual(c.name, "unified-container")
        self.assertEqual(c.image, "registry/inference@sha256:deadbeef")
        # non-owned baked env carried through untouched
        d = {e.name: e.value for e in c.env}
        self.assertEqual(d["KEEP_ME"], "keep")
        self.assertEqual(d["RANK"], "32")

    def test_inference_template(self):
        exec_id = queue_trigger.trigger_container_job("job-42", "user-99")
        c = self._container()
        self._assert_common(c)
        names = [e.name for e in c.env]
        d = {e.name: e.value for e in c.env}
        # owned {JOB_ID, USER_ID, MODE} stripped-then-set (no duplicates), values are the new ones
        for k in ("JOB_ID", "USER_ID", "MODE"):
            self.assertEqual(names.count(k), 1, f"{k} duplicated/not-stripped")
        self.assertEqual(d["MODE"], "infer")
        self.assertEqual(d["USER_ID"], "user-99")
        self.assertEqual(d["JOB_ID"], "job-42")
        self.assertEqual(exec_id, "exec-happy")

    def test_training_template(self):
        files = [{"blob": "user-7/input/crop_upperbody/img0.jpg"}]
        exec_id = training_trigger.trigger_training_job("user-7", files, "woman")
        c = self._container()
        self._assert_common(c)
        names = [e.name for e in c.env]
        d = {e.name: e.value for e in c.env}
        # _OWNED_ENV {USER_ID, FILES_JSON, MODE, ...} stripped-then-set (no duplicates)
        for k in ("USER_ID", "FILES_JSON", "MODE", "CLASS_WORD", "INSTANCE_PROMPT", "CLASS_PROMPT"):
            self.assertEqual(names.count(k), 1, f"{k} duplicated/not-stripped")
        self.assertEqual(d["MODE"], "train")
        self.assertEqual(d["USER_ID"], "user-7")
        self.assertEqual(json.loads(d["FILES_JSON"]), files)
        self.assertEqual(d["CLASS_WORD"], "woman")
        self.assertEqual(exec_id, "exec-happy")

    def test_execution_id_recovery_fallback(self):
        # When poller.result() raises (fast failure), the id is recovered from the list.
        CAPTURED["raise_result"] = True
        exec_id = queue_trigger.trigger_container_job("job-1", "user-1")
        self.assertEqual(exec_id, "exec-recovered")

    def test_execution_outcome_uses_authoritative_aca_resource(self):
        outcome = queue_trigger.get_job_execution_outcome("exec-oom")

        self.assertEqual(outcome["execution_id"], "exec-oom")
        self.assertEqual(outcome["execution_status"], "failed")
        self.assertEqual(outcome["start_time"], "start")
        self.assertEqual(outcome["end_time"], "end")
        self.assertEqual(
            CAPTURED["outcome_request"],
            (queue_trigger.RESOURCE_GROUP, queue_trigger.JOB_NAME, "exec-oom"),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
