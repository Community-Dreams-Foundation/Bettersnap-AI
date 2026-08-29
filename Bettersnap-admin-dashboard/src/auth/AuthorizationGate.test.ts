import{describe,expect,it}from'vitest'
import{isAuthorized}from'./authorization'
import{PERMISSIONS,type Permission}from'./rbac'
describe('authorization gates',()=>{const granted=new Set<Permission>([PERMISSIONS.USERS_READ,PERMISSIONS.JOBS_READ]);it('allows a granted permission',()=>expect(isAuthorized(granted,PERMISSIONS.USERS_READ)).toBe(true));it('denies a missing permission',()=>expect(isAuthorized(granted,PERMISSIONS.BILLING_READ)).toBe(false));it('supports all-of checks',()=>expect(isAuthorized(granted,[PERMISSIONS.USERS_READ,PERMISSIONS.JOBS_READ],true)).toBe(true));it('supports any-of checks',()=>expect(isAuthorized(granted,[PERMISSIONS.BILLING_READ,PERMISSIONS.JOBS_READ],false)).toBe(true))})
