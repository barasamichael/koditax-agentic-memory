import { useEffect, useMemo, useState } from 'react'
import { AppShell } from '@/components/layout/AppShell'
import { KnowledgeActionBar } from '@/components/knowledge/KnowledgeActionBar'
import { KnowledgeBulkActionBar } from '@/components/knowledge/KnowledgeBulkActionBar'
import { KnowledgeDetailPanel } from '@/components/knowledge/KnowledgeDetailPanel'
import { KnowledgeIntakeForm } from '@/components/knowledge/KnowledgeIntakeForm'
import {
  KnowledgeList,
  type KnowledgeListItem,
} from '@/components/knowledge/KnowledgeList'
import { KnowledgeSearchBar } from '@/components/knowledge/KnowledgeSearchBar'
import { KnowledgeSourceDetailPanel } from '@/components/knowledge/KnowledgeSourceDetailPanel'
import { KnowledgeSourceVersionList } from '@/components/knowledge/KnowledgeSourceVersionList'
import { KnowledgeTabs } from '@/components/knowledge/KnowledgeTabs'
import { useToast } from '@/components/shared/Toast'
import { useAuthStore } from '@/stores/authStore'
import * as knowledgeApi from '@/api/knowledge.api'
import {
  labelForIngestionState,
  labelForInputOrigin,
  labelForSourceClass,
} from '@/lib/knowledgeStateLabels'
import type {
  KnowledgeAdminTab,
  KnowledgeBulkActionResult,
  KnowledgeIngestionDetail,
  KnowledgeIngestionSummary,
  KnowledgeSourceDetail,
  KnowledgeSourceSummary,
  KnowledgeSourceVersionLifecycle,
  KnowledgeSourceVersionSummary,
} from '@/types/knowledge'

const ADMIN_ROLE = 'Administrator'

const INITIAL_QUERIES: Record<KnowledgeAdminTab, string> = {
  ingestion: '',
  reviewQueue: '',
  sourceVersions: '',
  sources: '',
}

function hasPublicationPayload(item: KnowledgeIngestionDetail | null): item is KnowledgeIngestionDetail {
  return Boolean(
    item &&
      item.proposed_source_record &&
      Object.keys(item.proposed_source_record).length > 0
  )
}

function isIngestionSelectable(item: KnowledgeIngestionSummary): boolean {
  return ['uploaded', 'review_pending', 'approved_for_publication'].includes(item.ingestion_state)
}

function getIngestionSelectionDisabledReason(item: KnowledgeIngestionSummary): string | null {
  return isIngestionSelectable(item) ? null : 'Already finalized and unavailable for bulk actions.'
}

function isArchivableSourceVersion(item: KnowledgeSourceVersionSummary): boolean {
  return item.publication_state === 'published' || item.publication_state === 'superseded'
}

export default function KnowledgeAdminPage() {
  const role = useAuthStore((state) => state.role)
  const userId = useAuthStore((state) => state.userId)
  const toast = useToast()

  const isAdministrator = role === ADMIN_ROLE

  const [activeTab, setActiveTab] = useState<KnowledgeAdminTab>('ingestion')
  const [queries, setQueries] = useState(INITIAL_QUERIES)

  const [ingestionItems, setIngestionItems] = useState<KnowledgeIngestionSummary[]>([])
  const [ingestionLoading, setIngestionLoading] = useState(true)
  const [ingestionError, setIngestionError] = useState<string | null>(null)
  const [selectedIngestionId, setSelectedIngestionId] = useState<string | null>(null)
  const [selectedIngestionIds, setSelectedIngestionIds] = useState<string[]>([])
  const [ingestionDetail, setIngestionDetail] = useState<KnowledgeIngestionDetail | null>(null)
  const [ingestionDetailLoading, setIngestionDetailLoading] = useState(false)
  const [ingestionDetailError, setIngestionDetailError] = useState<string | null>(null)
  const [ingestionBusyAction, setIngestionBusyAction] = useState<
    'review' | 'approve' | 'reject' | 'publish' | 'bulkReject' | 'bulkPublish' | null
  >(null)
  const [ingestionBulkResult, setIngestionBulkResult] = useState<KnowledgeBulkActionResult | null>(
    null
  )
  const [intakeBusy, setIntakeBusy] = useState(false)
  const [metadataCorrectionBusy, setMetadataCorrectionBusy] = useState(false)

  const [sourceVersionItems, setSourceVersionItems] = useState<KnowledgeSourceVersionSummary[]>([])
  const [sourceVersionLoading, setSourceVersionLoading] = useState(true)
  const [sourceVersionError, setSourceVersionError] = useState<string | null>(null)
  const [selectedSourceVersionId, setSelectedSourceVersionId] = useState<string | null>(null)
  const [selectedSourceVersionIds, setSelectedSourceVersionIds] = useState<string[]>([])
  const [sourceVersionDetail, setSourceVersionDetail] = useState<KnowledgeSourceVersionLifecycle | null>(
    null
  )
  const [sourceVersionDetailLoading, setSourceVersionDetailLoading] = useState(false)
  const [sourceVersionDetailError, setSourceVersionDetailError] = useState<string | null>(null)
  const [sourceVersionBusyAction, setSourceVersionBusyAction] = useState<'archive' | 'supersede' | 'bulkArchive' | null>(
    null
  )
  const [sourceVersionBulkResult, setSourceVersionBulkResult] =
    useState<KnowledgeBulkActionResult | null>(null)

  const [sourceItems, setSourceItems] = useState<KnowledgeSourceSummary[]>([])
  const [sourceLoading, setSourceLoading] = useState(true)
  const [sourceError, setSourceError] = useState<string | null>(null)
  const [selectedSourceId, setSelectedSourceId] = useState<string | null>(null)
  const [sourceDetail, setSourceDetail] = useState<KnowledgeSourceDetail | null>(null)
  const [sourceDetailLoading, setSourceDetailLoading] = useState(false)
  const [sourceDetailError, setSourceDetailError] = useState<string | null>(null)

  const loadIngestionItems = async () => {
    setIngestionLoading(true)
    setIngestionError(null)
    try {
      const result = await knowledgeApi.listKnowledgeIngestionJobs({
        limit: 100,
        sort_by: 'created_at',
        sort_order: 'desc',
      })
      setIngestionItems(result.items)
    } catch {
      setIngestionError('Unable to load incoming items right now. Try refreshing the page.')
      toast.error('Knowledge ingestion listing failed.')
    } finally {
      setIngestionLoading(false)
    }
  }

  const loadSourceVersionItems = async () => {
    setSourceVersionLoading(true)
    setSourceVersionError(null)
    try {
      const result = await knowledgeApi.listKnowledgeSourceVersions({
        limit: 100,
        sort_by: 'effective_from',
        sort_order: 'desc',
      })
      setSourceVersionItems(result.items)
    } catch {
      setSourceVersionError('Unable to load published sources right now. Try refreshing the page.')
      toast.error('Knowledge source-version listing failed.')
    } finally {
      setSourceVersionLoading(false)
    }
  }

  const loadSourceItems = async () => {
    setSourceLoading(true)
    setSourceError(null)
    try {
      const result = await knowledgeApi.listKnowledgeSources({
        limit: 100,
        sort_by: 'source_family_id',
        sort_order: 'asc',
      })
      setSourceItems(result.items)
    } catch {
      setSourceError('Unable to load the source library right now. Try refreshing the page.')
      toast.error('Knowledge source listing failed.')
    } finally {
      setSourceLoading(false)
    }
  }

  const loadIngestionDetail = async (ingestionJobId: string) => {
    setIngestionDetailLoading(true)
    setIngestionDetailError(null)
    try {
      const result = await knowledgeApi.getKnowledgeIngestionJob(ingestionJobId)
      setIngestionDetail(result)
    } catch {
      setIngestionDetail(null)
      setIngestionDetailError('Unable to load item detail right now. Try selecting the item again.')
      toast.error('Knowledge ingestion detail load failed.')
    } finally {
      setIngestionDetailLoading(false)
    }
  }

  const loadSourceVersionDetail = async (sourceVersionId: string) => {
    setSourceVersionDetailLoading(true)
    setSourceVersionDetailError(null)
    try {
      const result = await knowledgeApi.getKnowledgeSourceVersion(sourceVersionId)
      setSourceVersionDetail(result)
    } catch {
      setSourceVersionDetail(null)
      setSourceVersionDetailError('Unable to load source detail right now. Try selecting the item again.')
      toast.error('Knowledge source-version detail load failed.')
    } finally {
      setSourceVersionDetailLoading(false)
    }
  }

  const loadSourceDetail = async (sourceId: string) => {
    setSourceDetailLoading(true)
    setSourceDetailError(null)
    try {
      const result = await knowledgeApi.getKnowledgeSource(sourceId)
      setSourceDetail(result)
    } catch {
      setSourceDetail(null)
      setSourceDetailError('Unable to load source detail right now. Try selecting the item again.')
      toast.error('Knowledge source detail load failed.')
    } finally {
      setSourceDetailLoading(false)
    }
  }

  const handleIntakeUrl = async (params: { url: string; sourceClass: string }) => {
    if (!userId) return
    setIntakeBusy(true)
    try {
      await knowledgeApi.ingestKnowledgeUrl({
        requestedBy: userId,
        idempotencyKey: `${userId}-${Date.now()}`,
        url: params.url,
        sourceClass: params.sourceClass || undefined,
      })
      await loadIngestionItems()
      toast.success('Source URL registered. It will appear in Incoming Items shortly.')
    } catch {
      toast.error('URL registration failed. Check the URL and try again.')
    } finally {
      setIntakeBusy(false)
    }
  }

  const handleMetadataCorrection = async (params: {
    note: string
    updates: Record<string, unknown>
  }) => {
    if (!ingestionDetail || !userId) return
    setMetadataCorrectionBusy(true)
    try {
      const updated = await knowledgeApi.correctKnowledgeIngestionMetadata({
        ingestionJobId: ingestionDetail.ingestion_job_id,
        correctedBy: userId,
        note: params.note,
        publicationPayloadUpdates: params.updates,
      })
      await refreshIngestionAfterAction(updated)
      toast.success('Metadata correction saved.')
    } catch {
      toast.error('Metadata correction failed. Check the values and try again.')
    } finally {
      setMetadataCorrectionBusy(false)
    }
  }

  useEffect(() => {
    if (!isAdministrator) return
    void Promise.allSettled([loadIngestionItems(), loadSourceVersionItems(), loadSourceItems()])
  }, [isAdministrator])

  const filteredIngestionItems = useMemo(() => {
    const normalized = queries.ingestion.trim().toLowerCase()
    if (!normalized) return ingestionItems
    return ingestionItems.filter((item) =>
      [
        item.source_input_ref,
        item.source_class ?? '',
        item.requested_by,
        item.ingestion_state,
        item.source_input_origin,
      ]
        .join(' ')
        .toLowerCase()
        .includes(normalized)
    )
  }, [ingestionItems, queries.ingestion])

  const reviewQueueItems = useMemo(
    () => ingestionItems.filter((item) => item.ingestion_state === 'review_pending'),
    [ingestionItems]
  )

  const filteredReviewQueueItems = useMemo(() => {
    const normalized = queries.reviewQueue.trim().toLowerCase()
    if (!normalized) return reviewQueueItems
    return reviewQueueItems.filter((item) =>
      [
        item.source_input_ref,
        item.source_class ?? '',
        item.requested_by,
        item.source_input_origin,
      ]
        .join(' ')
        .toLowerCase()
        .includes(normalized)
    )
  }, [reviewQueueItems, queries.reviewQueue])

  const filteredSourceVersionItems = useMemo(() => {
    const normalized = queries.sourceVersions.trim().toLowerCase()
    if (!normalized) return sourceVersionItems
    return sourceVersionItems.filter((item) =>
      [
        item.title,
        item.tax_domain,
        item.source_class,
        item.publication_state,
        item.source_family_id,
        item.source_id,
      ]
        .join(' ')
        .toLowerCase()
        .includes(normalized)
    )
  }, [queries.sourceVersions, sourceVersionItems])

  const filteredSourceItems = useMemo(() => {
    const normalized = queries.sources.trim().toLowerCase()
    if (!normalized) return sourceItems
    return sourceItems.filter((item) =>
      [
        item.title,
        item.tax_domain,
        item.source_class,
        item.issuing_authority,
        item.source_family_id,
        item.source_id,
      ]
        .join(' ')
        .toLowerCase()
        .includes(normalized)
    )
  }, [queries.sources, sourceItems])

  useEffect(() => {
    if (!filteredIngestionItems.length) {
      setSelectedIngestionId(null)
      setIngestionDetail(null)
      return
    }
    setSelectedIngestionId((current) =>
      current && filteredIngestionItems.some((item) => item.ingestion_job_id === current)
        ? current
        : filteredIngestionItems[0]?.ingestion_job_id ?? null
    )
  }, [filteredIngestionItems])

  useEffect(() => {
    setSelectedIngestionIds((current) =>
      current.filter((id) =>
        ingestionItems.some(
          (item) => item.ingestion_job_id === id && getIngestionSelectionDisabledReason(item) === null
        )
      )
    )
  }, [ingestionItems])

  useEffect(() => {
    if (!filteredSourceVersionItems.length) {
      setSelectedSourceVersionId(null)
      setSourceVersionDetail(null)
      return
    }
    setSelectedSourceVersionId((current) =>
      current &&
      filteredSourceVersionItems.some((item) => item.source_version_id === current)
        ? current
        : filteredSourceVersionItems[0]?.source_version_id ?? null
    )
  }, [filteredSourceVersionItems])

  useEffect(() => {
    setSelectedSourceVersionIds((current) =>
      current.filter((id) =>
        sourceVersionItems.some((item) => item.source_version_id === id && isArchivableSourceVersion(item))
      )
    )
  }, [sourceVersionItems])

  useEffect(() => {
    if (!filteredSourceItems.length) {
      setSelectedSourceId(null)
      setSourceDetail(null)
      return
    }
    setSelectedSourceId((current) =>
      current && filteredSourceItems.some((item) => item.source_id === current)
        ? current
        : filteredSourceItems[0]?.source_id ?? null
    )
  }, [filteredSourceItems])

  useEffect(() => {
    if (!isAdministrator || !selectedIngestionId) return
    void loadIngestionDetail(selectedIngestionId)
  }, [isAdministrator, selectedIngestionId])

  useEffect(() => {
    if (!isAdministrator || !selectedSourceVersionId) return
    void loadSourceVersionDetail(selectedSourceVersionId)
  }, [isAdministrator, selectedSourceVersionId])

  useEffect(() => {
    if (!isAdministrator || !selectedSourceId) return
    void loadSourceDetail(selectedSourceId)
  }, [isAdministrator, selectedSourceId])

  const ingestionListItems = useMemo<KnowledgeListItem[]>(
    () =>
      filteredIngestionItems.map((item) => ({
        id: item.ingestion_job_id,
        title: item.source_input_ref,
        subtitle: `${labelForIngestionState(item.ingestion_state)} · ${labelForSourceClass(item.source_class)}`,
        meta: `${labelForInputOrigin(item.source_input_origin)} · submitted by ${item.requested_by}`,
        timestamp: item.created_at,
        selectionDisabledReason: getIngestionSelectionDisabledReason(item),
      })),
    [filteredIngestionItems]
  )

  const reviewQueueListItems = useMemo<KnowledgeListItem[]>(
    () =>
      filteredReviewQueueItems.map((item) => ({
        id: item.ingestion_job_id,
        title: item.source_input_ref,
        subtitle: `Under Review · ${labelForSourceClass(item.source_class)}`,
        meta: `${labelForInputOrigin(item.source_input_origin)} · submitted by ${item.requested_by}`,
        timestamp: item.created_at,
        selectionDisabledReason: getIngestionSelectionDisabledReason(item),
      })),
    [filteredReviewQueueItems]
  )

  const sourceListItems = useMemo<KnowledgeListItem[]>(
    () =>
      filteredSourceItems.map((item) => ({
        id: item.source_id,
        title: item.title,
        subtitle: `${labelForSourceClass(item.source_class)} · ${item.tax_domain}`,
        meta: `${item.version_count} version${item.version_count === 1 ? '' : 's'} · ${item.anchor_count} anchor${item.anchor_count === 1 ? '' : 's'}`,
        timestamp: item.created_at,
      })),
    [filteredSourceItems]
  )

  const activeSourceVersionSummary = useMemo(
    () =>
      sourceVersionItems.find((item) => item.source_version_id === selectedSourceVersionId) ?? null,
    [selectedSourceVersionId, sourceVersionItems]
  )

  const searchConfig = useMemo(
    () => ({
      ingestion: {
        label: 'Search incoming items',
        placeholder: 'Search by source, type, submitter, or status...',
        helperText: 'Bulk Reject and Bulk Publish apply only to items in an eligible status.',
      },
      reviewQueue: {
        label: 'Search review queue',
        placeholder: 'Search by source, type, or submitter...',
        helperText: 'Showing items that are ready for administrator review.',
      },
      sourceVersions: {
        label: 'Search published sources',
        placeholder: 'Search by title, tax domain, or status...',
        helperText: 'Archive is available for Published and Superseded versions only.',
      },
      sources: {
        label: 'Search source library',
        placeholder: 'Search by title, authority, tax domain, or family...',
        helperText: 'Lifecycle actions such as Archive and Supersede operate on individual versions.',
      },
    }),
    []
  )

  const tabs = useMemo(
    () => [
      {
        id: 'ingestion' as const,
        label: 'Incoming Items',
        count: ingestionItems.length,
        helper: 'All submitted items. Register new sources, and track every intake job.',
      },
      {
        id: 'reviewQueue' as const,
        label: 'Review Queue',
        count: reviewQueueItems.length,
        helper: 'Items awaiting review. Approve or reject items to move them forward.',
      },
      {
        id: 'sourceVersions' as const,
        label: 'Published Sources',
        count: sourceVersionItems.length,
        helper: 'View published and superseded source versions. Archive eligible records.',
      },
      {
        id: 'sources' as const,
        label: 'Source Library',
        count: sourceItems.length,
        helper: 'Browse the full source catalog and inspect source history and coverage.',
      },
    ],
    [ingestionItems.length, reviewQueueItems.length, sourceItems.length, sourceVersionItems.length]
  )

  if (!isAdministrator) {
    return (
      <AppShell>
        <div className="flex h-full items-center justify-center p-6">
          <div className="max-w-lg rounded-3xl border border-red-200 bg-red-50 p-8 text-center">
            <p className="text-xs font-medium uppercase tracking-wide text-red-700">
              Access restricted
            </p>
            <h1 className="mt-2 text-2xl font-semibold text-red-900">
              Knowledge Base is not available for your account
            </h1>
            <p className="mt-3 text-sm text-red-800">
              The Knowledge Base workspace is available only to administrators. If you believe
              this is an error, contact your platform administrator.
            </p>
          </div>
        </div>
      </AppShell>
    )
  }

  const refreshIngestionAfterAction = async (updated: KnowledgeIngestionDetail) => {
    setIngestionDetail(updated)
    setSelectedIngestionId(updated.ingestion_job_id)
    await loadIngestionItems()
  }

  const refreshSourceVersionsAfterAction = async (
    updated: KnowledgeSourceVersionLifecycle | null = null
  ) => {
    if (updated) {
      setSourceVersionDetail(updated)
      setSelectedSourceVersionId(updated.source_version_id)
    }
    await loadSourceVersionItems()
  }

  const currentSearch = searchConfig[activeTab]

  const visibleSelectableIngestionIds = filteredIngestionItems
    .filter((item) => getIngestionSelectionDisabledReason(item) === null)
    .map((item) => item.ingestion_job_id)

  const visibleArchivableSourceVersionIds = filteredSourceVersionItems
    .filter((item) => isArchivableSourceVersion(item))
    .map((item) => item.source_version_id)

  return (
    <AppShell>
      <div className="flex h-full overflow-hidden bg-gray-50">
        <aside className="hidden w-80 shrink-0 border-r border-gray-100 bg-white lg:flex lg:flex-col">
          <KnowledgeSearchBar
            label={currentSearch.label}
            placeholder={currentSearch.placeholder}
            helperText={currentSearch.helperText}
            query={queries[activeTab]}
            onChange={(value) => setQueries((current) => ({ ...current, [activeTab]: value }))}
            resultCount={
              activeTab === 'ingestion'
                ? filteredIngestionItems.length
                : activeTab === 'reviewQueue'
                  ? filteredReviewQueueItems.length
                  : activeTab === 'sourceVersions'
                    ? filteredSourceVersionItems.length
                    : filteredSourceItems.length
            }
          />

          {activeTab === 'ingestion' ? (
            <KnowledgeList
              items={ingestionListItems}
              selectedId={selectedIngestionId}
              onSelect={setSelectedIngestionId}
              loading={ingestionLoading}
              error={ingestionError}
              emptyMessage="No incoming items match the current search."
              selectableIds={selectedIngestionIds}
              onToggleSelection={(id) =>
                setSelectedIngestionIds((current) =>
                  current.includes(id)
                    ? current.filter((value) => value !== id)
                    : [...current, id]
                )
              }
            />
          ) : activeTab === 'reviewQueue' ? (
            <KnowledgeList
              items={reviewQueueListItems}
              selectedId={selectedIngestionId}
              onSelect={setSelectedIngestionId}
              loading={ingestionLoading}
              error={ingestionError}
              emptyMessage="No items are currently waiting for review."
              selectableIds={selectedIngestionIds}
              onToggleSelection={(id) =>
                setSelectedIngestionIds((current) =>
                  current.includes(id)
                    ? current.filter((value) => value !== id)
                    : [...current, id]
                )
              }
            />
          ) : activeTab === 'sourceVersions' ? (
            <KnowledgeSourceVersionList
              items={filteredSourceVersionItems}
              selectedId={selectedSourceVersionId}
              onSelect={setSelectedSourceVersionId}
              selectedIds={selectedSourceVersionIds}
              onToggleSelection={(id) =>
                setSelectedSourceVersionIds((current) =>
                  current.includes(id)
                    ? current.filter((value) => value !== id)
                    : [...current, id]
                )
              }
              loading={sourceVersionLoading}
              error={sourceVersionError}
            />
          ) : (
            <KnowledgeList
              items={sourceListItems}
              selectedId={selectedSourceId}
              onSelect={setSelectedSourceId}
              loading={sourceLoading}
              error={sourceError}
              emptyMessage="No sources match the current search."
            />
          )}
        </aside>

        <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <div className="border-b border-gray-100 bg-white px-4 py-4 sm:px-6">
            <div className="space-y-4">
              <div>
                <p className="text-xs font-medium uppercase tracking-wide text-gray-500">
                  Knowledge Base — Administrator workspace
                </p>
                <h1 className="mt-1 text-2xl font-semibold text-navy-900">
                  Knowledge Base
                </h1>
                <p className="mt-2 max-w-3xl text-sm text-gray-600">
                  Upload official-source documents, review and approve submitted items, publish
                  approved material, and manage the lifecycle of published sources. This workspace
                  is visible only to administrators.
                </p>
              </div>

              <KnowledgeTabs activeTab={activeTab} tabs={tabs} onChange={setActiveTab} />

              <div className="lg:hidden">
                <KnowledgeSearchBar
                  label={currentSearch.label}
                  placeholder={currentSearch.placeholder}
                  helperText={currentSearch.helperText}
                  query={queries[activeTab]}
                  onChange={(value) =>
                    setQueries((current) => ({ ...current, [activeTab]: value }))
                  }
                  resultCount={
                    activeTab === 'ingestion'
                      ? filteredIngestionItems.length
                      : activeTab === 'reviewQueue'
                        ? filteredReviewQueueItems.length
                        : activeTab === 'sourceVersions'
                          ? filteredSourceVersionItems.length
                          : filteredSourceItems.length
                  }
                />
              </div>
            </div>
          </div>

          <div className="flex-1 overflow-y-auto p-4 sm:p-6">
            {activeTab === 'ingestion' ? (
              <div className="grid gap-6 xl:grid-cols-[minmax(0,1.3fr)_minmax(340px,0.9fr)]">
                <KnowledgeDetailPanel
                  item={ingestionDetail}
                  loading={ingestionDetailLoading}
                  error={ingestionDetailError}
                  onCorrectMetadata={ingestionDetail ? handleMetadataCorrection : undefined}
                  metadataCorrectionBusy={metadataCorrectionBusy}
                />
                <div className="space-y-6">
                  <KnowledgeIntakeForm onSubmit={handleIntakeUrl} busy={intakeBusy} />
                  <KnowledgeActionBar
                    mode="ingestion"
                    item={ingestionDetail}
                    currentUserId={userId}
                    busyAction={
                      ingestionBusyAction === 'bulkReject' || ingestionBusyAction === 'bulkPublish'
                        ? null
                        : ingestionBusyAction
                    }
                    onReview={async (note) => {
                      if (!ingestionDetail || !userId || !note.trim()) return
                      setIngestionBusyAction('review')
                      try {
                        const updated = await knowledgeApi.reviewKnowledgeIngestionJob({
                          ingestionJobId: ingestionDetail.ingestion_job_id,
                          reviewedBy: userId,
                          note,
                        })
                        await refreshIngestionAfterAction(updated)
                        toast.success('Review note saved.')
                      } catch {
                        toast.error('Review update failed.')
                      } finally {
                        setIngestionBusyAction(null)
                      }
                    }}
                    onApprove={async (note) => {
                      if (!ingestionDetail || !userId || !note.trim() || !hasPublicationPayload(ingestionDetail)) {
                        return
                      }
                      setIngestionBusyAction('approve')
                      try {
                        const updated = await knowledgeApi.approveKnowledgeIngestionJob({
                          ingestionJobId: ingestionDetail.ingestion_job_id,
                          reviewedBy: userId,
                          note,
                          publicationPayload: ingestionDetail.proposed_source_record,
                        })
                        await refreshIngestionAfterAction(updated)
                        toast.success('Knowledge item approved for publication.')
                      } catch {
                        toast.error('Approve action failed.')
                      } finally {
                        setIngestionBusyAction(null)
                      }
                    }}
                    onReject={async (note) => {
                      if (!ingestionDetail || !userId || !note.trim()) return
                      setIngestionBusyAction('reject')
                      try {
                        const updated = await knowledgeApi.rejectKnowledgeIngestionJob({
                          ingestionJobId: ingestionDetail.ingestion_job_id,
                          reviewedBy: userId,
                          note,
                        })
                        await refreshIngestionAfterAction(updated)
                        toast.success('Knowledge item rejected.')
                      } catch {
                        toast.error('Reject action failed.')
                      } finally {
                        setIngestionBusyAction(null)
                      }
                    }}
                    onPublish={async () => {
                      if (!ingestionDetail || !userId) return
                      setIngestionBusyAction('publish')
                      try {
                        const updated = await knowledgeApi.publishKnowledgeIngestionJob({
                          ingestionJobId: ingestionDetail.ingestion_job_id,
                          publishedBy: userId,
                        })
                        await refreshIngestionAfterAction(updated)
                        toast.success('Knowledge item published.')
                      } catch {
                        toast.error('Publish action failed.')
                      } finally {
                        setIngestionBusyAction(null)
                      }
                    }}
                  />

                  <KnowledgeBulkActionBar
                    mode="ingestion"
                    selectedIds={selectedIngestionIds}
                    visibleIds={visibleSelectableIngestionIds}
                    busyAction={
                      ingestionBusyAction === 'bulkReject' || ingestionBusyAction === 'bulkPublish'
                        ? ingestionBusyAction
                        : null
                    }
                    result={ingestionBulkResult}
                    ingestionItems={ingestionItems}
                    onSelectAllVisible={() => setSelectedIngestionIds(visibleSelectableIngestionIds)}
                    onClearSelection={() => setSelectedIngestionIds([])}
                    onBulkReject={async (note) => {
                      if (!userId || !selectedIngestionIds.length || !note.trim()) return
                      setIngestionBusyAction('bulkReject')
                      try {
                        const result = await knowledgeApi.bulkRejectKnowledgeIngestionJobs({
                          actingUser: userId,
                          ids: selectedIngestionIds,
                          note,
                        })
                        setIngestionBulkResult(result)
                        setSelectedIngestionIds([])
                        await loadIngestionItems()
                        if (selectedIngestionId) {
                          await loadIngestionDetail(selectedIngestionId)
                        }
                        toast.success('Bulk reject completed.')
                      } catch {
                        toast.error('Bulk reject failed.')
                      } finally {
                        setIngestionBusyAction(null)
                      }
                    }}
                    onBulkPublish={async () => {
                      if (!userId || !selectedIngestionIds.length) return
                      setIngestionBusyAction('bulkPublish')
                      try {
                        const result = await knowledgeApi.bulkPublishKnowledgeIngestionJobs({
                          actingUser: userId,
                          ids: selectedIngestionIds,
                        })
                        setIngestionBulkResult(result)
                        setSelectedIngestionIds([])
                        await loadIngestionItems()
                        if (selectedIngestionId) {
                          await loadIngestionDetail(selectedIngestionId)
                        }
                        toast.success('Bulk publish completed.')
                      } catch {
                        toast.error('Bulk publish failed.')
                      } finally {
                        setIngestionBusyAction(null)
                      }
                    }}
                  />
                </div>
              </div>
            ) : activeTab === 'reviewQueue' ? (
              <div className="grid gap-6 xl:grid-cols-[minmax(0,1.3fr)_minmax(340px,0.9fr)]">
                <KnowledgeDetailPanel
                  item={ingestionDetail}
                  loading={ingestionDetailLoading}
                  error={ingestionDetailError}
                  onCorrectMetadata={ingestionDetail ? handleMetadataCorrection : undefined}
                  metadataCorrectionBusy={metadataCorrectionBusy}
                />
                <div className="space-y-6">
                  <KnowledgeActionBar
                    mode="ingestion"
                    item={ingestionDetail}
                    currentUserId={userId}
                    busyAction={
                      ingestionBusyAction === 'bulkReject' || ingestionBusyAction === 'bulkPublish'
                        ? null
                        : ingestionBusyAction
                    }
                    onReview={async (note) => {
                      if (!ingestionDetail || !userId || !note.trim()) return
                      setIngestionBusyAction('review')
                      try {
                        const updated = await knowledgeApi.reviewKnowledgeIngestionJob({
                          ingestionJobId: ingestionDetail.ingestion_job_id,
                          reviewedBy: userId,
                          note,
                        })
                        await refreshIngestionAfterAction(updated)
                        toast.success('Review note saved.')
                      } catch {
                        toast.error('Review update failed.')
                      } finally {
                        setIngestionBusyAction(null)
                      }
                    }}
                    onApprove={async (note) => {
                      if (!ingestionDetail || !userId || !note.trim() || !hasPublicationPayload(ingestionDetail)) {
                        return
                      }
                      setIngestionBusyAction('approve')
                      try {
                        const updated = await knowledgeApi.approveKnowledgeIngestionJob({
                          ingestionJobId: ingestionDetail.ingestion_job_id,
                          reviewedBy: userId,
                          note,
                          publicationPayload: ingestionDetail.proposed_source_record,
                        })
                        await refreshIngestionAfterAction(updated)
                        toast.success('Item approved for publication.')
                      } catch {
                        toast.error('Approve action failed.')
                      } finally {
                        setIngestionBusyAction(null)
                      }
                    }}
                    onReject={async (note) => {
                      if (!ingestionDetail || !userId || !note.trim()) return
                      setIngestionBusyAction('reject')
                      try {
                        const updated = await knowledgeApi.rejectKnowledgeIngestionJob({
                          ingestionJobId: ingestionDetail.ingestion_job_id,
                          reviewedBy: userId,
                          note,
                        })
                        await refreshIngestionAfterAction(updated)
                        toast.success('Item rejected.')
                      } catch {
                        toast.error('Reject action failed.')
                      } finally {
                        setIngestionBusyAction(null)
                      }
                    }}
                    onPublish={async () => {
                      if (!ingestionDetail || !userId) return
                      setIngestionBusyAction('publish')
                      try {
                        const updated = await knowledgeApi.publishKnowledgeIngestionJob({
                          ingestionJobId: ingestionDetail.ingestion_job_id,
                          publishedBy: userId,
                        })
                        await refreshIngestionAfterAction(updated)
                        toast.success('Item published.')
                      } catch {
                        toast.error('Publish action failed.')
                      } finally {
                        setIngestionBusyAction(null)
                      }
                    }}
                  />

                  <KnowledgeBulkActionBar
                    mode="ingestion"
                    selectedIds={selectedIngestionIds}
                    visibleIds={filteredReviewQueueItems
                      .filter((item) => getIngestionSelectionDisabledReason(item) === null)
                      .map((item) => item.ingestion_job_id)}
                    busyAction={
                      ingestionBusyAction === 'bulkReject' || ingestionBusyAction === 'bulkPublish'
                        ? ingestionBusyAction
                        : null
                    }
                    result={ingestionBulkResult}
                    ingestionItems={reviewQueueItems}
                    onSelectAllVisible={() =>
                      setSelectedIngestionIds(
                        filteredReviewQueueItems
                          .filter((item) => getIngestionSelectionDisabledReason(item) === null)
                          .map((item) => item.ingestion_job_id)
                      )
                    }
                    onClearSelection={() => setSelectedIngestionIds([])}
                    onBulkReject={async (note) => {
                      if (!userId || !selectedIngestionIds.length || !note.trim()) return
                      setIngestionBusyAction('bulkReject')
                      try {
                        const result = await knowledgeApi.bulkRejectKnowledgeIngestionJobs({
                          actingUser: userId,
                          ids: selectedIngestionIds,
                          note,
                        })
                        setIngestionBulkResult(result)
                        setSelectedIngestionIds([])
                        await loadIngestionItems()
                        if (selectedIngestionId) {
                          await loadIngestionDetail(selectedIngestionId)
                        }
                        toast.success('Bulk reject completed.')
                      } catch {
                        toast.error('Bulk reject failed.')
                      } finally {
                        setIngestionBusyAction(null)
                      }
                    }}
                    onBulkPublish={async () => {
                      if (!userId || !selectedIngestionIds.length) return
                      setIngestionBusyAction('bulkPublish')
                      try {
                        const result = await knowledgeApi.bulkPublishKnowledgeIngestionJobs({
                          actingUser: userId,
                          ids: selectedIngestionIds,
                        })
                        setIngestionBulkResult(result)
                        setSelectedIngestionIds([])
                        await loadIngestionItems()
                        if (selectedIngestionId) {
                          await loadIngestionDetail(selectedIngestionId)
                        }
                        toast.success('Bulk publish completed.')
                      } catch {
                        toast.error('Bulk publish failed.')
                      } finally {
                        setIngestionBusyAction(null)
                      }
                    }}
                  />
                </div>
              </div>
            ) : activeTab === 'sourceVersions' ? (
              <div className="grid gap-6 xl:grid-cols-[minmax(0,1.3fr)_minmax(340px,0.9fr)]">
                <KnowledgeSourceDetailPanel
                  mode="sourceVersion"
                  sourceVersionSummary={activeSourceVersionSummary}
                  sourceVersionDetail={sourceVersionDetail}
                  loading={sourceVersionDetailLoading}
                  error={sourceVersionDetailError}
                />
                <div className="space-y-6">
                  <KnowledgeActionBar
                    mode="sourceVersion"
                    item={activeSourceVersionSummary}
                    lifecycle={sourceVersionDetail}
                    busyAction={
                      sourceVersionBusyAction === 'bulkArchive'
                        ? null
                        : (sourceVersionBusyAction as 'archive' | 'supersede' | null)
                    }
                    publishedVersions={sourceVersionItems}
                    onArchive={async () => {
                      if (!activeSourceVersionSummary || !userId) return
                      setSourceVersionBusyAction('archive')
                      try {
                        const updated = await knowledgeApi.archiveKnowledgeSourceVersion({
                          sourceVersionId: activeSourceVersionSummary.source_version_id,
                          archivedBy: userId,
                        })
                        await refreshSourceVersionsAfterAction(updated)
                        toast.success('Source version archived.')
                      } catch {
                        toast.error('Archive action failed.')
                      } finally {
                        setSourceVersionBusyAction(null)
                      }
                    }}
                    onSupersede={async (successorId) => {
                      if (!activeSourceVersionSummary || !userId) return
                      setSourceVersionBusyAction('supersede')
                      try {
                        const updated = await knowledgeApi.supersedeKnowledgeSourceVersion({
                          sourceVersionId: activeSourceVersionSummary.source_version_id,
                          successorSourceVersionId: successorId,
                          supersededBy: userId,
                        })
                        await refreshSourceVersionsAfterAction(updated)
                        toast.success('Source version superseded.')
                      } catch {
                        toast.error('Supersede action failed.')
                      } finally {
                        setSourceVersionBusyAction(null)
                      }
                    }}
                  />

                  <KnowledgeBulkActionBar
                    mode="sourceVersions"
                    selectedIds={selectedSourceVersionIds}
                    visibleIds={visibleArchivableSourceVersionIds}
                    busyAction={sourceVersionBusyAction === 'bulkArchive' ? sourceVersionBusyAction : null}
                    result={sourceVersionBulkResult}
                    sourceVersionItems={sourceVersionItems}
                    onSelectAllVisible={() =>
                      setSelectedSourceVersionIds(visibleArchivableSourceVersionIds)
                    }
                    onClearSelection={() => setSelectedSourceVersionIds([])}
                    onBulkArchive={async () => {
                      if (!userId || !selectedSourceVersionIds.length) return
                      setSourceVersionBusyAction('bulkArchive')
                      try {
                        const result = await knowledgeApi.bulkArchiveKnowledgeSourceVersions({
                          actingUser: userId,
                          ids: selectedSourceVersionIds,
                        })
                        setSourceVersionBulkResult(result)
                        setSelectedSourceVersionIds([])
                        await refreshSourceVersionsAfterAction()
                        if (selectedSourceVersionId) {
                          await loadSourceVersionDetail(selectedSourceVersionId)
                        }
                        toast.success('Bulk archive completed.')
                      } catch {
                        toast.error('Bulk archive failed.')
                      } finally {
                        setSourceVersionBusyAction(null)
                      }
                    }}
                  />
                </div>
              </div>
            ) : (
              <KnowledgeSourceDetailPanel
                mode="source"
                sourceDetail={sourceDetail}
                loading={sourceDetailLoading}
                error={sourceDetailError}
              />
            )}
          </div>
        </div>
      </div>
    </AppShell>
  )
}
