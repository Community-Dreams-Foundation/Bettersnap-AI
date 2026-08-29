export type RouteAccess='loading'|'login'|'forbidden'|'allowed'
export function resolveRouteAccess(input:{loading:boolean;authenticated:boolean;hasPermission:boolean}):RouteAccess{if(input.loading)return'loading';if(!input.authenticated)return'login';if(!input.hasPermission)return'forbidden';return'allowed'}
