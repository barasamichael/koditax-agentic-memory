export type FormStatus = 'draft' | 'pending_verification' | 'ready' | 'blocked' | 'submitted' | 'processing'

export interface TaxForm {
  form_id: string
  form_type: string
  tax_year: number
  status: FormStatus
  version: string
  created_at: string
}
