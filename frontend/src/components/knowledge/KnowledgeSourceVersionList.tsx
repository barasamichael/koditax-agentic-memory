import { cn } from '@/lib/utils'
import type { KnowledgeSourceVersionSummary } from '@/types/knowledge'
import { labelForPublicationState, labelForSourceClass } from '@/lib/knowledgeStateLabels'

interface KnowledgeSourceVersionListProps {
  items: KnowledgeSourceVersionSummary[]
  selectedId: string | null
  onSelect: (sourceVersionId: string) => void
  selectedIds: string[]
  onToggleSelection: (sourceVersionId: string) => void
  loading: boolean
  error: string | null
}

function getArchiveDisabledReason(item: KnowledgeSourceVersionSummary): string | null {
  if (item.publication_state === 'archived') {
    return 'Already archived.'
  }
  if (item.publication_state === 'published' || item.publication_state === 'superseded') {
    return null
  }
  return 'Archive is only available for published or superseded versions.'
}

export function KnowledgeSourceVersionList({
  items,
  selectedId,
  onSelect,
  selectedIds,
  onToggleSelection,
  loading,
  error,
}: KnowledgeSourceVersionListProps) {
  if (loading) {
    return <div className="p-4 text-sm text-gray-500">Loading published sources...</div>
  }

  if (error) {
    return <div className="p-4 text-sm text-red-700">{error}</div>
  }

  if (items.length === 0) {
    return (
      <div className="p-4 text-sm text-gray-500">
        No published sources match the current filters.
      </div>
    )
  }

  return (
    <div className="flex-1 space-y-2 overflow-y-auto p-3">
      {items.map((item) => {
        const isSelected = item.source_version_id === selectedId
        const isChecked = selectedIds.includes(item.source_version_id)
        const archiveDisabledReason = getArchiveDisabledReason(item)

        return (
          <button
            key={item.source_version_id}
            onClick={() => onSelect(item.source_version_id)}
            className={cn(
              'w-full rounded-2xl border px-3 py-3 text-left transition-all',
              isSelected
                ? 'border-navy-200 bg-navy-50 shadow-sm'
                : 'border-gray-200 bg-white hover:border-gray-300 hover:bg-gray-50'
            )}
          >
            <div className="flex items-start gap-3">
              <input
                type="checkbox"
                checked={isChecked}
                disabled={Boolean(archiveDisabledReason)}
                onChange={() => onToggleSelection(item.source_version_id)}
                onClick={(event) => event.stopPropagation()}
                className="mt-1 h-4 w-4 rounded border-gray-300 text-navy-700 focus:ring-navy-500 disabled:cursor-not-allowed"
                aria-label={`Select ${item.title}`}
              />

              <div className="min-w-0 flex-1">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium text-gray-900">{item.title}</p>
                    <p className="mt-1 text-xs text-gray-500">
                      {labelForPublicationState(item.publication_state)} · {item.tax_domain}
                    </p>
                    <p className="mt-1 text-[11px] text-gray-400">
                      Effective {item.effective_from}
                      {item.effective_to ? ` to ${item.effective_to}` : ' onward'}
                    </p>
                    {archiveDisabledReason ? (
                      <p className="mt-1 text-[11px] text-amber-700">{archiveDisabledReason}</p>
                    ) : null}
                  </div>
                  <span className="shrink-0 rounded-full bg-gray-100 px-2 py-1 text-[11px] font-medium text-gray-600">
                    {labelForSourceClass(item.source_class)}
                  </span>
                </div>
              </div>
            </div>
          </button>
        )
      })}
    </div>
  )
}
