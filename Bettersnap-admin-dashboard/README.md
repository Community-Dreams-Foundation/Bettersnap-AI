# BetterSnap.AI Super Admin Dashboard

A production-ready frontend foundation for BetterSnap.AI's internal administration console. Built with React, TypeScript, and Vite, with a responsive interface and centralized API boundary.

## Setup

```bash
npm install
cp .env.example .env
npm run dev
```

Open `http://localhost:5173`. In development, the same-origin `/api` path is proxied by Vite to the production Azure Functions backend, avoiding browser CORS restrictions while authenticating with Microsoft Entra. Override the base URL with `http://localhost:7071/api` when running the Functions backend locally.

Production builds use `.env.production`, including the Firebase SPA redirect URI `https://bettersnapai-superadmin-2c258.web.app` and the production Azure Functions API URL. The exact Firebase origin must also be registered as an SPA redirect URI in Microsoft Entra.

## Scripts

- `npm run dev` — start development
- `npm run build` — typecheck and production build
- `npm run typecheck` — validate TypeScript
- `npm run lint` — run ESLint
- `npm test` — run RBAC and protected-route tests
- `npm run preview` — preview the build

## Project structure

`src/components` contains shared UI, `src/features` contains route features, `src/lib/api` contains the typed live API boundary, and `src/types` contains shared domain types.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `VITE_API_BASE_URL` | Production Functions URL | Azure Functions API base URL |
| `VITE_USE_MOCK_API` | `false` | Reserved switch for fixture development |
| `VITE_ENTRA_CLIENT_ID` | — | Microsoft Entra SPA application client ID |
| `VITE_ENTRA_TENANT_ID` | — | Microsoft Entra tenant ID |
| `VITE_ENTRA_API_SCOPE` | — | Delegated backend API scope |

Copy `.env.example` to `.env` and register `http://localhost:5173` as an SPA redirect URI. Never place `ADMIN_API_KEY`, client secrets, or other secrets in browser environment variables.

The current backend exposes customer-scoped profile, jobs, credits, and subscription endpoints, plus public health/catalog endpoints. Platform-wide user, job, payment, and audit data requires dedicated admin endpoints. Operations routes protected by `X-Admin-Key` must be proxied through a trusted server and are intentionally not called directly by this browser app.

See [`docs/auth-and-rbac.md`](docs/auth-and-rbac.md) for Entra setup and mandatory backend authorization requirements.

See [`docs/live-superadmin-integration-status.md`](docs/live-superadmin-integration-status.md) for integrated endpoints and remaining backend gaps.

## Routes

Includes login, dashboard, user and job list/detail routes, payments, subscriptions, credits, system health, audit logs, catalog/plans, and settings. Configure the production host to serve `index.html` as the fallback for client-side paths.
