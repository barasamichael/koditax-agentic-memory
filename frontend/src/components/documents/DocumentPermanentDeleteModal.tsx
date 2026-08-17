import { useEffect, useState } from 'react'
import { X } from 'lucide-react'
import { usePurgeDocument, usePurgeDryRun } from '@/hooks/useDocuments'
import { useToast } from '@/components/shared/Toast'
import { Spinner } from '@/components/shared/Spinner'
import { cn } from '@/lib/utils'
import { DOCUMENT_DELETE_BLOCKER_MESSAGES, getDocumentErrorMessage } from '@/lib/documents/document-errors'
import type { PurgeDryRunBlocker } from '@/types/document'

interface DocumentPermanentDeleteModalProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  documentId: string
  displayName: string
  onDeleted?: () => void
}

export function DocumentPermanentDeleteModal({
  open,
  onOpenChange,
  documentId,
  displayName,
  onDeleted,
}: DocumentPermanentDeleteModalProps) {
  const toast = useToast()
  const [confirmValue, setConfirmValue] = useState('')
  const dryRunQuery = usePurgeDryRun(documentId, open)
  const purgeMutation = usePurgeDocument(documentId)

  useEffect(() => {
    if (!open) {
      setConfirmValue('')
    }
  }, [open])

  if (!open) {
    return null
  }

  const blockers = dryRunQuery.data?.blockers ?? []
  const purgeReady = dryRunQuery.data?.purge_ready ?? false
  const isChecking = dryRunQuery.isLoading || dryRunQuery.isFetching
  const confirmEnabled =
    purgeReady &&
    confirmValue.trim().toUpperCase() === 'DELETE' &&
    !purgeMutation.isPending &&
    !isChecking

  const handleDelete = async () => {
    if (!confirmEnabled) return
    try {
      await purgeMutation.mutateAsync({})
      toast.success('Document removed from Trash.')
      onOpenChange(false)
      onDeleted?.()
    } catch (error) {
      toast.error(getDocumentErrorMessage(error, { action: 'delete_permanently' }))
    }
  }

  const renderBlocker = (blocker: PurgeDryRunBlocker) => (
    <li key={blocker}>{DOCUMENT_DELETE_BLOCKER_MESSAGES[blocker] ?? blocker}</li>
  )

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="delete-permanently-title"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) {
          onOpenChange(false)
        }
      }}
    >
      <div className="w-full max-w-lg rounded-2xl bg-white p-6 shadow-xl">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 id="delete-permanently-title" className="text-base font-semibold text-gray-900">
              Delete permanently?
            </h2>
            <p className="mt-1 text-sm text-gray-600">
              {displayName} and its prepared content will be permanently removed and cannot be restored.
            </p>
          </div>
          <button
            type="button"
            onClick={() => onOpenChange(false)}
            className="text-gray-400 transition-colors hover:text-gray-600"
            aria-label="Close"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="mt-5 space-y-4">
          <div className="rounded-2xl border border-red-100 bg-red-50 px-4 py-3 text-sm text-red-900">
            This action is permanent. The operation may continue after you leave this page.
          </div>

          {isChecking ? (
            <div className="rounded-2xl border border-gray-200 bg-gray-50 px-4 py-3 text-sm text-gray-700">
              <div className="flex items-center gap-2">
                <Spinner size="sm" className="h-4 w-4" />
                Checking whether this document can be deleted permanently...
              </div>
            </div>
          ) : dryRunQuery.isError ? (
            <div className="rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
              <p className="font-medium">We could not check deletion readiness.</p>
              <p className="mt-1 text-sm text-amber-800">
                {getDocumentErrorMessage(dryRunQuery.error, { action: 'delete_permanently' })}
              </p>
              <button
                type="button"
                onClick={() => void dryRunQuery.refetch()}
                className="mt-3 rounded-lg border border-amber-300 bg-white px-3 py-2 text-sm font-medium text-amber-900 transition-all hover:bg-amber-100"
              >
                Check again
              </button>
            </div>
          ) : purgeReady ? (
            <div className="rounded-2xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-900">
              This document is ready for permanent deletion.
            </div>
          ) : (
            <div className="rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
              <p className="font-medium">This document cannot be deleted permanently yet.</p>
              <ul className="mt-2 list-inside list-disc space-y-1 text-amber-800">
                {blockers.map(renderBlocker)}
              </ul>
            </div>
          )}

          <div className="rounded-2xl border border-gray-100 bg-gray-50 px-4 py-3">
            <label className="block text-xs font-medium uppercase tracking-wide text-gray-500">
              Type DELETE to confirm
            </label>
            <input
              value={confirmValue}
              onChange={(event) => setConfirmValue(event.target.value)}
              disabled={!purgeReady || purgeMutation.isPending}
              className={cn(
                'mt-2 w-full rounded-lg border px-3 py-2 text-sm outline-none transition-all',
                'focus:border-navy-500 focus:ring-1 focus:ring-navy-500',
                !purgeReady || purgeMutation.isPending
                  ? 'cursor-not-allowed border-gray-200 bg-gray-100 text-gray-400'
                  : 'border-gray-200 bg-white text-gray-900',
              )}
              placeholder='DELETE'
            />
          </div>
        </div>

        <div className="mt-6 flex items-center justify-end gap-3">
          <button
            type="button"
            onClick={() => onOpenChange(false)}
            disabled={purgeMutation.isPending}
            className="rounded-lg border border-gray-200 bg-white px-4 py-2 text-sm font-medium text-gray-700 transition-all hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-60"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void handleDelete()}
            disabled={!confirmEnabled}
            className={cn(
              'inline-flex items-center gap-2 rounded-lg border px-4 py-2 text-sm font-medium transition-all',
              confirmEnabled
                ? 'border-red-600 bg-red-600 text-white hover:bg-red-700'
                : 'cursor-not-allowed border-red-200 bg-red-300 text-white',
            )}
          >
            {purgeMutation.isPending ? <Spinner size="sm" className="h-4 w-4" /> : null}
            Delete permanently
          </button>
        </div>
      </div>
    </div>
  )
}
