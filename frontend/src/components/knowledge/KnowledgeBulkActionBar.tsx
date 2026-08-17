import { useMemo, useState } from 'react'
import type {
  KnowledgeBulkActionResult,
  KnowledgeIngestionSummary,
  KnowledgeSourceVersionSummary,
} from '@/types/knowledge'
import { labelForBulkItemStatus, labelForBulkStatus } from '@/lib/knowledgeStateLabels'

type BulkMode = 'ingestion' | 'sourceVersions'
type BusyAction = 'bulkReject' | 'bulkPublish' | 'bulkArchive' | null

interface KnowledgeBulkActionBarProps {
  mode: BulkMode
  selectedIds: string[]
  visibleIds: string[]
  busyAction: BusyAction
  result: KnowledgeBulkActionResult | null
  onSelectAllVisible: () => void
  onClearSelection: () => void
  onBulkReject?: (note: string) => Promise<void>
  onBulkPublish?: () => Promise<void>
  onBulkArchive?: () => Promise<void>
  ingestionItems?: KnowledgeIngestionSummary[]
  sourceVersionItems?: KnowledgeSourceVersionSummary[]
}

export function KnowledgeBulkActionBar({
  mode,
  selectedIds,
  visibleIds,
  busyAction,
  result,
  onSelectAllVisible,
  onClearSelection,
  onBulkReject,
  onBulkPublish,
  onBulkArchive,
  ingestionItems = [],
  sourceVersionItems = [],
}: KnowledgeBulkActionBarProps) {
  const [note, setNote] = useState('')
  const [confirmPublish, setConfirmPublish] = useState(false)
  const [confirmArchive, setConfirmArchive] = useState(false)

  const support = useMemo(() => {
    if (mode === 'ingestion') {
      const selectedItems = ingestionItems.filter((item) =>
        selectedIds.includes(item.ingestion_job_id)
      )
      const rejectableCount = selectedItems.filter((item) =>
        ['uploaded', 'review_pending', 'approved_for_publication'].includes(item.ingestion_state)
      ).length
      const publishableCount = selectedItems.filter(
        (item) => item.ingestion_state === 'approved_for_publication'
      ).length

      return { rejectableCount, publishableCount, archivableCount: 0 }
    }

    const selectedItems = sourceVersionItems.filter((item) =>
      selectedIds.includes(item.source_version_id)
    )
    const archivableCount = selectedItems.filter(
      (item) =>
        item.publication_state === 'published' || item.publication_state === 'superseded'
    ).length

    return { rejectableCount: 0, publishableCount: 0, archivableCount }
  }, [ingestionItems, mode, selectedIds, sourceVersionItems])

  // Build a human-readable title for a result item by matching it back to the
  // source list, falling back to a plain "Item N" label so raw IDs are never
  // shown to admins.
  const itemTitleFor = (id: string, index: number): string => {
    if (mode === 'ingestion') {
      const match = ingestionItems.find((item) => item.ingestion_job_id === id)
      return match?.source_input_ref ?? `Item ${index + 1}`
    }
    const match = sourceVersionItems.find((item) => item.source_version_id === id)
    return match?.title ?? `Item ${index + 1}`
  }

  const isBusy = busyAction !== null

  return (
    <div className="space-y-4 rounded-3xl border border-gray-200 bg-white p-6 shadow-card">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-gray-500">
            Bulk lifecycle actions
          </p>
          <p className="mt-1 text-sm text-gray-600">
            Apply actions to all selected items at once. Only eligible items in the selection will
            be affected.
          </p>
        </div>

        <div className="flex flex-wrap gap-2">
          <button
            onClick={onSelectAllVisible}
            disabled={visibleIds.length === 0 || isBusy}
            className="rounded-xl border border-gray-200 px-3 py-2 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Select visible
          </button>
          <button
            onClick={() => {
              onClearSelection()
              setConfirmPublish(false)
              setConfirmArchive(false)
            }}
            disabled={selectedIds.length === 0 || isBusy}
            className="rounded-xl border border-gray-200 px-3 py-2 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Clear selection
          </button>
        </div>
      </div>

      <div className="rounded-2xl bg-gray-50 p-4 text-sm text-gray-700">
        {selectedIds.length === 0 ? (
          <p className="text-gray-500">No items selected. Use the checkboxes in the list to select items.</p>
        ) : (
          <>
            <p>
              Selected: <span className="font-semibold text-gray-900">{selectedIds.length}</span> item{selectedIds.length === 1 ? '' : 's'}
            </p>
            {mode === 'ingestion' ? (
              <p className="mt-1 text-xs text-gray-500">
                {support.rejectableCount} eligible for reject
                {support.publishableCount > 0
                  ? `, ${support.publishableCount} eligible for publish`
                  : ''}
                {support.rejectableCount === 0 && support.publishableCount === 0
                  ? ' — none are eligible for bulk actions in their current states'
                  : ''}
              </p>
            ) : (
              <p className="mt-1 text-xs text-gray-500">
                {support.archivableCount > 0
                  ? `${support.archivableCount} eligible for archive`
                  : 'None of the selected versions are eligible for archive in their current states'}
              </p>
            )}
          </>
        )}
      </div>

      {mode === 'ingestion' ? (
        <div>
          <label className="block text-xs font-medium uppercase tracking-wide text-gray-500">
            Bulk reject note <span className="text-red-500">*</span>
          </label>
          <textarea
            value={note}
            onChange={(event) => {
              setNote(event.target.value)
            }}
            rows={3}
            placeholder="Record the shared governance reason for rejecting all selected items..."
            className="mt-2 w-full rounded-xl border border-gray-200 px-3 py-2 text-sm text-gray-800 placeholder-gray-400 focus:border-transparent focus:outline-none focus-visible:ring-2 focus-visible:ring-navy-500"
            disabled={isBusy}
          />
        </div>
      ) : null}

      {/* Bulk publish confirmation */}
      {mode === 'ingestion' && support.publishableCount > 0 && !confirmPublish ? (
        <p className="text-xs text-gray-500">
          Bulk publish will make {support.publishableCount} approved item{support.publishableCount === 1 ? '' : 's'} searchable immediately. This cannot be undone through this dashboard.
        </p>
      ) : null}

      {mode === 'ingestion' && confirmPublish ? (
        <div className="rounded-2xl border border-green-200 bg-green-50 p-3 text-sm text-green-900">
          <p className="font-medium">Confirm bulk publish</p>
          <p className="mt-1 text-green-800">
            Publishing {support.publishableCount} approved item{support.publishableCount === 1 ? '' : 's'} will make them searchable to users immediately. This step cannot be undone through this dashboard.
          </p>
        </div>
      ) : null}

      {/* Bulk archive confirmation */}
      {mode === 'sourceVersions' && support.archivableCount > 0 && !confirmArchive ? (
        <p className="text-xs text-gray-500">
          Bulk archive will remove {support.archivableCount} source version{support.archivableCount === 1 ? '' : 's'} from active knowledge results. Archived versions remain visible here for audit purposes.
        </p>
      ) : null}

      {mode === 'sourceVersions' && confirmArchive ? (
        <div className="rounded-2xl border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
          <p className="font-medium">Confirm bulk archive</p>
          <p className="mt-1 text-amber-800">
            Archiving {support.archivableCount} source version{support.archivableCount === 1 ? '' : 's'} will remove them from user answers. They remain visible here for audit purposes.
          </p>
        </div>
      ) : null}

      <div className="flex flex-wrap gap-3">
        {mode === 'ingestion' ? (
          <>
            <button
              onClick={async () => {
                if (!onBulkReject) return
                setConfirmPublish(false)
                await onBulkReject(note)
                setNote('')
              }}
              disabled={
                selectedIds.length === 0 ||
                support.rejectableCount === 0 ||
                !note.trim() ||
                isBusy
              }
              className="rounded-xl border border-red-200 px-4 py-2 text-sm font-medium text-red-700 transition-colors hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {busyAction === 'bulkReject'
                ? 'Rejecting...'
                : `Reject ${support.rejectableCount > 0 ? support.rejectableCount : ''} item${support.rejectableCount === 1 ? '' : 's'}`}
            </button>

            {!confirmPublish ? (
              <button
                onClick={() => setConfirmPublish(true)}
                disabled={
                  selectedIds.length === 0 || support.publishableCount === 0 || isBusy
                }
                className="rounded-xl border border-green-200 px-4 py-2 text-sm font-medium text-green-700 transition-colors hover:bg-green-50 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {`Publish ${support.publishableCount > 0 ? support.publishableCount : ''} approved item${support.publishableCount === 1 ? '' : 's'}`}
              </button>
            ) : (
              <>
                <button
                  onClick={async () => {
                    if (!onBulkPublish) return
                    setConfirmPublish(false)
                    await onBulkPublish()
                  }}
                  disabled={isBusy}
                  className="rounded-xl border border-green-300 bg-green-50 px-4 py-2 text-sm font-medium text-green-800 transition-colors hover:bg-green-100 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {busyAction === 'bulkPublish' ? 'Publishing...' : 'Confirm publish'}
                </button>
                <button
                  onClick={() => setConfirmPublish(false)}
                  disabled={isBusy}
                  className="rounded-xl border border-gray-200 px-4 py-2 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  Cancel
                </button>
              </>
            )}
          </>
        ) : !confirmArchive ? (
          <button
            onClick={() => setConfirmArchive(true)}
            disabled={selectedIds.length === 0 || support.archivableCount === 0 || isBusy}
            className="rounded-xl border border-amber-200 px-4 py-2 text-sm font-medium text-amber-700 transition-colors hover:bg-amber-50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {`Archive ${support.archivableCount > 0 ? support.archivableCount : ''} version${support.archivableCount === 1 ? '' : 's'}`}
          </button>
        ) : (
          <>
            <button
              onClick={async () => {
                if (!onBulkArchive) return
                setConfirmArchive(false)
                await onBulkArchive()
              }}
              disabled={isBusy}
              className="rounded-xl border border-amber-300 bg-amber-50 px-4 py-2 text-sm font-medium text-amber-800 transition-colors hover:bg-amber-100 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {busyAction === 'bulkArchive' ? 'Archiving...' : 'Confirm archive'}
            </button>
            <button
              onClick={() => setConfirmArchive(false)}
              disabled={isBusy}
              className="rounded-xl border border-gray-200 px-4 py-2 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Cancel
            </button>
          </>
        )}
      </div>

      {result ? (
        <div className="space-y-3 rounded-2xl bg-gray-50 p-4">
          <div>
            <p className="text-xs font-medium uppercase tracking-wide text-gray-500">
              Last bulk outcome
            </p>
            <p className={`mt-1 text-sm font-medium ${
              result.bulk_status === 'full_success'
                ? 'text-emerald-700'
                : result.bulk_status === 'partial_failure'
                  ? 'text-amber-700'
                  : 'text-red-700'
            }`}>
              {labelForBulkStatus(result.bulk_status)}
            </p>
            <p className="mt-0.5 text-xs text-gray-500">
              {result.total} item{result.total === 1 ? '' : 's'} in batch
            </p>
          </div>
          <div className="space-y-2">
            {result.items.map((item, index) => {
              const title = itemTitleFor(item.id, index)
              const isDone = item.status === 'ok'
              return (
                <div key={item.id} className="rounded-xl border border-gray-200 bg-white p-3">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <p className="text-sm font-medium text-gray-900">{title}</p>
                    <span
                      className={`rounded-full px-2 py-1 text-[11px] font-medium ${
                        isDone ? 'bg-emerald-100 text-emerald-700' : 'bg-red-100 text-red-700'
                      }`}
                    >
                      {labelForBulkItemStatus(item.status)}
                    </span>
                  </div>
                  {!isDone ? (
                    <p className="mt-1 text-xs text-red-700">
                      This item could not be processed. Check its current state and try again, or
                      contact support if the issue persists.
                    </p>
                  ) : null}
                </div>
              )
            })}
          </div>
        </div>
      ) : null}
    </div>
  )
}
