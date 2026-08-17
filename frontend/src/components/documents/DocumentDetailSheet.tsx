import { useState, type ReactNode } from 'react'
import { X, ChevronDown, ChevronRight } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { StatusChip } from '@/components/shared/StatusChip'
import { formatDocumentDate } from '@/lib/documents/document-formatters'
import { documentQueryKeys } from '@/lib/documents/document-query-keys'
import { normalizeDocumentRecord } from '@/lib/documents/document-lifecycle'
import { DocumentLifecycleActions } from '@/components/documents/DocumentLifecycleActions'
import { getDocument } from '@/api/document.api'
import type { DocumentRecord } from '@/types/document'

interface DocumentDetailSheetProps { document: DocumentRecord; onClose: () => void }

export function DocumentDetailSheet({ document: initialDoc, onClose }: DocumentDetailSheetProps) {
  const [lifecycleOpen, setLifecycleOpen] = useState(false)
  const docQuery = useQuery({
    queryKey: documentQueryKeys.detail(initialDoc.document_id),
    queryFn: () => getDocument(initialDoc.document_id),
    staleTime: 15_000,
  })
  const doc = docQuery.data?.document ?? initialDoc
  const viewModel = normalizeDocumentRecord(doc)

  return (
    <>
      <div className="fixed inset-0 z-40 bg-black/20" onClick={onClose} />
      <div className="fixed bottom-0 right-0 top-0 z-40 flex w-full flex-col overflow-hidden bg-white shadow-2xl md:w-[560px]">
        <div className="flex shrink-0 items-start justify-between border-b border-gray-100 px-6 py-4">
          <div className="min-w-0 flex-1 pr-4">
            <h2 className="truncate text-base font-semibold text-gray-900">{viewModel.displayName}</h2>
            <div className="mt-1">
              <StatusChip status={viewModel.status} label={viewModel.statusLabel} />
            </div>
            {viewModel.statusDescription ? (
              <p className="mt-2 text-xs text-gray-500">{viewModel.statusDescription}</p>
            ) : null}
            <p className="mt-2 text-xs text-gray-500">
              {viewModel.isSaved ? 'Saved to your library.' : 'This document has been removed.'}
              {' '}
              {viewModel.requiresUserAction
                ? 'Action is needed before it can be used.'
                : 'No user action is needed right now.'}
            </p>
          </div>
          <button onClick={onClose} className="shrink-0 text-gray-400 transition-colors hover:text-gray-600" aria-label="Close"><X className="h-4 w-4" /></button>
        </div>
        <div className="flex-1 space-y-6 overflow-y-auto px-6 py-5">
          <section>
            <p className="mb-3 text-xs font-medium uppercase tracking-wide text-gray-400">Document info</p>
            <div className="space-y-2.5">
              <InfoRow label="Added" value={formatDocumentDate(viewModel.addedAt) ?? 'Unknown'} />
              {doc.display_name ? <InfoRow label="Display name" value={doc.display_name} /> : null}
              {doc.category ? <InfoRow label="Category" value={doc.category} /> : null}
              {doc.tags.length > 0 ? <InfoRow label="Tags" value={doc.tags.join(', ')} /> : null}
              {doc.description ? <InfoRow label="Description" value={doc.description} /> : null}
              <InfoRow
                label="Compliance"
                value={doc.compliance_lock_until ? `Locked until ${formatDocumentDate(doc.compliance_lock_until)}` : 'No lock active'}
              />
            </div>
          </section>
          <section>
            <button onClick={() => setLifecycleOpen((open) => !open)} className="mb-3 flex w-full items-center gap-2 text-xs font-medium uppercase tracking-wide text-gray-400">
              {lifecycleOpen ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />} Document lifecycle
            </button>
            {lifecycleOpen && (
              <div className="space-y-3">
                <DocumentLifecycleActions document={viewModel} onDeleted={onClose} />
              </div>
            )}
          </section>
        </div>
      </div>
    </>
  )
}

function InfoRow({ label, value, children }: { label: string; value?: string; children?: ReactNode }) {
  return <div className="flex items-start gap-4"><span className="w-36 shrink-0 text-sm text-gray-500">{label}</span>{children ?? <span className="text-sm text-gray-900">{value}</span>}</div>
}
