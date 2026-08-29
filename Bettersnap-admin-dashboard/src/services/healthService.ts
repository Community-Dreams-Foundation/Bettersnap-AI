import { api } from '../lib/api/client';import type{HealthResponse}from'./contracts';export const healthService={getHealth:()=>api.getDetailed<HealthResponse>('/health',false)}
