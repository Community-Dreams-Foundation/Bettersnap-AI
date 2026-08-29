import { api, queryString } from '../lib/api/client'
import type { SuperAdminPaymentsResponse, SuperAdminSubscriptionsResponse } from './contracts'
export interface FinanceQuery{limit?:number;offset?:number}
export const billingService={listPayments:(query:FinanceQuery={})=>api.getDetailed<SuperAdminPaymentsResponse>(`/superadmin/payments${queryString(query)}`),listSubscriptions:(query:FinanceQuery={})=>api.getDetailed<SuperAdminSubscriptionsResponse>(`/superadmin/subscriptions${queryString(query)}`)}
