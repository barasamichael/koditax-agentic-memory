import { MessageSquarePlus } from 'lucide-react'

interface EmptyStateProps {
  onStartNewChat: () => void
}

export function EmptyState({ onStartNewChat }: EmptyStateProps) {
  return (
    <div className="flex flex-1 items-center justify-center p-6">
      <div className="w-full max-w-md text-center">
        <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-2xl bg-navy-900">
          <MessageSquarePlus className="h-5 w-5 text-white" />
        </div>
        <h2 className="mt-5 text-lg font-semibold text-gray-900">
          Ask Kodi anything about tax
        </h2>
        <p className="mt-2 text-sm leading-relaxed text-gray-500">
          Get answers on income tax, PAYE, reliefs, and filings — grounded in Kenyan tax law.
          Your conversation history is loaded from the backend when you return.
        </p>
        <button
          onClick={onStartNewChat}
          className="mt-6 inline-flex items-center gap-2 rounded-xl bg-navy-900 px-5 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-navy-700 active:scale-[0.97]"
        >
          <MessageSquarePlus className="h-4 w-4" />
          Start a conversation
        </button>
      </div>
    </div>
  )
}
