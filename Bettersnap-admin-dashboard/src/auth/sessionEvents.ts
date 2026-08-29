type Listener=()=>void
const listeners=new Set<Listener>()
export function notifySessionExpired(){listeners.forEach(listener=>listener())}
export function onSessionExpired(listener:Listener){listeners.add(listener);return()=>{listeners.delete(listener)}}
