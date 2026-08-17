import { cn } from '@/lib/utils'
import type { KnowledgeAdminTab } from '@/types/knowledge'

interface KnowledgeTabItem {
  id: KnowledgeAdminTab
  label: string
  count: number
  helper: string
}

interface KnowledgeTabsProps {
  activeTab: KnowledgeAdminTab
  tabs: KnowledgeTabItem[]
  onChange: (tab: KnowledgeAdminTab) => void
}

// Tab id-to-label map aligns with the approved dashboard spec nav labels.
// Do not use internal backend terminology here; use plain operational labels.
export const KNOWLEDGE_TAB_LABELS: Record<KnowledgeAdminTab, string> = {
  ingestion: 'Incoming Items',
  reviewQueue: 'Review Queue',
  sourceVersions: 'Published Sources',
  sources: 'Source Library',
}

export function KnowledgeTabs({ activeTab, tabs, onChange }: KnowledgeTabsProps) {
  return (
    <div className="flex flex-wrap gap-2">
      {tabs.map((tab) => {
        const isActive = tab.id === activeTab
        return (
          <button
            key={tab.id}
            onClick={() => onChange(tab.id)}
            className={cn(
              'group min-w-[160px] rounded-xl border px-4 py-3 text-left transition-all',
              isActive
                ? 'border-navy-300 bg-navy-50 shadow-sm'
                : 'border-gray-200 bg-white hover:border-gray-300 hover:bg-gray-50/70'
            )}
          >
            <div className="flex items-center justify-between gap-2">
              <span
                className={cn(
                  'text-sm font-semibold',
                  isActive ? 'text-navy-900' : 'text-gray-700 group-hover:text-gray-900'
                )}
              >
                {tab.label}
              </span>
              <span
                className={cn(
                  'shrink-0 rounded-md px-2 py-0.5 text-[11px] font-semibold tabular-nums',
                  isActive
                    ? 'bg-navy-100 text-navy-700'
                    : 'bg-gray-100 text-gray-500 group-hover:bg-gray-200'
                )}
              >
                {tab.count}
              </span>
            </div>
            <p
              className={cn(
                'mt-1.5 text-xs leading-snug',
                isActive ? 'text-navy-700' : 'text-gray-400'
              )}
            >
              {tab.helper}
            </p>
          </button>
        )
      })}
    </div>
  )
}
