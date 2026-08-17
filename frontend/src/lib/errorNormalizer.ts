import axios from 'axios'
import type { AuthError } from '@/types/auth'

export interface NormalizedError {
  error_code: string
  message: string
  reason: string
  reason_code?: string
  // Auth-specific promoted fields (present when relevant)
  lockout_expires_at?: string
  lockout_remaining_seconds?: number
  current_state?: string
  requested_state?: string
  account_deletion_state?: string
  incident_code?: string
}

// Keep CanonicalError as alias for backward compatibility
export type CanonicalError = NormalizedError

export const normalizeError = (err: unknown): NormalizedError => {
  if (axios.isAxiosError(err) && err.response?.data) {
    const data = err.response.data as Partial<AuthError> & { detail?: unknown }

    // Auth service errors: top-level error_code field (not nested in 'detail')
    if (data.error_code) {
      const reasonCode = data.reason_code ?? data.reason ?? data.error_code
      return {
        error_code: data.error_code,
        message: data.message ?? 'An error occurred.',
        reason: reasonCode ?? data.error_code,
        reason_code: reasonCode,
        lockout_expires_at: data.lockout_expires_at,
        lockout_remaining_seconds: data.lockout_remaining_seconds,
        current_state: data.current_state,
        requested_state: data.requested_state,
        account_deletion_state: data.account_deletion_state,
        incident_code: data.incident_code,
      }
    }

    // Orchestration/other services: nested in 'detail'
    if (data.detail && typeof data.detail === 'object') {
      const detail = data.detail as Record<string, unknown>
      const reasonCode = detail.reason_code ?? detail.reason ?? detail.error_code
      return {
        error_code: String(detail.error_code ?? 'UNKNOWN'),
        message: String(detail.message ?? 'An error occurred.'),
        reason: String(reasonCode ?? detail.error_code ?? 'UNKNOWN'),
        reason_code: reasonCode == null ? undefined : String(reasonCode),
      }
    }

    // FastAPI validation errors: detail is an array
    if (Array.isArray(data.detail)) {
      return {
        error_code: 'validation_error',
        message: 'Request validation failed.',
        reason: 'validation_error',
      }
    }
  }

  return {
    error_code: 'UNKNOWN',
    message: 'Something went wrong.',
    reason: 'UNKNOWN',
    reason_code: 'UNKNOWN',
  }
}
