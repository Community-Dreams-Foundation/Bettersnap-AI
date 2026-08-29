import { describe, expect, it } from 'vitest'
import { renderToString } from 'react-dom/server'
import { DataSourceBadge } from './DashboardPage'

describe('Dashboard DataSourceBadge and Statuses', () => {
  it('renders Live backend data source badge', () => {
    const html = renderToString(<DataSourceBadge type="live" />)
    expect(html).toContain('data-source-badge live')
    expect(html).toContain('Live backend')
  })

  it('renders Backend API required badge', () => {
    const html = renderToString(<DataSourceBadge type="required" />)
    expect(html).toContain('data-source-badge required')
    expect(html).toContain('Backend API required')
  })
})
