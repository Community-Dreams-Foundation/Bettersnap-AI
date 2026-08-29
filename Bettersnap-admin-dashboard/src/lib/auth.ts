import { PublicClientApplication, type AccountInfo } from '@azure/msal-browser'

const clientId = import.meta.env.VITE_ENTRA_CLIENT_ID?.trim()
const tenantId = import.meta.env.VITE_ENTRA_TENANT_ID?.trim()
const apiScope = import.meta.env.VITE_ENTRA_API_SCOPE?.trim()
const configuredRedirectUri = import.meta.env.VITE_ENTRA_REDIRECT_URI?.trim()

/**
 * Which redirect URI MSAL should use.
 *
 * A configured VITE_ENTRA_REDIRECT_URI always wins. Without one, a browser falls
 * back to its own origin -- the behaviour this module has always had. Outside a
 * browser (SSR, a Vitest `node` environment, any tooling that imports this file)
 * there is no origin to fall back to, and guessing a production redirect would be
 * worse than useless: MSAL would be configured against a URI Entra never
 * registered. So it returns null and the caller declines to build a client.
 *
 * Exported as a pure function so both branches are testable without a DOM. This
 * was previously `... || window.location.origin` at module scope, which made the
 * whole module -- and everything importing it -- throw on a bare Node import.
 */
export function pickRedirectUri(configured: string|undefined, origin: string|null): string|null {
  const trimmed = configured?.trim()
  if (trimmed) return trimmed
  return origin
}

function currentOrigin(): string|null {
  return typeof window !== 'undefined' && window.location ? window.location.origin : null
}

export function resolveRedirectUri(): string|null {
  return pickRedirectUri(configuredRedirectUri, currentOrigin())
}

export const authConfigured = Boolean(clientId && tenantId && apiScope)

// Built on first use rather than at module scope, so importing this file never
// touches `window`. Null when auth is unconfigured OR when no redirect URI can be
// resolved -- every caller below already handles a null client by failing closed.
let msal: PublicClientApplication|null = null
function client(): PublicClientApplication|null {
  if (msal) return msal
  if (!authConfigured) return null
  const redirectUri = resolveRedirectUri()
  if (!redirectUri) return null
  msal = new PublicClientApplication({ auth: { clientId: clientId!, authority: `https://login.microsoftonline.com/${tenantId}`, redirectUri, postLogoutRedirectUri:redirectUri }, cache: { cacheLocation: 'sessionStorage' } })
  return msal
}
let initialization: Promise<void>|null = null
let signInRequest: Promise<AccountInfo>|null = null
async function ready() {
  const msal = client()
  if (!msal) return null
  // Authentication uses popup APIs exclusively. Calling handleRedirectPromise here
  // makes the SPA loaded inside the popup attempt to consume redirect-only cache
  // state and can raise no_token_request_cache_error.
  initialization ||= msal.initialize()
  await initialization
  return msal
}
export async function initializeAuth() { await ready() }
export function getAccount(): AccountInfo|null { const app = client(); return app?.getActiveAccount() || app?.getAllAccounts()[0] || null }
export async function signIn(): Promise<AccountInfo> {
  if (signInRequest) return signInRequest
  const request = (async () => {
    const app = await ready()
    if (!app || !apiScope) throw new Error('Microsoft Entra is not configured. Add the VITE_ENTRA_* values to .env.')
    // Calls are serialized above, so any remaining interaction marker belongs
    // to an abandoned popup and can be safely replaced by this new request.
    const result = await app.loginPopup({scopes:[apiScope],loginHint:'admin',prompt:'select_account',overrideInteractionInProgress:true})
    app.setActiveAccount(result.account)
    return result.account
  })()
  signInRequest = request
  try { return await request }
  finally { if (signInRequest === request) signInRequest = null }
}
export async function signOut() { const app = await ready(); if (app) await app.logoutPopup({ account: app.getActiveAccount() || undefined,postLogoutRedirectUri: resolveRedirectUri() || undefined }); }
export async function getAccessToken(): Promise<string|null> { const app = await ready(); const account = app?.getActiveAccount() || app?.getAllAccounts()[0]; if (!app || !account || !apiScope) return null; app.setActiveAccount(account); try { return (await app.acquireTokenSilent({ account, scopes: [apiScope] })).accessToken } catch { return null } }
