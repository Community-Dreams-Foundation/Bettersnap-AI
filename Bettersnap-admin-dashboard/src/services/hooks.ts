import { useApiResource } from '../lib/useApiResource';import type{ApiResult}from'../lib/api/client';export function useServiceQuery<T>(query:()=>Promise<ApiResult<T>>){return useApiResource(query)}
