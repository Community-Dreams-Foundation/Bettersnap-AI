import { api } from '../lib/api/client'
import type{SuperAdminDashboardSummary,SuperAdminMe,SuperAdminSystemHealth}from'./contracts'
export const adminService={getMe:()=>api.getDetailed<SuperAdminMe>('/superadmin/me'),getSystemHealth:()=>api.getDetailed<SuperAdminSystemHealth>('/superadmin/system-health'),getDashboardSummary:()=>api.getDetailed<SuperAdminDashboardSummary>('/superadmin/dashboard/summary')}
