import { describe, expect, it } from 'vitest'
import { MAX_LOGIN_SESSION_MS, clampLoginSessionExpiresAt } from '@/lib/authSession'

describe('clampLoginSessionExpiresAt', () => {
  it('caps expiry to seven days from the reference time', () => {
    const referenceTimeMs = Date.UTC(2026, 6, 31, 12, 0, 0)
    const futureExpiry = new Date(referenceTimeMs + MAX_LOGIN_SESSION_MS * 2).toISOString()

    expect(clampLoginSessionExpiresAt(futureExpiry, referenceTimeMs)).toBe(
      new Date(referenceTimeMs + MAX_LOGIN_SESSION_MS).toISOString()
    )
  })

  it('keeps an earlier expiry unchanged', () => {
    const referenceTimeMs = Date.UTC(2026, 6, 31, 12, 0, 0)
    const earlierExpiry = new Date(referenceTimeMs + 2 * 60 * 60 * 1000).toISOString()

    expect(clampLoginSessionExpiresAt(earlierExpiry, referenceTimeMs)).toBe(earlierExpiry)
  })
})
