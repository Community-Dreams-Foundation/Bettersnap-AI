# Admin tenant handoff

## Frontend status

The frontend is configured for the separate `adminbettersnap.onmicrosoft.com` tenant:

| Setting | Value |
|---|---|
| Tenant ID | `e853dd89-1910-4e66-805b-909b4477acdb` |
| Client ID | `a33131bf-3632-49a0-b4c3-693accec62cd` |
| Authority | `https://login.microsoftonline.com/e853dd89-1910-4e66-805b-909b4477acdb` |
| Delegated scope | `api://a33131bf-3632-49a0-b4c3-693accec62cd/access_as_admin` |
| Local redirect URI | `http://localhost:5173` |
| Required app role | `Admin` |

MSAL requests the delegated scope and attaches the access token as `Authorization: Bearer <token>`. `GET /api/superadmin/me` is preferred for the backend-authored identity. While that route returns `404`, the frontend can create its UI session from the Entra ID-token email and case-sensitive `Admin` role; it never bypasses backend authorization on API requests. The browser contains no client secret or backend admin key.

## Sivaram's backend tasks

Sivaram must update the Azure Functions application settings so the SuperAdmin API trusts tokens issued by the new admin tenant and app:

```text
ADMIN_ISSUER=https://login.microsoftonline.com/e853dd89-1910-4e66-805b-909b4477acdb/v2.0,https://sts.windows.net/e853dd89-1910-4e66-805b-909b4477acdb/
ADMIN_JWKS_URI=https://login.microsoftonline.com/e853dd89-1910-4e66-805b-909b4477acdb/discovery/v2.0/keys
ADMIN_AUD=api://a33131bf-3632-49a0-b4c3-693accec62cd,a33131bf-3632-49a0-b4c3-693accec62cd
ADMIN_ROLE=Admin
```

He must then:

1. Restart or redeploy the Function App so these settings are loaded.
2. Confirm all deployed routes are under `/api/superadmin/*`.
3. Confirm JWT validation checks signature, issuer, audience, expiry, and the case-sensitive `Admin` role.
4. Add the final production dashboard origin to Azure Functions CORS if the frontend and API use different origins.
5. Return `401` for invalid or missing tokens and `403` for valid tokens without the `Admin` role.

No client secret is required or allowed in this SPA.

## Joint smoke test

After the backend settings are active:

1. Sign in as `admin@bettersnap.ai` using the work/organization identity.
2. Verify `GET /api/superadmin/me` returns the expected email and `roles: ["Admin"]`.
3. Verify the users, jobs, payments, subscriptions, credits, audit, and system-health routes return authenticated data.
4. Verify an unassigned account receives `403`.
5. Verify logout returns to the configured dashboard redirect URI and protected pages return to `/login`.

## Production frontend task

When the production dashboard URL is known, add that exact URL as an SPA redirect URI in the Entra app registration and configure:

```text
VITE_ENTRA_REDIRECT_URI=https://<dashboard-domain>/
VITE_API_BASE_URL=https://bettersnap-functions-dagchpg8f0b7fjed.eastus-01.azurewebsites.net/api
```

If the production backend does not allow the dashboard origin through CORS, deploy a same-origin reverse proxy instead of calling Azure Functions directly from the browser.
