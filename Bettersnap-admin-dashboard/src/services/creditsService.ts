import { api, queryString } from '../lib/api/client'
import type { SuperAdminCreditsResponse } from './contracts'
export interface CreditsQuery{user_id?:string;type?:string;limit?:number;offset?:number}
export const creditsService={list:(query:CreditsQuery={})=>api.getDetailed<SuperAdminCreditsResponse>(`/superadmin/credits${queryString(query)}`)}
