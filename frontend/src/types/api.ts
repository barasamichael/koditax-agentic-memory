export interface CanonicalError {
  error_code: string
  message: string
  reason: string
  reason_code?: string
  context?: Record<string, unknown>
}

export interface ApiResponse<T> {
  data: T
  status: number
}

export interface PaginatedResponse<T> {
  items: T[]
  total: number
  page: number
  page_size: number
  has_next: boolean
}

export type CorrelationID = string
