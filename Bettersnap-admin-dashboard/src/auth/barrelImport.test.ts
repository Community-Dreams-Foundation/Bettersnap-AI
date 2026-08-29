import { beforeEach, describe, expect, it, vi } from 'vitest'

// REGRESSION: src/auth/index.ts used to be a circular import.
//
//   src/auth/index.ts
//     -> ProtectedRoute.tsx
//       -> ../components/index.ts
//         -> layout.tsx
//           -> '../auth'          <-- back into the barrel, still initializing
//
// The barrel's last line is `export * from './rbac'`, so when layout.tsx re-entered
// it, PERMISSIONS was still undefined and layout.tsx:6 threw
//   TypeError: Cannot read properties of undefined (reading 'DASHBOARD_READ')
//
// It only bit when the barrel was the ENTRY point, which is why the app worked:
// main.tsx reaches components first, fully evaluating '../auth' before layout's
// module body runs. Anything importing src/auth first -- a test, SSR, a script --
// crashed.
//
// Fixed by making layout.tsx import the leaf modules (../auth/AuthProvider,
// ../auth/rbac) instead of the barrel, which removes the edge rather than relying
// on export order inside the barrel.
//
// This file imports the barrel FIRST, from a clean registry, and nothing else.
// Against e3afbac every assertion below throws at import time.

describe('src/auth/index.ts is importable as an entry point', () => {
  beforeEach(() => {
    // Guarantee a clean module state: no other module may have already resolved
    // the graph and masked the cycle.
    vi.resetModules()
  })

  it('completes without throwing', async () => {
    await expect(import('./index')).resolves.toBeDefined()
  })

  it('has PERMISSIONS fully initialized, not undefined', async () => {
    const auth = await import('./index')
    expect(auth.PERMISSIONS).toBeDefined()
    // The exact property whose read threw during the cycle.
    expect(auth.PERMISSIONS.DASHBOARD_READ).toBe('dashboard.read')
    expect(Object.keys(auth.PERMISSIONS).length).toBeGreaterThan(1)
  })

  it('exposes the auth exports layout.tsx depends on', async () => {
    const auth = await import('./index')
    expect(typeof auth.useAuth).toBe('function')
    expect(typeof auth.AuthProvider).toBe('function')
    expect(typeof auth.permissionsForRoles).toBe('function')
  })

  it('loads layout.tsx itself, whose module body reads PERMISSIONS at import time', async () => {
    // layout.tsx builds its `nav` array from PERMISSIONS.* while the module
    // evaluates -- that is the code the cycle broke.
    const layout = await import('../components/layout')
    expect(typeof layout.Sidebar).toBe('function')
  })

  it('loads the components barrel after the auth barrel, in that order', async () => {
    await import('./index')
    await expect(import('../components/index')).resolves.toBeDefined()
  })

  it('resolves identically in the reverse order too', async () => {
    // Order-independence is the actual property being restored: with a cycle,
    // one entry order works and the other throws.
    vi.resetModules()
    await import('../components/index')
    const auth = await import('./index')
    expect(auth.PERMISSIONS.DASHBOARD_READ).toBe('dashboard.read')
  })

  // A source-text assertion that layout.tsx avoids the barrel would need
  // node:fs, and this tsconfig does not include Node types. It would also be
  // redundant: re-adding `from '../auth'` to layout.tsx recreates the cycle, and
  // the first two tests above throw at import time when it exists.
})
