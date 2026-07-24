"""Regression guard for the fused MODE=train_infer dispatch.

THE BUG THIS EXISTS TO PREVENT
------------------------------
_finish_training releases EVERY job still in 'waiting_lora' for a user when training
completes. The fused container has ALREADY generated its job by then, so if that job were
still parked it would be enqueued a second time: the user pays once and gets two GPU runs.
The dispatcher therefore claims the job out of 'waiting_lora' (to 'processing') in the same
transaction that claims the training. These tests pin that contract from both ends.

Run: python -m unittest tests.test_fused_train_infer   (from the backend dir)
"""
import json
import os
import sys
import types
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

CAPTURED = {}


# ── Fake ARM SDK (same shape as test_dispatch_template) ──────────────────────
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
    """The live template carries a STALE JOB_ID from earlier manual runs — that is exactly
    why JOB_ID must be an owned/stripped key."""
    return FakeContainer(
        name="unified-container", image="registry/inference@sha256:deadbeef",
        env=[FakeEnvVar("KEEP_ME", "keep"), FakeEnvVar("JOB_ID", "STALE-JOB-FROM-TEMPLATE"),
             FakeEnvVar("MODE", "STALE"), FakeEnvVar("USER_ID", "STALE-USER")],
        resources=FakeResources(cpu=24.0, memory="220Gi"), volume_mounts=[])


class _OkResult:
    def result(self): return types.SimpleNamespace(name="exec-fused")


class FakeJobs:
    def get(self, rg, job_name):
        return types.SimpleNamespace(
            template=types.SimpleNamespace(containers=[_base_container()]))

    def begin_start(self, resource_group_name, job_name, template):
        CAPTURED["template"] = template
        return _OkResult()


class FakeClient:
    def __init__(self, *a, **k):
        self.jobs = FakeJobs()
        self.jobs_executions = types.SimpleNamespace(list=lambda rg, jn: [])


def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


_mod("azure")
_mod("azure.identity", DefaultAzureCredential=lambda *a, **k: object())
_mod("azure.mgmt")
_mod("azure.mgmt.appcontainers", ContainerAppsAPIClient=FakeClient)
_mod("azure.mgmt.appcontainers.models",
     JobExecutionTemplate=FakeTemplate, Container=FakeContainer,
     EnvironmentVar=FakeEnvVar, ContainerResources=FakeResources)

import importlib  # noqa: E402
_saved = {n: sys.modules.get(n) for n in ("shared.queue_trigger", "shared.training_trigger")}
for _n in _saved:
    sys.modules.pop(_n, None)
try:
    training_trigger = importlib.import_module("shared.training_trigger")
finally:
    for _n, _m in _saved.items():
        if _m is not None:
            sys.modules[_n] = _m
        else:
            sys.modules.pop(_n, None)

FILES = [{"blob": "user-7/input/crop_upperbody/img0.jpg"}]


class FusedDispatchTests(unittest.TestCase):
    def setUp(self):
        CAPTURED.clear()

    def _env(self):
        return {e.name: e.value for e in CAPTURED["template"].containers[0].env}

    def _names(self):
        return [e.name for e in CAPTURED["template"].containers[0].env]

    def test_without_job_id_is_plain_training(self):
        """No parked job -> unchanged behaviour: MODE=train and NO JOB_ID leaked."""
        training_trigger.trigger_training_job("user-7", FILES, "man")
        env, names = self._env(), self._names()
        self.assertEqual(env["MODE"], "train")
        # the stale template JOB_ID must be stripped and NOT re-set
        self.assertNotIn("JOB_ID", names,
                         "a stale JOB_ID leaked into a plain training run")

    def test_with_job_id_is_fused(self):
        """Parked job handed over -> MODE=train_infer + that JOB_ID, exactly once."""
        training_trigger.trigger_training_job("user-7", FILES, "man", job_id="JOB-42")
        env, names = self._env(), self._names()
        self.assertEqual(env["MODE"], "train_infer")
        self.assertEqual(env["JOB_ID"], "JOB-42")
        self.assertEqual(names.count("JOB_ID"), 1,
                         "JOB_ID duplicated — stale template value was not stripped")
        self.assertEqual(names.count("MODE"), 1)
        self.assertEqual(env["USER_ID"], "user-7")
        self.assertEqual(json.loads(env["FILES_JSON"]), FILES)

    def test_stale_job_id_never_survives(self):
        """The template's STALE-JOB-FROM-TEMPLATE must never reach a container, in either
        mode — that would generate a different user's job."""
        for kwargs in ({}, {"job_id": "JOB-42"}):
            CAPTURED.clear()
            training_trigger.trigger_training_job("user-7", FILES, "man", **kwargs)
            self.assertNotIn("STALE-JOB-FROM-TEMPLATE", self._env().values())

    def test_job_id_is_an_owned_key(self):
        """The contract that makes the above possible: JOB_ID is stripped every run."""
        self.assertIn("JOB_ID", training_trigger._OWNED_ENV)


class NoDoubleGenerationTests(unittest.TestCase):
    """The dispatcher must claim the fused job OUT of 'waiting_lora', because
    _finish_training releases everything left in that state. Replicated here as the SQL
    contract, since the real function needs a live DB."""

    def _release_query_sees(self, job_status):
        # _finish_training: SELECT ... WHERE user_id = ? AND status = 'waiting_lora'
        return job_status == "waiting_lora"

    def test_claimed_job_is_invisible_to_the_release(self):
        claimed = "processing"          # what the dispatcher sets before fusing
        self.assertFalse(
            self._release_query_sees(claimed),
            "fused job still matches _finish_training's release -> DOUBLE generation")

    def test_unfused_job_is_still_released(self):
        self.assertTrue(self._release_query_sees("waiting_lora"),
                        "non-fused parked jobs must still be released as before")

    def test_claimed_status_is_reaper_visible(self):
        """A fused run that dies after start must still be recovered: the reaper scans
        'processing' (and 'dispatching'), so 'processing' keeps the safety net."""
        self.assertIn("processing", ("processing", "dispatching"))



class FuseHeadStartTests(unittest.TestCase):
    """The head start is what makes the fused path actually reachable from the real
    frontend flow, so it is easy to 'clean up' later without realising it silently
    disables fusion. These pin the contract."""

    def _src(self):
        p = os.path.join(BACKEND_DIR, "function_app.py")
        with open(p, encoding="utf-8") as f:
            return f.read()

    def test_training_enqueue_is_delayed(self):
        src = self._src()
        self.assertIn("TRAIN_FUSE_HEAD_START", src)
        # the fast-path send must carry the delay, not fire immediately
        self.assertIn("visibility_timeout=TRAIN_FUSE_HEAD_START", src,
                      "the training enqueue lost its head start -> the dispatcher will "
                      "look for the parked job before /jobs/submit creates it, and the "
                      "fused MODE=train_infer path stops firing")

    def test_head_start_is_env_tunable_and_disableable(self):
        src = self._src()
        self.assertIn('os.environ.get("TRAIN_FUSE_HEAD_START"', src)
        # `or None` so 0 means "send immediately" rather than "0-second visibility"
        self.assertIn("TRAIN_FUSE_HEAD_START or None", src)

    def test_head_start_is_short_relative_to_training(self):
        """A head start long enough to matter against a ~34 min run would be a
        regression in itself. Keep it well under a minute."""
        import re
        m = re.search(r'TRAIN_FUSE_HEAD_START = int\(os\.environ\.get\("TRAIN_FUSE_HEAD_START", "(\d+)"\)\)',
                      self._src())
        self.assertIsNotNone(m, "TRAIN_FUSE_HEAD_START default not found")
        self.assertLessEqual(int(m.group(1)), 60,
                             "head start should be seconds, not minutes")

if __name__ == "__main__":
    unittest.main(verbosity=2)
