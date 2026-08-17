import { documentClient } from './client'
import { generateIdempotencyKey, generateUniqueIdempotencyKey } from '@/lib/idempotency'
import { useAuthStore } from '@/stores/authStore'
import type {
  UploadSessionResponse,
  DocumentRecordEnvelope,
  DocumentBindingEnvelope,
  DocumentBindingRole,
  DocumentListEnvelope,
  SignedDownloadCapabilityEnvelope,
  SignedDownloadValidationEnvelope,
  ComplianceOverrideResponseEnvelope,
  ComplianceOverrideAction,
  DocumentPurgeSafetyDryRunEnvelope,
} from '@/types/document'

// Approved conditional public adapter: document_ai is only user-facing when the
// product explicitly includes direct document workflows.

export interface UploadProgressSnapshot {
  loadedBytes: number
  totalBytes: number
  percent: number
}

export interface PutFileToStorageOptions {
  signal?: AbortSignal
  onProgress?: (progress: UploadProgressSnapshot) => void
}

// Returns the current tenant_id. Always 'default_tenant' in the current runtime.
const tenantId = () => useAuthStore.getState().tenantId
const userId = () => useAuthStore.getState().userId!

const MIME_BY_EXTENSION: Record<string, string> = {
  pdf: 'application/pdf', txt: 'text/plain', md: 'text/markdown', csv: 'text/csv',
  tsv: 'text/tab-separated-values', json: 'application/json', xml: 'application/xml',
  rtf: 'application/rtf', docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  odt: 'application/vnd.oasis.opendocument.text', ods: 'application/vnd.oasis.opendocument.spreadsheet',
  odp: 'application/vnd.oasis.opendocument.presentation', png: 'image/png', jpg: 'image/jpeg',
  jpeg: 'image/jpeg', webp: 'image/webp', tif: 'image/tiff', tiff: 'image/tiff',
}

export const documentContentType = (file: File): string => {
  const extension = file.name.split('.').pop()?.toLowerCase() ?? ''
  return MIME_BY_EXTENSION[extension] ?? file.type
}

export const resolveDocumentStorageObjectKey = (
  resolvedTenantId: string,
  documentId: string,
): string => `${resolvedTenantId}/docs/${documentId}`

// Computes a SHA-256 hex digest of a file's bytes.
// Required by the backend for upload integrity verification.
export const computeFileSha256 = async (file: File): Promise<string> => {
  const buffer = await file.arrayBuffer()
  const hashBuffer = await crypto.subtle.digest('SHA-256', buffer)
  return Array.from(new Uint8Array(hashBuffer))
    .map(b => b.toString(16).padStart(2, '0'))
    .join('')
}

// Creates an upload session and returns a presigned PUT URL.
// The client must PUT the file directly to upload_url with the required
// encryption headers before calling completeUpload().
export const createUploadSession = async (params: {
  file_name: string
  content_type: string
  expected_size_bytes: number
  checksum_sha256: string
  lane_hint?: string
}): Promise<UploadSessionResponse> =>
  documentClient.post<UploadSessionResponse>('/v1/documents/upload-sessions', {
    tenant_id: tenantId(),
    owner_user_id: userId(),
    ...params,
  }, {
    headers: {
      'Idempotency-Key': generateUniqueIdempotencyKey(
        'upload-session', userId(), params.checksum_sha256
      ),
    },
  }).then(r => r.data)

// Uploads the file bytes directly to the presigned storage URL.
// Must include the required Kodi encryption headers — the backend
// enforces that AES-256 encryption-at-rest is declared on every upload.
export const putFileToStorage = (
  uploadUrl: string,
  file: File,
  options: PutFileToStorageOptions = {},
): Promise<void> =>
  new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    const handleAbort = () => xhr.abort()

    if (options.signal?.aborted) {
      reject(new DOMException('Upload cancelled.', 'AbortError'))
      return
    }

    xhr.open('PUT', uploadUrl)
    xhr.setRequestHeader('Content-Type', file.type || 'application/octet-stream')
    xhr.setRequestHeader('x-kodi-encryption-at-rest', 'required')
    xhr.setRequestHeader('x-kodi-encryption-algorithm', 'AES256')

    xhr.upload.onprogress = (event) => {
      if (!options.onProgress || !event.lengthComputable) return
      const loadedBytes = event.loaded
      const totalBytes = event.total || file.size
      const percent = totalBytes > 0 ? Math.min(100, Math.round((loadedBytes / totalBytes) * 100)) : 0
      options.onProgress({ loadedBytes, totalBytes, percent })
    }

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        options.onProgress?.({ loadedBytes: file.size, totalBytes: file.size, percent: 100 })
        resolve()
        return
      }
      reject(new Error(`Storage PUT failed: ${xhr.status}`))
    }

    xhr.onerror = () => {
      reject(new Error('Storage PUT failed.'))
    }

    xhr.onabort = () => {
      reject(new DOMException('Upload cancelled.', 'AbortError'))
    }

    if (options.signal) {
      options.signal.addEventListener('abort', handleAbort, { once: true })
    }

    xhr.send(file)
  })

// Registers upload completion after the file has been PUT to storage.
// The backend verifies the object exists in storage and matches the
// declared checksum, size, and content type before creating the document record.
export const completeUpload = (params: {
  document_id: string
  upload_session_id: string
  object_key: string
  checksum_sha256: string
  size_bytes: number
  content_type: string
}): Promise<DocumentRecordEnvelope> =>
  documentClient.post<DocumentRecordEnvelope>(
    `/v1/documents/${params.document_id}/upload-completion`,
    {
      upload_session_id: params.upload_session_id,
      object_key: params.object_key,
      checksum_sha256: params.checksum_sha256,
      size_bytes: params.size_bytes,
      content_type: params.content_type,
    },
    {
      headers: {
        'Idempotency-Key': generateUniqueIdempotencyKey(
          'upload-complete', params.upload_session_id
        ),
      },
    }
  ).then(r => r.data)

export const createDocumentBinding = (params: {
  document_id: string
  binding_role: DocumentBindingRole
  conversation_id?: string | null
  turn_id?: string | null
  workflow_id?: string | null
  document_version_id?: string | null
  attachment_order?: number | null
}): Promise<DocumentBindingEnvelope> =>
  documentClient.post<DocumentBindingEnvelope>('/v1/document-bindings', params).then(
    (response) => response.data
  )

// Lists documents scoped to the current user and tenant.
// All filter parameters are optional.
export const listDocuments = (params?: {
  state?: string
  uploaded_from?: string
  uploaded_to?: string
  computation_id?: string
}): Promise<DocumentListEnvelope> =>
  documentClient.get<DocumentListEnvelope>('/v1/documents', {
    params: { tenant_id: tenantId(), ...params },
  }).then(r => r.data)

// Returns a single document record scoped to the current user and tenant.
export const getDocument = (documentId: string): Promise<DocumentRecordEnvelope> =>
  documentClient.get<DocumentRecordEnvelope>(`/v1/documents/${documentId}`, {
    params: { tenant_id: tenantId() },
  }).then(r => r.data)

export const renameDocument = (
  documentId: string,
  params: {
    display_name: string
    expected_revision: number
  },
): Promise<DocumentRecordEnvelope> =>
  documentClient.patch<DocumentRecordEnvelope>(
    `/v1/documents/${documentId}`,
    params,
    {
      params: { tenant_id: tenantId() },
      headers: {
        'Idempotency-Key': generateIdempotencyKey(
          'document-rename',
          userId(),
          documentId,
          String(params.expected_revision),
          params.display_name,
        ),
      },
    },
  ).then(r => r.data)

// Issues a 15-minute signed download capability token for a document.
// Use the returned capability_token with validateDownloadCapability() before
// serving the download_url to the user.
// Capabilities are single-use — reuse returns 409.
export const issueDownloadCapability = (
  documentId: string
): Promise<SignedDownloadCapabilityEnvelope> =>
  documentClient.post<SignedDownloadCapabilityEnvelope>(
    `/v1/documents/${documentId}/download-capabilities`,
    null,
    { params: { tenant_id: tenantId() } }
  ).then(r => r.data)

// Validates a signed download capability token before use.
// Returns 409 download_capability_rejected on invalid signature,
// expired token, or scope mismatch.
export const validateDownloadCapability = (
  documentId: string,
  capabilityToken: string,
): Promise<SignedDownloadValidationEnvelope> =>
  documentClient.post<SignedDownloadValidationEnvelope>(
    `/v1/documents/${documentId}/download-capabilities/validate`,
    { capability_token: capabilityToken },
    { params: { tenant_id: tenantId() } }
  ).then(r => r.data)

// Moves a document to the trashed state.
// Requires a compliance_override_id if the document has an active compliance lock.
export const trashDocument = (
  documentId: string,
  complianceOverrideId?: string,
): Promise<DocumentRecordEnvelope> =>
  documentClient.post<DocumentRecordEnvelope>(
    `/v1/documents/${documentId}/trash`,
    null,
    {
      params: {
        tenant_id: tenantId(),
        ...(complianceOverrideId && { compliance_override_id: complianceOverrideId }),
      },
    }
  ).then(r => r.data)

// Restores a trashed document back to processing state.
export const restoreDocument = (
  documentId: string,
  complianceOverrideId?: string,
): Promise<DocumentRecordEnvelope> =>
  documentClient.post<DocumentRecordEnvelope>(
    `/v1/documents/${documentId}/restore`,
    null,
    {
      params: {
        tenant_id: tenantId(),
        ...(complianceOverrideId && { compliance_override_id: complianceOverrideId }),
      },
    }
  ).then(r => r.data)

// Marks a document as eligible for purge from the given timestamp.
// purge_eligible_at must be in the past or present and >= uploaded_at.
export const markEligibleForPurge = (
  documentId: string,
  purgeEligibleAt: string,
  complianceOverrideId?: string,
): Promise<DocumentRecordEnvelope> =>
  documentClient.post<DocumentRecordEnvelope>(
    `/v1/documents/${documentId}/purge-eligibility`,
    { purge_eligible_at: purgeEligibleAt },
    {
      params: {
        tenant_id: tenantId(),
        ...(complianceOverrideId && { compliance_override_id: complianceOverrideId }),
      },
    }
  ).then(r => r.data)

// Executes a permanent purge for a document in eligible_for_purge state.
// Run purge-dry-run first to confirm there are no blockers.
// This action is irreversible.
export const purgeDocument = (
  documentId: string,
  purgedAt?: string,
  complianceOverrideId?: string,
): Promise<DocumentRecordEnvelope> =>
  documentClient.post<DocumentRecordEnvelope>(
    `/v1/documents/${documentId}/purge`,
    { purged_at: purgedAt ?? null },
    {
      params: {
        tenant_id: tenantId(),
        ...(complianceOverrideId && { compliance_override_id: complianceOverrideId }),
      },
    }
  ).then(r => r.data)

// Evaluates purge readiness without executing the purge.
// Returns a list of named blockers if the document is not yet purgeable.
// Always run this before purgeDocument() to surface compliance locks and timing issues.
export const purgeDryRun = (
  documentId: string
): Promise<DocumentPurgeSafetyDryRunEnvelope> =>
  documentClient.post<DocumentPurgeSafetyDryRunEnvelope>(
    `/v1/documents/${documentId}/purge-dry-run`,
    null,
    { params: { tenant_id: tenantId() } }
  ).then(r => r.data)

// Requests a compliance override for a locked document action.
// Only IndividualTaxpayer role may request. A ComplianceOfficer must
// approve before the action can proceed. Override TTL is 15 minutes.
export const requestComplianceOverride = (
  documentId: string,
  params: {
    requested_action: ComplianceOverrideAction
    justification: string
  }
): Promise<ComplianceOverrideResponseEnvelope> =>
  documentClient.post<ComplianceOverrideResponseEnvelope>(
    `/v1/documents/${documentId}/compliance-overrides`,
    params,
    { params: { tenant_id: tenantId() } }
  ).then(r => r.data)

// Approves a pending compliance override. Requires ComplianceOfficer role.
// The approver must be a different user from the requester.
export const approveComplianceOverride = (
  documentId: string,
  overrideId: string,
  params: {
    requested_action: ComplianceOverrideAction
    justification: string
  }
): Promise<ComplianceOverrideResponseEnvelope> =>
  documentClient.post<ComplianceOverrideResponseEnvelope>(
    `/v1/documents/${documentId}/compliance-overrides/${overrideId}/approve`,
    params,
    { params: { tenant_id: tenantId() } }
  ).then(r => r.data)

// Rejects a pending compliance override. Requires ComplianceOfficer role.
export const rejectComplianceOverride = (
  documentId: string,
  overrideId: string,
  params: {
    requested_action: ComplianceOverrideAction
    justification: string
  }
): Promise<ComplianceOverrideResponseEnvelope> =>
  documentClient.post<ComplianceOverrideResponseEnvelope>(
    `/v1/documents/${documentId}/compliance-overrides/${overrideId}/reject`,
    params,
    { params: { tenant_id: tenantId() } }
  ).then(r => r.data)
