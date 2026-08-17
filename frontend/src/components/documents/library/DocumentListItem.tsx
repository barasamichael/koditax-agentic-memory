import { useState } from 'react'
import { ChevronRight, FileText, Lock, Tags } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { StatusChip } from '@/components/shared/StatusChip'
import { formatDate } from '@/lib/utils'
import {
  formatDocumentDate,
  formatDocumentName,
  formatFileTypeLabel,
} from '@/lib/documents/document-formatters'
import type { DocumentViewModel } from '@/lib/documents/document-lifecycle'
import { DocumentAccessActions } from '@/components/documents/DocumentAccessActions'
import { DocumentLifecycleActions } from '@/components/documents/DocumentLifecycleActions'

export interface DocumentListItemProps {
  document: DocumentViewModel
}

export function DocumentListItem({ document }: DocumentListItemProps) {
  const [expanded, setExpanded] = useState(false)
  const navigate = useNavigate()
  const fileTypeLabel = formatFileTypeLabel(document.fileExtension)

  return (
    <li
      className={[
        'rounded-2xl border border-gray-200 bg-white shadow-sm',
        'transition-shadow hover:shadow-md',
      ].join(' ')}
    >
      <div className="flex flex-col gap-4 p-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 items-start gap-3">
          <div
            className={[
              'mt-0.5 flex h-10 w-10 shrink-0 items-center justify-center rounded-xl',
              'bg-navy-50 text-navy-700',
            ].join(' ')}
          >
            <FileText className="h-5 w-5" aria-hidden="true" />
          </div>

          <div className="min-w-0">
            <button
              type="button"
              onClick={() => setExpanded((value) => !value)}
              className={[
                'block max-w-full text-left',
                'focus:outline-none focus-visible:ring-2 focus-visible:ring-navy-500',
                'focus-visible:ring-offset-2',
              ].join(' ')}
              aria-expanded={expanded}
              aria-label={`Expand ${document.displayName}`}
            >
              <span className="block truncate text-sm font-semibold text-gray-900">
                {formatDocumentName(document.displayName)}
              </span>
            </button>

            <div className="mt-2 flex flex-wrap items-center gap-2">
              <span
                className={[
                  'rounded-full bg-gray-100 px-2.5 py-1 text-[11px] font-medium',
                  'uppercase tracking-wide text-gray-600',
                ].join(' ')}
              >
                {fileTypeLabel}
              </span>
              <StatusChip status={document.status} label={document.statusLabel} />
              {document.addedAt ? (
                <span className="text-xs text-gray-500">
                  Added {formatDocumentDate(document.addedAt) ?? 'Unknown'}
                </span>
              ) : null}
              {document.updatedAt ? (
                <span className="text-xs text-gray-500">
                  Updated {formatDate(document.updatedAt)}
                </span>
              ) : null}
            </div>

            {document.statusDescription ? (
              <p className="mt-2 max-w-2xl text-sm text-gray-600">
                {document.statusDescription}
              </p>
            ) : null}
          </div>
        </div>

        <div className="flex shrink-0 flex-col items-start gap-2 self-start sm:items-end sm:pt-1">
          <DocumentAccessActions
            documentId={document.id}
            displayName={document.displayName}
            canOpen={document.availableActions.canOpen}
            canDownload={document.availableActions.canDownload}
            availabilityMessage={document.statusDescription}
          />
          <DocumentLifecycleActions
            document={document}
            className="w-full justify-start sm:justify-end"
          />
          <button
            type="button"
            onClick={() => navigate(`/documents/${document.id}`)}
            className={[
              'inline-flex items-center gap-2 rounded-lg border border-gray-200 px-3 py-2',
              'text-sm font-medium text-gray-700 transition-all hover:bg-gray-50',
            ].join(' ')}
            aria-label={`View details for ${document.displayName}`}
          >
            View details
            <ChevronRight className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>
      </div>

      {expanded && (
        <div className="border-t border-gray-100 px-4 py-4">
          <dl className="grid gap-3 sm:grid-cols-2">
            {document.category ? (
              <div>
                <dt className="text-xs font-medium uppercase tracking-wide text-gray-500">
                  Category
                </dt>
                <dd className="mt-1 text-sm text-gray-900">{document.category}</dd>
              </div>
            ) : null}

            {document.complianceLockUntil ? (
              <div>
                <dt
                  className={[
                    'flex items-center gap-1 text-xs font-medium uppercase',
                    'tracking-wide text-gray-500',
                  ].join(' ')}
                >
                  <Lock className="h-3.5 w-3.5" aria-hidden="true" />
                  Locked until
                </dt>
                <dd className="mt-1 text-sm text-gray-900">
                  {formatDocumentDate(document.complianceLockUntil) ?? 'Unknown'}
                </dd>
              </div>
            ) : null}

            {document.tags.length > 0 ? (
              <div className="sm:col-span-2">
                <dt
                  className={[
                    'flex items-center gap-1 text-xs font-medium uppercase',
                    'tracking-wide text-gray-500',
                  ].join(' ')}
                >
                  <Tags className="h-3.5 w-3.5" aria-hidden="true" />
                  Tags
                </dt>
                <dd className="mt-1 flex flex-wrap gap-2">
                  {document.tags.map((tag) => (
                    <span
                      key={tag}
                      className="rounded-full bg-gray-100 px-2.5 py-1 text-xs text-gray-700"
                    >
                      {tag}
                    </span>
                  ))}
                </dd>
              </div>
            ) : null}

            {document.description ? (
              <div className="sm:col-span-2">
                <dt className="text-xs font-medium uppercase tracking-wide text-gray-500">
                  Notes
                </dt>
                <dd className="mt-1 text-sm text-gray-900">{document.description}</dd>
              </div>
            ) : null}
          </dl>
        </div>
      )}
    </li>
  )
}
