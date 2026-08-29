import type { ReactNode } from 'react'
import type { Permission } from './rbac'
import { AuthorizationGate } from './AuthorizationGate'
export function PermissionGate({permission,children,fallback=null}:{permission:Permission;children:ReactNode;fallback?:ReactNode}){return <AuthorizationGate permission={permission} fallback={fallback}>{children}</AuthorizationGate>}
