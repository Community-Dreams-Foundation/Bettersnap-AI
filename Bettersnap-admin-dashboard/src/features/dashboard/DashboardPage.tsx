import { AlertTriangle, Bot, Clock3, Coins, RefreshCw, Users } from 'lucide-react'
import { useCallback } from 'react'
import { EmptyState, ErrorState, LoadingState, PageHeader, StatusBadge } from '../../components'
import { Link } from '../../lib/router'
import { useApiResource } from '../../lib/useApiResource'
import { dashboardService } from '../../services'

export type DataSourceType='live'|'required'
export function DataSourceBadge({type}:{type:DataSourceType}){const label=type==='live'?'Live backend':'Backend API required';return <span className={`data-source-badge ${type}`}><span/>{label}</span>}

export function DashboardPage(){
  const load=useCallback(()=>dashboardService.get(),[])
  const state=useApiResource(load)
  if(state.loading&&!state.data)return <LoadingState label="Loading live platform data..."/>
  if(state.error||!state.data)return <ErrorState retry={state.reload}/>

  const dashboard=state.data
  const successRate=dashboard.jobs.today?Math.round(dashboard.jobs.completedToday/dashboard.jobs.today*100):null
  const value=(amount:number|null)=>amount===null?'Not returned':amount.toLocaleString()
  const metrics=[
    ['Total registered users',dashboard.users.total.toLocaleString(),<Users size={16}/>],
    ['New users (30d)',dashboard.users.new30d.toLocaleString(),<Users size={16}/>],
    ['Suspended users',dashboard.users.suspended.toLocaleString(),<Users size={16}/>],
    ['Paying users',dashboard.users.paying.toLocaleString(),<Coins size={16}/>],
    ['Active subscriptions',dashboard.billing.activeSubscriptions.toLocaleString(),<Coins size={16}/>],
    ['Total generation jobs',dashboard.jobs.total.toLocaleString(),<Bot size={16}/>],
    ['Images generated',value(dashboard.jobs.totalImagesGenerated),<Bot size={16}/>],
    ['Jobs today',dashboard.jobs.today.toLocaleString(),<Bot size={16}/>],
    ['Completed today',dashboard.jobs.completedToday.toLocaleString(),<Bot size={16}/>],
    ['Failed today',dashboard.jobs.failedToday.toLocaleString(),<AlertTriangle size={16}/>],
    ['Current queue size',dashboard.systemHealth.queue_depth.toLocaleString(),<Clock3 size={16}/>],
    ['Average recorded duration',dashboard.jobs.averageProcessingSeconds===null?'Not returned':`${dashboard.jobs.averageProcessingSeconds}s`,<Clock3 size={16}/>],
    ['Success rate today',successRate===null?'No jobs today':`${successRate}%`,<Bot size={16}/>],
    ['Credits purchased',dashboard.billing.creditsPurchased.toLocaleString(),<Coins size={16}/>],
    ['Credits used',dashboard.billing.creditsUsed.toLocaleString(),<Coins size={16}/>],
  ] as const

  return <div className="dashboard-view">
    <PageHeader title="Super Admin Dashboard" description="Live values composed from the documented SuperAdmin users, jobs, billing, credit, audit, and health endpoints." action={<div className="header-meta-actions"><span className="last-updated">Last updated: {dashboard.updatedAt.toLocaleString()}</span><button className="button secondary" onClick={state.reload}><RefreshCw size={14} className={state.loading?'spin':''}/>Refresh</button></div>}/>
    <div className="notice-banner"><div className="notice-content"><strong>No summary endpoint dependency</strong><p>Every displayed number is calculated from live <code>/api/superadmin/*</code> list and health responses. Missing backend fields are shown as “Not returned.”</p></div></div>

    <section className="dashboard-grid-top">
      <article className="card info-card"><div className="card-heading"><div><h2>Platform status</h2><p>GET /health</p></div><DataSourceBadge type="live"/></div><dl className="status-list"><div><dt>Backend API</dt><dd className="status-val">{dashboard.health.status}<StatusBadge status={dashboard.health.status==='OK'?'healthy':'failed'}/></dd></div><div><dt>Face gate</dt><dd className="status-val">{dashboard.health.face_gate}<StatusBadge status={dashboard.health.face_gate==='ok'?'healthy':'failed'}/></dd></div></dl></article>
      <article className="card info-card"><div className="card-heading"><div><h2>Operations</h2><p>GET /superadmin/system-health</p></div><DataSourceBadge type="live"/></div><dl className="status-list"><div><dt>Queue depth</dt><dd>{dashboard.systemHealth.queue_depth.toLocaleString()}</dd></div><div><dt>Failed jobs (24h)</dt><dd>{dashboard.systemHealth.failed_jobs_24h.toLocaleString()}</dd></div><div><dt>GPU executions</dt><dd>{dashboard.systemHealth.gpu_active_executions.toLocaleString()}</dd></div></dl><Link className="button secondary" to="/jobs">Open AI Jobs</Link></article>
    </section>

    <section className="dashboard-section"><div className="section-title-wrap"><div><h2>Live platform metrics</h2><p>Aggregated from complete paginated API results; no placeholder values are used.</p></div><DataSourceBadge type="live"/></div><div className="metrics-grid">{metrics.map(([label,metric,icon])=><article className="metric-card" key={label}><div className="metric-top"><span>{label}</span><span className="metric-icon">{icon}</span></div><strong>{metric}</strong></article>)}</div></section>

    <section className="dashboard-grid-bottom">
      <article className="card"><div className="card-heading"><div><h2>Currency revenue</h2><p>The documented payment response has no currency amount.</p></div><DataSourceBadge type="required"/></div><EmptyState title="Not available" description="Revenue cannot be calculated until the backend joins Stripe payment amounts with plan prices."/></article>
      <article className="card"><div className="card-heading"><div><h2>Recent admin activity</h2><p>GET /superadmin/audit-logs?limit=5</p></div><DataSourceBadge type="live"/></div>{dashboard.recentActivity.length?<dl className="status-list">{dashboard.recentActivity.map(event=><div key={event.event_id}><dt><strong>{event.action}</strong><small>{event.actor_email} · {new Date(event.created_at).toLocaleString()}</small></dt><dd><StatusBadge status={event.result}/></dd></div>)}</dl>:<EmptyState title="No admin activity" description="The audit endpoint returned no events."/>}<Link className="button secondary" to="/audit-logs">Open Audit Logs</Link></article>
    </section>
  </div>
}
