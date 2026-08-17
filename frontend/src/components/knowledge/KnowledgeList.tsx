import { cn, formatDate } from '@/lib/utils'

export interface KnowledgeListItem {
  id: string
  title: string
  subtitle: string
  meta?: string | null
  timestamp?: string | null
  selectionDisabledReason?: string | null
}

interface KnowledgeListProps {
  items: KnowledgeListItem[]
  selectedId: string | null
  onSelect: (id: string) => void
  loading: boolean
  error: string | null
  emptyMessage: string
  selectableIds?: string[]
  onToggleSelection?: (id: string) => void
}

export function KnowledgeList({
  items,
  selectedId,
  onSelect,
  loading,
  error,
  emptyMessage,
  selectableIds,
  onToggleSelection,
}: KnowledgeListProps) {
  if (loading) {
    return <div className="p-4 text-sm text-gray-500">Loading items...</div>
  }

  if (error) {
    return <div className="p-4 text-sm text-red-700">{error}</div>
  }

  if (items.length === 0) {
    return <div className="p-4 text-sm text-gray-500">{emptyMessage}</div>
  }

  return (
    <div className="flex-1 space-y-2 overflow-y-auto p-3">
      {items.map((item) => {
        const isSelected = item.id === selectedId
        const isChecked = selectableIds?.includes(item.id) ?? false
        const canSelect = !item.selectionDisabledReason

        return (
          <button
            key={item.id}
            onClick={() => onSelect(item.id)}
            className={cn(
              'w-full rounded-2xl border px-3 py-3 text-left transition-all',
              isSelected
                ? 'border-navy-200 bg-navy-50 shadow-sm'
                : 'border-gray-200 bg-white hover:border-gray-300 hover:bg-gray-50'
            )}
          >
            <div className="flex items-start gap-3">
              {onToggleSelection ? (
                <input
                  type="checkbox"
                  checked={isChecked}
                  disabled={!canSelect}
                  onChange={() => onToggleSelection(item.id)}
                  onClick={(event) => event.stopPropagation()}
                  className="mt-1 h-4 w-4 rounded border-gray-300 text-navy-700 focus:ring-navy-500 disabled:cursor-not-allowed"
                  aria-label={`Select ${item.title}`}
                />
              ) : null}

              <div className="min-w-0 flex-1">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium text-gray-900">{item.title}</p>
                    <p className="mt-1 text-xs text-gray-500">{item.subtitle}</p>
                    {item.meta ? <p className="mt-1 text-[11px] text-gray-400">{item.meta}</p> : null}
                    {item.selectionDisabledReason ? (
                      <p className="mt-1 text-[11px] text-amber-700">{item.selectionDisabledReason}</p>
                    ) : null}
                  </div>
                  {item.timestamp ? (
                    <span className="shrink-0 text-[11px] text-gray-400">
                      {formatDate(item.timestamp)}
                    </span>
                  ) : null}
                </div>
              </div>
            </div>
          </button>
        )
      })}
    </div>
  )
}
