import type { KnowledgeIngestionDetail } from '@/types/knowledge'
import {
  labelForIngestionState,
  labelForInputOrigin,
  labelForSourceClass,
} from '@/lib/knowledgeStateLabels'
import { KnowledgeMetadataCorrectionPanel } from './KnowledgeMetadataCorrectionPanel'

interface KnowledgeDetailPanelProps {
  item: KnowledgeIngestionDetail | null
  loading: boolean
  error: string | null
  onCorrectMetadata?: (params: { note: string; updates: Record<string, unknown> }) => Promise<void>
  metadataCorrectionBusy?: boolean
}

function ReviewNoteList({ notes }: { notes: KnowledgeIngestionDetail['review_notes'] }) {
  if (!notes || notes.length === 0) {
    return <p className="text-sm text-gray-500">No review notes have been added yet.</p>
  }
  return (
    <div className="space-y-2">
      {notes.map((note, index) => (
        <div key={index} className="rounded-xl border border-gray-100 bg-gray-50 p-3 text-sm">
          <p className="text-gray-800">{note.note}</p>
          <p className="mt-1 text-[11px] text-gray-400">
            {note.actor_user_id} · {new Date(note.created_at).toLocaleString()}
          </p>
        </div>
      ))}
    </div>
  )
}

export function KnowledgeDetailPanel({
  item,
  loading,
  error,
  onCorrectMetadata,
  metadataCorrectionBusy = false,
}: KnowledgeDetailPanelProps) {
  if (loading) {
    return (
      <div className="rounded-3xl border border-gray-200 bg-white p-6 text-sm text-gray-500">
        Loading item detail...
      </div>
    )
  }

  if (error) {
    return (
      <div className="rounded-3xl border border-red-200 bg-red-50 p-6 text-sm text-red-700">
        {error}
      </div>
    )
  }

  if (!item) {
    return (
      <div className="rounded-3xl border border-gray-200 bg-white p-6 text-sm text-gray-500">
        Select an item from the list to inspect its details and review readiness.
      </div>
    )
  }

  const hasProposedRecord =
    item.proposed_source_record &&
    Object.keys(item.proposed_source_record).length > 0

  return (
    <div className="space-y-5">
      <div className="space-y-5 rounded-3xl border border-gray-200 bg-white p-6 shadow-card">
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-gray-500">Incoming item</p>
          <h2 className="mt-1 text-lg font-semibold text-navy-900">{item.source_input_ref}</h2>
          <div className="mt-2 flex flex-wrap gap-2 text-xs text-gray-600">
            <span className="rounded-full bg-gray-100 px-2 py-1 font-medium">
              {labelForIngestionState(item.ingestion_state)}
            </span>
            <span className="rounded-full bg-gray-100 px-2 py-1">
              {labelForSourceClass(item.source_class)}
            </span>
            <span className="rounded-full bg-gray-100 px-2 py-1">
              {labelForInputOrigin(item.source_input_origin)}
            </span>
          </div>
        </div>

        <div className="grid gap-4 md:grid-cols-2">
          <div>
            <p className="text-xs font-medium uppercase tracking-wide text-gray-500">Submitted by</p>
            <p className="mt-1 text-sm text-gray-700">{item.requested_by}</p>
          </div>
          <div>
            <p className="text-xs font-medium uppercase tracking-wide text-gray-500">Completed</p>
            <p className="mt-1 text-sm text-gray-700">
              {item.completed_at
                ? new Date(item.completed_at).toLocaleString()
                : 'Still in progress'}
            </p>
          </div>
        </div>

        <section>
          <p className="text-xs font-medium uppercase tracking-wide text-gray-500">
            Extracted document metadata
          </p>
          <div className="mt-2">
            {item.extracted_metadata && Object.keys(item.extracted_metadata).length > 0 ? (
              <dl className="space-y-1">
                {Object.entries(item.extracted_metadata).map(([key, value]) => (
                  <div key={key} className="flex flex-wrap gap-2 text-sm">
                    <dt className="font-medium text-gray-600 capitalize">
                      {key.replace(/_/g, ' ')}:
                    </dt>
                    <dd className="text-gray-800">{String(value)}</dd>
                  </div>
                ))}
              </dl>
            ) : (
              <p className="text-sm text-gray-500">No metadata has been extracted yet.</p>
            )}
          </div>
        </section>

        <section>
          <p className="text-xs font-medium uppercase tracking-wide text-gray-500">
            Proposed source record
          </p>
          <div className="mt-2">
            {hasProposedRecord ? (
              <p className="text-sm text-emerald-700">
                Source record is ready for review and approval.
              </p>
            ) : (
              <p className="text-sm text-amber-700">
                No source record has been proposed yet. Approval requires the backend to produce a
                source record from the submitted content before it can proceed.
              </p>
            )}
          </div>
        </section>

        <section>
          <p className="text-xs font-medium uppercase tracking-wide text-gray-500">Review notes</p>
          <div className="mt-2">
            <ReviewNoteList notes={item.review_notes} />
          </div>
        </section>
      </div>

      {onCorrectMetadata ? (
        <KnowledgeMetadataCorrectionPanel
          ingestionJobId={item.ingestion_job_id}
          ingestionState={item.ingestion_state}
          onCorrect={onCorrectMetadata}
          busy={metadataCorrectionBusy}
        />
      ) : null}
    </div>
  )
}
