import { useMemo, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { MessageSquarePlus, AlertTriangle, Clock } from 'lucide-react'
import { AppShell } from '@/components/layout/AppShell'
import { useChatStore } from '@/stores/chatStore'
import { useAuthStore } from '@/stores/authStore'
import { formatDate } from '@/lib/utils'
import { resolveShare } from '@/lib/shareStore'
import { v4 as uuid } from 'uuid'

export default function SharedChatPage() {
  const { shareId } = useParams<{ shareId: string }>()
  const navigate = useNavigate()
  const userId = useAuthStore((state) => state.userId ?? '')
  const { appendMessage, createConversation, setConversationId } = useChatStore()
  const [importing, setImporting] = useState(false)
  const [imported, setImported] = useState(false)

  const payload = useMemo(
    () => (shareId ? resolveShare(shareId) : null),
    [shareId]
  )

  const handleContinue = () => {
    if (!payload || !userId) return
    setImporting(true)

    const conversationId = createConversation(userId)
    setConversationId(userId, conversationId)

    for (const msg of payload.messages) {
      appendMessage(
        userId,
        {
          id: uuid(),
          role: msg.role,
          content: msg.content,
          timestamp: msg.timestamp,
          type: msg.type as 'text' | 'outcome',
          metadata: {
            assistantState: 'completed',
          },
        },
        conversationId
      )
    }

    setImporting(false)
    setImported(true)
    setTimeout(() => navigate('/chat'), 500)
  }

  if (!payload) {
    return (
      <AppShell>
        <div className="flex flex-1 items-center justify-center px-6 py-16">
          <div className="max-w-sm text-center">
            <div className="mx-auto mb-5 flex h-14 w-14 items-center justify-center rounded-full bg-amber-50">
              <AlertTriangle className="h-7 w-7 text-amber-400" />
            </div>
            <h2 className="mb-2 text-lg font-semibold text-gray-900">Link not found</h2>
            <p className="text-sm text-gray-500 leading-relaxed">
              This shared chat link may have expired or was opened on a different device.
              Share links are available on the device that created them.
            </p>
            <button
              onClick={() => navigate('/chat')}
              className="mt-6 rounded-xl bg-navy-900 px-5 py-2.5 text-sm font-medium text-white hover:bg-navy-700 transition-colors"
            >
              Go to Kodi
            </button>
          </div>
        </div>
      </AppShell>
    )
  }

  const userMessages = payload.messages.filter((m) => m.role === 'user').length
  const expiresAt = new Date(payload.expiresAt)
  const daysLeft = Math.ceil((expiresAt.getTime() - Date.now()) / (1000 * 60 * 60 * 24))

  return (
    <AppShell>
      <div className="flex flex-1 flex-col overflow-hidden">
        {/* Header */}
        <div className="shrink-0 border-b border-gray-100 bg-white px-6 py-4">
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0">
              <p className="text-[10px] font-semibold uppercase tracking-widest text-gray-400">
                Shared chat
              </p>
              <h1 className="mt-0.5 truncate text-base font-semibold text-gray-900">
                {payload.title}
              </h1>
              <div className="mt-1 flex items-center gap-2 text-[11px] text-gray-400">
                <span>{userMessages} question{userMessages !== 1 ? 's' : ''}</span>
                <span>·</span>
                <span className="flex items-center gap-1">
                  <Clock className="h-3 w-3" />
                  Link expires in {daysLeft} day{daysLeft !== 1 ? 's' : ''}
                </span>
              </div>
            </div>
            <button
              onClick={handleContinue}
              disabled={importing || imported}
              className="flex shrink-0 items-center gap-2 rounded-xl bg-navy-900 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-navy-700 active:scale-[0.98] disabled:opacity-60"
            >
              <MessageSquarePlus className="h-4 w-4" />
              <span className="hidden sm:inline">
                {imported ? 'Opening…' : importing ? 'Copying…' : 'Continue in my chat'}
              </span>
            </button>
          </div>
        </div>

        {/* Messages */}
        <div className="flex flex-1 flex-col gap-4 overflow-y-auto px-4 py-6 sm:px-8">
          {payload.messages.map((message, index) => (
            <div
              key={index}
              className={`flex ${message.role === 'user' ? 'justify-end' : 'justify-start'}`}
            >
              <div className="w-full max-w-[640px] space-y-1">
                {message.role === 'user' ? (
                  <div className="ml-auto max-w-[88%] rounded-[18px_18px_4px_18px] bg-navy-900 px-4 py-3 text-sm leading-relaxed text-white">
                    {message.content}
                  </div>
                ) : message.type === 'outcome' && message.content ? (
                  <div className="overflow-hidden rounded-xl bg-green-50">
                    <div className="flex items-center gap-2 px-4 py-2.5">
                      <span className="h-1.5 w-1.5 rounded-full bg-kodi-accent" />
                      <span className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
                        Answer
                      </span>
                    </div>
                    <div className="markdown-response px-4 pb-4 text-sm text-gray-800">
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
                    </div>
                  </div>
                ) : message.content ? (
                  <div className="rounded-[4px_18px_18px_18px] border border-gray-100 bg-white px-4 py-3 text-sm text-gray-800 shadow-sm">
                    {message.content}
                  </div>
                ) : null}
                <p
                  className={`px-1 text-[11px] text-gray-400 ${message.role === 'user' ? 'text-right' : ''}`}
                >
                  {formatDate(message.timestamp)}
                </p>
              </div>
            </div>
          ))}
        </div>

        {/* Footer CTA */}
        <div className="shrink-0 border-t border-gray-100 bg-gray-50 px-6 py-4">
          <div className="flex flex-col items-center gap-2 text-center sm:flex-row sm:justify-between sm:text-left">
            <p className="text-sm text-gray-500">
              Want to ask follow-up questions? Continue this conversation in your own chat.
            </p>
            <button
              onClick={handleContinue}
              disabled={importing || imported}
              className="flex shrink-0 items-center gap-2 rounded-xl bg-navy-900 px-5 py-2.5 text-sm font-medium text-white transition-colors hover:bg-navy-700 active:scale-[0.98] disabled:opacity-60"
            >
              <MessageSquarePlus className="h-4 w-4" />
              {imported ? 'Opening…' : importing ? 'Copying…' : 'Continue in my chat'}
            </button>
          </div>
        </div>
      </div>
    </AppShell>
  )
}
