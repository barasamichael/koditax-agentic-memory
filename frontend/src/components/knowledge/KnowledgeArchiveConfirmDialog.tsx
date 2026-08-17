import { useState } from 'react'
import { labelForPublicationState } from '@/lib/knowledgeStateLabels'
import type { KnowledgePublicationState } from '@/types/knowledge'

interface KnowledgeArchiveConfirmDialogProps {
  versionTitle: string
  publicationState: KnowledgePublicationState | string
  busy: boolean
  onConfirm: () => Promise<void>
  onCancel: () => void
}

export function KnowledgeArchiveConfirmDialog({
  versionTitle,
  publicationState,
  busy,
  onConfirm,
  onCancel,
}: KnowledgeArchiveConfirmDialogProps) {
  const [confirmed, setConfirmed] = useState(false)

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="archive-dialog-title"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
    >
      <div className="w-full max-w-md rounded-3xl border border-amber-200 bg-white p-6 shadow-2xl">
        <p className="text-xs font-medium uppercase tracking-wide text-amber-700">
          Confirm archive
        </p>
        <h2
          id="archive-dialog-title"
          className="mt-1 text-lg font-semibold text-gray-900"
        >
          Archive this source version?
        </h2>

        <div className="mt-4 rounded-2xl bg-amber-50 p-4 text-sm text-amber-900">
          <p className="font-medium">{versionTitle}</p>
          <p className="mt-1 text-amber-800">
            Current status: {labelForPublicationState(publicationState as KnowledgePublicationState)}
          </p>
        </div>

        <p className="mt-4 text-sm text-gray-700">
          Archiving this version will remove it from active knowledge results. Users will no longer
          receive answers grounded in this source version.
        </p>
        <p className="mt-2 text-sm text-gray-700">
          The archived version will remain visible in this dashboard for audit purposes, but cannot
          be unarchived through this interface.
        </p>

        <label className="mt-4 flex cursor-pointer items-start gap-3 rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
          <input
            type="checkbox"
            checked={confirmed}
            onChange={(event) => setConfirmed(event.target.checked)}
            disabled={busy}
            className="mt-0.5 h-4 w-4 rounded border-amber-300 accent-amber-600"
          />
          <span>
            I confirm that this source version should be archived and will no longer appear in user
            answers.
          </span>
        </label>

        <div className="mt-5 flex flex-wrap gap-3">
          <button
            onClick={async () => {
              if (!confirmed || busy) return
              await onConfirm()
            }}
            disabled={!confirmed || busy}
            className="rounded-xl border border-amber-300 bg-amber-50 px-4 py-2 text-sm font-medium text-amber-800 transition-colors hover:bg-amber-100 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy ? 'Archiving...' : 'Archive version'}
          </button>
          <button
            onClick={onCancel}
            disabled={busy}
            className="rounded-xl border border-gray-200 bg-white px-4 py-2 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  )
}
