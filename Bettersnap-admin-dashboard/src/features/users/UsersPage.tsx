import { AlertTriangle, RefreshCw } from 'lucide-react'
import { useCallback, useState } from 'react'
import { DataTable, ErrorState, LoadingState, PageHeader, Pagination, SearchFilterBar, StatusBadge } from '../../components'
import { useApiResource } from '../../lib/useApiResource'
import { useRouter } from '../../lib/router'
import { usersService, type SuperAdminUser } from '../../services'
import { DataSourceBadge } from '../dashboard/DashboardPage'

export function UsersPage(){
  const{navigate}=useRouter()
  const[search,setSearch]=useState('')
  const[page,setPage]=useState(1)
  const limit=20
  const load=useCallback(()=>usersService.list({q:search||undefined,limit,offset:(page-1)*limit}),[search,page])
  const state=useApiResource(load,`${search}|${page}`)
  if(state.loading&&!state.data)return <LoadingState label="Loading platform users directory..."/>
  if(state.error||!state.data)return <ErrorState retry={state.reload}/>
  const response=state.data.data

  return <div className="users-page-view">
    <PageHeader title="User Management" description="Search live platform accounts by name, email, or user ID." action={<div className="header-meta-actions"><DataSourceBadge type="live"/><button className="button secondary refresh-btn" onClick={state.reload}><RefreshCw size={14} className={state.loading?'spin':''}/>Refresh</button></div>}/>
    <div className="notice-banner"><AlertTriangle size={18} className="notice-icon"/><div className="notice-content"><strong>Live user directory and audited actions</strong><p>Data comes from <code>/api/superadmin/users</code>. Open a user to suspend, reactivate, adjust credits, or add an internal note.</p></div></div>
    <section className="card table-card">
      <SearchFilterBar value={search} onChange={value=>{setSearch(value);setPage(1)}} placeholder="Search by name, email, or user ID..."/>
      <DataTable<SuperAdminUser> rows={response.users} getRowKey={user=>user.user_id} onRowClick={user=>navigate(`/users/${user.user_id}`)} emptyTitle="No users found" columns={[
        {key:'user',header:'User',render:user=><div className="person"><span className="avatar soft">{user.full_name?.split(' ').map(part=>part[0]).join('').slice(0,2)||'?'}</span><div><strong>{user.full_name}</strong><small>{user.email}</small></div></div>},
        {key:'id',header:'User ID',render:user=><span className="mono">{user.user_id}</span>},
        {key:'credits',header:'Credits',render:user=><strong>{(user.credits_remaining??user.credits).toLocaleString()}</strong>},
        {key:'plan',header:'Plan',render:user=>user.plan_name||'Not returned'},
        {key:'subscription',header:'Subscription',render:user=>user.subscription_type||'Not returned'},
        {key:'lora',header:'LoRA status',render:user=><StatusBadge status={user.lora_status||'none'}/>},
        {key:'status',header:'Account status',render:user=><StatusBadge status={user.account_status||(user.suspended?'suspended':'active')}/>},
        {key:'created',header:'Registered',render:user=>user.created_at?new Date(user.created_at).toLocaleDateString():'Not returned'},
      ]}/>
      <div className="table-footer"><span>Displaying {response.users.length} of {response.total.toLocaleString()} users</span><Pagination page={page} totalPages={Math.max(1,Math.ceil(response.total/limit))} onChange={setPage}/></div>
    </section>
  </div>
}
