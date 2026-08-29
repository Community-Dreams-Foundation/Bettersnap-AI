import type { ApiResult } from '../lib/api/client'
import { adminService } from './adminService'
import { auditService } from './auditService'
import { billingService } from './billingService'
import type { HealthResponse, SuperAdminAuditEvent, SuperAdminCreditEntry, SuperAdminJob, SuperAdminPayment, SuperAdminSubscription, SuperAdminSystemHealth, SuperAdminUser } from './contracts'
import { creditsService } from './creditsService'
import { healthService } from './healthService'
import { jobsService } from './jobsService'
import { usersService } from './usersService'

const pageSize=200
interface PagePayload<T>{items:T[];total:number}

async function collectAll<T>(load:(limit:number,offset:number)=>Promise<PagePayload<T>>):Promise<T[]> {
  const first=await load(pageSize,0)
  const offsets:number[]=[]
  for(let offset=pageSize;offset<first.total;offset+=pageSize)offsets.push(offset)
  const remaining=await Promise.all(offsets.map(offset=>load(pageSize,offset)))
  return[first,...remaining].flatMap(page=>page.items)
}

export interface DashboardAggregate {
  health:HealthResponse
  systemHealth:SuperAdminSystemHealth
  users:{total:number;new30d:number;paying:number;suspended:number}
  jobs:{total:number;today:number;completedToday:number;failedToday:number;averageProcessingSeconds:number|null;totalImagesGenerated:number|null}
  billing:{activeSubscriptions:number;creditsPurchased:number;creditsUsed:number}
  recentActivity:SuperAdminAuditEvent[]
  updatedAt:Date
}

function data<T>(result:ApiResult<T>):T{return result.data}

export const dashboardService={
  async get():Promise<DashboardAggregate>{
    const[health,systemHealth,users,jobs,payments,subscriptions,credits,audit]=await Promise.all([
      healthService.getHealth().then(data),
      adminService.getSystemHealth().then(data),
      collectAll<SuperAdminUser>(async(limit,offset)=>{const page=data(await usersService.list({limit,offset}));return{items:page.users,total:page.total}}),
      collectAll<SuperAdminJob>(async(limit,offset)=>{const page=data(await jobsService.list({limit,offset}));return{items:page.jobs,total:page.total}}),
      collectAll<SuperAdminPayment>(async(limit,offset)=>{const page=data(await billingService.listPayments({limit,offset}));return{items:page.payments,total:page.total}}),
      collectAll<SuperAdminSubscription>(async(limit,offset)=>{const page=data(await billingService.listSubscriptions({limit,offset}));return{items:page.subscriptions,total:page.total}}),
      collectAll<SuperAdminCreditEntry>(async(limit,offset)=>{const page=data(await creditsService.list({limit,offset}));return{items:page.entries,total:page.total}}),
      auditService.list({limit:5,offset:0}).then(result=>result.data.events),
    ])

    const cutoff30d=Date.now()-30*24*60*60*1000
    const today=new Date().toISOString().slice(0,10)
    const jobsToday=jobs.filter(job=>job.created_at.slice(0,10)===today)
    const completedJobs=jobs.filter(job=>job.status==='completed')
    const durations=completedJobs.map(job=>job.processing_seconds).filter((value):value is number=>typeof value==='number')
    const allCompletedImageCountsAvailable=completedJobs.every(job=>typeof job.image_count==='number')
    const payingUserIds=new Set([...payments.map(item=>item.user_id),...subscriptions.map(item=>item.user_id)])

    return{
      health,
      systemHealth,
      users:{
        total:users.length,
        new30d:users.filter(user=>new Date(user.created_at).getTime()>=cutoff30d).length,
        paying:payingUserIds.size,
        suspended:users.filter(user=>user.suspended||user.account_status==='suspended').length,
      },
      jobs:{
        total:jobs.length,
        today:jobsToday.length,
        completedToday:jobsToday.filter(job=>job.status==='completed').length,
        failedToday:jobsToday.filter(job=>job.status==='failed').length,
        averageProcessingSeconds:durations.length?Math.round(durations.reduce((sum,value)=>sum+value,0)/durations.length):null,
        totalImagesGenerated:allCompletedImageCountsAvailable?completedJobs.reduce((sum,job)=>sum+(job.image_count as number),0):null,
      },
      billing:{
        activeSubscriptions:subscriptions.length,
        creditsPurchased:credits.filter(entry=>entry.amount>0&&entry.type.toLowerCase().includes('purchase')).reduce((sum,entry)=>sum+entry.amount,0),
        creditsUsed:credits.filter(entry=>entry.amount<0).reduce((sum,entry)=>sum+Math.abs(entry.amount),0),
      },
      recentActivity:audit,
      updatedAt:new Date(),
    }
  },
}
