import { describe, expect, it } from 'vitest'
import type { CatalogResponse } from '../../services'
import { catalogSummary } from './catalogSummary'
import { healthBadgeStatus, isHealthDegraded } from './healthStatus'

describe('system health badges', () => {
  it('maps OK dependencies to healthy and other values to failed', () => {
    expect(healthBadgeStatus('OK')).toBe('healthy')
    expect(healthBadgeStatus('unavailable')).toBe('failed')
    expect(isHealthDegraded({ status: 'OK', face_gate: 'failed' })).toBe(true)
  })
})

describe('catalog rendering summary', () => {
  it('counts categories, attires, and backgrounds', () => {
    const catalog: CatalogResponse = { categories: [{ id: 'professional', name: 'Professional', type: 'professional', attires: [{ id: 'suit', name: 'Suit' }], backgrounds: [{ id: 'gray', name: 'Gray' }, { id: 'office', name: 'Office' }] }] }
    expect(catalogSummary(catalog)).toEqual({ categories: 1, attires: 1, backgrounds: 2 })
  })
})
