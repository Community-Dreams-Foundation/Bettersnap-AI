export const PERMISSIONS = {
  DASHBOARD_READ: 'dashboard.read', USERS_READ: 'users.read', USERS_SUSPEND: 'users.suspend', USERS_CREDITS_ADJUST: 'users.credits.adjust', JOBS_READ: 'jobs.read', JOBS_RETRY: 'jobs.retry', JOBS_CANCEL: 'jobs.cancel', JOBS_FAIL_REFUND: 'jobs.fail_refund', PAYMENTS_READ: 'payments.read', PAYMENTS_REFUND: 'payments.refund', BILLING_READ: 'payments.read', BILLING_REFUND: 'payments.refund', SUBSCRIPTIONS_READ: 'subscriptions.read', CREDITS_READ: 'credits.read', HEALTH_READ: 'health.read', AUDIT_READ: 'audit.read', ADMINS_MANAGE: 'admins.manage', CATALOG_READ: 'catalog.read',
} as const
export type Permission = typeof PERMISSIONS[keyof typeof PERMISSIONS]
export const ROLES = ['SuperAdmin','OperationsAdmin','FinanceAdmin','SupportAdmin','ProductAdmin'] as const
export type AdminRole = typeof ROLES[number]
const allPermissions = Array.from(new Set(Object.values(PERMISSIONS)))
export const ROLE_PERMISSIONS: Record<AdminRole, readonly Permission[]> = {
  SuperAdmin: allPermissions,
  OperationsAdmin: [PERMISSIONS.DASHBOARD_READ,PERMISSIONS.USERS_READ,PERMISSIONS.JOBS_READ,PERMISSIONS.JOBS_RETRY,PERMISSIONS.JOBS_CANCEL,PERMISSIONS.JOBS_FAIL_REFUND,PERMISSIONS.HEALTH_READ,PERMISSIONS.AUDIT_READ],
  FinanceAdmin: [PERMISSIONS.DASHBOARD_READ,PERMISSIONS.USERS_READ,PERMISSIONS.USERS_CREDITS_ADJUST,PERMISSIONS.PAYMENTS_READ,PERMISSIONS.PAYMENTS_REFUND,PERMISSIONS.SUBSCRIPTIONS_READ,PERMISSIONS.CREDITS_READ,PERMISSIONS.AUDIT_READ],
  SupportAdmin: [PERMISSIONS.DASHBOARD_READ,PERMISSIONS.USERS_READ,PERMISSIONS.USERS_SUSPEND,PERMISSIONS.JOBS_READ,PERMISSIONS.SUBSCRIPTIONS_READ,PERMISSIONS.CREDITS_READ],
  ProductAdmin: [PERMISSIONS.DASHBOARD_READ,PERMISSIONS.JOBS_READ,PERMISSIONS.HEALTH_READ,PERMISSIONS.CATALOG_READ],
}
export function isAdminRole(value: unknown): value is AdminRole { return typeof value === 'string' && ROLES.includes(value as AdminRole) }
export function isPermission(value:unknown):value is Permission{return typeof value==='string'&&Object.values(PERMISSIONS).includes(value as Permission)}
export function permissionsForRoles(roles: readonly AdminRole[]): ReadonlySet<Permission> { return new Set(roles.flatMap(role=>ROLE_PERMISSIONS[role])) }
export function hasPermission(roles: readonly AdminRole[], permission: Permission): boolean { return roles.some(role=>ROLE_PERMISSIONS[role].includes(permission)) }
