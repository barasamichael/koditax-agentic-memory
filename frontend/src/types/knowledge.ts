export type KnowledgeAdminTab = 'ingestion' | 'reviewQueue' | 'sourceVersions' | 'sources'

export type KnowledgeIngestionState =
  | 'uploaded'
  | 'review_pending'
  | 'approved_for_publication'
  | 'published'
  | 'rejected'

export type KnowledgePublicationState =
  | 'draft'
  | 'review_pending'
  | 'approved'
  | 'published'
  | 'superseded'
  | 'archived'
  | 'rejected'

export type KnowledgeSourceClass =
  | 'tax_law'
  | 'regulation'
  | 'guidance'
  | 'commentary'

export type KnowledgeAuthorityLevel =
  | 'statute'
  | 'regulation'
  | 'guidance'
  | 'commentary'

export type KnowledgeSourceInputOrigin =
  | 'official_source_upload'
  | 'official_source_url'

export type KnowledgeSourceVersionForm =
  | 'as_issued'
  | 'point_in_time_consolidation'

export interface KnowledgeEnvelope<T> {
  status: string
  service: string
  correlation_id: string
  trace_id: string
  result: T
}

export interface KnowledgeCollectionPage {
  limit?: number | null
  offset?: number | null
  sort_by?: string | null
  sort_order?: string | null
}

export interface KnowledgeCollectionResult<T> {
  total: number
  items: T[]
  page?: KnowledgeCollectionPage
}

export interface KnowledgeReviewNote {
  code?: string
  note: string
  created_at: string
  actor_user_id: string
}

export interface KnowledgeBulkActionItem {
  id: string
  status: 'ok' | 'error'
  outcome: string
  error_code: string | null
  reason: string | null
}

export interface KnowledgeBulkActionResult {
  bulk_status: 'full_success' | 'partial_failure' | 'full_rejection'
  total: number
  items: KnowledgeBulkActionItem[]
}

export interface KnowledgeBulkIngestionItem {
  index: number
  idempotency_key: string
  status: 'ok' | 'error'
  outcome: string
  ingestion_job_id: string | null
  error_code: string | null
  reason: string | null
}

export interface KnowledgeBulkIngestionResult {
  bulk_status: 'full_success' | 'partial_failure' | 'full_rejection'
  total: number
  items: KnowledgeBulkIngestionItem[]
}

export interface KnowledgeSearchResultItem {
  source_id: string
  title: string
  url: string
  source_type: string
  tax_domain: string
  authority_level: string
  effective_from: string
  effective_to: string | null
  tax_year: number | null
  anchor_id: string
}

export interface KnowledgeTimelineResultItem {
  source_id: string
  source_version_id: string
  anchor_id: string
  title: string
  source_type: string
  authority_level: string
  tax_domain: string
  effective_from: string
  effective_to: string | null
  publication_state: KnowledgePublicationState
  timeline_position: number
}

export interface KnowledgeIngestionSummary {
  ingestion_job_id: string
  document_id: string
  requested_by: string
  ingestion_state: KnowledgeIngestionState
  source_input_origin: KnowledgeSourceInputOrigin
  source_input_ref: string
  payload_checksum_sha256: string
  source_class: KnowledgeSourceClass | null
  created_at: string
  completed_at: string | null
}

export interface KnowledgeIngestionDetail {
  ingestion_job_id: string
  document_id: string
  requested_by: string
  ingestion_state: KnowledgeIngestionState
  source_input_origin: KnowledgeSourceInputOrigin
  source_input_ref: string
  payload_checksum_sha256: string
  source_class: KnowledgeSourceClass | null
  extracted_metadata: Record<string, unknown>
  proposed_source_record: Record<string, unknown>
  review_notes: KnowledgeReviewNote[]
  completed_at: string | null
}

export interface KnowledgeSourceVersionSummary {
  source_version_id: string
  source_id: string
  source_family_id: string
  title: string
  source_class: KnowledgeSourceClass
  tax_domain: string
  authority_level: KnowledgeAuthorityLevel
  publication_state: KnowledgePublicationState
  source_input_origin: KnowledgeSourceInputOrigin
  source_version_form: KnowledgeSourceVersionForm
  effective_from: string
  effective_to: string | null
  tax_year: number | null
  supersedes_source_version_id: string | null
  superseded_by_source_version_id: string | null
}

export interface KnowledgeSourceVersionLifecycle {
  source_version_id: string
  source_id: string
  source_family_id: string
  publication_state: 'published' | 'superseded' | 'archived'
  source_input_origin: KnowledgeSourceInputOrigin
  source_version_form: KnowledgeSourceVersionForm
  effective_from: string
  effective_to: string | null
  tax_year: number | null
  supersedes_source_version_id: string | null
  superseded_by_source_version_id: string | null
}

export interface KnowledgeSourceSummary {
  source_id: string
  source_family_id: string
  title: string
  canonical_url: string
  source_class: KnowledgeSourceClass
  tax_domain: string
  authority_level: KnowledgeAuthorityLevel
  issuing_authority: string
  version_count: number
  anchor_count: number
  created_at: string
  retired_at: string | null
}

export interface KnowledgeRetentionSummary {
  lineage_preserved: boolean
  has_document_lineage: boolean
  has_purged_document_lineage: boolean
  retention_policy_code: string
  purge_supported: boolean
}

export interface KnowledgeSourceDetail extends KnowledgeSourceSummary {
  chunk_count: number
  versions: KnowledgeSourceVersionSummary[]
  retention_summary: KnowledgeRetentionSummary
}

export interface KnowledgeAnchorChunkSummary {
  chunk_id: string
  chunk_index: number
  has_embedding: boolean
}

export interface KnowledgeAnchorDetail {
  anchor_id: string
  source_id: string
  source_family_id: string
  source_version_id: string
  source_title: string
  source_type: KnowledgeSourceClass
  tax_domain: string
  authority_level: KnowledgeAuthorityLevel
  publication_state: KnowledgePublicationState
  anchor_title: string
  anchor_path: string
  temporal_scope_from: string
  temporal_scope_to: string | null
  chunk_count: number
  chunks: KnowledgeAnchorChunkSummary[]
}
