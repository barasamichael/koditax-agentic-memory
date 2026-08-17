import { useMemo, useState } from 'react'
import { ChevronLeft, FileText, PenLine, Tag } from 'lucide-react'
import { useQueryClient, type QueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { EmptyState } from '@/components/shared/EmptyState'
import { StatusChip } from '@/components/shared/StatusChip'
import { DocumentDetailsLoadingState } from '@/components/documents/DocumentDetailsLoadingState'
import { DocumentAccessActions } from '@/components/documents/DocumentAccessActions'
import { DocumentLifecycleActions } from '@/components/documents/DocumentLifecycleActions'
import { DocumentRenameModal } from '@/components/documents/DocumentRenameModal'
import { documentQueryKeys } from '@/lib/documents/document-query-keys'
import { getDocumentErrorMessage, DOCUMENT_ERROR_MESSAGES } from '@/lib/documents/document-errors'
import { normalizeDocumentRecord } from '@/lib/documents/document-lifecycle'
import { formatDocumentDate, formatFileTypeLabel } from '@/lib/documents/document-formatters'
import { normalizeError } from '@/lib/errorNormalizer'
import { useDocument, useRenameDocument } from '@/hooks/useDocuments'
import { useToast } from '@/components/shared/Toast'
import type { DocumentRecordEnvelope, DocumentListEnvelope } from '@/types/document'

function isDocumentListEnvelope(data: unknown): data is DocumentListEnvelope {
  return typeof data === 'object' && data !== null && 'documents' in data
}

function isDocumentRecordEnvelope(data: unknown): data is DocumentRecordEnvelope {
  return typeof data === 'object' && data !== null && 'document' in data && !('documents' in data)
}

function getCachedDocumentEnvelope(
  queryClient: QueryClient,
  documentId: string,
): DocumentRecordEnvelope | undefined {
  const cachedQueries = queryClient.getQueriesData({ queryKey: documentQueryKeys.all })
  for (const [, data] of cachedQueries) {
    if (!data) continue
    if (isDocumentRecordEnvelope(data) && data.document.document_id === documentId) {
      return data
    }
    if (isDocumentListEnvelope(data)) {
      const match = data.documents.find((item) => item.document_id === documentId)
      if (match) {
        return {
          status: 'ok',
          document: match,
          traceability: {
            trace_id: null,
            correlation_id: null,
          },
        }
      }
    }
  }
  return undefined
}

interface DocumentDetailsViewProps {
  documentId: string
}

export function DocumentDetailsView({ documentId }: DocumentDetailsViewProps) {
  const navigate = useNavigate()
  const toast = useToast()
  const queryClient = useQueryClient()
  const [renameOpen, setRenameOpen] = useState(false)
  const [renameError, setRenameError] = useState<string | null>(null)

  const placeholderData = useMemo(
    () => getCachedDocumentEnvelope(queryClient, documentId),
    [documentId, queryClient],
  )

  const documentQuery = useDocument(documentId, { placeholderData })
  const renameMutation = useRenameDocument(documentId)

  const hasAuthoritativeData = Boolean(documentQuery.data && !documentQuery.isPlaceholderData)
  const documentRecord = documentQuery.data?.document ?? null
  const viewModel = documentRecord ? normalizeDocumentRecord(documentRecord) : null

  const detailError = useMemo(() => {
    if (!documentQuery.isError) return null
    const normalized = normalizeError(documentQuery.error)
    if (normalized.error_code === 'document_access_denied') {
      return {
        title: 'Access denied',
        description: DOCUMENT_ERROR_MESSAGES.document_access_denied,
      }
    }
    return {
      title: 'Document not found',
      description: 'This document may have been removed or is no longer available.',
    }
  }, [documentQuery.error, documentQuery.isError])

  const handleRename = async (displayName: string) => {
    if (!documentRecord) return
    setRenameError(null)
    try {
      await renameMutation.mutateAsync({
        display_name: displayName,
        expected_revision: documentRecord.revision,
      })
      toast.success('Document renamed.')
      setRenameOpen(false)
    } catch (error) {
      const normalized = normalizeError(error)
      setRenameError(getDocumentErrorMessage(error))
      if (normalized.error_code === 'stale_document_revision') {
        void documentQuery.refetch()
      }
    }
  }

  if (documentQuery.isPending && !documentRecord) {
    return (
      <div className="flex flex-1 flex-col overflow-hidden p-4 sm:p-6">
        <DocumentDetailsLoadingState />
      </div>
    )
  }

  if (documentQuery.isError && !hasAuthoritativeData) {
    return (
      <div className="flex flex-1 flex-col overflow-hidden p-4 sm:p-6">
        <div className="mb-4">
          <button
            type="button"
            onClick={() => navigate('/documents')}
            className={[
              'inline-flex items-center gap-2 rounded-lg border border-gray-200 px-3 py-2',
              'text-sm font-medium text-gray-700 transition-all hover:bg-gray-50',
            ].join(' ')}
          >
            <ChevronLeft className="h-4 w-4" aria-hidden="true" />
            Back to documents
          </button>
        </div>
        <EmptyState
          title={detailError?.title ?? 'Document not found'}
          description={
            detailError?.description ??
            'This document may have been removed or is no longer available.'
          }
          icon={<FileText className="h-10 w-10" aria-hidden="true" />}
          action={{ label: 'Try again', onClick: () => void documentQuery.refetch() }}
        />
      </div>
    )
  }

  if (!viewModel || !documentRecord) {
    return null
  }

  const fileTypeLabel = formatFileTypeLabel(viewModel.fileExtension)
  const canRename = viewModel.availableActions.canRename
  const canOpen = viewModel.availableActions.canOpen
  const canDownload = viewModel.availableActions.canDownload
  const accessAvailabilityMessage = viewModel.statusDescription
  const savedLabel = viewModel.isSaved ? 'Saved to your library' : 'Removed from your library'
  const nextStepLabel = !viewModel.isSaved
    ? 'This document is no longer available'
    : viewModel.requiresUserAction
      ? 'Action needed before this document can be used'
      : 'No action needed right now'

  return (
    <div className="flex flex-1 flex-col overflow-hidden p-4 sm:p-6">
      <div className="mb-4 flex items-center justify-between gap-3">
        <button
          type="button"
          onClick={() => navigate('/documents')}
          className={[
            'inline-flex items-center gap-2 rounded-lg border border-gray-200 px-3 py-2',
            'text-sm font-medium text-gray-700 transition-all hover:bg-gray-50',
          ].join(' ')}
        >
          <ChevronLeft className="h-4 w-4" aria-hidden="true" />
          Back to documents
        </button>
        <span className="text-xs font-medium uppercase tracking-wide text-gray-500">
          Document details
        </span>
      </div>

      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-4xl space-y-5">
          <section className="rounded-3xl border border-gray-200 bg-white p-6 shadow-sm">
            <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
              <div className="min-w-0">
                <p className="text-[11px] font-semibold uppercase tracking-[0.28em] text-navy-600">
                  Document
                </p>
                <h1 className="mt-2 truncate text-2xl font-semibold text-gray-900">
                  {viewModel.displayName}
                </h1>
                <div className="mt-3 flex flex-wrap items-center gap-2">
                  <StatusChip status={viewModel.status} label={viewModel.statusLabel} />
                  <span className="text-sm text-gray-600">
                    {viewModel.statusDescription}
                  </span>
                </div>
                <div className="mt-4 grid gap-3 sm:grid-cols-2">
                  <div className="rounded-2xl border border-gray-100 bg-gray-50 px-4 py-3">
                    <p className="text-xs font-medium uppercase tracking-wide text-gray-500">
                      Saved state
                    </p>
                    <p className="mt-1 text-sm text-gray-700">{savedLabel}</p>
                  </div>
                  <div className="rounded-2xl border border-gray-100 bg-gray-50 px-4 py-3">
                    <p className="text-xs font-medium uppercase tracking-wide text-gray-500">
                      Next step
                    </p>
                    <p className="mt-1 text-sm text-gray-700">{nextStepLabel}</p>
                  </div>
                </div>
              </div>

              {canRename ? (
                <button
                  type="button"
                  onClick={() => setRenameOpen(true)}
                  className={[
                    'inline-flex items-center gap-2 rounded-lg border border-navy-200 bg-navy-50 px-3 py-2',
                    'text-sm font-medium text-navy-800 transition-all hover:bg-navy-100',
                  ].join(' ')}
                >
                  <PenLine className="h-4 w-4" aria-hidden="true" />
                  Rename
                </button>
              ) : null}
            </div>
          </section>

          <section className="grid gap-5 lg:grid-cols-[minmax(0,1.3fr)_minmax(0,0.7fr)]">
            <div className="space-y-5 rounded-3xl border border-gray-200 bg-white p-6 shadow-sm">
              <div>
                <p className="text-xs font-medium uppercase tracking-wide text-gray-500">
                  File details
                </p>
                <dl className="mt-4 grid gap-4 sm:grid-cols-2">
                  <DetailRow label="Document name" value={viewModel.displayName} />
                  <DetailRow label="File type" value={fileTypeLabel} />
                  <DetailRow label="Date added" value={formatDocumentDate(viewModel.addedAt) ?? 'Unknown'} />
                  {viewModel.category ? <DetailRow label="Category" value={viewModel.category} /> : null}
                </dl>
              </div>

              {viewModel.tags.length > 0 ? (
                <div>
                  <p className="flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-gray-500">
                    <Tag className="h-3.5 w-3.5" aria-hidden="true" />
                    Tags
                  </p>
                  <div className="mt-3 flex flex-wrap gap-2">
                    {viewModel.tags.map((tag) => (
                      <span
                        key={tag}
                        className="rounded-full bg-gray-100 px-3 py-1 text-xs text-gray-700"
                      >
                        {tag}
                      </span>
                    ))}
                  </div>
                </div>
              ) : null}

              {viewModel.description ? (
                <div>
                  <p className="text-xs font-medium uppercase tracking-wide text-gray-500">
                    Notes
                  </p>
                  <p className="mt-2 text-sm text-gray-700">{viewModel.description}</p>
                </div>
              ) : null}
            </div>

            <div className="space-y-5 rounded-3xl border border-gray-200 bg-white p-6 shadow-sm">
              <div>
                <p className="text-xs font-medium uppercase tracking-wide text-gray-500">
                  Readiness
                </p>
                <div className="mt-3 flex items-center gap-2">
                  <StatusChip status={viewModel.status} label={viewModel.statusLabel} />
                </div>
                <p className="mt-3 text-sm text-gray-600">
                  {viewModel.statusDescription}
                </p>
              </div>

              <div>
                <p className="text-xs font-medium uppercase tracking-wide text-gray-500">
                  Open and download
                </p>
                <p className="mt-3 text-sm text-gray-600">
                  Open this document in your browser or download the original file.
                </p>
                <DocumentAccessActions
                  documentId={documentRecord.document_id}
                  displayName={viewModel.displayName}
                  canOpen={canOpen}
                  canDownload={canDownload}
                  availabilityMessage={accessAvailabilityMessage}
                  className="mt-3"
                />
                <div className="mt-4 rounded-2xl border border-gray-100 bg-gray-50 px-4 py-3 text-sm text-gray-600">
                  {canRename
                    ? 'Rename is available for this document.'
                    : 'Rename is not available in the document’s current state.'}
                </div>
                <div className="mt-4 rounded-2xl border border-gray-100 bg-gray-50 px-4 py-3">
                <p className="text-xs font-medium uppercase tracking-wide text-gray-500">
                  Trash, restore, and delete permanently
                </p>
                <p className="mt-2 text-sm text-gray-600">
                    Move a document to trash when you want to set it aside. If the document is ready, you can also delete it permanently.
                </p>
                <DocumentLifecycleActions
                  document={viewModel}
                  className="mt-3"
                  onDeleted={() => navigate('/documents/trash')}
                />
              </div>
              </div>
            </div>
          </section>
        </div>
      </div>

      <DocumentRenameModal
        open={renameOpen}
        onOpenChange={(open) => {
          setRenameOpen(open)
          if (!open) {
            setRenameError(null)
          }
        }}
        currentName={viewModel.displayName}
        loading={renameMutation.isPending}
        onRename={(displayName) => void handleRename(displayName)}
        errorMessage={renameError}
      />
    </div>
  )
}

function DetailRow({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs font-medium uppercase tracking-wide text-gray-500">{label}</dt>
      <dd className="mt-1 text-sm text-gray-900">{value}</dd>
    </div>
  )
}
