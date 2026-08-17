import { useState, useCallback } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Copy, Check, ExternalLink, FileText } from 'lucide-react'
import { useToast } from '@/components/shared/Toast'
import { openDocumentPreview } from '@/lib/documents/document-access'
import type { ChatMessage, ChatSourceReference } from '@/types/chat'

interface FinalOutcomeCardProps {
  message: ChatMessage
  questionText?: string
}

function CopyButton({ text, label }: { text: string; label: string }) {
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
      title={label}
      className="flex items-center gap-1 rounded-md px-2 py-1 text-[11px] font-medium text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-600"
    >
      {copied ? <Check className="h-3 w-3 text-emerald-500" /> : <Copy className="h-3 w-3" />}
      {copied ? 'Copied' : label}
    </button>
  )
}

function SourceReferenceList({ sourceReferences }: { sourceReferences: ChatSourceReference[] }) {
  const toast = useToast()
  const [openingDocumentId, setOpeningDocumentId] = useState<string | null>(null)

  const handleOpen = async (reference: ChatSourceReference) => {
    if (!reference.openable || openingDocumentId !== null) return
    setOpeningDocumentId(reference.document_id)
    try {
      await openDocumentPreview({
        documentId: reference.document_id,
        displayName: reference.document_label,
      })
      toast.success('Source opened in a new tab.')
    } catch (error) {
      toast.error(
        error instanceof Error && error.message.trim()
          ? error.message
          : 'The source could not be opened right now.'
      )
    } finally {
      setOpeningDocumentId(null)
    }
  }

  const availabilityLabel = (reference: ChatSourceReference): string => {
    if (reference.document_status === 'available') return 'Available'
    if (reference.document_status === 'partial') return 'Partial'
    return 'Unavailable'
  }

  return (
    <div className="border-t border-green-100 px-4 py-3">
      <p className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
        Sources
      </p>
      <ul className="mt-2 space-y-2">
        {sourceReferences.map((reference) => {
          const isOpening = openingDocumentId === reference.document_id
          const isUnavailable = !reference.openable
          return (
            <li
              key={`${reference.document_id}:${reference.source_location.location_label}`}
              className="rounded-xl border border-gray-100 bg-white px-3 py-2"
            >
              <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <FileText className="h-4 w-4 shrink-0 text-gray-400" aria-hidden="true" />
                    <p className="truncate text-sm font-medium text-gray-800">
                      {reference.document_label}
                    </p>
                    <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-gray-500">
                      {availabilityLabel(reference)}
                    </span>
                  </div>
                  <p className="mt-1 text-xs text-gray-500">
                    {reference.source_location.location_label}
                  </p>
                  <p className="mt-1 text-[11px] text-gray-400">
                    {reference.accessibility_label ?? 'Source reference'}
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => void handleOpen(reference)}
                  disabled={isUnavailable || isOpening}
                  className={[
                    'inline-flex items-center gap-2 self-start rounded-lg border px-3 py-2 text-xs font-medium transition-colors',
                    isUnavailable || isOpening
                      ? 'cursor-not-allowed border-gray-200 bg-gray-50 text-gray-400'
                      : 'border-green-200 bg-green-50 text-green-800 hover:bg-green-100',
                  ].join(' ')}
                >
                  {isOpening ? (
                    <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-current border-r-transparent" />
                  ) : (
                    <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
                  )}
                  {isUnavailable ? 'Unavailable' : 'Open source'}
                </button>
              </div>
            </li>
          )
        })}
      </ul>
    </div>
  )
}

export function FinalOutcomeCard({ message, questionText }: FinalOutcomeCardProps) {
  const { metadata } = message
  const isStreaming =
    metadata?.assistantState === 'pending' || metadata?.assistantState === 'running'
  const sourceReferences = metadata?.sourceReferences ?? []

  return (
    <>
      <div className="overflow-hidden rounded-xl bg-green-50">
        <div className="flex items-center gap-2 px-4 py-2.5">
          <span
            className={[
              'h-1.5 w-1.5 rounded-full',
              isStreaming ? 'animate-pulse bg-emerald-500' : 'bg-kodi-accent',
            ].join(' ')}
          />
          <span className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
            {isStreaming ? 'Generating answer' : 'Answer'}
          </span>
        </div>

        <div className={`markdown-response px-4 pb-3 text-sm text-gray-800${isStreaming ? ' markdown-streaming' : ''}`}>
          {isStreaming && !message.content.trim() ? (
            <p className="text-sm text-gray-400 italic">Thinking...</p>
          ) : (
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
          )}
        </div>

        {sourceReferences.length > 0 && !isStreaming ? (
          <SourceReferenceList sourceReferences={sourceReferences} />
        ) : null}

        {/* Footer is always rendered at a fixed height to prevent layout shifts */}
        <div className="flex min-h-[36px] items-center justify-between border-t border-green-100 px-3 py-2">
          <div className="flex items-center gap-1">
            {!isStreaming && message.content.trim() && (
              <CopyButton text={message.content} label="Copy answer" />
            )}
            {questionText && !isStreaming && (
              <CopyButton text={questionText} label="Copy question" />
            )}
          </div>

          <div />
        </div>
      </div>
    </>
  )
}
