import { ProtectedRoute, PERMISSIONS, type Permission } from './auth'
import { AppShell } from './components'
import { LoginPage } from './features/auth/LoginPage'
import { ResourcePage } from './features/common/ResourcePage'
import { DashboardPage } from './features/dashboard/DashboardPage'
import { DetailPage } from './features/detail/DetailPage'
import { CreditsPage, PaymentsPage, SubscriptionsPage } from './features/finance/FinancePages'
import { JobsPage } from './features/jobs/JobsPage'
import { AuditLogsPage } from './features/operations/AuditLogsPage'
import { CatalogPlansPage, SystemHealthPage } from './features/operations'
import { UsersPage } from './features/users/UsersPage'
import { useRouter } from './lib/router'

export const ROUTE_PERMISSIONS:Readonly<Record<string,Permission>>={'/dashboard':PERMISSIONS.DASHBOARD_READ,'/users':PERMISSIONS.USERS_READ,'/jobs':PERMISSIONS.JOBS_READ,'/payments':PERMISSIONS.BILLING_READ,'/subscriptions':PERMISSIONS.SUBSCRIPTIONS_READ,'/credits':PERMISSIONS.CREDITS_READ,'/system-health':PERMISSIONS.HEALTH_READ,'/audit-logs':PERMISSIONS.AUDIT_READ,'/catalog-plans':PERMISSIONS.CATALOG_READ}
export function permissionForPath(path:string):Permission|undefined{const base=`/${path.split('/').filter(Boolean)[0]||''}`;return ROUTE_PERMISSIONS[base]}

export default function App(){
  const{path}=useRouter()
  if(path==='/'||path==='/login')return <LoginPage/>
  let page
  const userMatch=path.match(/^\/users\/([^/]+)$/)
  const jobMatch=path.match(/^\/jobs\/([^/]+)$/)
  if(path==='/dashboard')page=<DashboardPage/>
  else if(path==='/users')page=<UsersPage/>
  else if(path==='/jobs')page=<JobsPage/>
  else if(path==='/payments')page=<PaymentsPage/>
  else if(path==='/subscriptions')page=<SubscriptionsPage/>
  else if(path==='/credits')page=<CreditsPage/>
  else if(path==='/audit-logs')page=<AuditLogsPage/>
  else if(path==='/system-health')page=<SystemHealthPage/>
  else if(path==='/catalog-plans')page=<CatalogPlansPage/>
  else if(userMatch)page=<DetailPage type="user" id={userMatch[1]}/>
  else if(jobMatch)page=<DetailPage type="job" id={jobMatch[1]}/>
  else page=<ResourcePage resource={path.slice(1)}/>
  return <ProtectedRoute permission={permissionForPath(path)} onLoginRequired={()=><LoginPage/>}><AppShell>{page}</AppShell></ProtectedRoute>
}
