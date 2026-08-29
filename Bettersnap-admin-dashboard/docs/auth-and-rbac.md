# BetterSnap SuperAdmin authentication

The dashboard has one MVP administrator: `sivmm29@gmail.com`. Microsoft Entra assigns this account the backend `Admin` application role. The frontend presents it as `SuperAdmin` and grants access to every dashboard section.

There are no OperationsAdmin, FinanceAdmin, SupportAdmin, or ProductAdmin accounts in the current MVP. Frontend authorization gates remain user-experience controls only; the backend must validate the Entra token and `Admin` role for every `/api/superadmin/*` request.

## Public SPA configuration

```env
VITE_API_BASE_URL=/api
VITE_ENTRA_CLIENT_ID=a33131bf-3632-49a0-b4c3-693accec62cd
VITE_ENTRA_TENANT_ID=e853dd89-1910-4e66-805b-909b4477acdb
VITE_ENTRA_API_SCOPE=api://a33131bf-3632-49a0-b4c3-693accec62cd/access_as_admin
VITE_ENTRA_REDIRECT_URI=http://localhost:5173
```

Production API base URL:

```text
https://bettersnap-functions-dagchpg8f0b7fjed.eastus-01.azurewebsites.net/api
```

These SPA identifiers are public configuration, not confidential credentials. Never place a client secret, `ADMIN_API_KEY`, `ADMIN_TEST_SECRET`, Stripe secret, or raw access token in frontend configuration, source code, browser storage, or requests.

MSAL derives the authority from the tenant setting as `https://login.microsoftonline.com/e853dd89-1910-4e66-805b-909b4477acdb`.

For local development, Vite forwards `/api` to the production Functions host. A production deployment must set `VITE_API_BASE_URL` and `VITE_ENTRA_REDIRECT_URI` for its public URL, and that exact redirect URI must be registered as an SPA redirect URI in Entra.

## Login and session flow

1. `/login` starts an MSAL Microsoft Entra popup.
2. The application requests the `access_as_admin` API scope.
3. Only `sivmm29@gmail.com` with the Entra `Admin` role is accepted by the frontend.
4. The central API client attaches `Authorization: Bearer <token>` to protected requests.
5. `GET /api/superadmin/me` is the preferred backend-authored administrator identity. Until that route is deployed, a `404` falls back to the Entra ID-token email and `Admin` role for frontend session creation. `401` and `403` never fall back.
6. A `401` clears the frontend session and redirects to `/login`.
7. A `403` means the authenticated account does not have backend authorization.
8. Logout clears the local session and signs out through MSAL.
9. The provider warns before the idle timeout and redirects to login when the timeout or token expiration is reached.

There is no frontend authentication bypass and no simulated administrator identity.

## Route namespace

Admin endpoints use `/api/superadmin/*`. Never use `/api/admin/*`; Azure Functions reserves that namespace and returns `404`.

The legacy `/api/ops/*` endpoints require server-side `X-Admin-Key`. They must never be called directly from this browser dashboard.
