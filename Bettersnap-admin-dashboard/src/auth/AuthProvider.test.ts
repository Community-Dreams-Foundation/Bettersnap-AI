import { describe,expect,it } from 'vitest'
import { isSuperAdminEmail } from './superAdminAccess'

describe('SuperAdmin email allowlist',()=>{
  it('allows only the configured administrator case-insensitively',()=>{
    expect(isSuperAdminEmail(' ADMIN@BETTERSNAP.AI ')).toBe(true)
    expect(isSuperAdminEmail('sivmm29@gmail.com')).toBe(false)
  })

  it('rejects unrelated addresses',()=>expect(isSuperAdminEmail('user@example.com')).toBe(false))
})
