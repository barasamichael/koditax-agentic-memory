import { internalOnlyServiceClient } from './client'
import { generateIdempotencyKey, generateUniqueIdempotencyKey } from '@/lib/idempotency'
import { DEFAULT_TENANT_ID } from '@/lib/constants'
import { useAuthStore } from '@/stores/authStore'
import type { Computation } from '@/types/computation'

// Quarantined internal adapter:
// tax_core is an internal deterministic engine and not an approved standard
// end-user frontend boundary.

// ─── Auth context header helper ───────────────────────────────────────────────

const withAuthContext = (): Record<string, string> => {
  const accessToken = useAuthStore.getState().accessToken
  return accessToken ? { 'X-Auth-Context': accessToken } : {}
}

// ─── Create computation ───────────────────────────────────────────────────────

export interface CreateComputationRequest {
  tax_year: number
  document_ids?: string[]
}

export interface CreateComputationResponse {
  status: string
  computation: Computation
}

export const createComputation = async (
  taxYear: number,
  documentIds?: string[]
): Promise<Computation> => {
  const userId = useAuthStore.getState().userId ?? ''
  const idempotencyKey = generateUniqueIdempotencyKey('create-computation')
  const res = await internalOnlyServiceClient.post<CreateComputationResponse>(
    '/computations',
    {
      tenant_id: DEFAULT_TENANT_ID,
      owner_user_id: userId,
      tax_year: taxYear,
      ...(documentIds?.length ? { document_ids: documentIds } : {}),
    },
    {
      headers: {
        ...withAuthContext(),
        'Idempotency-Key': idempotencyKey,
      },
    }
  )
  return res.data.computation
}

// ─── Get computation ──────────────────────────────────────────────────────────

export const getComputation = async (computationId: string): Promise<Computation> => {
  const res = await internalOnlyServiceClient.get<{ status: string; computation: Computation }>(
    `/computations/${computationId}`,
    { headers: withAuthContext() }
  )
  return res.data.computation
}

// ─── List computations ────────────────────────────────────────────────────────

export const listComputations = async (): Promise<Computation[]> => {
  const res = await internalOnlyServiceClient.get<{ status: string; computations: Computation[] }>(
    '/computations',
    {
      params: { tenant_id: DEFAULT_TENANT_ID },
      headers: withAuthContext(),
    }
  )
  return res.data.computations
}

// ─── Submit computation ───────────────────────────────────────────────────────

export interface SubmitComputationResponse {
  status: string
  computation: Computation
}

export const submitComputation = async (computationId: string): Promise<Computation> => {
  const userId = useAuthStore.getState().userId ?? ''
  const idempotencyKey = generateIdempotencyKey('submit-computation', computationId)
  const res = await internalOnlyServiceClient.post<SubmitComputationResponse>(
    `/computations/${computationId}/submit`,
    { submitted_by: userId },
    {
      headers: {
        ...withAuthContext(),
        'Idempotency-Key': idempotencyKey,
      },
    }
  )
  return res.data.computation
}

// ─── Error messages ───────────────────────────────────────────────────────────

export const TAX_CORE_ERROR_MESSAGES: Record<string, string> = {
  computation_not_found: 'The computation could not be found.',
  computation_already_submitted: 'This computation has already been submitted.',
  computation_blocked: 'This computation is blocked and cannot be submitted.',
  insufficient_documents: 'Please attach all required documents before submitting.',
  tax_year_invalid: 'The specified tax year is not supported.',
  tenant_not_authorized: 'Your account is not authorized for this operation.',
}
