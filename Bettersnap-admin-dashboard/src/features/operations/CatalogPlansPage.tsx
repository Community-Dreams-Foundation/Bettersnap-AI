import { RefreshCw } from 'lucide-react'
import { useCallback } from 'react'
import { DataTable, EmptyState, ErrorState, LoadingState, PageHeader } from '../../components'
import { useApiResource } from '../../lib/useApiResource'
import { catalogService, plansService, type Plan, type SubscriptionPlan } from '../../services'
import { DataSourceBadge } from '../dashboard/DashboardPage'
import { catalogSummary } from './catalogSummary'

export function CatalogPlansPage() {
  const load = useCallback(async () => {
    const [catalog, plans, subscriptions] = await Promise.all([catalogService.getCatalog(), plansService.getPlans(), plansService.getSubscriptionPlans()])
    return { catalog: catalog.data, plans: plans.data, subscriptions: subscriptions.data }
  }, [])
  const state = useApiResource(load)
  if (state.loading && !state.data) return <LoadingState label="Loading published catalog and plans..." />
  if (state.error || !state.data) return <ErrorState retry={state.reload} />
  const { catalog, plans, subscriptions } = state.data
  const summary = catalogSummary(catalog)
  const billingRows = [...subscriptions.one_time.map(plan => ({ ...plan, type: 'One-time' })), ...subscriptions.monthly.map(plan => ({ ...plan, type: 'Monthly' }))]

  return <div className="catalog-page">
    <PageHeader title="Catalog & Plans" description="Read-only published product configuration from the BetterSnap backend." action={<div className="header-meta-actions"><DataSourceBadge type="live"/><span className="badge badge-neutral"><span/>Read only</span><button className="button secondary" onClick={state.reload}><RefreshCw size={14}/>Refresh</button></div>}/>
    <section className="metrics"><article className="metric-card"><span>Categories</span><strong>{summary.categories}</strong></article><article className="metric-card"><span>Attires</span><strong>{summary.attires}</strong></article><article className="metric-card"><span>Backgrounds</span><strong>{summary.backgrounds}</strong></article><article className="metric-card"><span>Generation plans</span><strong>{plans.plans.length}</strong></article></section>
    <section className="card"><div className="section-heading"><div><h2>Catalog</h2><p>GET /catalog</p></div><DataSourceBadge type="live"/></div>{catalog.categories.length === 0 ? <EmptyState title="No published categories"/> : <div className="catalog-category-grid">{catalog.categories.map(category => <article className="catalog-category" key={category.id}><div><h3>{category.name}</h3><span className="badge badge-neutral"><span/>{category.type}</span></div><div><strong>Attires</strong>{category.attires.length ? <ul>{category.attires.map(item => <li key={item.id}>{item.name}<code>{item.id}</code></li>)}</ul> : <p className="muted">None published</p>}</div><div><strong>Backgrounds</strong>{category.backgrounds.length ? <ul>{category.backgrounds.map(item => <li key={item.id}>{item.name}<code>{item.id}</code></li>)}</ul> : <p className="muted">None published</p>}</div></article>)}</div>}</section>
    <section className="card table-card"><div className="section-heading"><div><h2>Generation plan rules</h2><p>GET /plans</p></div><DataSourceBadge type="live"/></div><DataTable<Plan> rows={plans.plans} getRowKey={p => p.key} columns={[{key:'name',header:'Plan',render:p=><><strong>{p.name}</strong><small className="mono">{p.key}</small></>},{key:'type',header:'Type',render:p=>p.plan_type.replaceAll('_',' ')},{key:'images',header:'Images',render:p=>p.image_count},{key:'attires',header:'Max attires',render:p=>p.max_attires},{key:'backgrounds',header:'Max backgrounds',render:p=>p.max_backgrounds},{key:'rule',header:'Category rule',render:p=>p.category_rule},{key:'credits',header:'Credits / image',render:p=>p.credits_per_image}]}/></section>
    <section className="card table-card"><div className="section-heading"><div><h2>Billing plans</h2><p>Monthly and one-time pricing from GET /subscriptions/plans</p></div><DataSourceBadge type="live"/></div><DataTable<SubscriptionPlan & {type:string}> rows={billingRows} getRowKey={p=>`${p.type}-${p.plan}`} columns={[{key:'plan',header:'Plan',render:p=>p.plan},{key:'type',header:'Structure',render:p=>p.type},{key:'images',header:'Images',render:p=>p.images},{key:'credits',header:'Credits',render:p=>p.credits.toLocaleString()},{key:'price',header:'Price',render:p=>new Intl.NumberFormat('en-US',{style:'currency',currency:'USD'}).format((p.discounted_cents ?? p.price_cents ?? p.original_cents ?? 0)/100)}]}/></section>
  </div>
}
