import type { ApiResult } from '../lib/api/client'
import { adminService } from './adminService'
import { auditService } from './auditService'
import type { HealthResponse, SuperAdminAuditEvent, SuperAdminSystemHealth } from './contracts'
import { healthService } from './healthService'

export interface DashboardAggregate {
  health:HealthResponse
  systemHealth:SuperAdminSystemHealth
  users:{total:number;new30d:number;active30d:number;paying:number;suspended:number|null}
  jobs:{total:number;today:number;completedToday:number;failedToday:number;averageProcessingSeconds:number|null;totalImagesGenerated:number|null}
  billing:{activeSubscriptions:number|null;creditsPurchased30d:number;creditsUsed:number|null}
  recentActivity:SuperAdminAuditEvent[]
  updatedAt:Date
}

function data<T>(result:ApiResult<T>):T{return result.data}

export const dashboardService={
  async get():Promise<DashboardAggregate>{
    const[health,systemHealth,summary,audit]=await Promise.all([
      healthService.getHealth().then(data),
      adminService.getSystemHealth().then(data),
      adminService.getDashboardSummary().then(data),
      auditService.list({limit:5,offset:0}).then(result=>result.data.events),
    ])

    return{
      health,
      systemHealth,
      users:{total:summary.users.total,new30d:summary.users.new_30d,active30d:summary.users.active_30d,paying:summary.users.paying,suspended:null},
      jobs:{total:summary.jobs.total,today:summary.jobs.today,completedToday:summary.jobs.completed_today,failedToday:summary.jobs.failed_today,averageProcessingSeconds:summary.jobs.avg_processing_seconds,totalImagesGenerated:null},
      billing:{activeSubscriptions:null,creditsPurchased30d:summary.billing.credits_purchased_30d,creditsUsed:null},
      recentActivity:audit,
      updatedAt:new Date(),
    }
  },
}
