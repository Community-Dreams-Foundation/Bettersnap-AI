import { api } from '../lib/api/client';import type{CatalogResponse}from'./contracts';export const catalogService={getCatalog:()=>api.getDetailed<CatalogResponse>('/catalog',false)}
