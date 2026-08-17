import { normalizeError } from '@/lib/errorNormalizer'
import type { PurgeDryRunBlocker } from '@/types/document'

export type DocumentLifecycleActionContext = 'trash' | 'restore' | 'delete_permanently'

interface DocumentErrorContext {
  action?: DocumentLifecycleActionContext
}

export const DOCUMENT_ERROR_MESSAGES: Record<string, string> = {
  unsupported_mime_type: 'This file type is not supported.',
  format_not_supported_in_production: 'This file type is not supported.',
  upload_size_exceeds_limit: 'This file is larger than the 200 MB limit.',
  unsupported_format: 'This file type is not supported.',
  declared_media_type_mismatch: 'The file contents do not match its file type.',
  malformed_document: 'We could not read this document because it appears to be damaged.',
  encrypted_document: 'This document is password-protected. Remove the password and upload it again.',
  invalid_checksum_format: 'File integrity check failed. Please try again.',
  invalid_upload_session_request: 'The upload could not be prepared. Please try again.',
  invalid_upload_completion_request: 'The upload could not be finalized. Please try again.',
  upload_session_forbidden: 'You do not have permission to upload to this account.',
  upload_session_expired: 'Upload session expired. Please start the upload again.',
  upload_session_not_found: 'Upload session not found.',
  upload_session_invalid_state: 'This upload session is no longer active. Please start again.',
  upload_session_document_mismatch: 'Upload session does not match this document.',
  storage_retryable_failure: 'The service is temporarily unavailable. Please try again.',
  storage_non_retryable_failure: 'The request could not be completed.',
  checksum_mismatch: 'The uploaded file does not match the selected file. Please try again.',
  size_mismatch: 'The uploaded file size does not match the selected file.',
  content_type_mismatch: 'The selected file type does not match the upload session.',
  tenant_mismatch: 'The uploaded file could not be saved in this account.',
  document_not_found: 'Document not found.',
  document_not_found_or_forbidden: 'Document not found.',
  document_access_denied: 'You do not have access to this document.',
  stale_document_revision: 'This document changed while you were editing it. Refresh and try again.',
  idempotency_key_payload_mismatch: 'This request was already submitted differently. Refresh and try again.',
  document_lifecycle_blocks_mutation: 'This document cannot be changed in its current state.',
  invalid_document_state_transition: 'This document cannot be changed in its current state.',
  document_retention_action_forbidden: 'You do not have permission to change this document.',
  document_retention_lock_active: 'Document is compliance-locked and cannot be modified.',
  invalid_document_state_for_extraction: 'Document is not in a state that allows extraction.',
  extraction_not_found: 'Extraction record not found.',
  invalid_evidence_link_request: 'Evidence link request is invalid. Extraction must be verified first.',
  evidence_linkage_conflict: 'An evidence link already exists for this extraction.',
  download_capability_rejected: 'Download link is invalid or has expired.',
  compliance_override_rejected: 'Compliance override was rejected.',
  missing_idempotency_key: 'Request is missing a required deduplication key.',
  idempotency_key_conflict: 'A duplicate request was detected.',
  storage_object_not_found: 'The uploaded file could not be found.',
  storage_object_checksum_mismatch: 'The uploaded file failed verification. Please try again.',
  storage_object_size_mismatch: 'The uploaded file failed size verification. Please try again.',
  storage_object_content_type_mismatch: 'The uploaded file type could not be verified.',
  invalid_capability_signature: 'The document link is invalid or has expired.',
  capability_scope_mismatch: 'The document link cannot be used for this document.',
  capability_expired: 'The document link has expired. Please try again.',
  capability_already_consumed: 'This document link has already been used.',
  unauthorized_download_access: 'You do not have access to this document.',
  storage_capability_not_found: 'The document is temporarily unavailable. Please try again.',
  storage_capability_expired: 'The document link has expired. Please request a fresh one.',
  validation_error: 'Please check the document details and try again.',
}

export const DOCUMENT_DELETE_BLOCKER_MESSAGES: Record<PurgeDryRunBlocker, string> = {
  compliance_lock_active: 'This document is currently locked.',
  already_purged: 'This document has already been removed.',
  invalid_execute_purge_state_transition: 'This document is not ready for permanent deletion yet.',
  invalid_uploaded_at: 'The document date is invalid.',
  missing_purge_eligible_at: 'The document still needs a readiness check.',
  invalid_purge_eligible_at: 'The document readiness date is invalid.',
  purge_eligible_at_before_uploaded_at: 'The deletion timing is inconsistent with the upload date.',
  purge_not_yet_eligible: 'The document is not ready for permanent deletion yet.',
}

const LIFECYCLE_ACTION_MESSAGES: Record<
  DocumentLifecycleActionContext,
  Partial<Record<string, string>>
> = {
  trash: {
    invalid_document_state_transition: 'This document cannot be moved to trash in its current state.',
    document_lifecycle_blocks_mutation: 'This document cannot be moved to trash in its current state.',
    document_retention_action_forbidden: 'You do not have permission to move this document to trash.',
    document_retention_lock_active: 'This document is locked and cannot be moved to trash right now.',
  },
  restore: {
    invalid_document_state_transition: 'This document cannot be restored in its current state.',
    document_lifecycle_blocks_mutation: 'This document cannot be restored in its current state.',
    document_retention_action_forbidden: 'You do not have permission to restore this document.',
    document_retention_lock_active: 'This document is locked and cannot be restored right now.',
  },
  delete_permanently: {
    invalid_execute_purge_state_transition:
      'This document cannot be deleted permanently in its current state.',
    invalid_document_state_transition:
      'This document cannot be deleted permanently in its current state.',
    document_lifecycle_blocks_mutation:
      'This document cannot be deleted permanently in its current state.',
    document_retention_action_forbidden:
      'You do not have permission to delete this document permanently.',
    document_retention_lock_active:
      'This document is locked and cannot be deleted permanently right now.',
  },
}

export const getDocumentErrorMessage = (err: unknown, context?: DocumentErrorContext): string => {
  const normalized = normalizeError(err)
  if (context?.action) {
    const lifecycleMessage = LIFECYCLE_ACTION_MESSAGES[context.action][normalized.error_code]
    if (lifecycleMessage) {
      return lifecycleMessage
    }
  }
  if (DOCUMENT_ERROR_MESSAGES[normalized.error_code]) {
    return DOCUMENT_ERROR_MESSAGES[normalized.error_code]
  }
  if (err instanceof Error && err.message.trim().length > 0) {
    return err.message
  }
  return normalized.message
}
