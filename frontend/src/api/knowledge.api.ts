import { internalOnlyServiceClient } from './client'
import type {
  KnowledgeAnchorDetail,
  KnowledgeBulkActionResult,
  KnowledgeCollectionResult,
  KnowledgeEnvelope,
  KnowledgeIngestionDetail,
  KnowledgeIngestionSummary,
  KnowledgeSourceDetail,
  KnowledgeSourceSummary,
  KnowledgeSourceVersionLifecycle,
  KnowledgeSourceVersionSummary,
  KnowledgeSearchResultItem,
  KnowledgeTimelineResultItem,
} from '@/types/knowledge'

// Internal/admin-only adapter for governed knowledge-management workflows.
// This is not the normal end-user chat surface.

interface ListKnowledgeIngestionParams {
  limit?: number
  offset?: number
  ingestion_state?: string
  source_class?: string
  requested_by?: string
  sort_by?: 'created_at'
  sort_order?: 'asc' | 'desc'
}

interface ListKnowledgeSourceVersionParams {
  limit?: number
  offset?: number
  publication_state?: string
  source_id?: string
  source_family_id?: string
  tax_domain?: string
  source_class?: string
  sort_by?: 'source_family_id' | 'effective_from'
  sort_order?: 'asc' | 'desc'
}

interface ListKnowledgeSourceParams {
  limit?: number
  offset?: number
  source_class?: string
  tax_domain?: string
  sort_by?: 'source_family_id' | 'tax_domain'
  sort_order?: 'asc' | 'desc'
}

const buildReviewNote = (code: string, note: string, actorUserId: string) => ({
  code,
  note,
  actor_user_id: actorUserId,
  created_at: new Date().toISOString(),
})

export const searchKnowledge = async (params: {
  query: string
  source_type?: string
  tax_domain?: string
  effective_date?: string
}): Promise<KnowledgeCollectionResult<KnowledgeSearchResultItem>> => {
  const response = await internalOnlyServiceClient.post<
    KnowledgeEnvelope<KnowledgeCollectionResult<KnowledgeSearchResultItem>>
  >('/knowledge/search', params)
  return response.data.result
}

export const retrieveKnowledge = async (params: {
  source_ids: string[]
  anchor_ids: string[]
}): Promise<KnowledgeCollectionResult<KnowledgeSearchResultItem>> => {
  const response = await internalOnlyServiceClient.post<
    KnowledgeEnvelope<KnowledgeCollectionResult<KnowledgeSearchResultItem>>
  >('/knowledge/retrieve', params)
  return response.data.result
}

export const timelineSearchKnowledge = async (params: {
  query: string
  tax_domain: string
  source_type?: string
  start_date: string
  end_date: string
}): Promise<KnowledgeCollectionResult<KnowledgeTimelineResultItem>> => {
  const response = await internalOnlyServiceClient.post<
    KnowledgeEnvelope<KnowledgeCollectionResult<KnowledgeTimelineResultItem>>
  >('/knowledge/timeline/search', params)
  return response.data.result
}

export const listKnowledgeIngestionJobs = async (
  params: ListKnowledgeIngestionParams = {}
): Promise<KnowledgeCollectionResult<KnowledgeIngestionSummary>> => {
  const response = await internalOnlyServiceClient.get<
    KnowledgeEnvelope<KnowledgeCollectionResult<KnowledgeIngestionSummary>>
  >('/knowledge/ingestion', { params })
  return response.data.result
}

export const getKnowledgeIngestionJob = async (
  ingestionJobId: string
): Promise<KnowledgeIngestionDetail> => {
  const response = await internalOnlyServiceClient.get<
    KnowledgeEnvelope<KnowledgeIngestionDetail>
  >(`/knowledge/ingestion/${ingestionJobId}`)
  return response.data.result
}

export const reviewKnowledgeIngestionJob = async (params: {
  ingestionJobId: string
  reviewedBy: string
  note: string
}): Promise<KnowledgeIngestionDetail> => {
  const response = await internalOnlyServiceClient.post<
    KnowledgeEnvelope<KnowledgeIngestionDetail>
  >(`/knowledge/ingestion/${params.ingestionJobId}/review`, {
    reviewed_by: params.reviewedBy,
    review_notes: [buildReviewNote('frontend_review_note', params.note, params.reviewedBy)],
  })
  return response.data.result
}

export const approveKnowledgeIngestionJob = async (params: {
  ingestionJobId: string
  reviewedBy: string
  note: string
  publicationPayload: Record<string, unknown>
}): Promise<KnowledgeIngestionDetail> => {
  const response = await internalOnlyServiceClient.post<
    KnowledgeEnvelope<KnowledgeIngestionDetail>
  >(`/knowledge/ingestion/${params.ingestionJobId}/approve`, {
    reviewed_by: params.reviewedBy,
    review_notes: [buildReviewNote('frontend_approve_note', params.note, params.reviewedBy)],
    publication_payload: params.publicationPayload,
  })
  return response.data.result
}

export const rejectKnowledgeIngestionJob = async (params: {
  ingestionJobId: string
  reviewedBy: string
  note: string
}): Promise<KnowledgeIngestionDetail> => {
  const response = await internalOnlyServiceClient.post<
    KnowledgeEnvelope<KnowledgeIngestionDetail>
  >(`/knowledge/ingestion/${params.ingestionJobId}/reject`, {
    reviewed_by: params.reviewedBy,
    review_notes: [buildReviewNote('frontend_reject_note', params.note, params.reviewedBy)],
  })
  return response.data.result
}

export const publishKnowledgeIngestionJob = async (params: {
  ingestionJobId: string
  publishedBy: string
}): Promise<KnowledgeIngestionDetail> => {
  const response = await internalOnlyServiceClient.post<
    KnowledgeEnvelope<KnowledgeIngestionDetail>
  >(`/knowledge/ingestion/${params.ingestionJobId}/publish`, {
    published_by: params.publishedBy,
  })
  return response.data.result
}

export const bulkRejectKnowledgeIngestionJobs = async (params: {
  actingUser: string
  ids: string[]
  note: string
}): Promise<KnowledgeBulkActionResult> => {
  const response = await internalOnlyServiceClient.post<
    KnowledgeEnvelope<KnowledgeBulkActionResult>
  >('/knowledge/ingestion/bulk/reject', {
    acting_user: params.actingUser,
    ids: params.ids,
    review_notes: [buildReviewNote('frontend_bulk_reject_note', params.note, params.actingUser)],
  })
  return response.data.result
}

export const bulkPublishKnowledgeIngestionJobs = async (params: {
  actingUser: string
  ids: string[]
}): Promise<KnowledgeBulkActionResult> => {
  const response = await internalOnlyServiceClient.post<
    KnowledgeEnvelope<KnowledgeBulkActionResult>
  >('/knowledge/ingestion/bulk/publish', {
    acting_user: params.actingUser,
    ids: params.ids,
  })
  return response.data.result
}

export const listKnowledgeSourceVersions = async (
  params: ListKnowledgeSourceVersionParams = {}
): Promise<KnowledgeCollectionResult<KnowledgeSourceVersionSummary>> => {
  const response = await internalOnlyServiceClient.get<
    KnowledgeEnvelope<KnowledgeCollectionResult<KnowledgeSourceVersionSummary>>
  >('/knowledge/source-versions', { params })
  return response.data.result
}

export const getKnowledgeSourceVersion = async (
  sourceVersionId: string
): Promise<KnowledgeSourceVersionLifecycle> => {
  const response = await internalOnlyServiceClient.get<
    KnowledgeEnvelope<KnowledgeSourceVersionLifecycle>
  >(`/knowledge/source-versions/${sourceVersionId}`)
  return response.data.result
}

export const archiveKnowledgeSourceVersion = async (params: {
  sourceVersionId: string
  archivedBy: string
}): Promise<KnowledgeSourceVersionLifecycle> => {
  const response = await internalOnlyServiceClient.post<
    KnowledgeEnvelope<KnowledgeSourceVersionLifecycle>
  >(`/knowledge/source-versions/${params.sourceVersionId}/archive`, {
    archived_by: params.archivedBy,
  })
  return response.data.result
}

export const bulkArchiveKnowledgeSourceVersions = async (params: {
  actingUser: string
  ids: string[]
}): Promise<KnowledgeBulkActionResult> => {
  const response = await internalOnlyServiceClient.post<
    KnowledgeEnvelope<KnowledgeBulkActionResult>
  >('/knowledge/source-versions/bulk/archive', {
    acting_user: params.actingUser,
    ids: params.ids,
  })
  return response.data.result
}

export const listKnowledgeSources = async (
  params: ListKnowledgeSourceParams = {}
): Promise<KnowledgeCollectionResult<KnowledgeSourceSummary>> => {
  const response = await internalOnlyServiceClient.get<
    KnowledgeEnvelope<KnowledgeCollectionResult<KnowledgeSourceSummary>>
  >('/knowledge/sources', { params })
  return response.data.result
}

export const getKnowledgeSource = async (
  sourceId: string
): Promise<KnowledgeSourceDetail> => {
  const response = await internalOnlyServiceClient.get<
    KnowledgeEnvelope<KnowledgeSourceDetail>
  >(`/knowledge/sources/${sourceId}`)
  return response.data.result
}

export const ingestKnowledgeUrl = async (params: {
  requestedBy: string
  idempotencyKey: string
  url: string
  sourceClass?: string
}): Promise<KnowledgeIngestionDetail> => {
  const response = await internalOnlyServiceClient.post<
    KnowledgeEnvelope<KnowledgeIngestionDetail>
  >('/knowledge/ingestion/urls', {
    requested_by: params.requestedBy,
    idempotency_key: params.idempotencyKey,
    url: params.url,
    source_input_origin: 'official_source_url',
    ...(params.sourceClass ? { source_class: params.sourceClass } : {}),
  })
  return response.data.result
}

export const correctKnowledgeIngestionMetadata = async (params: {
  ingestionJobId: string
  correctedBy: string
  note: string
  publicationPayloadUpdates: Record<string, unknown>
}): Promise<KnowledgeIngestionDetail> => {
  const response = await internalOnlyServiceClient.post<
    KnowledgeEnvelope<KnowledgeIngestionDetail>
  >(`/knowledge/ingestion/${params.ingestionJobId}/metadata-correction`, {
    corrected_by: params.correctedBy,
    review_notes: [buildReviewNote('frontend_metadata_correction', params.note, params.correctedBy)],
    publication_payload_updates: params.publicationPayloadUpdates,
  })
  return response.data.result
}

export const getKnowledgeAnchor = async (
  anchorId: string
): Promise<KnowledgeAnchorDetail> => {
  const response = await internalOnlyServiceClient.get<
    KnowledgeEnvelope<KnowledgeAnchorDetail>
  >(`/knowledge/anchors/${anchorId}`)
  return response.data.result
}

export const supersedeKnowledgeSourceVersion = async (params: {
  sourceVersionId: string
  successorSourceVersionId: string
  supersededBy: string
}): Promise<KnowledgeSourceVersionLifecycle> => {
  const response = await internalOnlyServiceClient.post<
    KnowledgeEnvelope<KnowledgeSourceVersionLifecycle>
  >(`/knowledge/source-versions/${params.sourceVersionId}/supersede`, {
    successor_source_version_id: params.successorSourceVersionId,
    superseded_by: params.supersededBy,
  })
  return response.data.result
}
