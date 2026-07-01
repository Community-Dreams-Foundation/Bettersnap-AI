from azure.storage.blob import BlobServiceClient
from .keyvault import get_secret

_client = None

def get_blob_client():
    global _client
    if _client is None:
        conn_str = get_secret("storage-connection-string")
        _client = BlobServiceClient.from_connection_string(conn_str)
    return _client

def upload_blob(container: str, blob_name: str, data: bytes) -> str:
    client = get_blob_client()
    blob = client.get_blob_client(container=container, blob=blob_name)
    blob.upload_blob(data, overwrite=True)
    return blob.url

def download_blob(container: str, blob_name: str) -> bytes:
    client = get_blob_client()
    blob = client.get_blob_client(container=container, blob=blob_name)
    return blob.download_blob().readall()