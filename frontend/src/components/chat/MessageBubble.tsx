import { useState, useCallback } from 'react'
import { Check, Copy, FileText } from 'lucide-react'
import type { ChatMessage } from '@/types/chat'
import { ActionApprovalCard } from './ActionApprovalCard'
import { FinalOutcomeCard } from './FinalOutcomeCard'
import { useChatStore } from '@/stores/chatStore'
import { cn, formatDate } from '@/lib/utils'

interface MessageBubbleProps {
  message: ChatMessage
  conversationId?: string | null
  onRetry?: (prompt: string, conversationId: string, messageId: string) => void
  onDismiss?: (messageId: string, conversationId?: string) => void
  questionText?: string
}

function TypingIndicator() {
  return (
    <div className="flex gap-1">
      {[0, 1, 2].map((index) => (
        <span
          key={index}
          className="h-1.5 w-1.5 rounded-full bg-gray-300 animate-bounce"
          style={{ animationDelay: `${index * 120}ms` }}
        />
      ))}
    </div>
  )
}

function Timestamp({ value, align = 'left' }: { value: string; align?: 'left' | 'right' }) {
  return (
    <p className={cn('text-[11px] text-gray-400 px-1', align === 'right' && 'text-right')}>
      {formatDate(value)}
    </p>
  )
}

function InlineCopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)

  const handleCopy = useCallback(() => {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    })
  }, [text])

  return (
    <button
      onClick={handleCopy}
      title="Copy"
      className="ml-2 inline-flex items-center gap-1 rounded-md p-1 text-[11px] text-white/50 transition-colors hover:text-white/90"
    >
      {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
    </button>
  )
}

export function MessageBubble({
  message,
  conversationId,
  onRetry,
  onDismiss,
  questionText,
}: MessageBubbleProps) {
  const pendingAction = useChatStore((state) => state.pendingAction)
  const isUser = message.role === 'user'
  const assistantState = message.metadata?.assistantState

  if (message.type === 'action_approval') {
    if (pendingAction?.id === message.id) {
      return (
        <div className="flex justify-start">
          <div className="w-full max-w-[640px] space-y-1">
            <ActionApprovalCard action={pendingAction} />
            <Timestamp value={message.timestamp} />
          </div>
        </div>
      )
    }

    return (
      <div className="flex justify-start">
        <div className="w-full max-w-[640px] space-y-1">
          <div className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3">
            <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-amber-600">
              Confirmation expired
            </p>
            <p className="text-sm text-amber-900">
              This step is no longer active. Ask the question again to start fresh.
            </p>
          </div>
          <Timestamp value={message.timestamp} />
        </div>
      </div>
    )
  }

  if (message.type === 'outcome') {
    return (
      <div className="flex justify-start">
        <div className="w-full max-w-[640px] space-y-1">
          <FinalOutcomeCard message={message} questionText={questionText} />
          <Timestamp value={message.timestamp} />
        </div>
      </div>
    )
  }

  if (!isUser && (assistantState === 'pending' || assistantState === 'running')) {
    return (
      <div className="flex justify-start">
        <div className="w-full max-w-[640px] space-y-1">
          <div className="rounded-xl border border-gray-100 bg-white px-4 py-3 shadow-sm">
            <div className="flex items-center gap-3">
              <TypingIndicator />
              <p className="text-xs text-gray-400">{message.metadata?.progressLabel ?? message.content}</p>
            </div>
          </div>
          <Timestamp value={message.timestamp} />
        </div>
      </div>
    )
  }

  if (message.type === 'error' || assistantState === 'failed') {
    const retryPrompt = message.metadata?.retryPrompt
    const retryConversationId = conversationId ?? undefined

    return (
      <div className="flex justify-start">
        <div className="w-full max-w-[640px] space-y-1">
          <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3">
            <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-red-500">
              Something went wrong
            </p>
            <p className="text-sm text-red-900">{message.content}</p>
            {message.metadata?.progressLabel && (
              <p className="mt-1.5 text-xs text-red-600/70">{message.metadata.progressLabel}</p>
            )}
            <div className="mt-3 flex items-center gap-2">
              {message.metadata?.retryable && retryPrompt && retryConversationId && (
                <button
                  onClick={() => onRetry?.(retryPrompt, retryConversationId, message.id)}
                  disabled={!onRetry}
                  className="rounded-lg bg-red-600 px-3 py-1.5 text-xs font-semibold text-white transition-colors hover:bg-red-700 active:scale-[0.97]"
                >
                  Try again
                </button>
              )}
              <button
                onClick={() => onDismiss?.(message.id, retryConversationId)}
                disabled={!onDismiss}
                className="rounded-lg border border-red-200 px-3 py-1.5 text-xs font-medium text-red-700 transition-colors hover:bg-red-100"
              >
                Dismiss
              </button>
            </div>
          </div>
          <Timestamp value={message.timestamp} />
        </div>
      </div>
    )
  }

  return (
    <div className={cn('flex', isUser ? 'justify-end' : 'justify-start')}>
      <div className={cn('max-w-[88%] space-y-1 sm:max-w-[640px]')}>
        <div
          className={cn(
            'group relative px-4 py-3 text-sm leading-relaxed',
            isUser
              ? 'rounded-[18px_18px_4px_18px] bg-navy-900 text-white'
              : 'rounded-[4px_18px_18px_18px] border border-gray-100 bg-white text-gray-800 shadow-sm'
          )}
        >
          {message.content}
          {isUser && (
            <span className="opacity-0 group-hover:opacity-100 transition-opacity absolute top-2 right-2">
              <InlineCopyButton text={message.content} />
            </span>
          )}
          {isUser && message.metadata?.documents?.length ? (
            <div className="mt-2 flex flex-wrap gap-2">
              {message.metadata.documents.map((document) => (
                <span
                  key={document.id}
                  className="inline-flex max-w-full items-center gap-2 rounded-full border border-white/20 bg-white/10 px-3 py-1 text-[11px] text-white/90"
                >
                  <FileText className="h-3 w-3 shrink-0" />
                  <span className="truncate">{document.displayName}</span>
                </span>
              ))}
            </div>
          ) : null}
        </div>
        <Timestamp value={message.timestamp} align={isUser ? 'right' : 'left'} />
      </div>
    </div>
  )
}
