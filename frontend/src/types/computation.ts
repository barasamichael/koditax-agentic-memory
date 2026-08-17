export type ComputationStatus = 'draft' | 'pending_verification' | 'ready' | 'blocked' | 'submitted' | 'processing'

export interface Computation {
  computation_id: string
  tax_year: number
  status: ComputationStatus
  created_at: string
  updated_at: string
}
