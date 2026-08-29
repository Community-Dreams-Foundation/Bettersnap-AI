import { useCallback, useReducer } from 'react'
import { DataTable, ErrorState, LoadingState, PageHeader, Pagination, StatusBadge } from '../../components'
import { ApiError } from '../../lib/api/client'
import { useApiResource } from '../../lib/useApiResource'
import { auditService, type SuperAdminAuditEvent } from '../../services'
import { DataSourceBadge } from '../dashboard/DashboardPage'
import { auditQueryKey, auditQueryReducer, initialAuditQuery } from './auditQuery'

function auditErrorMessage(error:Error){
  if(error instanceof ApiError){
    if(error.status===401)return 'Your Microsoft session is no longer authorized. Sign out and sign in again.'
    if(error.status===403)return 'The signed-in account does not have permission to read SuperAdmin audit logs.'
    if(error.status===404)return 'The deployed backend does not expose GET /api/superadmin/audit-logs.'
    return `The audit API returned HTTP ${error.status}: ${error.message}`
  }
  return error.message||'The audit-log request could not reach the backend.'
}

export function AuditLogsPage(){
  const[q,dispatch]=useReducer(auditQueryReducer,initialAuditQuery),{targetType,targetId,action,page}=q,limit=50
  // Filter changes reset to page 1 inside the reducer, on the event itself --
  // never in an effect reacting to the change afterwards.
  const setFilter=useCallback((field:'targetType'|'targetId'|'action',value:string)=>dispatch({type:'setFilter',field,value}),[])
  const setPage=useCallback((next:number)=>dispatch({type:'setPage',page:next}),[])
  const load=useCallback(()=>auditService.list({target_type:targetType||undefined,target_id:targetId||undefined,action:action||undefined,limit,offset:(page-1)*limit}),[targetType,targetId,action,page])
  const s=useApiResource(load,auditQueryKey(q))
  if(s.loading&&!s.data)return <LoadingState label="Loading live audit logs…"/>
  if(s.error||!s.data)return <ErrorState retry={s.reload} title="Audit logs could not be loaded" description={s.error?auditErrorMessage(s.error):'The audit API returned no response.'}/>
  const d=s.data.data
  return <><PageHeader title="Audit Logs" description="Live immutable history for every SuperAdmin mutation." action={<div className="header-meta-actions"><DataSourceBadge type="live"/><button className="button secondary" onClick={s.reload}>Refresh</button></div>}/><section className="card table-card"><div className="filter-bar"><input value={targetType} onChange={e=>setFilter('targetType',e.target.value)} placeholder="Target type"/><input value={targetId} onChange={e=>setFilter('targetId',e.target.value)} placeholder="Target ID"/><input value={action} onChange={e=>setFilter('action',e.target.value)} placeholder="Action"/></div><DataTable<SuperAdminAuditEvent> rows={d.events||[]} getRowKey={x=>x.event_id} emptyTitle="No audit events recorded" columns={[{key:'time',header:'Timestamp',render:x=>new Date(x.created_at).toLocaleString()},{key:'actor',header:'Admin',render:x=>x.actor_email},{key:'action',header:'Action',render:x=><code>{x.action}</code>},{key:'target',header:'Target',render:x=><>{x.target_type}<small className="mono">{x.target_id}</small></>},{key:'reason',header:'Reason',render:x=>x.reason||'—'},{key:'result',header:'Result',render:x=><StatusBadge status={x.result}/>},{key:'changes',header:'Changes',render:x=><details><summary>View</summary><pre className="audit-json">{JSON.stringify({before:x.previous_value,after:x.new_value},null,2)}</pre></details>}]}/><div className="table-footer"><span>{d.total||0} events</span><Pagination page={page} totalPages={Math.max(1,Math.ceil((d.total||0)/limit))} onChange={setPage}/></div></section></>
}
