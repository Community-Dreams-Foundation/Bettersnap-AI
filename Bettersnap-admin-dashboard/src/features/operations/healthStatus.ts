import type { HealthResponse } from '../../services'

export function healthBadgeStatus(value: string) {
  return value.toLowerCase() === 'ok' ? 'healthy' : 'failed'
}

export function isHealthDegraded(health: HealthResponse) {
  return healthBadgeStatus(health.status) !== 'healthy' || healthBadgeStatus(health.face_gate) !== 'healthy'
}
