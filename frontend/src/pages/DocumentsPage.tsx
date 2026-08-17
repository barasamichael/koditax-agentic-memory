import { useState } from 'react'
import { FolderOpen, RefreshCw, Trash2 } from 'lucide-react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import { AppShell } from '@/components/layout/AppShell'
import { UploadZone } from '@/components/documents/UploadZone'
import { DocumentDetailsView } from '@/components/documents/DocumentDetailsView'
import { DocumentsEmptyState } from '@/components/documents/library/DocumentsEmptyState'
import { DocumentsErrorState } from '@/components/documents/library/DocumentsErrorState'
import { DocumentsList } from '@/components/documents/library/DocumentsList'
import { DocumentsLoadingState } from '@/components/documents/library/DocumentsLoadingState'
import { DocumentsTrashEmptyState } from '@/components/documents/library/DocumentsTrashEmptyState'
import { Spinner } from '@/components/shared/Spinner'
import { useDocumentViewModels } from '@/hooks/useDocuments'
import { getDocumentErrorMessage } from '@/lib/documents/document-errors'

export default function DocumentsPage() {
  const { documentId } = useParams()
  const location = useLocation()

  if (location.pathname.startsWith('/documents/trash')) {
    return <DocumentsTrashRoute />
  }

  if (documentId) {
    return <DocumentDetailsRoute documentId={documentId} />
  }

  return <DocumentsLibraryRoute />
}

function DocumentsLibraryRoute() {
  const navigate = useNavigate()
  const [uploadOpen, setUploadOpen] = useState(false)
  const {
    documentViews,
    isLoading,
    isFetching,
    isError,
    error,
    refetch,
  } = useDocumentViewModels()

  const hasDocuments = documentViews.length > 0
  const showInitialError = isError && !hasDocuments
  const showRefreshError = isError && hasDocuments

  return (
    <AppShell>
      <div className="flex flex-1 flex-col overflow-hidden">
        <div className="shrink-0 border-b border-gray-100 bg-white px-4 py-4 sm:px-6">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
            <div className="min-w-0">
              <p className="text-[11px] font-medium uppercase tracking-wide text-gray-500">
                Document library
              </p>
              <h1 className="mt-1 text-lg font-semibold text-gray-900">Documents</h1>
              <p className="mt-1 max-w-2xl text-sm text-gray-500">
                View and manage your saved documents.
              </p>
              <p className="mt-2 text-xs text-gray-500">
                This view shows saved documents and their current readiness.
              </p>
            </div>

            <div className="flex shrink-0 flex-col gap-2 self-start sm:items-end">
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => navigate('/documents/trash')}
                  className={[
                    'inline-flex items-center gap-2 rounded-lg border border-gray-200 bg-white px-3 py-2',
                    'text-sm font-medium text-gray-700 transition-all hover:bg-gray-50',
                  ].join(' ')}
                >
                  <Trash2 className="h-4 w-4" aria-hidden="true" />
                  Trash
                </button>
                <button
                  type="button"
                  onClick={() => setUploadOpen(true)}
                  className={[
                    'inline-flex items-center gap-2 rounded-lg border border-navy-200 bg-navy-50 px-3 py-2',
                    'text-sm font-medium text-navy-800 transition-all hover:bg-navy-100',
                  ].join(' ')}
                >
                  Add document
                </button>
                <button
                  type="button"
                  onClick={() => refetch()}
                  className={[
                    'inline-flex items-center gap-2 rounded-lg border border-gray-200 px-3 py-2',
                    'text-sm font-medium text-gray-700 transition-all hover:bg-gray-50',
                    'disabled:cursor-not-allowed disabled:opacity-60',
                  ].join(' ')}
                  disabled={isFetching}
                  aria-label="Refresh documents"
                >
                  {isFetching ? (
                    <Spinner size="sm" className="h-4 w-4" />
                  ) : (
                    <RefreshCw className="h-4 w-4" aria-hidden="true" />
                  )}
                  Refresh
                </button>
              </div>
              <span
                className={[
                  'inline-flex items-center gap-2 self-start rounded-full border border-gray-200',
                  'bg-gray-50 px-2.5 py-1 text-xs font-medium text-gray-700',
                ].join(' ')}
              >
                {documentViews.length} document{documentViews.length === 1 ? '' : 's'}
              </span>
            </div>
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-gray-500">
            {isFetching && !isLoading ? (
              <span className="inline-flex items-center gap-1">
                <Spinner size="sm" className="h-3 w-3" />
                Refreshing documents
              </span>
            ) : null}
          </div>
        </div>

        <div className="flex flex-1 overflow-hidden">
          <div className="flex-1 overflow-y-auto p-4 sm:p-6">
            {isLoading && <DocumentsLoadingState />}

            {showInitialError && !isLoading ? (
              <DocumentsErrorState
                message={getDocumentErrorMessage(error)}
                onRetry={() => refetch()}
              />
            ) : null}

            {showRefreshError && !isLoading ? (
              <div
                className={[
                  'mb-4 rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3',
                  'text-sm text-amber-900',
                ].join(' ')}
              >
                <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                  <div>
                    <p className="font-medium">The latest update could not be loaded.</p>
                    <p className="mt-1 text-sm text-amber-800">
                      {getDocumentErrorMessage(error)}
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={() => refetch()}
                    className={[
                      'inline-flex items-center gap-2 rounded-lg border',
                      'border-amber-300 bg-white px-3 py-2',
                      'text-sm font-medium text-amber-900 transition-all hover:bg-amber-100',
                    ].join(' ')}
                  >
                    Try again
                  </button>
                </div>
              </div>
            ) : null}

            {!isLoading && !isError && documentViews.length === 0 ? (
              <DocumentsEmptyState onAddDocument={() => setUploadOpen(true)} />
            ) : null}

            {!isLoading && documentViews.length > 0 ? (
              <DocumentsList documents={documentViews} />
            ) : null}
          </div>
        </div>
      </div>
      <UploadZone open={uploadOpen} onOpenChange={setUploadOpen} />
    </AppShell>
  )
}

function DocumentsTrashRoute() {
  const navigate = useNavigate()
  const {
    documentViews,
    isLoading,
    isFetching,
    isError,
    error,
    refetch,
  } = useDocumentViewModels({ state: 'trashed' })

  const hasDocuments = documentViews.length > 0
  const showInitialError = isError && !hasDocuments
  const showRefreshError = isError && hasDocuments

  return (
    <AppShell>
      <div className="flex flex-1 flex-col overflow-hidden">
        <div className="shrink-0 border-b border-gray-100 bg-white px-4 py-4 sm:px-6">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
            <div className="min-w-0">
              <p className="text-[11px] font-medium uppercase tracking-wide text-gray-500">
                Document trash
              </p>
              <h1 className="mt-1 text-lg font-semibold text-gray-900">Trash</h1>
              <p className="mt-1 max-w-2xl text-sm text-gray-500">
                Review documents you moved to trash and restore anything you still need.
              </p>
              <p className="mt-2 text-xs text-gray-500">
                Trashed documents stay separate from the ordinary library until restored.
              </p>
            </div>

            <div className="flex shrink-0 items-center gap-2 self-start">
              <button
                type="button"
                onClick={() => navigate('/documents')}
                className={[
                  'inline-flex items-center gap-2 rounded-lg border border-gray-200 bg-white px-3 py-2',
                  'text-sm font-medium text-gray-700 transition-all hover:bg-gray-50',
                ].join(' ')}
              >
                <FolderOpen className="h-4 w-4" aria-hidden="true" />
                Documents
              </button>
              <button
                type="button"
                onClick={() => refetch()}
                className={[
                  'inline-flex items-center gap-2 rounded-lg border border-gray-200 px-3 py-2',
                  'text-sm font-medium text-gray-700 transition-all hover:bg-gray-50',
                  'disabled:cursor-not-allowed disabled:opacity-60',
                ].join(' ')}
                disabled={isFetching}
                aria-label="Refresh trash"
              >
                {isFetching ? (
                  <Spinner size="sm" className="h-4 w-4" />
                ) : (
                  <RefreshCw className="h-4 w-4" aria-hidden="true" />
                )}
                Refresh
              </button>
            </div>
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-gray-500">
            <span className="rounded-full bg-gray-100 px-2.5 py-1 font-medium text-gray-700">
              {documentViews.length} document{documentViews.length === 1 ? '' : 's'}
            </span>
            {isFetching && !isLoading ? (
              <span className="inline-flex items-center gap-1">
                <Spinner size="sm" className="h-3 w-3" />
                Refreshing trash
              </span>
            ) : null}
          </div>
        </div>

        <div className="flex flex-1 overflow-hidden">
          <div className="flex-1 overflow-y-auto p-4 sm:p-6">
            {isLoading && <DocumentsLoadingState />}

            {showInitialError && !isLoading ? (
              <DocumentsErrorState
                title="We could not load trash"
                message={getDocumentErrorMessage(error)}
                onRetry={() => refetch()}
              />
            ) : null}

            {showRefreshError && !isLoading ? (
              <div
                className={[
                  'mb-4 rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3',
                  'text-sm text-amber-900',
                ].join(' ')}
              >
                <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                  <div>
                    <p className="font-medium">The latest trash update could not be loaded.</p>
                    <p className="mt-1 text-sm text-amber-800">
                      {getDocumentErrorMessage(error)}
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={() => refetch()}
                    className={[
                      'inline-flex items-center gap-2 rounded-lg border',
                      'border-amber-300 bg-white px-3 py-2',
                      'text-sm font-medium text-amber-900 transition-all hover:bg-amber-100',
                    ].join(' ')}
                  >
                    Try again
                  </button>
                </div>
              </div>
            ) : null}

            {!isLoading && !isError && documentViews.length === 0 ? (
              <DocumentsTrashEmptyState onBackToDocuments={() => navigate('/documents')} />
            ) : null}

            {!isLoading && documentViews.length > 0 ? (
              <DocumentsList documents={documentViews} />
            ) : null}
          </div>
        </div>
      </div>
    </AppShell>
  )
}

function DocumentDetailsRoute({ documentId }: { documentId: string }) {
  return (
    <AppShell>
      <DocumentDetailsView documentId={documentId} />
    </AppShell>
  )
}
