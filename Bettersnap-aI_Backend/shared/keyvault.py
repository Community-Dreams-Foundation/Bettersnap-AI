from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

VAULT_URL = "https://bettersnapkeyvault.vault.azure.net/"

_credential = DefaultAzureCredential()
_client = SecretClient(vault_url=VAULT_URL, credential=_credential)

def get_secret(secret_name: str) -> str:
    return _client.get_secret(secret_name).value