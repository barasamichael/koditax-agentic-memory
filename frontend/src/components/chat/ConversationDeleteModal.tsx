import { useMemo } from 'react'
import { Search, Trash2 } from 'lucide-react'
import type { ChatConversation } from '@/types/chat'
import { cn, formatDate } from '@/lib/utils'

interface ConversationDeleteModalProps {
  conversations: ChatConversation[]
  selectedConversationIds: string[]
  searchQuery: string
  busy: boolean
  onChangeSearch: (value: string) => void
  onToggleConversation: (conversationId: string) => void
  onToggleAll: () => void
  onCancel: () => void
  onDeleteSelected: () => Promise<void>
}

export function ConversationDeleteModal({
  conversations,
  selectedConversationIds,
  searchQuery,
  busy,
  onChangeSearch,
  onToggleConversation,
  onToggleAll,
  onCancel,
  onDeleteSelected,
}: ConversationDeleteModalProps) {
  const selectedCount = selectedConversationIds.length
  const conversationCount = conversations.length

  const selectedSet = useMemo(() => new Set(selectedConversationIds), [selectedConversationIds])
  const allSelected = conversationCount > 0 && selectedCount === conversationCount

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="conversation-delete-title"
      className="fixed inset-0 z-40 flex items-center justify-center bg-black/40 p-4 backdrop-blur-[1px]"
    >
      <div className="flex max-h-[88vh] w-full max-w-2xl flex-col overflow-hidden rounded-3xl border border-gray-200 bg-white shadow-2xl">
        <div className="border-b border-gray-100 px-5 py-4">
          <p className="text-[10px] font-semibold uppercase tracking-[0.28em] text-navy-600">
            Bulk delete chats
          </p>
          <div className="mt-1 flex items-start justify-between gap-3">
            <div>
              <h2 id="conversation-delete-title" className="text-lg font-semibold text-gray-900">
                Remove selected conversations
              </h2>
              <p className="mt-1 text-sm text-gray-500">
                Deleted chats disappear from your sidebar. Conversation continuity is cleared in
                orchestration, but audit history is retained.
              </p>
            </div>
            <button
              onClick={onToggleAll}
              disabled={conversationCount === 0 || busy}
              className="rounded-xl border border-gray-200 px-3 py-2 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {allSelected ? 'Clear all' : 'Select all'}
            </button>
          </div>
        </div>

        <div className="border-b border-gray-100 px-5 py-4">
          <label className="block text-xs font-medium uppercase tracking-wide text-gray-500">
            Search chats
          </label>
          <div className="mt-2 flex items-center gap-2 rounded-2xl border border-gray-200 bg-gray-50 px-3 py-2">
            <Search className="h-4 w-4 shrink-0 text-gray-400" />
            <input
              type="text"
              value={searchQuery}
              onChange={(event) => onChangeSearch(event.target.value)}
              placeholder="Search chat titles"
              disabled={busy}
              className="w-full bg-transparent text-sm text-gray-800 placeholder:text-gray-400 focus:outline-none disabled:cursor-not-allowed"
            />
          </div>
          <p className="mt-2 text-xs text-gray-400">
            Search is UI only for now and does not filter the list yet.
          </p>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-4">
          {conversationCount === 0 ? (
            <div className="rounded-2xl border border-dashed border-gray-200 px-4 py-8 text-center">
              <p className="text-sm font-medium text-gray-700">No conversations available</p>
              <p className="mt-1 text-xs text-gray-400">Start a chat before using bulk delete.</p>
            </div>
          ) : (
            <div className="space-y-2">
              {conversations.map((conversation) => {
                const checked = selectedSet.has(conversation.conversationId)
                return (
                  <label
                    key={conversation.conversationId}
                    className={cn(
                      'flex cursor-pointer items-center gap-3 rounded-2xl border px-4 py-3 transition-colors',
                      checked
                        ? 'border-red-200 bg-red-50/70'
                        : 'border-gray-200 bg-white hover:bg-gray-50'
                    )}
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => onToggleConversation(conversation.conversationId)}
                      disabled={busy}
                      className="h-4 w-4 rounded border-gray-300 text-red-600 accent-red-600"
                    />
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm font-medium text-gray-900">
                        {conversation.title}
                      </p>
                      <p className="mt-0.5 text-[11px] text-gray-400">
                        Updated {formatDate(conversation.updatedAt)}
                      </p>
                    </div>
                    <span className="rounded-full bg-white px-2.5 py-1 text-[11px] font-medium text-gray-500 shadow-sm">
                      {conversation.messages.length} message
                      {conversation.messages.length === 1 ? '' : 's'}
                    </span>
                  </label>
                )
              })}
            </div>
          )}
        </div>

        <div className="border-t border-gray-100 px-5 py-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-sm text-gray-500">
              {selectedCount === 0
                ? 'Choose one or more conversations to delete.'
                : `${selectedCount} conversation${selectedCount === 1 ? '' : 's'} selected.`}
            </p>
            <div className="flex flex-wrap gap-3">
              <button
                onClick={onCancel}
                disabled={busy}
                className="rounded-xl border border-gray-200 bg-white px-4 py-2 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                onClick={async () => {
                  if (busy || selectedCount === 0) return
                  await onDeleteSelected()
                }}
                disabled={busy || selectedCount === 0}
                className="inline-flex items-center gap-2 rounded-xl border border-red-200 bg-red-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-red-700 disabled:cursor-not-allowed disabled:opacity-50"
              >
                <Trash2 className="h-4 w-4" />
                {busy ? 'Deleting...' : `Delete selected${selectedCount > 0 ? ` (${selectedCount})` : ''}`}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
