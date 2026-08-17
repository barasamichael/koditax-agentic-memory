/**
 * Translate backend knowledge state values into plain operational labels
 * suitable for novice-administrator display.
 *
 * Administrators must never see raw backend state names such as
 * "review_pending" or "approved_for_publication" in the UI. This module is
 * the single translation point; update here when backend states change.
 */

import type { KnowledgeIngestionState, KnowledgePublicationState } from '@/types/knowledge'

// Ingestion job states → admin-visible labels
const INGESTION_STATE_LABELS: Record<KnowledgeIngestionState, string> = {
  uploaded: 'Waiting',
  review_pending: 'Under Review',
  approved_for_publication: 'Approved',
  published: 'Published',
  rejected: 'Rejected',
}

// Source-version publication states → admin-visible labels
const PUBLICATION_STATE_LABELS: Record<KnowledgePublicationState, string> = {
  draft: 'Waiting',
  review_pending: 'Under Review',
  approved: 'Approved',
  published: 'Published',
  superseded: 'Superseded',
  archived: 'Archived',
  rejected: 'Rejected',
}

// Source-class → admin-visible label
const SOURCE_CLASS_LABELS: Record<string, string> = {
  tax_law: 'Tax Law',
  regulation: 'Regulation',
  guidance: 'Guidance',
  commentary: 'Commentary',
}

// Source-input-origin → admin-visible label
const INPUT_ORIGIN_LABELS: Record<string, string> = {
  official_source_upload: 'Document upload',
  official_source_url: 'URL submission',
}

// Source-version form → admin-visible label
const SOURCE_VERSION_FORM_LABELS: Record<string, string> = {
  as_issued: 'As issued',
  point_in_time_consolidation: 'Consolidated',
}

// Authority level → admin-visible label
const AUTHORITY_LEVEL_LABELS: Record<string, string> = {
  statute: 'Statute',
  regulation: 'Regulation',
  guidance: 'Guidance',
  commentary: 'Commentary',
}

// Bulk action status → admin-visible label
const BULK_STATUS_LABELS: Record<string, string> = {
  full_success: 'Completed',
  partial_failure: 'Some items need attention',
  full_rejection: 'Could not finish',
}

// Bulk item status → admin-visible label
const BULK_ITEM_STATUS_LABELS: Record<string, string> = {
  ok: 'Done',
  error: 'Failed',
}

export function labelForIngestionState(state: KnowledgeIngestionState): string {
  return INGESTION_STATE_LABELS[state] ?? state
}

export function labelForPublicationState(state: KnowledgePublicationState): string {
  return PUBLICATION_STATE_LABELS[state] ?? state
}

export function labelForSourceClass(sourceClass: string | null | undefined): string {
  if (!sourceClass) return 'Unclassified'
  return SOURCE_CLASS_LABELS[sourceClass] ?? sourceClass
}

export function labelForInputOrigin(origin: string | null | undefined): string {
  if (!origin) return 'Unknown origin'
  return INPUT_ORIGIN_LABELS[origin] ?? origin
}

export function labelForSourceVersionForm(form: string | null | undefined): string {
  if (!form) return 'Unknown form'
  return SOURCE_VERSION_FORM_LABELS[form] ?? form
}

export function labelForAuthorityLevel(level: string | null | undefined): string {
  if (!level) return 'Unknown level'
  return AUTHORITY_LEVEL_LABELS[level] ?? level
}

export function labelForBulkStatus(status: string | null | undefined): string {
  if (!status) return 'Unknown'
  return BULK_STATUS_LABELS[status] ?? status
}

export function labelForBulkItemStatus(status: string | null | undefined): string {
  if (!status) return 'Unknown'
  return BULK_ITEM_STATUS_LABELS[status] ?? status
}
