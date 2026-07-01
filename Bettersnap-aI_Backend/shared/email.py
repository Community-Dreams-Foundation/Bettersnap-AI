from azure.communication.email import EmailClient
from .keyvault import get_secret

_client = None

def get_email_client():
    global _client
    if _client is None:
        conn_str = get_secret("acs-connection-string")
        _client = EmailClient.from_connection_string(conn_str)
    return _client

def send_completion_email(to_email: str, job_id: str, output_url: str):
    client = get_email_client()
    message = {
        "senderAddress": "noreply@bettersnap.ai",
        "recipients": {"to": [{"address": to_email}]},
        "content": {
            "subject": "Your BetterSnap AI headshot is ready!",
            "plainText": f"Your headshot (Job ID: {job_id}) is ready. Download it here: {output_url}",
            "html": f"""
                <h2>Your headshot is ready!</h2>
                <p>Job ID: {job_id}</p>
                <p><a href="{output_url}">Click here to download your headshot</a></p>
            """
        }
    }
    client.begin_send(message)