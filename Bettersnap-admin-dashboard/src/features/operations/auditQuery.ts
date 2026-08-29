// Filter + pagination state for the audit-log page.
//
// This used to live in the component as four useState hooks plus
//   useEffect(()=>setPage(1),[targetType,targetId,action])
// which react-hooks/set-state-in-effect (new in eslint-plugin-react-hooks 7)
// correctly flags: setting state synchronously in an effect body renders twice
// and, on a filter change, briefly issues a request for the OLD page.
//
// Resetting the page belongs to the event that changes the filter, not to a
// reaction after the fact. Keeping it in a pure reducer also makes the rule
// testable without a DOM, matching how catalogSummary/healthStatus are tested.

export type AuditFilterField = 'targetType' | 'targetId' | 'action'

export interface AuditQueryState {
  targetType: string
  targetId: string
  action: string
  page: number
}

export type AuditQueryAction =
  | { type: 'setFilter'; field: AuditFilterField; value: string }
  | { type: 'setPage'; page: number }

export const initialAuditQuery: AuditQueryState = {
  targetType: '', targetId: '', action: '', page: 1,
}

export function auditQueryReducer(state: AuditQueryState, action: AuditQueryAction): AuditQueryState {
  switch (action.type) {
    case 'setFilter': {
      // A no-op edit must NOT reset the page: the old effect only fired when a
      // dependency actually changed, and re-typing the same value left the
      // reader where they were. Same identity out means React skips the render.
      if (state[action.field] === action.value) return state
      return { ...state, [action.field]: action.value, page: 1 }
    }
    case 'setPage': {
      if (state.page === action.page) return state
      // Page is 1-based; the table's offset is (page - 1) * limit, so a page
      // below 1 would compute a negative offset.
      return { ...state, page: Math.max(1, Math.trunc(action.page)) }
    }
    default:
      return state
  }
}

/** The cache key the resource hook re-fetches on. */
export function auditQueryKey(s: AuditQueryState): string {
  return `${s.targetType}|${s.targetId}|${s.action}|${s.page}`
}
