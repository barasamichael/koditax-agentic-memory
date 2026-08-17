import { X, FileText, Calculator } from 'lucide-react'
import { motion } from 'framer-motion'
import { useUIStore } from '@/stores/uiStore'
import { useChatStore } from '@/stores/chatStore'
import { StatusChip } from '@/components/shared/StatusChip'
import { cn } from '@/lib/utils'

export function ContextRail() {
  const railOpen = useUIStore((s) => s.railOpen)
  const toggleRail = useUIStore((s) => s.toggleRail)
  const contextDocuments = useChatStore((s) => s.contextDocuments)
  const activeComputationId = useChatStore((s) => s.activeComputationId)

  const hasContent = contextDocuments.length > 0 || !!activeComputationId

  return (
    <motion.aside
      initial={false}
      animate={{ width: railOpen ? 288 : 0 }}
      transition={{ type: 'tween', duration: 0.2 }}
      className="shrink-0 overflow-hidden border-l border-gray-100 bg-white flex flex-col"
    >
      {/* Inner div holds the fixed 288px content so it doesn't squish during animation */}
      <div className="w-72 flex flex-col h-full">
        <div className="h-14 flex items-center justify-between px-4 border-b border-gray-100 shrink-0">
          <span className="text-label uppercase text-gray-500 tracking-wide">Context</span>
          <button
            onClick={toggleRail}
            className="p-1 rounded-lg text-gray-400 hover:bg-gray-50 hover:text-gray-600 transition-all"
            aria-label="Close context rail"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-4 space-y-5">
          {!hasContent && (
            <p className="text-small text-gray-400 text-center pt-8">
              Start a conversation to see context here
            </p>
          )}

          {contextDocuments.length > 0 && (
            <section>
              <p className="text-label uppercase text-gray-400 mb-2">Linked documents</p>
              <div className="space-y-2">
                {contextDocuments.map((doc) => (
                  <div
                    key={doc.id}
                    className={cn(
                      'flex items-center gap-2 p-2 rounded-lg bg-gray-50 border border-gray-100'
                    )}
                  >
                    <FileText className="w-4 h-4 text-gray-400 shrink-0" />
                    <p className="text-small text-gray-700 truncate">{doc.name}</p>
                  </div>
                ))}
              </div>
            </section>
          )}

          {activeComputationId && (
            <section>
              <p className="text-label uppercase text-gray-400 mb-2">Active computation</p>
              <div className="flex items-center gap-2 p-2 rounded-lg bg-gray-50 border border-gray-100">
                <Calculator className="w-4 h-4 text-gray-400 shrink-0" />
                <p className="text-small text-gray-700 font-mono truncate">{activeComputationId}</p>
                <StatusChip status="processing" className="ml-auto" />
              </div>
            </section>
          )}
        </div>
      </div>
    </motion.aside>
  )
}
