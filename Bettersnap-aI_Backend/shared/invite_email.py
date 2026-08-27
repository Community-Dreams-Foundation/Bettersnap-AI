"""Invite emails for the Teams layer.

Mirrors the completion-email pattern in main.py: pull the ACS connection string
from Key Vault, send via EmailClient, and treat a send failure as NON-FATAL.

Why non-fatal: the invite row and its token are already committed by the time we
get here. If the email fails, the seat is still reserved and the admin can copy
the link from the create-invitations response or the members list. Blowing up the
whole request because a mail server hiccuped would leave the admin thinking no
invites were created at all, when in fact they were.
"""

import logging
import os
from urllib.parse import quote

log = logging.getLogger(__name__)


def build_invite_url(token: str) -> str:
    """The link the employee clicks.

    Points at the frontend, not the API — the employee has to sign in with
    Microsoft first, and only then does the frontend POST the token to
    /invitations/{token}/accept. APP_BASE_URL is env-driven so staging and prod
    point at the right host (same convention as the completion email).
    """
    app_base = os.environ.get("APP_BASE_URL", "https://bettersnap.ai").rstrip("/")
    return f"{app_base}/invite/{quote(token, safe='')}"


def send_invite_email(to_email: str, org_name: str, token: str, credits: int) -> bool:
    """Send one invite. Returns True if handed to ACS, False on any failure.

    Never raises — the caller has already committed the invite row.
    """
    try:
        # Imported lazily so a missing ACS package or secret can't break module
        # import for the whole function app — same reason keyvault.py defers its
        # credential import.
        from azure.communication.email import EmailClient
        from .keyvault import get_secret

        invite_url = build_invite_url(token)
        org = org_name or "your team"

        # Configurable, not hardcoded — the sender address is tied to whichever
        # domain is actually connected in Azure Communication Services, and
        # that can change (moving from a free Azure subdomain to a real
        # bettersnap.ai domain later) without needing a code change/redeploy.
        sender_address = os.environ.get(
            "INVITE_SENDER_ADDRESS",
            "DoNotReply@75369587-cacf-491b-b5ab-ba6f0a34c870.azurecomm.net",
        )

        acs_conn_str = get_secret("acs-connection-string")
        client = EmailClient.from_connection_string(acs_conn_str)
        client.begin_send({
            "senderAddress": sender_address,
            "recipients": {"to": [{"address": to_email}]},
            "content": {
                "subject": f"{org} invited you to BetterSnap AI",
                "plainText": (
                    f"{org} has set up BetterSnap AI headshots for the team, and "
                    f"you have {credits} credits waiting.\n\n"
                    f"Get started here: {invite_url}\n\n"
                    f"Sign in with your work email, upload a few photos, and your "
                    f"AI headshots will be generated for you."
                ),
                "html": (
                    f"<h2>You've been invited to BetterSnap AI</h2>"
                    f"<p><strong>{org}</strong> has set up AI headshots for the team, "
                    f"and you have <strong>{credits} credits</strong> waiting.</p>"
                    f"<p><a href=\"{invite_url}\">Accept your invitation</a></p>"
                    f"<p>Sign in with your work email, upload a few photos, and your "
                    f"headshots will be generated for you.</p>"
                ),
            },
        })
        log.info(f"invite email sent to {to_email}")
        return True
    except Exception as e:
        # Deliberately swallowed. See module docstring.
        log.warning(f"invite email FAILED for {to_email} (non-fatal): {e}")
        return False