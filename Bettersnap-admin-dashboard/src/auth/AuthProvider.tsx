import { createContext,useCallback,useContext,useEffect,useMemo,useRef,useState,type ReactNode } from 'react'
import type { AccountInfo } from '@azure/msal-browser'
import { getAccount,getAccessToken,initializeAuth,signIn as entraSignIn,signOut as entraSignOut } from '../lib/auth'
import { onSessionExpired } from './sessionEvents'
import { setAccessTokenResolver } from './tokenBroker'
import { permissionsForRoles,type Permission } from './rbac'
import { adminService } from '../services/adminService'
import { ApiError } from '../lib/api/client'
import { isSuperAdminEmail } from './superAdminAccess'

export interface AdminIdentity{id:string;name:string;email:string;roles:string[];permissions:Permission[];sessionExpiresAt?:string;developmentOnly?:boolean}
interface AuthContextValue{identity:AdminIdentity|null;loading:boolean;isAuthenticated:boolean;idleWarning:boolean;permissions:ReadonlySet<Permission>;login:()=>Promise<void>;logout:()=>Promise<void>;continueSession:()=>void;hasPermission:(permission:Permission)=>boolean}
const AuthContext=createContext<AuthContextValue|null>(null)
setAccessTokenResolver(getAccessToken)
const idleMs=Math.max(1,Number(import.meta.env.VITE_AUTH_IDLE_TIMEOUT_MINUTES)||30)*60_000
const warningMs=Math.min(idleMs-1000,Math.max(0,Number(import.meta.env.VITE_AUTH_IDLE_WARNING_MINUTES)||2)*60_000)
function identityFromClaims(account:AccountInfo):AdminIdentity{const claims=account.idTokenClaims as Record<string,unknown>|undefined;const tokenRoles=Array.isArray(claims?.roles)?claims.roles.filter((role):role is string=>typeof role==='string'):[];const claimEmail=typeof claims?.email==='string'?claims.email:account.username;const isAdmin=tokenRoles.includes('Admin')&&isSuperAdminEmail(claimEmail);const expires=typeof claims?.exp==='number'?new Date(claims.exp*1000).toISOString():undefined;return{id:account.localAccountId,name:account.name||'BetterSnap Super Admin',email:claimEmail,roles:isAdmin?['SuperAdmin']:tokenRoles,permissions:isAdmin?[...permissionsForRoles(['SuperAdmin'])]:[],sessionExpiresAt:expires}}
async function toIdentity(account:AccountInfo):Promise<AdminIdentity>{const claimsIdentity=identityFromClaims(account);try{const server=(await adminService.getMe()).data;const isAdmin=server.roles.includes('Admin')&&isSuperAdminEmail(server.email);return{...claimsIdentity,id:server.oid,name:server.name||claimsIdentity.name,email:server.email,roles:isAdmin?['SuperAdmin']:server.roles,permissions:isAdmin?[...permissionsForRoles(['SuperAdmin'])]:[]}}catch(error){if(error instanceof ApiError&&error.status===404)return claimsIdentity;throw error}}
function redirectToLogin(){if(window.location.pathname!=='/login'){window.history.replaceState({},'', '/login');window.dispatchEvent(new PopStateEvent('popstate'))}}

export function AuthProvider({children}:{children:ReactNode}){
  const[identity,setIdentity]=useState<AdminIdentity|null>(null)
  const[loading,setLoading]=useState(true)
  const[idleWarning,setIdleWarning]=useState(false)
  const warningTimer=useRef<number|undefined>(undefined)
  const expiryTimer=useRef<number|undefined>(undefined)
  const serverTimer=useRef<number|undefined>(undefined)
  const clearTimers=useCallback(()=>{[warningTimer,expiryTimer,serverTimer].forEach(ref=>{if(ref.current)window.clearTimeout(ref.current)})},[])
  const expire=useCallback(()=>{clearTimers();setIdleWarning(false);setIdentity(null);redirectToLogin()},[clearTimers])
  const startIdleTimers=useCallback(()=>{if(!identity)return;clearTimers();warningTimer.current=window.setTimeout(()=>setIdleWarning(true),Math.max(0,idleMs-warningMs));expiryTimer.current=window.setTimeout(expire,idleMs);if(identity.sessionExpiresAt){const delay=new Date(identity.sessionExpiresAt).getTime()-Date.now();serverTimer.current=window.setTimeout(expire,Math.max(0,delay))}},[identity,clearTimers,expire])
  const scheduleIdle=useCallback(()=>{setIdleWarning(false);startIdleTimers()},[startIdleTimers])
  useEffect(()=>{let active=true;initializeAuth().then(async()=>{const account=getAccount();if(active&&account){const next=await toIdentity(account);if(active)setIdentity(next.permissions.length?next:null)}}).catch(()=>{if(active)setIdentity(null)}).finally(()=>{if(active)setLoading(false)});return()=>{active=false}},[])
  useEffect(()=>onSessionExpired(expire),[expire])
  useEffect(()=>{if(!identity){clearTimers();return}const timer=window.setTimeout(startIdleTimers,0);const events=['pointerdown','keydown','scroll','touchstart'] as const;events.forEach(event=>window.addEventListener(event,scheduleIdle,{passive:true}));return()=>{window.clearTimeout(timer);events.forEach(event=>window.removeEventListener(event,scheduleIdle))}},[identity,startIdleTimers,scheduleIdle,clearTimers])
  const login=useCallback(async()=>{const account=await entraSignIn();const next=await toIdentity(account);if(!next.permissions.length)throw new Error('Access is restricted to Admin within BetterSnap.');setIdentity(next)},[])
  const logout=useCallback(async()=>{clearTimers();setIdentity(null);setIdleWarning(false);await entraSignOut();redirectToLogin()},[clearTimers])
  const permissions=useMemo(()=>new Set(identity?.permissions||[]),[identity])
  const value=useMemo<AuthContextValue>(()=>({identity,loading,isAuthenticated:Boolean(identity),idleWarning,permissions,login,logout,continueSession:scheduleIdle,hasPermission:p=>permissions.has(p)}),[identity,loading,idleWarning,permissions,login,logout,scheduleIdle])
  return <AuthContext.Provider value={value}>{children}{idleWarning&&<div className="dialog-backdrop"><section className="dialog" role="alertdialog" aria-modal="true" aria-labelledby="idle-title"><h2 id="idle-title">Your session is about to expire</h2><p>You have been inactive. Continue now to keep your admin session open.</p><div className="dialog-actions"><button className="button secondary" onClick={logout}>Log out</button><button className="button" onClick={scheduleIdle}>Continue session</button></div></section></div>}</AuthContext.Provider>
}
export function useAuth(){const value=useContext(AuthContext);if(!value)throw new Error('useAuth must be used inside AuthProvider');return value}
