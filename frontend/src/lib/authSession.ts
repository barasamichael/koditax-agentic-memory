export const MAX_LOGIN_SESSION_MS = 7 * 24 * 60 * 60 * 1000

export const clampLoginSessionExpiresAt = (
  expiresAt: string,
  nowMs: number = Date.now()
): string => {
  const expiresAtMs = Date.parse(expiresAt)
  if (Number.isNaN(expiresAtMs)) return expiresAt
  return new Date(Math.min(expiresAtMs, nowMs + MAX_LOGIN_SESSION_MS)).toISOString()
}

