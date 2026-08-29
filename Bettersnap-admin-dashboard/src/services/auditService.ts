import { api, queryString } from '../lib/api/client'
import type { SuperAdminAuditResponse } from './contracts'
export interface AuditQuery{target_type?:string;target_id?:string;action?:string;limit?:number;offset?:number}
export const auditService={list:(query:AuditQuery={})=>api.getDetailed<SuperAdminAuditResponse>(`/superadmin/audit-logs${queryString(query)}`)}
