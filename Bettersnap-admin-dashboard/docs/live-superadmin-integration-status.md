# Live SuperAdmin Integration Status

The frontend uses `/api/superadmin/*`, never `/api/admin/*`. Microsoft Entra uses admin tenant `e853dd89-1910-4e66-805b-909b4477acdb`, app `a33131bf-3632-49a0-b4c3-693accec62cd`, and the single authorized dashboard identity is `sivmm29@gmail.com`. The browser only contains public SPA configuration; it never contains `ADMIN_API_KEY` or `ADMIN_TEST_SECRET`.

## Integrated now

| Dashboard capability | Backend endpoint | UI |
|---|---|---|
| Dashboard aggregates | `GET /superadmin/users`, `/jobs`, `/payments`, `/subscriptions`, `/credits`, `/system-health`, `/audit-logs` | The frontend composes dashboard values from complete paginated responses; there is no summary endpoint dependency |
| User directory/search | `GET /superadmin/users?q=&limit=&offset=` | Users list and pagination |
| User detail | `GET /superadmin/users/{user_id}` | Account, subscription fields, recent jobs, and per-user credit ledger |
| Jobs directory/filter | `GET /superadmin/jobs?status=&user_id=&limit=&offset=` | Jobs list and pagination |
| Job detail | `GET /superadmin/jobs/{job_id}` | Status, parameters, timings, execution metadata |
| Basic platform health | `GET /health` | API and face-gate status |
| Catalog | `GET /catalog` | Categories, attires, backgrounds |
| Plan rules | `GET /plans` | Generation-plan constraints |
| Public billing plans | `GET /subscriptions/plans` | Monthly and one-time published price/credit structure |
| Admin identity | `GET /superadmin/me` | Signed-in administrator identity and Admin role |
| User suspension/reactivation | `POST /superadmin/users/{id}/suspend`, `/reactivate` | Reason-required audited account actions |
| User notes | `GET/POST /superadmin/users/{id}/notes` | Live internal notes |
| Credit adjustment and ledger | `POST /superadmin/users/{id}/credits/adjust`, `GET /superadmin/credits` | Audited adjustment and platform ledger |
| Job actions | `POST /superadmin/jobs/{id}/retry`, `/cancel`, `/restore-credit` | Reason-required audited operations |
| Payments | `GET /superadmin/payments` | Read-only credit-grant payment records |
| Subscriptions | `GET /superadmin/subscriptions` | Read-only platform subscription records |
| Audit logs | `GET /superadmin/audit-logs` | Live mutation history |
| Detailed health | `GET /superadmin/system-health` | SQL, Blob, GPU activity, queue depth, failed jobs |

## Missing backend endpoints

Suggested routes below use the working `/superadmin` namespace.

| Missing capability | Required endpoint(s) | Priority |
|---|---|---|
| Subscription cancellation/reactivation | `POST /superadmin/subscriptions/{id}/cancel`, `POST /superadmin/subscriptions/{id}/reactivate` | P1 |
| Currency revenue and payment detail | `GET /superadmin/payments/{id}`, `GET /superadmin/billing/summary` | P1 |
| Controlled refunds | `POST /superadmin/payments/{id}/refund-requests` | P1 |
| Approved email resend and deletion workflow | `POST /superadmin/users/{id}/emails/{type}`, `POST /superadmin/users/{id}/deletion-requests` | P2 |
| Audit event detail | `GET /superadmin/audit-logs/{id}` | P2 |
| Alerts and additional component health | Email, Stripe webhook, outbox, stale-job and alert endpoints | P1 |
| Last login, retry count and model version | Backend persistence and response fields | P2 |

Active subscription count is now returned by the dashboard summary endpoint.

## Authentication

Production uses MSAL and sends `Authorization: Bearer <token>` through the central API client. Local `admin-test:<secret>` tokens remain a backend-only development mechanism: configure the secret in the backend's uncommitted `local.settings.json`, then inject/use it only through local developer tooling. Do not place it in frontend environment files.
