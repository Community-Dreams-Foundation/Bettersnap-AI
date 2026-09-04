export const SUPER_ADMIN_EMAILS=new Set(['admin@bettersnap.ai'])

export function isSuperAdminEmail(email:string){
  return SUPER_ADMIN_EMAILS.has(email.trim().toLowerCase())
}
