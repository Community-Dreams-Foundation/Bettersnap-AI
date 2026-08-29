import { RefreshCw } from 'lucide-react'
import { useCallback, useState } from 'react'
import { DataTable, ErrorState, LoadingState, PageHeader, Pagination, SearchFilterBar, StatusBadge } from '../../components'
import { useApiResource } from '../../lib/useApiResource'
import { useRouter } from '../../lib/router'
import { jobsService, type SuperAdminJob } from '../../services'
import { DataSourceBadge } from '../dashboard/DashboardPage'

export function JobsPage(){
  const{navigate}=useRouter()
  const[status,setStatus]=useState('')
  const[userId,setUserId]=useState('')
  const[page,setPage]=useState(1)
  const limit=20
  const load=useCallback(()=>jobsService.list({status:status||undefined,user_id:userId||undefined,limit,offset:(page-1)*limit}),[status,userId,page])
  const state=useApiResource(load,`${status}|${userId}|${page}`)
  if(state.loading&&!state.data)return <LoadingState label="Loading generation jobs..."/>
  if(state.error||!state.data)return <ErrorState retry={state.reload}/>
  const response=state.data.data

  return <div className="jobs-view">
    <PageHeader title="AI Generation Jobs" description="Live platform jobs with the filters supported by the backend." action={<div className="header-meta-actions"><DataSourceBadge type="live"/><button className="button secondary" onClick={state.reload}><RefreshCw size={14} className={state.loading?'spin':''}/>Refresh</button></div>}/>
    <section className="card table-card">
      <div className="filters-grid-bar"><SearchFilterBar value={userId} onChange={value=>{setUserId(value);setPage(1)}} placeholder="Filter by user ID..."/><div className="inline-filters"><select value={status} onChange={event=>{setStatus(event.target.value);setPage(1)}} aria-label="Filter job status"><option value="">All statuses</option><option value="waiting_lora">Waiting for LoRA</option><option value="queued">Queued</option><option value="dispatching">Dispatching</option><option value="processing">Processing</option><option value="completed">Completed</option><option value="failed">Failed</option></select></div></div>
      <DataTable<SuperAdminJob> rows={response.jobs} getRowKey={job=>job.job_id} onRowClick={job=>navigate(`/jobs/${job.job_id}`)} emptyTitle="No jobs found" columns={[
        {key:'id',header:'Job ID',render:job=><span className="mono">{job.job_id}</span>},
        {key:'user',header:'User ID',render:job=><span className="mono">{job.user_id}</span>},
        {key:'type',header:'Type',render:job=>job.job_type||'Not returned'},
        {key:'status',header:'Status',render:job=><StatusBadge status={job.status}/>},
        {key:'images',header:'Images',render:job=>job.image_count??'Not returned'},
        {key:'credits',header:'Credits used',render:job=>job.credits_consumed??'Not returned'},
        {key:'duration',header:'Duration',render:job=>job.processing_seconds==null?'Not returned':`${job.processing_seconds}s`},
        {key:'created',header:'Submitted',render:job=>new Date(job.created_at).toLocaleString()},
      ]}/>
      <div className="table-footer"><span>{response.total.toLocaleString()} jobs</span><Pagination page={page} totalPages={Math.max(1,Math.ceil(response.total/limit))} onChange={setPage}/></div>
    </section>
  </div>
}
