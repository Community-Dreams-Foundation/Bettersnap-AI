import type { ApiResult } from '../lib/api/client'
export type ServiceResult<T>=Promise<ApiResult<T>>
export interface HealthResponse{status:'OK'|string;face_gate:'ok'|string}
export interface CatalogItem{id:string;name:string;category?:string}
export interface CatalogCategory{id:string;name:string;type:string;attires:CatalogItem[];backgrounds:CatalogItem[]}
export interface CatalogResponse{categories:CatalogCategory[]}
export interface Plan{key:string;name:string;plan_type:'one_time'|'monthly'|string;image_count:number;max_attires:number;max_backgrounds:number;category_rule:string;credits_per_image:number}
export interface PlansResponse{plans:Plan[]}
export interface SubscriptionPlan{plan:string;images:number;credits:number;original_cents?:number;discounted_cents?:number;price_cents?:number}
export interface SubscriptionPlansResponse{one_time:SubscriptionPlan[];monthly:SubscriptionPlan[]}
export interface PageQuery{page?:number;pageSize?:number;search?:string}
export interface Page<T>{items:T[];total:number;page:number;pageSize:number}
export interface AdminSummary{totalUsers:number;activeSubscriptions:number;totalJobs:number;revenueCents:number}
export interface AdminUser{id:string;name:string;email:string;status:string;credits:number}
export interface AdminJob{id:string;userId:string;status:string;createdAt:string}

export interface AdminPayment {
  id: string
  userId: string
  userEmail?: string
  stripeReference: string
  plan: string
  amountCents: number
  currency: string
  status: 'succeeded' | 'pending' | 'failed' | 'refunded' | string
  creditsGranted: number
  refundStatus: 'none' | 'pending' | 'refunded' | string
  refundReason?: string
  refundedAt?: string
  createdAt: string
}

export interface AdminSubscription {
  id: string
  userId: string
  userEmail?: string
  plan: string
  status: 'active' | 'past_due' | 'canceled' | 'suspended' | string
  monthlyCredits: number
  currentPeriodEnd: string
  renewalDate: string
  paymentFailed: boolean
  cancellationState: 'none' | 'pending' | 'canceled' | string
  cancelAt?: string | null
  createdAt: string
}

export interface CreditEntry {
  id: string
  userId: string
  userEmail?: string
  transactionType: 'purchase' | 'generation_spend' | 'support_adjustment' | 'refund_reversal' | 'monthly_grant' | string
  delta: number
  previousBalance: number
  updatedBalance: number
  relatedReference?: string // e.g. job_a9182 or pay_10291
  adminActor?: string
  reason: string
  createdAt: string
}

export interface AuditEntry{
  id:string
  actor:string
  role?:string
  action:string
  targetType?:string
  targetId:string
  reason?:string
  result:string
  correlationId?:string
  createdAt:string
  before?:Record<string,unknown>
  after?:Record<string,unknown>
}

export interface SuperAdminMe{oid:string;email:string;name:string;roles:string[]}
export interface SuperAdminPayment{transaction_id:string;user_id:string;email:string;stripe_customer_id:string|null;credits_granted:number;type:string;created_at:string}
export interface SuperAdminPaymentsResponse{payments:SuperAdminPayment[];total:number;limit:number;offset:number}
export interface SuperAdminSubscription{user_id:string;email:string;subscription_type:string;subscription_plan:string;monthly_credits_remaining:number;start:string|null;end:string|null;cancel_at:string|null;payment_failed:boolean}
export interface SuperAdminSubscriptionsResponse{subscriptions:SuperAdminSubscription[];total:number;limit:number;offset:number}
export interface SuperAdminCreditEntry{transaction_id:string;user_id:string;email:string;amount:number;type:string;job_id:string|null;created_at:string}
export interface SuperAdminCreditsResponse{entries:SuperAdminCreditEntry[];total:number;limit:number;offset:number}
export interface SuperAdminAuditEvent{event_id:string;actor_email:string;action:string;target_type:string;target_id:string;previous_value:unknown;new_value:unknown;reason:string;result:string;created_at:string}
export interface SuperAdminAuditResponse{events:SuperAdminAuditEvent[];total:number;limit:number;offset:number}
export interface SuperAdminSystemHealth{sql:string|Record<string,unknown>;blob:string|Record<string,unknown>;gpu_active_executions:number;queue_depth:number;failed_jobs_24h:number}
export interface SuperAdminDashboardSummary{
  users:{total:number;new_30d:number;active_30d:number;paying:number}
  jobs:{total:number;today:number;completed_today:number;failed_today:number;queue_depth:number;avg_processing_seconds:number|null}
  billing:{credits_purchased_30d:number;note?:string}
  organizations:{total:number}
  support:{open:number;note?:string}
}

export interface SuperAdminUser{
  user_id:string
  email:string
  full_name:string
  plan_name:string
  subscription_type:string
  subscription_status?:string
  lora_status:string
  credits:number
  one_time_credits:number
  monthly_credits:number
  created_at:string
  last_login_at?:string
  status?:'active'|'suspended'|'pending_deletion'|string
  subscription_plan?:string
  retrain_count?:number
  credits_remaining?:number
  subscription_start?:string|null
  subscription_end?:string|null
  stripe_customer_id?:string|null
  terms_accepted_at?:string|null
  recent_jobs?:SuperAdminJob[]
  credit_ledger?:CreditEntry[]
  payments?:AdminPayment[]
  audit_history?:AuditEntry[]
  account_status?:string
  suspended?:boolean
  suspended_at?:string|null
}
export interface SuperAdminUsersResponse{users:SuperAdminUser[];total:number;limit:number;offset:number}
export interface SuperAdminJob{job_id:string;user_id:string;status:string;created_at:string;job_type?:string;dispatched_at?:string|null;image_count?:number;processing_seconds?:number|null;params?:Record<string,unknown>;credits_consumed?:number;execution_id?:string|null;completed_at?:string|null}
export interface SuperAdminJobsResponse{jobs:SuperAdminJob[];total:number;limit:number;offset:number}
