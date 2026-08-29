import { describe, expect, it } from 'vitest'
import { auditQueryKey, auditQueryReducer, initialAuditQuery, type AuditQueryState } from './auditQuery'

const at = (page: number, over: Partial<AuditQueryState> = {}): AuditQueryState =>
  ({ ...initialAuditQuery, page, ...over })

const setFilter = (s: AuditQueryState, field: 'targetType'|'targetId'|'action', value: string) =>
  auditQueryReducer(s, { type: 'setFilter', field, value })
const setPage = (s: AuditQueryState, page: number) =>
  auditQueryReducer(s, { type: 'setPage', page })

describe('audit filters reset pagination', () => {
  // The behaviour the removed `useEffect(()=>setPage(1),[...])` provided. Losing
  // it would leave a reader on page 7 of a result set that now has one page, so
  // the table would come back empty for no visible reason.
  it.each(['targetType', 'targetId', 'action'] as const)(
    'changing %s sends the reader back to page 1', field => {
      expect(setFilter(at(7), field, 'x').page).toBe(1)
    })

  it('resets from any page, not just the second one', () => {
    for (const page of [2, 5, 99]) {
      expect(setFilter(at(page), 'action', 'credits.adjust').page).toBe(1)
    }
  })

  it('keeps the other filters while resetting the page', () => {
    const state = setFilter(setFilter(at(4), 'targetType', 'user'), 'action', 'suspend')
    expect(state).toEqual({ targetType: 'user', targetId: '', action: 'suspend', page: 1 })
  })

  it('does NOT reset the page when a filter is set to the value it already has', () => {
    // The old effect fired on dependency CHANGE. Re-emitting the same value must
    // not yank the reader back to page 1.
    const state = at(6, { action: 'suspend' })
    expect(setFilter(state, 'action', 'suspend')).toBe(state)
  })

  it('treats clearing a filter as a change and resets', () => {
    expect(setFilter(at(3, { targetId: 'abc' }), 'targetId', '').page).toBe(1)
  })
})

describe('ordinary pagination still works', () => {
  it('moves to the requested page and leaves filters alone', () => {
    const filtered = setFilter(initialAuditQuery, 'targetType', 'job')
    const paged = setPage(filtered, 3)
    expect(paged).toEqual({ targetType: 'job', targetId: '', action: '', page: 3 })
  })

  it('walks forward and back across several pages', () => {
    let s = initialAuditQuery
    for (const p of [2, 3, 4, 3, 2, 1]) s = setPage(s, p)
    expect(s.page).toBe(1)
  })

  it('returns the same object when the page does not change', () => {
    const state = at(2)
    expect(setPage(state, 2)).toBe(state)
  })

  it('clamps below 1, because offset is (page-1)*limit and would go negative', () => {
    expect(setPage(at(3), 0).page).toBe(1)
    expect(setPage(at(3), -5).page).toBe(1)
  })

  it('paging after filtering does not resurrect the old page', () => {
    const state = setPage(setFilter(at(9), 'action', 'retry'), 2)
    expect(state.page).toBe(2)
  })
})

describe('resource cache key', () => {
  it('changes with every filter and with the page', () => {
    const base = auditQueryKey(initialAuditQuery)
    expect(auditQueryKey(setFilter(initialAuditQuery, 'targetType', 'user'))).not.toBe(base)
    expect(auditQueryKey(setPage(initialAuditQuery, 2))).not.toBe(base)
  })

  it('is stable for equal state', () => {
    expect(auditQueryKey(at(2, { action: 'x' }))).toBe(auditQueryKey(at(2, { action: 'x' })))
  })
})
