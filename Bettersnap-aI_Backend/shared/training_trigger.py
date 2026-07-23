"""Start the per-user identity-LoRA training job on the A100.

Mirrors queue_trigger.trigger_container_job (same ACA quirks, same recovery), and
targets the SAME single job `bettersnapai-if` (JOB_NAME) — the unified train+infer image
runs both. This dispatcher sets MODE=train at START time so the container boots the
trainer; the inference dispatcher sets MODE=infer. (The old dedicated
`bettersnapai-lora-trainer` job is retired.) The per-user env is overridden at start too.

WHY THE ENV OVERRIDE IS THE WHOLE POINT
---------------------------------------
The deployed job template has ONE user's values baked into it (USER_ID, FILES_JSON,
INPUT_BLOB_PATH, INSTANCE_PROMPT — the manual Phase-2 run). The trainer CODE is fully
generic; only that env block is person-specific. Every field that identifies a user is
therefore listed in _OWNED_ENV below and is STRIPPED from the template and re-set from
the caller's arguments on every execution. If a key were left in the template and not
stripped, a new user's run would silently inherit the previous user's value — which for
USER_ID means training one person's face and uploading it as another person's adapter.
That is a privacy bug, not a config quirk, so the strip-list is explicit and the caller
passes a single user_id used for BOTH the photo paths and the output path.

INPUT_BLOB_PATH is stripped and never re-set: the trainer reads FILES_JSON, and a stale
INPUT_BLOB_PATH pointing at another user's folder must not survive.
"""
import json
import logging

from azure.identity import DefaultAzureCredential
from azure.mgmt.appcontainers import ContainerAppsAPIClient
from azure.mgmt.appcontainers.models import EnvironmentVar

from . import catalog
from .queue_trigger import (
    SUBSCRIPTION_ID, RESOURCE_GROUP, JOB_NAME, _start_execution,
)

# Env keys this dispatcher OWNS. Always removed from the job template, then re-set
# (or deliberately left unset) per execution. Anything user-identifying MUST be here.
_OWNED_ENV = {
    "USER_ID",
    "FILES_JSON",
    "INSTANCE_PROMPT",
    "CLASS_PROMPT",
    "CLASS_WORD",
    "INPUT_BLOB_PATH",   # stripped, never re-set — FILES_JSON is the input contract
    "MODE",              # shared job with inference — MUST re-set to "train" every run
}


def trigger_training_job(user_id: str, files: list, class_word: str):
    """Start a training execution for ONE user.

    user_id     — the authenticated user. Used for BOTH the input photo paths (by the
                  caller, when it built `files`) and the adapter output path
                  identity/<user_id>/adapter_model.safetensors (by the trainer).
                  There is exactly one user_id in this call by design.
    files       — [{"blob": "<user_id>/input/crop_upperbody/img0.jpg"}, ...]
                  Paths are relative to the `inputs` container (INPUT_CONTAINER), NOT
                  prefixed with it — the trainer joins them against the container name.
    class_word  — woman | man | person (from catalog.class_word).

    Returns the ACA execution id.
    """
    if not user_id:
        raise ValueError("user_id is required")
    if not files:
        raise ValueError("files is empty — nothing to train on")

    # Defence in depth: every training image MUST live under this user's own prefix.
    # If a caller ever builds `files` from a different user's blobs, refuse to start
    # rather than train one person's face into another person's adapter.
    prefix = f"{user_id}/".lower()
    stray = [f["blob"] for f in files if not f["blob"].lower().startswith(prefix)]
    if stray:
        raise ValueError(
            f"training file(s) outside user {user_id}'s prefix: {stray[:3]} — refusing to start"
        )

    # Strip every user-identifying key (_OWNED_ENV) and re-set ours. Non-owned keys
    # (STORAGE_CONNECTION_STRING secretRef, RANK, MAX_TRAIN_STEPS, LEARNING_RATE,
    # TEXT_ENCODER_LR, NUM_CLASS_IMAGES, PRIOR_LOSS_WEIGHT, AZURE_LORA_CONTAINER, ...) are
    # the tuned recipe, carried through untouched. Built as EnvironmentVar objects via the
    # ARM SDK (no shell), so the Windows --env-vars quote-stripping that mangled FILES_JSON
    # into invalid JSON cannot happen. MODE=train boots the trainer, not inference.
    execution_id = _start_execution(
        owned_env_keys=_OWNED_ENV,
        env_overrides=[
            EnvironmentVar(name="MODE", value="train"),
            EnvironmentVar(name="USER_ID", value=str(user_id)),
            EnvironmentVar(name="FILES_JSON", value=json.dumps(files)),
            EnvironmentVar(name="CLASS_WORD", value=class_word),
            EnvironmentVar(name="INSTANCE_PROMPT", value=catalog.instance_prompt(class_word)),
            EnvironmentVar(name="CLASS_PROMPT", value=catalog.class_prompt(class_word)),
        ],
    )
    logging.info(f"Started training job for user={user_id}, execution_id={execution_id}")
    return execution_id


def get_execution_status(execution_id: str) -> str:
    """Lowercased ACA status for a training execution ('running', 'succeeded',
    'failed', ...), or '' if it can't be found. The training watcher polls this to
    decide when users.lora_status flips."""
    credential = DefaultAzureCredential()
    client = ContainerAppsAPIClient(credential, SUBSCRIPTION_ID)
    for ex in client.jobs_executions.list(RESOURCE_GROUP, JOB_NAME):
        if getattr(ex, "name", None) == execution_id:
            return (getattr(ex, "status", "") or "").lower()
    return ""
