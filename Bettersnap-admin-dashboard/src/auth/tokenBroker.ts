export type AccessTokenResolver=()=>Promise<string|null>
let resolver:AccessTokenResolver=async()=>null
export function setAccessTokenResolver(next:AccessTokenResolver){resolver=next}
export function getAuthAccessToken(){return resolver()}
