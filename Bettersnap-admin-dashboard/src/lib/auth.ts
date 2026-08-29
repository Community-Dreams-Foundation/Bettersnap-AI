import { PublicClientApplication, type AccountInfo } from '@azure/msal-browser'

const clientId = import.meta.env.VITE_ENTRA_CLIENT_ID?.trim()
const tenantId = import.meta.env.VITE_ENTRA_TENANT_ID?.trim()
const apiScope = import.meta.env.VITE_ENTRA_API_SCOPE?.trim()
const redirectUri = import.meta.env.VITE_ENTRA_REDIRECT_URI?.trim() || window.location.origin

export const authConfigured = Boolean(clientId && tenantId && apiScope)
const msal = authConfigured ? new PublicClientApplication({ auth: { clientId: clientId!, authority: `https://login.microsoftonline.com/${tenantId}`, redirectUri, postLogoutRedirectUri:redirectUri }, cache: { cacheLocation: 'sessionStorage' } }) : null
let initialization: Promise<void>|null = null
let signInRequest: Promise<AccountInfo>|null = null
async function ready() {
  if (!msal) return null
  // Authentication uses popup APIs exclusively. Calling handleRedirectPromise here
  // makes the SPA loaded inside the popup attempt to consume redirect-only cache
  // state and can raise no_token_request_cache_error.
  initialization ||= msal.initialize()
  await initialization
  return msal
}
export async function initializeAuth() { await ready() }
export function getAccount(): AccountInfo|null { return msal?.getActiveAccount() || msal?.getAllAccounts()[0] || null }
export async function signIn(): Promise<AccountInfo> {
  if (signInRequest) return signInRequest
  const request = (async () => {
    const app = await ready()
    if (!app || !apiScope) throw new Error('Microsoft Entra is not configured. Add the VITE_ENTRA_* values to .env.')
    // Calls are serialized above, so any remaining interaction marker belongs
    // to an abandoned popup and can be safely replaced by this new request.
    const result = await app.loginPopup({scopes:[apiScope],loginHint:'sivmm29@gmail.com',prompt:'select_account',overrideInteractionInProgress:true})
    app.setActiveAccount(result.account)
    return result.account
  })()
  signInRequest = request
  try { return await request }
  finally { if (signInRequest === request) signInRequest = null }
}
export async function signOut() { const app = await ready(); if (app) await app.logoutPopup({ account: app.getActiveAccount() || undefined,postLogoutRedirectUri:redirectUri }); }
export async function getAccessToken(): Promise<string|null> { const app = await ready(); const account = app?.getActiveAccount() || app?.getAllAccounts()[0]; if (!app || !account || !apiScope) return null; app.setActiveAccount(account); try { return (await app.acquireTokenSilent({ account, scopes: [apiScope] })).accessToken } catch { return null } }
