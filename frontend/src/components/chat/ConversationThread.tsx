import { useEffect, useMemo, useRef, useCallback } from 'react'
import { useAuthStore } from '@/stores/authStore'
import { useChatStore } from '@/stores/chatStore'
import { EmptyState } from './EmptyState'
import { MessageBubble } from './MessageBubble'

interface ConversationThreadProps {
  onRetry: (prompt: string, conversationId: string, messageId: string) => void
  onDismissMessage: (messageId: string, conversationId?: string) => void
}

const SCROLL_THRESHOLD = 120

export function ConversationThread({
  onRetry,
  onDismissMessage,
}: ConversationThreadProps) {
  const userId = useAuthStore((state) => state.userId ?? '')
  const messages = useChatStore((state) => state.messages)
  const conversationId = useChatStore((state) => state.conversationId)
  const createConversation = useChatStore((state) => state.createConversation)
  const scrollRef = useRef<HTMLDivElement>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const userScrolledUp = useRef(false)
  // Tracks whether the bottom anchor div is controlling scroll (CSS overflow-anchor)
  const anchorEnabledRef = useRef(true)

  const isNearBottom = useCallback(() => {
    const el = scrollRef.current
    if (!el) return true
    return el.scrollHeight - el.scrollTop - el.clientHeight < SCROLL_THRESHOLD
  }, [])

  const scrollToBottom = useCallback((behavior: ScrollBehavior = 'smooth') => {
    bottomRef.current?.scrollIntoView({ behavior, block: 'end' })
  }, [])

  // Track user scroll intent so we don't hijack their position.
  // When the user is near the bottom, re-enable CSS overflow-anchor so streaming
  // content growth is handled by the browser with zero JS involvement.
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const onScroll = () => {
      const nearBottom = isNearBottom()
      userScrolledUp.current = !nearBottom
      // Enable anchor only when near bottom — the browser then keeps the
      // viewport pinned to the bottom sentinel as content grows.
      if (bottomRef.current) {
        bottomRef.current.style.overflowAnchor = nearBottom ? 'auto' : 'none'
        anchorEnabledRef.current = nearBottom
      }
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
  }, [isNearBottom])

  // Only scroll via JS for discrete events: new message appended, or stream end.
  // During streaming the CSS overflow-anchor on the bottom sentinel handles it.
  const prevMessageCountRef = useRef(messages.length)
  const wasStreamingRef = useRef(false)
  useEffect(() => {
    const isStreaming = messages.some(
      (m) =>
        m.role === 'assistant' &&
        (m.metadata?.assistantState === 'pending' ||
          m.metadata?.assistantState === 'running')
    )
    const newMessageArrived = messages.length > prevMessageCountRef.current
    const streamJustEnded = wasStreamingRef.current && !isStreaming

    prevMessageCountRef.current = messages.length
    wasStreamingRef.current = isStreaming

    // New message: always scroll to bottom with smooth animation
    if (newMessageArrived && !userScrolledUp.current) {
      scrollToBottom('smooth')
      return
    }

    // Stream ended: snap to true bottom in case anchor drifted slightly
    if (streamJustEnded && !userScrolledUp.current) {
      scrollToBottom('instant')
    }

    // During streaming: CSS overflow-anchor handles it — no JS scroll call
  }, [messages, scrollToBottom])

  // Conversation switch: jump instantly, reset state, re-enable anchor
  useEffect(() => {
    userScrolledUp.current = false
    anchorEnabledRef.current = true
    if (bottomRef.current) bottomRef.current.style.overflowAnchor = 'auto'
    scrollToBottom('instant')
  }, [conversationId, scrollToBottom])

  const hasMessages = messages.length > 0

  const handleStartNewChat = () => {
    if (!userId) return
    createConversation(userId)
  }

  const sortedMessages = useMemo(
    () =>
      [...messages].sort((left, right) =>
        left.timestamp.localeCompare(right.timestamp)
      ),
    [messages]
  )

  if (!hasMessages) {
    return <EmptyState onStartNewChat={handleStartNewChat} />
  }

  return (
    <div
      ref={scrollRef}
      // overflow-anchor: none on the container — we manage anchoring via the
      // bottom sentinel element only, not any arbitrary child mid-stream.
      className="flex flex-1 flex-col gap-4 overflow-y-auto px-4 py-4 sm:px-6 sm:py-6 [overflow-anchor:none]"
    >
      {sortedMessages.map((message, index) => {
        const precedingUserMessage =
          message.type === 'outcome'
            ? [...sortedMessages].slice(0, index).reverse().find((m) => m.role === 'user')
            : undefined
        return (
          <MessageBubble
            key={message.id}
            message={message}
            conversationId={conversationId}
            onRetry={onRetry}
            onDismiss={onDismissMessage}
            questionText={precedingUserMessage?.content}
          />
        )
      })}
      {/* overflow-anchor: auto here makes the browser keep this element
          pinned to the viewport bottom as streaming content grows above it */}
      <div ref={bottomRef} className="h-px shrink-0" style={{ overflowAnchor: 'auto' }} />
    </div>
  )
}
