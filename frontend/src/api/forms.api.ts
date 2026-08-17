import { internalOnlyServiceClient } from './client'
import { generateUniqueIdempotencyKey } from '@/lib/idempotency'
import { DEFAULT_TENANT_ID } from '@/lib/constants'
import { useAuthStore } from '@/stores/authStore'
import type { TaxForm } from '@/types/forms'

// Quarantined internal adapter:
// forms is not an approved normal end-user browser integration surface.
// Keep this module isolated until a later, explicitly approved admin/internal
// or orchestrated frontend path requires it.

// ─── Supported form types ─────────────────────────────────────────────────────
// IT1 (Individual income tax) is the only enabled type for pilot.
// IT2 and P10 are disabled — backend gates them, never offer them in UI.

export type SupportedFormType = 'IT1'

// ─── Auth context header helper ───────────────────────────────────────────────

const withAuthContext = (): Record<string, string> => {
  const accessToken = useAuthStore.getState().accessToken
  return accessToken ? { 'X-Auth-Context': accessToken } : {}
}

// ─── Step 1: Map ──────────────────────────────────────────────────────────────

interface MapRequest {
  tenant_id: string
  computation_id: string
  form_type: string
  tax_year: number
}

interface MapResponse {
  status: string
  mapping_id: string
  form_type: string
  tax_year: number
  mapped_fields: Record<string, unknown>
}

const mapForm = async (
  computationId: string,
  formType: SupportedFormType,
  taxYear: number
): Promise<MapResponse> => {
  const body: MapRequest = {
    tenant_id: DEFAULT_TENANT_ID,
    computation_id: computationId,
    form_type: formType,
    tax_year: taxYear,
  }
  const res = await internalOnlyServiceClient.post<MapResponse>(
    '/v1/forms/income-tax/map',
    body,
    {
      headers: {
        ...withAuthContext(),
        'Idempotency-Key': generateUniqueIdempotencyKey(`form-map-${computationId}-${formType}`),
      },
    }
  )
  return res.data
}

// ─── Step 2: Bind ─────────────────────────────────────────────────────────────

interface BindRequest {
  tenant_id: string
  mapping_id: string
  form_type: string
  tax_year: number
  field_values: Record<string, unknown>
}

interface BindResponse {
  status: string
  binding_id: string
  form_type: string
  tax_year: number
  bound_fields: Record<string, unknown>
}

const bindForm = async (
  mappingId: string,
  formType: SupportedFormType,
  taxYear: number,
  fieldValues: Record<string, unknown>
): Promise<BindResponse> => {
  const body: BindRequest = {
    tenant_id: DEFAULT_TENANT_ID,
    mapping_id: mappingId,
    form_type: formType,
    tax_year: taxYear,
    field_values: fieldValues,
  }
  const res = await internalOnlyServiceClient.post<BindResponse>(
    '/v1/forms/income-tax/bind',
    body,
    {
      headers: {
        ...withAuthContext(),
        'Idempotency-Key': generateUniqueIdempotencyKey(`form-bind-${mappingId}`),
      },
    }
  )
  return res.data
}

// ─── Step 3: Validate ─────────────────────────────────────────────────────────

interface ValidateRequest {
  tenant_id: string
  binding_id: string
  form_type: string
  tax_year: number
}

interface ValidateResponse {
  status: string
  validation_id: string
  is_valid: boolean
  errors: Array<{ field: string; message: string; code: string }>
  warnings: Array<{ field: string; message: string; code: string }>
}

const validateForm = async (
  bindingId: string,
  formType: SupportedFormType,
  taxYear: number
): Promise<ValidateResponse> => {
  const body: ValidateRequest = {
    tenant_id: DEFAULT_TENANT_ID,
    binding_id: bindingId,
    form_type: formType,
    tax_year: taxYear,
  }
  const res = await internalOnlyServiceClient.post<ValidateResponse>(
    '/v1/forms/income-tax/validate',
    body,
    {
      headers: {
        ...withAuthContext(),
        'Idempotency-Key': generateUniqueIdempotencyKey(`form-validate-${bindingId}`),
      },
    }
  )
  return res.data
}

// ─── Step 4: Generate ─────────────────────────────────────────────────────────

interface GenerateRequest {
  tenant_id: string
  validation_id: string
  form_type: string
  tax_year: number
}

interface GenerateResponse {
  status: string
  form: TaxForm
  download_url?: string
}

const generateFormStep = async (
  validationId: string,
  formType: SupportedFormType,
  taxYear: number
): Promise<GenerateResponse> => {
  const body: GenerateRequest = {
    tenant_id: DEFAULT_TENANT_ID,
    validation_id: validationId,
    form_type: formType,
    tax_year: taxYear,
  }
  const res = await internalOnlyServiceClient.post<GenerateResponse>(
    '/v1/forms/income-tax/generate',
    body,
    {
      headers: {
        ...withAuthContext(),
        'Idempotency-Key': generateUniqueIdempotencyKey(`form-generate-${validationId}`),
      },
    }
  )
  return res.data
}

// ─── generateForm: 4-step sequential pipeline ────────────────────────────────

export interface GenerateFormResult {
  form: TaxForm
  download_url?: string
  validation_errors: Array<{ field: string; message: string; code: string }>
  validation_warnings: Array<{ field: string; message: string; code: string }>
}

export const generateForm = async (params: {
  computationId: string
  formType: SupportedFormType
  taxYear: number
  fieldValues?: Record<string, unknown>
}): Promise<GenerateFormResult> => {
  const { computationId, formType, taxYear, fieldValues = {} } = params

  // Step 1: Map
  const mapRes = await mapForm(computationId, formType, taxYear)

  // Step 2: Bind — merge mapped fields with any user-supplied overrides
  const bindRes = await bindForm(
    mapRes.mapping_id,
    formType,
    taxYear,
    { ...mapRes.mapped_fields, ...fieldValues }
  )

  // Step 3: Validate
  const validateRes = await validateForm(bindRes.binding_id, formType, taxYear)

  if (!validateRes.is_valid) {
    throw new FormValidationError(validateRes.errors, validateRes.warnings)
  }

  // Step 4: Generate
  const generateRes = await generateFormStep(validateRes.validation_id, formType, taxYear)

  return {
    form: generateRes.form,
    download_url: generateRes.download_url,
    validation_errors: validateRes.errors,
    validation_warnings: validateRes.warnings,
  }
}

// ─── FormValidationError ──────────────────────────────────────────────────────

export class FormValidationError extends Error {
  constructor(
    public readonly errors: Array<{ field: string; message: string; code: string }>,
    public readonly warnings: Array<{ field: string; message: string; code: string }>
  ) {
    super('Form validation failed')
    this.name = 'FormValidationError'
  }
}

// ─── Error messages ───────────────────────────────────────────────────────────

export const FORMS_ERROR_MESSAGES: Record<string, string> = {
  form_type_not_supported: 'This form type is not available in the current pilot.',
  computation_not_ready: 'The computation is not ready for form generation. Please verify your documents.',
  mapping_failed: 'Could not map your data to the form. Please check your computation.',
  binding_failed: 'Could not bind field values to the form.',
  validation_failed: 'The form has validation errors that must be resolved.',
  generation_failed: 'Form generation failed. Please try again.',
}
