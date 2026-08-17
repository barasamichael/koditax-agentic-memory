import { useMemo, useState } from 'react'
import { MessageSquarePlus, PencilLine, Trash2, ListChecks } from 'lucide-react'
import { useAuthStore } from '@/stores/authStore'
import { useChatStore } from '@/stores/chatStore'
import { useUIStore } from '@/stores/uiStore'
import {
  deleteConversation as deleteConversationApi,
  bulkDeleteConversations,
  renameConversation as renameConversationApi,
} from '@/api/orchestration.api'
import { useToast } from '@/components/shared/Toast'
import { ConversationDeleteModal } from '@/components/chat/ConversationDeleteModal'
import { ConversationRenameModal } from '@/components/chat/ConversationRenameModal'
import { ConfirmModal } from '@/components/shared/ConfirmModal'
import { normalizeError } from '@/lib/errorNormalizer'
import { cn, formatDate } from '@/lib/utils'

export function ConversationList() {
  const userId = useAuthStore((state) => state.userId ?? '')
  const chatHistoryOpen = useUIStore((state) => state.chatHistoryOpen)
  const setChatHistoryOpen = useUIStore((state) => state.setChatHistoryOpen)
  const activeConversationId = useChatStore((state) => state.conversationId)
  const conversationLoadStatus = useChatStore((state) => state.conversationLoadStatus)
  const conversations = useChatStore((state) =>
    userId ? state.userStates[userId]?.conversations ?? [] : []
  )
  const createConversation = useChatStore((state) => state.createConversation)
  const selectConversation = useChatStore((state) => state.selectConversation)
  const deleteConversation = useChatStore((state) => state.deleteConversation)
  const deleteConversations = useChatStore((state) => state.deleteConversations)
  const renameConversation = useChatStore((state) => state.renameConversation)
  const toast = useToast()
  const [bulkDeleteOpen, setBulkDeleteOpen] = useState(false)
  const [bulkDeleteBusy, setBulkDeleteBusy] = useState(false)
  const [bulkDeleteSearchQuery, setBulkDeleteSearchQuery] = useState('')
  const [selectedConversationIds, setSelectedConversationIds] = useState<string[]>([])
  const [renameTarget, setRenameTarget] = useState<{
    conversationId: string
    title: string
  } | null>(null)
  const [renameBusy, setRenameBusy] = useState(false)
  const [singleDeleteTarget, setSingleDeleteTarget] = useState<{
    conversationId: string
    title: string
  } | null>(null)

  const selectableConversationIds = useMemo(
    () => conversations.map((conversation) => conversation.conversationId),
    [conversations]
  )

  const handleNewConversation = () => {
    if (!userId) return
    createConversation(userId)
    setChatHistoryOpen(false)
  }

  const handleSelectConversation = (conversationId: string) => {
    if (!userId) return
    selectConversation(userId, conversationId)
    setChatHistoryOpen(false)
  }

  const handleDeleteConversation = (conversationId: string) => {
    if (!userId) return
    const targetConversation = conversations.find(
      (conversation) => conversation.conversationId === conversationId
    )
    setSingleDeleteTarget({
      conversationId,
      title: targetConversation?.title ?? 'this chat',
    })
  }

  const handleRenameConversation = (conversationId: string) => {
    if (!userId) return
    const targetConversation = conversations.find(
      (conversation) => conversation.conversationId === conversationId
    )
    setRenameTarget({
      conversationId,
      title: targetConversation?.title ?? 'New chat',
    })
  }

  const handleConfirmRename = (conversationTitle: string) => {
    if (!userId || !renameTarget) return
    setRenameBusy(true)
    void (async () => {
      try {
        const response = await renameConversationApi(
          renameTarget.conversationId,
          conversationTitle
        )
        renameConversation(userId, renameTarget.conversationId, response.conversation_title)
        toast.success('Chat renamed.')
        setRenameTarget(null)
      } catch (error) {
        const canonical = normalizeError(error)
        console.error('[chat] rename conversation failed', {
          error,
          canonical,
        })
        toast.error(
          canonical.error_code === 'UNKNOWN'
            ? 'Could not rename that chat. Please try again.'
            : canonical.message
        )
      } finally {
        setRenameBusy(false)
      }
    })()
  }

  const handleConfirmSingleDelete = () => {
    if (!userId || !singleDeleteTarget) return
    void (async () => {
      try {
        await deleteConversationApi(singleDeleteTarget.conversationId)
        deleteConversation(userId, singleDeleteTarget.conversationId)
        toast.success('Chat deleted.')
        setSingleDeleteTarget(null)
      } catch (error) {
        const canonical = normalizeError(error)
        console.error('[chat] delete conversation failed', {
          error,
          canonical,
        })
        toast.error(
          canonical.error_code === 'UNKNOWN'
            ? 'Could not delete that chat. Please try again.'
            : canonical.message
        )
      }
    })()
  }

  const handleOpenBulkDelete = () => {
    if (!userId || conversations.length === 0) return
    setSelectedConversationIds([])
    setBulkDeleteSearchQuery('')
    setBulkDeleteOpen(true)
  }

  const handleToggleConversation = (conversationId: string) => {
    setSelectedConversationIds((current) =>
      current.includes(conversationId)
        ? current.filter((id) => id !== conversationId)
        : [...current, conversationId]
    )
  }

  const handleToggleAll = () => {
    setSelectedConversationIds((current) =>
      current.length === selectableConversationIds.length ? [] : selectableConversationIds
    )
  }

  const handleBulkDelete = async () => {
    if (!userId || selectedConversationIds.length === 0) return
    setBulkDeleteBusy(true)
    try {
      const response = await bulkDeleteConversations(selectedConversationIds)
      const deletedIds =
        response.deleted_conversation_ids.length > 0
          ? response.deleted_conversation_ids
          : selectedConversationIds
      deleteConversations(userId, deletedIds)
      toast.success(
        selectedConversationIds.length === 1
          ? 'Deleted 1 chat.'
          : `Deleted ${selectedConversationIds.length} chats.`
      )
      setBulkDeleteOpen(false)
      setSelectedConversationIds([])
    } catch (error) {
      const canonical = normalizeError(error)
      console.error('[chat] bulk delete conversations failed', {
        error,
        canonical,
      })
      toast.error(
        canonical.error_code === 'UNKNOWN'
          ? 'Could not delete the selected chats. Please try again.'
          : canonical.message
      )
    } finally {
      setBulkDeleteBusy(false)
    }
  }

  const panel = (
    <div className="flex h-full flex-col bg-gray-50">
      <div className="border-b border-gray-100 px-4 py-4">
        <p className="text-[10px] font-semibold uppercase tracking-widest text-gray-400">
          Conversations
        </p>
        <button
          onClick={handleNewConversation}
          className="mt-3 flex w-full items-center justify-center gap-2 rounded-xl bg-navy-900 px-4 py-2.5 text-sm font-medium text-white transition-colors hover:bg-navy-700 active:scale-[0.98]"
        >
          <MessageSquarePlus className="h-4 w-4" />
          New chat
        </button>
        <button
          onClick={handleOpenBulkDelete}
          disabled={conversations.length === 0}
          className="mt-2 flex w-full items-center justify-center gap-2 rounded-xl border border-gray-200 bg-white px-4 py-2.5 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <ListChecks className="h-4 w-4" />
          Manage chats
        </button>
      </div>

      <div className="flex-1 overflow-y-auto px-3 py-3">
        {conversationLoadStatus === 'failed' && (
          <p role="alert" className="mb-3 rounded-lg bg-red-50 px-3 py-2 text-xs text-red-700">
            Could not load saved conversations. Refresh to try again.
          </p>
        )}
        {conversations.length === 0 ? (
          <div className="mt-4 rounded-xl border border-dashed border-gray-200 px-4 py-6 text-center">
            <p className="text-xs text-gray-400">
              {conversationLoadStatus === 'loading'
                ? 'Loading conversations...'
                : conversationLoadStatus === 'failed'
                  ? 'Saved conversations could not be loaded'
                  : 'No conversations yet'}
            </p>
            {conversationLoadStatus !== 'loading' && (
              <p className="mt-1 text-[11px] text-gray-300">Start a chat to begin</p>
            )}
          </div>
        ) : (
          <div className="space-y-0.5">
            {conversations.map((conversation) => {
              const isActive = conversation.conversationId === activeConversationId
              return (
                <div
                  key={conversation.conversationId}
                  className={cn(
                    'group flex items-stretch gap-1 rounded-lg px-1 py-0.5 transition-all',
                    isActive ? 'bg-navy-50' : 'hover:bg-gray-50'
                  )}
                >
                  <button
                    onClick={() => handleSelectConversation(conversation.conversationId)}
                    className={cn(
                      'min-w-0 flex-1 rounded-lg px-3 py-2.5 text-left transition-all',
                      isActive ? 'text-navy-900' : 'text-gray-700'
                    )}
                  >
                    <p className={cn(
                      'truncate text-sm',
                      isActive ? 'font-semibold text-navy-900' : 'font-medium text-gray-800'
                    )}>
                      {conversation.title}
                    </p>
                    <p className="mt-0.5 text-[11px] text-gray-400">
                      {formatDate(conversation.updatedAt)}
                    </p>
                  </button>

                  <button
                    onClick={() => handleRenameConversation(conversation.conversationId)}
                    aria-label={`Rename ${conversation.title}`}
                    title="Rename chat"
                    className={cn(
                      'mt-1.5 inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-gray-300 transition-colors',
                      'hover:bg-gray-50 hover:text-gray-600 focus-visible:bg-gray-50 focus-visible:text-gray-600',
                      'opacity-0 group-hover:opacity-100 group-focus-within:opacity-100'
                    )}
                  >
                    <PencilLine className="h-4 w-4" />
                  </button>

                  <button
                    onClick={() => handleDeleteConversation(conversation.conversationId)}
                    aria-label={`Delete ${conversation.title}`}
                    title="Delete chat"
                    className={cn(
                      'mt-1.5 inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-gray-300 transition-colors',
                      'hover:bg-red-50 hover:text-red-600 focus-visible:bg-red-50 focus-visible:text-red-600',
                      'opacity-0 group-hover:opacity-100 group-focus-within:opacity-100'
                    )}
                  >
                    <Trash2 className="h-4 w-4" />
                  </button>
                </div>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )

  return (
    <>
      <aside className="hidden w-72 shrink-0 flex-col overflow-hidden border-r border-gray-200 md:flex">
        {panel}
      </aside>

      {chatHistoryOpen && (
        <div className="absolute inset-0 z-20 md:hidden">
          <button
            aria-label="Close previous chats"
            className="absolute inset-0 bg-black/30 backdrop-blur-sm"
            onClick={() => setChatHistoryOpen(false)}
          />
          <aside className="absolute inset-y-0 left-0 w-[82%] max-w-xs border-r border-gray-200 shadow-2xl">
            {panel}
          </aside>
        </div>
      )}

      {bulkDeleteOpen && (
        <ConversationDeleteModal
          conversations={conversations}
          selectedConversationIds={selectedConversationIds}
          searchQuery={bulkDeleteSearchQuery}
          busy={bulkDeleteBusy}
          onChangeSearch={setBulkDeleteSearchQuery}
          onToggleConversation={handleToggleConversation}
          onToggleAll={handleToggleAll}
          onCancel={() => {
            if (bulkDeleteBusy) return
            setBulkDeleteOpen(false)
            setSelectedConversationIds([])
          }}
          onDeleteSelected={handleBulkDelete}
        />
      )}

      {singleDeleteTarget && (
        <ConfirmModal
          open={true}
          onOpenChange={(open) => {
            if (!open) setSingleDeleteTarget(null)
          }}
          title="Delete conversation?"
          description={`Delete "${singleDeleteTarget.title}" from the backend and remove it from your sidebar? This cannot be undone from the chat history.`}
          confirmLabel="Delete chat"
          cancelLabel="Cancel"
          variant="danger"
          onConfirm={handleConfirmSingleDelete}
        />
      )}

      {renameTarget && (
        <ConversationRenameModal
          open={true}
          onOpenChange={(open) => {
            if (!open) setRenameTarget(null)
          }}
          currentTitle={renameTarget.title}
          loading={renameBusy}
          onRename={handleConfirmRename}
        />
      )}
    </>
  )
}
