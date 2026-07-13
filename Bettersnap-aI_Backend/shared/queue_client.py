import json
from azure.storage.queue import QueueClient, TextBase64EncodePolicy
from .keyvault import get_secret

INFERENCE_QUEUE = "inference-jobs"
TRAINING_QUEUE = "lora-training-jobs"

_clients = {}


def get_queue_client(queue_name: str = INFERENCE_QUEUE):
    client = _clients.get(queue_name)
    if client is None:
        conn_str = get_secret("storage-connection-string")
        # Base64-encode messages to match the Azure Functions queue extension
        # default (messageEncoding=base64). Without this the host fails to
        # decode the message and it never reaches the trigger.
        client = QueueClient.from_connection_string(
            conn_str,
            queue_name,
            message_encode_policy=TextBase64EncodePolicy(),
        )
        try:
            client.create_queue()
        except Exception:
            # Already exists (the common case). Never fatal here — if the queue is
            # genuinely unreachable, send_message below surfaces the real error.
            pass
        _clients[queue_name] = client
    return client


def _send(queue_name: str, payload: dict, visibility_timeout=None):
    client = get_queue_client(queue_name)
    message = json.dumps(payload)
    if visibility_timeout is not None:
        client.send_message(message, visibility_timeout=visibility_timeout)
    else:
        client.send_message(message)


def enqueue_job(payload: dict, visibility_timeout: int = None):
    """Enqueue an inference job. When visibility_timeout (seconds) is given the
    message stays hidden for that long before a trigger can pick it up — used for
    back-pressure: when the GPU is at its active-job cap we re-enqueue with a
    delay instead of starting another A100 (see process_inference_job)."""
    _send(INFERENCE_QUEUE, payload, visibility_timeout)


def enqueue_training_job(payload: dict, visibility_timeout: int = None):
    """Enqueue an identity-LoRA training run (see process_training_job). Training and
    inference contend for the SAME A100 cap and the SAME dispatch lease, so a training
    message defers under back-pressure exactly like an inference one."""
    _send(TRAINING_QUEUE, payload, visibility_timeout)
