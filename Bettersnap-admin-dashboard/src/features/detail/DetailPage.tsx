import { AlertCircle, AlertTriangle, ArrowLeft, Ban, Bot, Coins, CreditCard, History, Sparkles, User, UserCheck, X } from 'lucide-react'
import { useCallback, useState } from 'react'
import { DataTable, ErrorState, LoadingState, PageHeader, StatusBadge, useToast } from '../../components'
import { jobsService, usersService, type SuperAdminJob, type CreditEntry, type AdminPayment, type AuditEntry } from '../../services'
import { useApiResource } from '../../lib/useApiResource'
import { Link, useRouter } from '../../lib/router'
import { PermissionGate, useAuth } from '../../auth'
import { PERMISSIONS } from '../../auth/rbac'
import { DataSourceBadge } from '../dashboard/DashboardPage'

export function DetailPage({ type, id }: { type: 'user' | 'job'; id: string }) {
  return type === 'user' ? <UserDetailView userId={id} /> : <JobDetailView jobId={id} />
}

type UserTab = 'overview' | 'jobs' | 'payments' | 'credits' | 'subscription' | 'audit'

function UserDetailView({ userId }: { userId: string }) {
  const userMutationsAvailable = true
  const { navigate } = useRouter()
  const auth = useAuth()
  const { showToast } = useToast()
  const [activeTab, setActiveTab] = useState<UserTab>('overview')

  // Action Dialog states
  const [actionType, setActionType] = useState<'suspend' | 'reactivate' | 'adjust_credits' | null>(null)
  const [reason, setReason] = useState('')
  const [creditAmount, setCreditAmount] = useState<number>(100)
  const [actionLoading, setActionLoading] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  const loadUser = useCallback(() => usersService.get(userId), [userId])
  const userResource = useApiResource(loadUser)

  const openAction = (type: typeof actionType) => {
    setActionType(type)
    setReason('')
    setCreditAmount(100)
    setActionError(null)
  }

  const closeAction = () => {
    setActionType(null)
    setReason('')
    setActionError(null)
  }

  const handleExecuteAction = async () => {
    if (!actionType || !u) return
    if (!reason.trim()) {
      setActionError('A reason is mandatory for administrative audit logging.')
      return
    }

    setActionLoading(true)
    setActionError(null)

    try {
      if (actionType === 'suspend') {
        await usersService.suspend(u.user_id, { reason })
        showToast(`Account ${u.user_id} suspended.`, 'success')
      } else if (actionType === 'reactivate') {
        await usersService.reactivate(u.user_id, { reason })
        showToast(`Account ${u.user_id} reactivated.`, 'success')
      } else if (actionType === 'adjust_credits') {
        await usersService.adjustCredits(u.user_id, { amount: creditAmount, reason })
        showToast(`Credits adjusted (${creditAmount > 0 ? '+' : ''}${creditAmount}).`, 'success')
      }

      closeAction()
      userResource.reload()
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : 'Action failed')
    } finally {
      setActionLoading(false)
    }
  }

  if (userResource.loading && !userResource.data) {
    return <LoadingState label="Loading user account details…" />
  }

  if (userResource.error || !userResource.data?.data) {
    return <ErrorState retry={userResource.reload} />
  }

  const u = userResource.data.data
  const accountStatus=u.account_status||(u.suspended?'suspended':'active')

  return (
    <div className="user-detail-view">
      <div className="back-nav">
        <Link to="/users" className="back-link">
          <ArrowLeft size={15} /> Back to Users Directory
        </Link>
      </div>

      <PageHeader
        title={u.full_name}
        description={u.email}
        action={
          <div className="header-meta-actions">
            <DataSourceBadge type="live" />
            <StatusBadge status={accountStatus} />
          </div>
        }
      />

      {/* Backend API required indicator */}
      <div className="notice-banner">
        <AlertTriangle size={18} className="notice-icon" />
        <div className="notice-content">
          <strong>Live SuperAdmin user detail and audited actions</strong>
          <p>
            User data is live. Suspend, reactivate, and credit-adjustment actions require a reason and are audit-logged by the backend.
          </p>
        </div>
      </div>

      {/* Quick Summary Cards */}
      <div className="user-summary-bar">
        <div className="summary-item">
          <span>User ID</span>
          <strong className="mono">{u.user_id}</strong>
        </div>
        <div className="summary-item">
          <span>Credits Remaining</span>
          <strong>{(u.credits_remaining ?? u.credits).toLocaleString()}</strong>
        </div>
        <div className="summary-item">
          <span>Plan & Tier</span>
          <strong>{u.plan_name || 'Free'} ({u.subscription_type || 'one_time'})</strong>
        </div>
        <div className="summary-item">
          <span>LoRA Model</span>
          <StatusBadge status={u.lora_status || 'none'} />
        </div>
      </div>

      {/* Account Action Toolbar */}
      {userMutationsAvailable && <section className="card user-actions-toolbar">
        <div className="toolbar-header">
          <h3>Account Actions</h3>
          <span>Admin Role: {auth.identity?.roles.join(', ') || 'Admin'}</span>
        </div>

        <div className="action-button-group">
          {accountStatus === 'suspended' ? (
            <PermissionGate permission={PERMISSIONS.USERS_SUSPEND}>
              <button className="button secondary sm-btn" onClick={() => openAction('reactivate')}>
                <UserCheck size={14} /> Reactivate Account
              </button>
            </PermissionGate>
          ) : (
            <PermissionGate permission={PERMISSIONS.USERS_SUSPEND}>
              <button className="button danger-btn sm-btn" onClick={() => openAction('suspend')}>
                <Ban size={14} /> Suspend Account
              </button>
            </PermissionGate>
          )}

          <PermissionGate permission={PERMISSIONS.USERS_CREDITS_ADJUST}>
            <button className="button secondary sm-btn" onClick={() => openAction('adjust_credits')}>
              <Coins size={14} /> Add/Remove Credits
            </button>
          </PermissionGate>

        </div>
      </section>}

      {/* Detail Tabs */}
      <div className="user-tabs-container">
        <nav className="tabs-nav" role="tablist">
          <button
            className={`tab-btn ${activeTab === 'overview' ? 'active' : ''}`}
            onClick={() => setActiveTab('overview')}
            role="tab"
          >
            <User size={15} /> Overview
          </button>
          <button
            className={`tab-btn ${activeTab === 'jobs' ? 'active' : ''}`}
            onClick={() => setActiveTab('jobs')}
            role="tab"
          >
            <Bot size={15} /> Generation Jobs ({u.recent_jobs?.length || 0})
          </button>
          <button
            className={`tab-btn ${activeTab === 'payments' ? 'active' : ''}`}
            onClick={() => setActiveTab('payments')}
            role="tab"
          >
            <CreditCard size={15} /> Payments ({u.payments?.length || 0})
          </button>
          <button
            className={`tab-btn ${activeTab === 'credits' ? 'active' : ''}`}
            onClick={() => setActiveTab('credits')}
            role="tab"
          >
            <Coins size={15} /> Credit Ledger ({u.credit_ledger?.length || 0})
          </button>
          <button
            className={`tab-btn ${activeTab === 'subscription' ? 'active' : ''}`}
            onClick={() => setActiveTab('subscription')}
            role="tab"
          >
            <Sparkles size={15} /> Subscription
          </button>
          <button
            className={`tab-btn ${activeTab === 'audit' ? 'active' : ''}`}
            onClick={() => setActiveTab('audit')}
            role="tab"
          >
            <History size={15} /> Audit History ({u.audit_history?.length || 0})
          </button>
        </nav>

        <div className="tab-content">
          {/* TAB 1: OVERVIEW */}
          {activeTab === 'overview' && (
            <div className="overview-tab-grid">
              <article className="card info-card">
                <h2>Account Attributes</h2>
                <dl>
                  <div><dt>Full Name</dt><dd>{u.full_name}</dd></div>
                  <div><dt>Email</dt><dd>{u.email}</dd></div>
                  <div><dt>User ID</dt><dd className="mono">{u.user_id}</dd></div>
                  <div><dt>Stripe Customer</dt><dd className="mono">{u.stripe_customer_id || '—'}</dd></div>
                  <div><dt>Account Status</dt><dd><StatusBadge status={accountStatus} /></dd></div>
                  <div><dt>Registration</dt><dd>{u.created_at ? new Date(u.created_at).toLocaleString() : '—'}</dd></div>
                  <div><dt>Terms Accepted</dt><dd>{u.terms_accepted_at ? new Date(u.terms_accepted_at).toLocaleString() : '—'}</dd></div>
                  <div><dt>Last Active</dt><dd>{u.last_login_at ? new Date(u.last_login_at).toLocaleString() : '—'}</dd></div>
                </dl>
              </article>

              <article className="card info-card">
                <h2>AI & Model Status</h2>
                <dl>
                  <div><dt>LoRA State</dt><dd><StatusBadge status={u.lora_status || 'none'} /></dd></div>
                  <div><dt>Retrain Count</dt><dd>{u.retrain_count ?? 0}</dd></div>
                  <div><dt>Credits Available</dt><dd>{(u.credits_remaining ?? u.credits).toLocaleString()}</dd></div>
                  <div><dt>Monthly Grant</dt><dd>{(u.monthly_credits ?? 0).toLocaleString()} credits</dd></div>
                  <div><dt>One-time Grant</dt><dd>{(u.one_time_credits ?? 0).toLocaleString()} credits</dd></div>
                </dl>
              </article>
            </div>
          )}

          {/* TAB 2: GENERATION JOBS */}
          {activeTab === 'jobs' && (
            <article className="card table-card">
              <DataTable<SuperAdminJob>
                rows={u.recent_jobs || []}
                getRowKey={j => j.job_id}
                onRowClick={j => navigate(`/jobs/${j.job_id}`)}
                emptyTitle="No generation jobs recorded for this user"
                columns={[
                  { key: 'id', header: 'Job ID', render: j => <span className="mono">{j.job_id}</span> },
                  { key: 'type', header: 'Type', render: j => j.job_type || 'Not supplied' },
                  { key: 'status', header: 'Status', render: j => <StatusBadge status={j.status} /> },
                  { key: 'images', header: 'Images', render: j => j.image_count ?? 'Not supplied' },
                  { key: 'duration', header: 'Duration', render: j => (j.processing_seconds != null ? `${j.processing_seconds}s` : '—') },
                  { key: 'date', header: 'Date', render: j => new Date(j.created_at).toLocaleString() },
                ]}
              />
            </article>
          )}

          {/* TAB 3: PAYMENTS */}
          {activeTab === 'payments' && (
            <article className="card table-card">
              <DataTable<AdminPayment>
                rows={u.payments || []}
                getRowKey={p => p.id}
                emptyTitle="No payment transactions found"
                columns={[
                  { key: 'id', header: 'Transaction ID', render: p => <span className="mono">{p.id}</span> },
                  { key: 'amount', header: 'Amount', render: p => `$${(p.amountCents / 100).toFixed(2)}` },
                  { key: 'status', header: 'Status', render: p => <StatusBadge status={p.status} /> },
                  { key: 'date', header: 'Processed Date', render: p => new Date(p.createdAt).toLocaleString() },
                ]}
              />
            </article>
          )}

          {/* TAB 4: CREDITS LEDGER */}
          {activeTab === 'credits' && (
            <article className="card table-card">
              <DataTable<CreditEntry>
                rows={u.credit_ledger || []}
                getRowKey={c => c.id}
                emptyTitle="Credit ledger is empty"
                columns={[
                  { key: 'id', header: 'Entry ID', render: c => <span className="mono">{c.id}</span> },
                  {
                    key: 'delta',
                    header: 'Delta',
                    render: c => (
                      <span className={c.delta >= 0 ? 'delta-positive' : 'delta-negative'}>
                        {c.delta >= 0 ? `+${c.delta}` : c.delta}
                      </span>
                    ),
                  },
                  { key: 'reason', header: 'Reason / Audit Note', render: c => c.reason },
                  { key: 'date', header: 'Timestamp', render: c => new Date(c.createdAt).toLocaleString() },
                ]}
              />
            </article>
          )}

          {/* TAB 5: SUBSCRIPTION */}
          {activeTab === 'subscription' && (
            <article className="card info-card">
              <h2>Subscription Plan Details</h2>
              <dl>
                <div><dt>Plan Name</dt><dd>{u.plan_name || 'Free Tier'}</dd></div>
                <div><dt>Plan Key</dt><dd className="mono">{u.subscription_plan || 'none'}</dd></div>
                <div><dt>Billing Cadence</dt><dd>{u.subscription_type || 'One-time'}</dd></div>
                <div><dt>Subscription Status</dt><dd><StatusBadge status={u.subscription_status || 'active'} /></dd></div>
                <div><dt>Period Start</dt><dd>{u.subscription_start ? new Date(u.subscription_start).toLocaleDateString() : '—'}</dd></div>
                <div><dt>Renewal / Period End</dt><dd>{u.subscription_end ? new Date(u.subscription_end).toLocaleDateString() : '—'}</dd></div>
              </dl>
            </article>
          )}

          {/* TAB 6: AUDIT HISTORY */}
          {activeTab === 'audit' && (
            <article className="card table-card">
              <DataTable<AuditEntry>
                rows={u.audit_history || []}
                getRowKey={a => a.id}
                emptyTitle="No audit logs recorded for this account"
                columns={[
                  { key: 'id', header: 'Audit ID', render: a => <span className="mono">{a.id}</span> },
                  { key: 'actor', header: 'Operator', render: a => <strong>{a.actor}</strong> },
                  { key: 'action', header: 'Action', render: a => <code>{a.action}</code> },
                  { key: 'result', header: 'Result', render: a => <StatusBadge status={a.result} /> },
                  { key: 'date', header: 'Timestamp', render: a => new Date(a.createdAt).toLocaleString() },
                ]}
              />
            </article>
          )}
        </div>
      </div>

      {/* ACCOUNT ACTION MODAL */}
      {actionType && (
        <div className="dialog-backdrop" role="presentation" onMouseDown={closeAction}>
          <div
            className="dialog"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="dialog-action-title"
            onMouseDown={e => e.stopPropagation()}
          >
            <button className="dialog-close" onClick={closeAction} aria-label="Close dialog">
              <X size={18} />
            </button>

            <div className={`dialog-icon ${actionType === 'suspend' ? 'danger' : ''}`}>
              {actionType === 'suspend' && <Ban size={22} />}
              {actionType === 'reactivate' && <UserCheck size={22} />}
              {actionType === 'adjust_credits' && <Coins size={22} />}
            </div>

            <h2 id="dialog-action-title">
              {actionType === 'suspend' && 'Suspend User Account'}
              {actionType === 'reactivate' && 'Reactivate User Account'}
              {actionType === 'adjust_credits' && 'Adjust User Credits'}
            </h2>

            <div className="target-user-card">
              <span>Target User:</span>
              <strong>{u.full_name}</strong>
              <code>{u.user_id}</code>
            </div>

            {actionError && (
              <div className="inline-error" role="alert">
                <AlertCircle size={15} />
                <span>{actionError}</span>
              </div>
            )}

            <div className="dialog-form-fields">
              {actionType === 'adjust_credits' && (
                <label className="field">
                  <span>Credit Amount (positive to add, negative to deduct)</span>
                  <input
                    type="number"
                    value={creditAmount}
                    onChange={e => setCreditAmount(Number(e.target.value))}
                    step={10}
                    required
                  />
                </label>
              )}

              <label className="field">
                <span>Mandatory Operator Reason <em className="required">*</em></span>
                <textarea
                  value={reason}
                  onChange={e => setReason(e.target.value)}
                  placeholder="Explain why this action is being taken for the audit log..."
                  rows={3}
                  required
                />
              </label>
            </div>

            <div className="dialog-actions">
              <button className="button secondary" onClick={closeAction} disabled={actionLoading}>
                Cancel
              </button>
              <button
                className={`button ${actionType === 'suspend' ? 'danger-button' : ''}`}
                onClick={handleExecuteAction}
                disabled={actionLoading || !reason.trim()}
              >
                {actionLoading ? 'Executing…' : 'Confirm Action'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function JobDetailView({ jobId }: { jobId: string }) {
  const load=useCallback(()=>jobsService.get(jobId),[jobId])
  const state=useApiResource(load)
  const{showToast}=useToast()
  const[action,setAction]=useState<'retry'|'cancel'|'restore'|null>(null)
  const[reason,setReason]=useState('')
  const[actionError,setActionError]=useState('')
  const[actionLoading,setActionLoading]=useState(false)
  const close=()=>{setAction(null);setReason('');setActionError('')}
  const execute=async()=>{
    if(!action||!reason.trim())return
    setActionLoading(true);setActionError('')
    try{
      if(action==='retry')await jobsService.retry(jobId,reason.trim())
      else if(action==='cancel')await jobsService.cancel(jobId,reason.trim())
      else await jobsService.restoreCredit(jobId,reason.trim())
      showToast(action==='retry'?'Job queued for retry.':action==='cancel'?'Job cancelled and credits refunded.':'Job credits restored.','success')
      close();state.reload()
    }catch(error){setActionError(error instanceof Error?error.message:'Action failed')}
    finally{setActionLoading(false)}
  }
  if(state.loading&&!state.data)return <LoadingState label="Loading live job detail..."/>
  if(state.error||!state.data?.data)return <ErrorState retry={state.reload}/>
  const job=state.data.data
  return (
    <div className="job-detail-view">
      <Link to="/jobs" className="back-link">
        <ArrowLeft size={15} /> Back to AI Jobs
      </Link>
      <PageHeader title={`Job ${jobId}`} description="Live AI generation execution telemetry" action={<DataSourceBadge type="live"/>}/>
      <section className="card user-actions-toolbar"><div className="toolbar-header"><h3>Audited job actions</h3><span>Every action requires an operator reason.</span></div><div className="action-button-group">
        {job.status==='failed'&&<PermissionGate permission={PERMISSIONS.JOBS_RETRY}><button className="button secondary sm-btn" onClick={()=>setAction('retry')}>Retry failed job</button></PermissionGate>}
        {['waiting_lora','queued','dispatching','processing'].includes(job.status)&&<PermissionGate permission={PERMISSIONS.JOBS_CANCEL}><button className="button danger-btn sm-btn" onClick={()=>setAction('cancel')}>Cancel job</button></PermissionGate>}
        <PermissionGate permission={PERMISSIONS.JOBS_FAIL_REFUND}><button className="button secondary sm-btn" onClick={()=>setAction('restore')}>Restore job credits</button></PermissionGate>
      </div></section>
      <section className="card info-card">
        <h2>Job details</h2>
        <dl>
          <div><dt>Job ID</dt><dd className="mono">{jobId}</dd></div>
          <div><dt>User ID</dt><dd className="mono">{job.user_id}</dd></div>
          <div><dt>Status</dt><dd><StatusBadge status={job.status} /></dd></div>
          <div><dt>Job type</dt><dd>{job.job_type||'Not supplied'}</dd></div>
          <div><dt>Submitted</dt><dd>{new Date(job.created_at).toLocaleString()}</dd></div>
          <div><dt>Dispatched</dt><dd>{job.dispatched_at?new Date(job.dispatched_at).toLocaleString():'Not dispatched'}</dd></div>
          <div><dt>Completed</dt><dd>{job.completed_at?new Date(job.completed_at).toLocaleString():'Not completed'}</dd></div>
          <div><dt>Execution ID</dt><dd className="mono">{job.execution_id||'Not returned'}</dd></div>
          <div><dt>Images</dt><dd>{job.image_count??'Not supplied'}</dd></div>
          <div><dt>Credits consumed</dt><dd>{job.credits_consumed??'Not returned'}</dd></div>
          <div><dt>Processing duration</dt><dd>{job.processing_seconds==null?'Not supplied':`${job.processing_seconds}s`}</dd></div>
          <div><dt>Parameters</dt><dd><pre className="audit-json">{JSON.stringify(job.params||{},null,2)}</pre></dd></div>
        </dl>
      </section>
      {action&&<div className="dialog-backdrop" role="presentation" onMouseDown={close}><div className="dialog" role="alertdialog" aria-modal="true" aria-labelledby="job-action-title" onMouseDown={event=>event.stopPropagation()}><h2 id="job-action-title">{action==='retry'?'Retry failed job':action==='cancel'?'Cancel job and refund credits':'Restore job credits'}</h2><p>Target job: <code>{jobId}</code></p>{action==='cancel'&&<div className="alert-box-warning"><AlertTriangle size={16}/><span>Cancellation stops the execution and refunds credits according to the backend workflow.</span></div>}{actionError&&<div className="inline-error" role="alert">{actionError}</div>}<label className="field"><span>Mandatory operator reason</span><textarea value={reason} onChange={event=>setReason(event.target.value)} rows={4} required/></label><div className="dialog-actions"><button className="button secondary" onClick={close} disabled={actionLoading}>Cancel</button><button className={action==='cancel'?'button danger-button':'button'} onClick={execute} disabled={actionLoading||!reason.trim()}>{actionLoading?'Executing...':'Confirm action'}</button></div></div></div>}
    </div>
  )
}
