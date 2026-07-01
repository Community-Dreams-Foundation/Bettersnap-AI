import json
from azure.storage.queue import QueueClient, TextBase64EncodePolicy
from .keyvault import get_secret

_client = None

def get_queue_client():
    global _client
    if _client is None:
        conn_str = get_secret("storage-connection-string")
        # Base64-encode messages to match the Azure Functions queue extension
        # default (messageEncoding=base64). Without this the host fails to
        # decode the message and it never reaches the trigger.
        _client = QueueClient.from_connection_string(
            conn_str,
            "inference-jobs",
            message_encode_policy=TextBase64EncodePolicy(),
        )
    return _client

def enqueue_job(payload: dict, visibility_timeout: int = None):
    """Enqueue an inference job. When visibility_timeout (seconds) is given the
    message stays hidden for that long before a trigger can pick it up — used for
    back-pressure: when the GPU is at its active-job cap we re-enqueue with a
    delay instead of starting another A100 (see process_inference_job)."""
    client = get_queue_client()
    message = json.dumps(payload)
    if visibility_timeout is not None:
        client.send_message(message, visibility_timeout=visibility_timeout)
    else:
        client.send_message(message)