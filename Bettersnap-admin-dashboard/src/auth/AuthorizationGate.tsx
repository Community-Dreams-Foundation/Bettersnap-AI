import type{ReactNode}from'react'
import{useAuth}from'./AuthProvider'
import{isAuthorized}from'./authorization'
import type{Permission}from'./rbac'
export function AuthorizationGate({permission,permissions,requireAll=true,children,fallback=null}:{permission?:Permission;permissions?:readonly Permission[];requireAll?:boolean;children:ReactNode;fallback?:ReactNode}){const auth=useAuth();const required=permissions||permission;if(!required)return children;return isAuthorized(auth.permissions,required,requireAll)?children:fallback}
