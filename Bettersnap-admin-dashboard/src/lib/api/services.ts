import type { CatalogCategory, Health, Plan, Profile, SubscriptionStatus, UserJob } from '../../types'
import { api } from './client'
export const backend = {
  health: () => api.getPublic<Health>('/health'),
  catalog: () => api.getPublic<{categories:CatalogCategory[]}>('/catalog'),
  plans: () => api.getPublic<{plans:Plan[]}>('/plans'),
  profile: () => api.get<Profile>('/profiles/me'),
  credits: () => api.get<{credits_remaining:number}>('/users/credits'),
  jobs: () => api.get<{jobs:UserJob[]}>('/users/jobs'),
  jobStatus: (jobId:string) => api.get<{status:string;output_blob_path:string|string[]|null}>(`/jobs/${encodeURIComponent(jobId)}/status`),
  subscription: () => api.get<SubscriptionStatus>('/subscriptions/status'),
  subscriptionPlans: () => api.getPublic<{one_time:unknown[];monthly:unknown[]}>('/subscriptions/plans'),
}
