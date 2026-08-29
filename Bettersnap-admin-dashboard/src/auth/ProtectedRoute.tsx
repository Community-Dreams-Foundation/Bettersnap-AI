import type { ReactNode } from 'react'
import { ForbiddenState, LoadingState } from '../components'
import { useAuth } from './AuthProvider'
import type { Permission } from './rbac'
import { resolveRouteAccess } from './routeAccess'
export function ProtectedRoute({permission,children,onLoginRequired}:{permission?:Permission;children:ReactNode;onLoginRequired:()=>ReactNode}){const auth=useAuth();const access=resolveRouteAccess({loading:auth.loading,authenticated:auth.isAuthenticated,hasPermission:permission?auth.hasPermission(permission):true});if(access==='loading')return <LoadingState label="Checking authorization…"/>;if(access==='login')return onLoginRequired();if(access==='forbidden')return <ForbiddenState/>;return children}
