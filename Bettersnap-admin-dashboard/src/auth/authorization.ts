import type{Permission}from'./rbac'
export function isAuthorized(granted:ReadonlySet<Permission>,required:Permission|readonly Permission[],requireAll=true){const list=Array.isArray(required)?required:[required];return requireAll?list.every(permission=>granted.has(permission)):list.some(permission=>granted.has(permission))}
