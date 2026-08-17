import { useEffect, useState } from 'react'
import { AppShell } from '@/components/layout/AppShell'
import { ConversationList } from '@/components/chat/ConversationList'
import { ConversationThread } from '@/components/chat/ConversationThread'
import { MessageComposer } from '@/components/chat/MessageComposer'
import { useChat } from '@/hooks/useChat'
import { useAuthStore } from '@/stores/authStore'
import { useChatStore } from '@/stores/chatStore'
import { fetchConversations } from '@/api/orchestration.api'

export default function ChatPage() {
  const [composerValue, setComposerValue] = useState('')
  const userId = useAuthStore((state) => state.userId)
  const hydrateForUser = useChatStore((state) => state.hydrateForUser)
  const setConversationLoadStatus = useChatStore(
    (state) => state.setConversationLoadStatus
  )
  const setConversationsFromBackend = useChatStore((state) => state.setConversationsFromBackend)
  const conversationId = useChatStore((state) => state.conversationId)
  const messages = useChatStore((state) => state.messages)
  const {
    sendMessage,
    retryMessage,
    dismissMessage,
    cancelQuery,
    isPending,
    attachment,
    attachmentError,
    attachmentProgress,
    attachmentStage,
    attachDocument,
    removeAttachment,
  } = useChat()

  useEffect(() => {
    if (!userId) return
    hydrateForUser(userId)
  }, [userId, hydrateForUser])

  useEffect(() => {
    if (!userId) return
    const controller = new AbortController()
    setConversationLoadStatus('loading', userId)
    void (async () => {
      try {
        const response = await fetchConversations(controller.signal)
        setConversationsFromBackend(userId, response.conversations)
      } catch (error) {
        if (controller.signal.aborted) return
        console.error('[chat] failed to hydrate conversations from backend', error)
        setConversationLoadStatus('failed', userId)
      }
    })()
    return () => controller.abort()
  }, [setConversationLoadStatus, setConversationsFromBackend, userId])

  useEffect(() => {
    setComposerValue('')
  }, [conversationId])

  const currentProgressLabel =
    [...messages]
      .reverse()
      .find(
        (message) =>
          message.role === 'assistant' &&
          (message.metadata?.assistantState === 'pending' ||
            message.metadata?.assistantState === 'running')
      )?.metadata?.progressLabel ?? null
  const composerProgressLabel =
    attachmentStage === 'uploading'
      ? 'Uploading document...'
      : attachmentStage === 'binding'
        ? 'Saving the attachment...'
        : currentProgressLabel

  return (
    <AppShell>
      <div className="relative flex flex-1 overflow-hidden">
        <ConversationList />

        <div className="flex min-w-0 flex-1 flex-col overflow-hidden bg-white">
          <ConversationThread
            onRetry={retryMessage}
            onDismissMessage={dismissMessage}
          />
          <MessageComposer
            value={composerValue}
            onChange={setComposerValue}
            onSend={sendMessage}
            onCancel={cancelQuery}
            isPending={isPending}
            progressLabel={composerProgressLabel}
            attachmentError={attachmentError}
            attachment={
              attachment
                ? {
                    ...attachment,
                    progressLabel:
                      attachmentStage === 'uploading'
                        ? 'Uploading document...'
                        : attachmentStage === 'binding'
                          ? 'Saving the attachment...'
                          : 'Attached to this question',
                    progressPercent:
                      attachmentStage === 'uploading'
                        ? attachmentProgress?.percent ?? null
                        : null,
                  }
                : null
            }
            onAttachDocument={attachDocument}
            onRemoveAttachment={removeAttachment}
          />
        </div>
      </div>
    </AppShell>
  )
}
