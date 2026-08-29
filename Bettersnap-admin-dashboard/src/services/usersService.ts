import { api, queryString } from '../lib/api/client'
import type { SuperAdminUser, SuperAdminUsersResponse } from './contracts'

export interface UsersQuery { q?:string;limit?:number;offset?:number }
export interface UserCreditAdjustmentPayload { amount:number;reason:string }
export interface SuspendPayload { reason:string }
export interface AccountStateResponse { user_id:string;suspended:boolean }
export interface CreditAdjustmentResponse { user_id:string;requested:number;applied:number;one_time_credits_remaining:number }

export const usersService={
  list:(query:UsersQuery={})=>api.getDetailed<SuperAdminUsersResponse>(`/superadmin/users${queryString(query)}`),
  get:(id:string)=>api.getDetailed<SuperAdminUser>(`/superadmin/users/${encodeURIComponent(id)}`),
  suspend:(userId:string,payload:SuspendPayload)=>api.postDetailed<AccountStateResponse>(`/superadmin/users/${encodeURIComponent(userId)}/suspend`,payload),
  reactivate:(userId:string,payload:SuspendPayload)=>api.postDetailed<AccountStateResponse>(`/superadmin/users/${encodeURIComponent(userId)}/reactivate`,payload),
  adjustCredits:(userId:string,payload:UserCreditAdjustmentPayload)=>api.postDetailed<CreditAdjustmentResponse>(`/superadmin/users/${encodeURIComponent(userId)}/credits/adjust`,payload),
}
