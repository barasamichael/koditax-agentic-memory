import { useState } from 'react'
import type { PendingAction } from '@/types/chat'
import { useChatStore } from '@/stores/chatStore'
import { useAuthStore } from '@/stores/authStore'
import { Spinner } from '@/components/shared/Spinner'

interface ActionApprovalCardProps {
  action: PendingAction
}

export function ActionApprovalCard({ action }: ActionApprovalCardProps) {
  const [confirming, setConfirming] = useState(false)
  const setPendingAction = useChatStore((s) => s.setPendingAction)
  const updateMessage = useChatStore((s) => s.updateMessage)
  const userId = useAuthStore((s) => s.userId ?? '')

  const handleConfirm = async () => {
    setConfirming(true)
    try {
      await action.onConfirm()
    } finally {
      setConfirming(false)
      setPendingAction(null)
    }
  }

  return (
    <div className="overflow-hidden rounded-xl border border-navy-200 bg-white shadow-sm">
      <div className="flex items-center gap-2 border-b border-navy-100 bg-navy-50 px-4 py-2.5">
        <span className="h-1.5 w-1.5 rounded-full bg-navy-500" />
        <span className="text-[11px] font-semibold uppercase tracking-wide text-navy-700">
          Review required
        </span>
      </div>

      <div className="px-4 py-4 space-y-2">
        <p className="text-sm font-semibold text-gray-900">{action.label}</p>
        <p className="text-sm text-gray-600 leading-relaxed">{action.description}</p>
        <p className="text-xs text-amber-700 italic leading-relaxed">{action.consequence}</p>
      </div>

      <div className="flex items-center gap-2 border-t border-gray-100 px-4 py-3">
        <button
          onClick={handleConfirm}
          disabled={confirming}
          className="flex items-center gap-2 rounded-lg bg-navy-900 px-4 py-2 text-sm font-semibold text-white transition-all hover:bg-navy-700 active:scale-[0.97] disabled:opacity-60"
        >
          {confirming && <Spinner size="sm" />}
          Confirm
        </button>
        <button
          onClick={() => {
            setPendingAction(null)
            if (!userId) return
            updateMessage(
              userId,
              action.id,
              {
                type: 'error',
                content: 'Approval dismissed. Ask again if you want to rerun this action.',
                metadata: {
                  assistantState: 'failed',
                  progressLabel: 'Action approval dismissed.',
                  retryable: false,
                },
              },
              action.conversationId
            )
          }}
          disabled={confirming}
          className="rounded-lg border border-gray-200 px-4 py-2 text-sm font-medium text-gray-600 transition-all hover:bg-gray-50 disabled:opacity-50"
        >
          Dismiss
        </button>
      </div>
    </div>
  )
}
