import { afterEach, describe, expect, it } from 'vitest'
import { pickRedirectUri } from './auth'

// The bug: `lib/auth.ts` read `window.location.origin` at module scope, so ANY
// import outside a browser threw ReferenceError. DashboardPage.test.tsx pulled it
// in transitively (DashboardPage -> auth/index -> AuthProvider -> lib/auth) and
// the whole suite failed to load. Vitest runs in the `node` environment here, so
// simply importing this module is the regression test.

describe('importing the auth module without a browser', () => {
  it('does not throw, and window really is absent', async () => {
    expect(typeof window).toBe('undefined')
    await expect(import('./auth')).resolves.toBeDefined()
  })

  it('exposes the same public surface it always did', async () => {
    const mod = await import('./auth')
    for (const name of ['initializeAuth', 'getAccount', 'signIn', 'signOut', 'getAccessToken']) {
      expect(typeof mod[name as keyof typeof mod]).toBe('function')
    }
    expect(typeof mod.authConfigured).toBe('boolean')
  })

  it('resolves no redirect URI under Node rather than inventing one', async () => {
    const { resolveRedirectUri } = await import('./auth')
    // With no VITE_ENTRA_REDIRECT_URI and no window there is nothing truthful to
    // return. A guessed production URI would configure MSAL against a redirect
    // Entra never registered.
    expect(resolveRedirectUri()).toBeNull()
  })

  it('AuthProvider - the module that made DashboardPage fail - imports cleanly', async () => {
    // The real break was DashboardPage -> components -> layout -> ../auth ->
    // AuthProvider -> lib/auth -> window. This walks the same chain.
    await expect(import('../auth/AuthProvider')).resolves.toBeDefined()
  })

  // NOTE: `import('../auth/index')` still fails, for an unrelated reason this fix
  // does not address: that barrel is circular. auth/index -> ProtectedRoute ->
  // components/index -> layout.tsx -> '../auth', which re-enters the barrel while
  // it is still initializing, before `export * from './rbac'` (its last line) has
  // run, so PERMISSIONS is undefined and layout.tsx:6 throws on
  // PERMISSIONS.DASHBOARD_READ. Nothing imports the barrel first today, so it is
  // latent. Fixing it is a separate change.
})

describe('pickRedirectUri', () => {
  it('prefers the configured value over the browser origin', () => {
    expect(pickRedirectUri('https://bettersnapai-superadmin-2c258.web.app', 'https://localhost:5173'))
      .toBe('https://bettersnapai-superadmin-2c258.web.app')
  })

  it('trims the configured value', () => {
    expect(pickRedirectUri('  https://admin.example  ', null)).toBe('https://admin.example')
  })

  it('falls back to the browser origin when the variable is absent', () => {
    expect(pickRedirectUri(undefined, 'https://localhost:5173')).toBe('https://localhost:5173')
    expect(pickRedirectUri('', 'https://localhost:5173')).toBe('https://localhost:5173')
    expect(pickRedirectUri('   ', 'https://localhost:5173')).toBe('https://localhost:5173')
  })

  it('returns null when there is neither a configured value nor an origin', () => {
    expect(pickRedirectUri(undefined, null)).toBeNull()
    expect(pickRedirectUri('', null)).toBeNull()
  })
})

describe('the browser fallback, with a stubbed window', () => {
  afterEach(() => { delete (globalThis as Record<string, unknown>).window })

  it('uses the current origin once a window exists', async () => {
    ;(globalThis as Record<string, unknown>).window = { location: { origin: 'https://admin.local' } }
    const { resolveRedirectUri } = await import('./auth')
    expect(resolveRedirectUri()).toBe('https://admin.local')
  })

  it('tolerates a window with no location', async () => {
    ;(globalThis as Record<string, unknown>).window = {}
    const { resolveRedirectUri } = await import('./auth')
    expect(resolveRedirectUri()).toBeNull()
  })
})
