import { useState } from 'react'
import type {
  KnowledgeIngestionDetail,
  KnowledgeSourceVersionLifecycle,
  KnowledgeSourceVersionSummary,
} from '@/types/knowledge'
import { labelForPublicationState } from '@/lib/knowledgeStateLabels'
import { KnowledgeArchiveConfirmDialog } from './KnowledgeArchiveConfirmDialog'
import { KnowledgeSupersedePanel } from './KnowledgeSupersedePanel'

type IngestionBusyAction = 'review' | 'approve' | 'reject' | 'publish'
type SourceVersionBusyAction = 'archive' | 'supersede'

type KnowledgeActionBarProps =
  | {
      mode: 'ingestion'
      item: KnowledgeIngestionDetail | null
      busyAction: IngestionBusyAction | null
      currentUserId: string | null
      onReview: (note: string) => Promise<void>
      onApprove: (note: string) => Promise<void>
      onReject: (note: string) => Promise<void>
      onPublish: () => Promise<void>
    }
  | {
      mode: 'sourceVersion'
      item: KnowledgeSourceVersionSummary | null
      lifecycle: KnowledgeSourceVersionLifecycle | null
      busyAction: SourceVersionBusyAction | null
      publishedVersions: KnowledgeSourceVersionSummary[]
      onArchive: () => Promise<void>
      onSupersede: (successorId: string) => Promise<void>
    }

export function KnowledgeActionBar(props: KnowledgeActionBarProps) {
  const [note, setNote] = useState('')
  const [archiveDialogOpen, setArchiveDialogOpen] = useState(false)

  if (props.mode === 'ingestion') {
    const { item, busyAction, currentUserId, onReview, onApprove, onReject, onPublish } = props

    const actionSupport = !item
      ? {
          canReview: false,
          reviewReason: 'Select an item first.',
          canApprove: false,
          approveReason: 'Select an item first.',
          canReject: false,
          rejectReason: 'Select an item first.',
          canPublish: false,
          publishReason: 'Select an item first.',
        }
      : (() => {
          const canReview = ['uploaded', 'review_pending', 'approved_for_publication'].includes(
            item.ingestion_state
          )
          const hasPublicationPayload =
            typeof item.proposed_source_record === 'object' &&
            item.proposed_source_record !== null &&
            Object.keys(item.proposed_source_record).length > 0
          const canApprove =
            ['uploaded', 'review_pending'].includes(item.ingestion_state) && hasPublicationPayload
          const canReject = ['uploaded', 'review_pending', 'approved_for_publication'].includes(
            item.ingestion_state
          )
          const stateAllowsPublish = item.ingestion_state === 'approved_for_publication'
          // Two-actor rule: the user who approved must not be the one who publishes.
          // We surface this as a warning but cannot enforce it client-side — backend is authoritative.
          const approveNotes = item.review_notes?.filter((n) => n.code === 'frontend_approve_note') ?? []
          const approverUserId =
            approveNotes.length > 0
              ? approveNotes[approveNotes.length - 1]?.actor_user_id ?? null
              : null
          const isSameActorAsApprover =
            stateAllowsPublish &&
            approverUserId !== null &&
            currentUserId !== null &&
            approverUserId === currentUserId
          const canPublish = stateAllowsPublish && !isSameActorAsApprover

          return {
            canReview,
            reviewReason: canReview
              ? null
              : 'Review notes can only be added to items that are still in progress.',
            canApprove,
            approveReason: hasPublicationPayload
              ? 'Approve is only available while the item is waiting or under review.'
              : 'Approve requires a non-empty source record from the backend before it can proceed.',
            canReject,
            rejectReason: canReject
              ? null
              : 'This item is already finalized and cannot be rejected.',
            canPublish,
            publishReason: canPublish
              ? null
              : isSameActorAsApprover
                ? 'The administrator who approved this item cannot also publish it. A different administrator must complete the publish step.'
                : 'Publish is only available after the item has been approved.',
          }
        })()

    if (!item) {
      return (
        <div className="rounded-3xl border border-gray-200 bg-white p-6 text-sm text-gray-500 shadow-card">
          Select an item from the list to review, approve, reject, or publish it.
        </div>
      )
    }

    const isBusy = busyAction !== null

    return (
      <div className="space-y-4 rounded-3xl border border-gray-200 bg-white p-6 shadow-card">
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-gray-500">
            Review and publish actions
          </p>
          <p className="mt-1 text-sm text-gray-600">
            Review, approve, reject, and publish incoming items. Actions are only available for
            eligible items and require a note where indicated.
          </p>
        </div>

        <div>
          <label className="block text-xs font-medium uppercase tracking-wide text-gray-500">
            Operator note
          </label>
          <textarea
            value={note}
            onChange={(event) => setNote(event.target.value)}
            rows={4}
            placeholder="Record the governed reason for the next lifecycle decision..."
            className="mt-2 w-full rounded-xl border border-gray-200 px-3 py-2 text-sm text-gray-800 placeholder-gray-400 focus:border-transparent focus:outline-none focus-visible:ring-2 focus-visible:ring-navy-500"
          />
        </div>

        <div className="flex flex-wrap gap-3">
          <button
            onClick={async () => {
              await onReview(note)
              setNote('')
            }}
            disabled={!actionSupport.canReview || isBusy || !note.trim()}
            className="rounded-xl bg-navy-900 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-navy-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busyAction === 'review' ? 'Saving review...' : 'Save review'}
          </button>

          <button
            onClick={async () => {
              await onApprove(note)
              setNote('')
            }}
            disabled={!actionSupport.canApprove || isBusy || !note.trim()}
            className="rounded-xl border border-emerald-200 px-4 py-2 text-sm font-medium text-emerald-700 transition-colors hover:bg-emerald-50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busyAction === 'approve' ? 'Approving...' : 'Approve'}
          </button>

          <button
            onClick={async () => {
              await onReject(note)
              setNote('')
            }}
            disabled={!actionSupport.canReject || isBusy || !note.trim()}
            className="rounded-xl border border-red-200 px-4 py-2 text-sm font-medium text-red-700 transition-colors hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busyAction === 'reject' ? 'Rejecting...' : 'Reject'}
          </button>

          <button
            onClick={onPublish}
            disabled={!actionSupport.canPublish || isBusy}
            className="rounded-xl border border-green-200 px-4 py-2 text-sm font-medium text-green-700 transition-colors hover:bg-green-50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busyAction === 'publish' ? 'Publishing...' : 'Publish'}
          </button>
        </div>

        <div className="space-y-1 text-xs text-gray-500">
          {!actionSupport.canReview ? <p>{actionSupport.reviewReason}</p> : null}
          {!actionSupport.canApprove ? <p>{actionSupport.approveReason}</p> : null}
          {!actionSupport.canReject ? <p>{actionSupport.rejectReason}</p> : null}
          {!actionSupport.canPublish ? <p>{actionSupport.publishReason}</p> : null}
        </div>
      </div>
    )
  }

  const { item, lifecycle, busyAction, publishedVersions, onArchive, onSupersede } = props

  const currentPublicationState = lifecycle?.publication_state ?? item?.publication_state ?? ''

  const archiveReason = !item
    ? 'Select a published source version first.'
    : currentPublicationState === 'archived'
      ? 'This source version is already archived.'
      : currentPublicationState === 'published' || currentPublicationState === 'superseded'
        ? null
        : 'Archive is only available for published or superseded source versions.'

  const canArchive = item !== null && archiveReason === null

  return (
    <div className="space-y-4 rounded-3xl border border-gray-200 bg-white p-6 shadow-card">
      <div>
        <p className="text-xs font-medium uppercase tracking-wide text-gray-500">
          Source version lifecycle
        </p>
        <p className="mt-1 text-sm text-gray-600">
          Archive or supersede published source versions. Archived and superseded versions remain
          visible for audit purposes but are no longer served as active knowledge.
        </p>
      </div>

      {item ? (
        <div className="rounded-2xl bg-gray-50 p-4 text-sm text-gray-700">
          <p className="font-medium text-gray-900">{item.title}</p>
          <p className="mt-1">
            Status:{' '}
            {labelForPublicationState(lifecycle?.publication_state ?? item.publication_state)}
          </p>
        </div>
      ) : null}

      <div className="flex flex-wrap gap-3">
        <button
          onClick={() => setArchiveDialogOpen(true)}
          disabled={!canArchive || busyAction !== null}
          className="rounded-xl border border-amber-200 px-4 py-2 text-sm font-medium text-amber-700 transition-colors hover:bg-amber-50 disabled:cursor-not-allowed disabled:opacity-50"
        >
          Archive version
        </button>
      </div>

      {archiveReason ? <p className="text-xs text-gray-500">{archiveReason}</p> : null}

      {item ? (
        <KnowledgeSupersedePanel
          currentVersionId={item.source_version_id}
          currentFamilyId={item.source_family_id}
          currentPublicationState={currentPublicationState}
          publishedVersions={publishedVersions}
          busy={busyAction === 'supersede'}
          onSupersede={onSupersede}
        />
      ) : null}

      {archiveDialogOpen && item ? (
        <KnowledgeArchiveConfirmDialog
          versionTitle={item.title}
          publicationState={lifecycle?.publication_state ?? item.publication_state}
          busy={busyAction === 'archive'}
          onConfirm={async () => {
            await onArchive()
            setArchiveDialogOpen(false)
          }}
          onCancel={() => setArchiveDialogOpen(false)}
        />
      ) : null}
    </div>
  )
}
