export type DocumentState =
  | 'uploaded'
  | 'active'
  | 'trashed'
  | 'purge_pending'
  | 'processing'
  | 'validated'
  | 'eligible_for_purge'
  | 'purged'

export interface DocumentRecord {
  document_id: string
  state: DocumentState
  uploaded_at: string
  computation_id: string | null
  purge_eligible_at: string | null
  purged_at: string | null
  compliance_lock_until: string | null
  display_name: string | null
  category: string | null
  tags: string[]
  description: string | null
  revision: number
}

export interface Traceability {
  trace_id: string | null
  correlation_id: string | null
}

export interface DocumentRecordEnvelope {
  status: 'ok'
  duplicate_detected?: boolean
  document: DocumentRecord
  processing_operation_id?: string | null
  traceability: Traceability
}

export type DocumentBindingRole =
  | 'conversation_attachment'
  | 'current_turn_attachment'
  | 'existing_library_document'
  | 'workflow_reference'

export interface DocumentBindingRecord {
  document_binding_id: string
  tenant_id: string
  document_id: string
  document_version_id: string | null
  resolved_document_version_id: string | null
  binding_role: DocumentBindingRole
  conversation_id: string | null
  turn_id: string | null
  workflow_id: string | null
  attachment_order: number | null
  bound_by_user_id: string
  bound_at: string
  correlation_id: string
}

export interface DocumentBindingEnvelope {
  status: 'ok'
  binding: DocumentBindingRecord
}

export interface DocumentListEnvelope {
  status: 'ok'
  documents: DocumentRecord[]
  traceability: Traceability
}

// From README "UploadSessionResponse"
export interface UploadSessionResponse {
  status: 'upload_session_created'
  session_id: string
  upload_session_id: string
  session_state: 'active'
  document_id: string
  upload_url: string
  expires_at: string
  traceability: Traceability
}

// From README "SignedDownloadCapabilityEnvelope"
export interface SignedDownloadCapability {
  capability_id: string
  document_id: string
  issued_to_user_id: string
  tenant_id: string
  expires_at: string
  allowed_action: 'download'
  capability_token: string
  download_url: string
  method: 'GET'
  headers: Record<string, string>
}

export interface SignedDownloadCapabilityEnvelope {
  status: 'download_capability_issued'
  capability: SignedDownloadCapability
  traceability: Traceability
}

export interface SignedDownloadValidationEnvelope {
  status: 'download_capability_valid'
  capability_id: string
  document_id: string
  expires_at: string
  allowed_action: 'download'
  validated_at: string
  traceability: Traceability
}

// From README "ComplianceOverrideResponseEnvelope"
export type ComplianceOverrideStatus =
  | 'compliance_override_requested'
  | 'compliance_override_approved'
  | 'compliance_override_rejected'
  | 'compliance_override_consumed'

export type ComplianceOverrideRecordStatus =
  | 'requested'
  | 'approved'
  | 'rejected'
  | 'expired'
  | 'consumed'

export type ComplianceOverrideAction =
  | 'trash'
  | 'restore'
  | 'mark_eligible_for_purge'
  | 'execute_purge'

export interface ComplianceOverrideRecord {
  override_id: string
  document_id: string
  requested_action: ComplianceOverrideAction
  tenant_id: string
  requested_by_user_id: string
  requested_by_role: string
  justification: string
  status: ComplianceOverrideRecordStatus
  created_at: string
  expires_at: string
  approved_by_user_id: string | null
  approved_by_role: string | null
  approved_at: string | null
  rejected_by_user_id: string | null
  rejected_by_role: string | null
  rejected_at: string | null
  consumed_by_user_id: string | null
  consumed_at: string | null
}

export interface ComplianceOverrideResponseEnvelope {
  status: ComplianceOverrideStatus
  override: ComplianceOverrideRecord
  traceability: Traceability
}

// From README "DocumentPurgeSafetyDryRunEnvelope"
export type PurgeDryRunBlocker =
  | 'compliance_lock_active'
  | 'already_purged'
  | 'invalid_execute_purge_state_transition'
  | 'invalid_uploaded_at'
  | 'missing_purge_eligible_at'
  | 'invalid_purge_eligible_at'
  | 'purge_eligible_at_before_uploaded_at'
  | 'purge_not_yet_eligible'

export interface DocumentPurgeSafetyDryRunEnvelope {
  status: 'ok'
  document_id: string
  dry_run: true
  purge_ready: boolean
  blockers: PurgeDryRunBlocker[]
  evaluated_at: string
  traceability: Traceability
}
