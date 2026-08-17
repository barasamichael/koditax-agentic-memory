import { type ChangeEvent, type KeyboardEvent, useRef, useState } from 'react'
import { ArrowUp, FileText, Paperclip, Square, X } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { ChatAttachment } from '@/types/chat'

interface ComposerAttachmentState extends ChatAttachment {
  progressLabel?: string | null
  progressPercent?: number | null
  error?: string | null
}

interface MessageComposerProps {
  onSend: (message: string) => Promise<boolean> | boolean
  onCancel?: () => void
  isPending: boolean
  value: string
  onChange: (value: string) => void
  progressLabel?: string | null
  attachmentError?: string | null
  attachment?: ComposerAttachmentState | null
  onAttachDocument?: (file: File) => Promise<boolean> | boolean
  onRemoveAttachment?: () => void
}

export function MessageComposer({
  onSend,
  onCancel,
  isPending,
  value,
  onChange,
  progressLabel,
  attachmentError,
  attachment,
  onAttachDocument,
  onRemoveAttachment,
}: MessageComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [isSubmitting, setIsSubmitting] = useState(false)

  const adjustHeight = () => {
    const element = textareaRef.current
    if (!element) return

    element.style.height = 'auto'
    const lineHeight = 24
    const maxRows = 5
    const nextHeight = Math.min(element.scrollHeight, lineHeight * maxRows + 24)
    element.style.height = `${nextHeight}px`
  }

  const submit = async () => {
    const trimmed = value.trim()
    if (!trimmed || isPending || isSubmitting) return

    setIsSubmitting(true)
    try {
      const sent = await onSend(trimmed)
      if (sent) {
        onChange('')
        if (textareaRef.current) {
          textareaRef.current.style.height = 'auto'
        }
      }
    } finally {
      setIsSubmitting(false)
    }
  }

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      submit()
    }
  }

  const handleAttachClick = () => {
    if (isPending || isSubmitting || !onAttachDocument) return
    fileInputRef.current?.click()
  }

  const handleFileChange = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file || !onAttachDocument) return
    await onAttachDocument(file)
  }

  return (
    <div className="shrink-0 border-t border-gray-100 bg-white px-4 py-3 sm:px-5 sm:py-4">
      {attachment && (
        <div className="mb-2 rounded-2xl border border-navy-100 bg-navy-50 px-3 py-2">
          <div className="flex items-start gap-3">
            <div className="mt-0.5 rounded-lg bg-white p-2 text-navy-700">
              <FileText className="h-4 w-4" />
            </div>
            <div className="min-w-0 flex-1">
              <div className="flex min-w-0 items-start justify-between gap-2">
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-navy-900">
                    {attachment.displayName}
                  </p>
                  <p className="text-xs text-navy-700">
                    {attachment.progressLabel ??
                      (attachment.fileExtension ? attachment.fileExtension.toUpperCase() : 'Document')}
                  </p>
                </div>
                {onRemoveAttachment && (
                  <button
                    onClick={onRemoveAttachment}
                    className="rounded-md p-1 text-navy-700 transition-colors hover:bg-white hover:text-navy-900"
                    aria-label="Remove attached document"
                  >
                    <X className="h-4 w-4" />
                  </button>
                )}
              </div>
              {attachment.progressPercent != null && attachment.progressPercent >= 0 && (
                <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-white/80">
                  <div
                    className="h-full rounded-full bg-navy-700 transition-all"
                    style={{ width: `${Math.min(100, attachment.progressPercent)}%` }}
                  />
                </div>
              )}
            </div>
          </div>
        </div>
      )}
      <div className="relative flex items-end gap-2 rounded-2xl border border-gray-200 bg-gray-50 px-4 py-3 transition-all focus-within:border-navy-400 focus-within:bg-white focus-within:shadow-sm">
        <input
          ref={fileInputRef}
          type="file"
          className="hidden"
          accept=".pdf,.txt,.md,.csv,.tsv,.json,.xml,.rtf,.docx,.xlsx,.pptx,.odt,.ods,.odp,.png,.jpg,.jpeg,.webp,.tif,.tiff"
          onChange={handleFileChange}
        />
        <button
          type="button"
          onClick={handleAttachClick}
          disabled={isPending || isSubmitting}
          className="mb-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-gray-500 transition-all hover:bg-white hover:text-navy-700 disabled:cursor-not-allowed disabled:opacity-50"
          aria-label="Attach document"
          title="Attach document"
        >
          <Paperclip className="h-3.5 w-3.5" />
        </button>
        <textarea
          ref={textareaRef}
          value={value}
          onChange={(event) => {
            onChange(event.target.value)
            adjustHeight()
          }}
          onKeyDown={handleKeyDown}
          placeholder="Ask Kodi about your tax situation..."
          rows={1}
          className="flex-1 resize-none bg-transparent text-sm text-gray-800 placeholder-gray-400 focus:outline-none overflow-hidden"
        />
        {isPending || isSubmitting ? (
          <button
            onClick={onCancel}
            className="mb-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-red-100 text-red-600 transition-all hover:bg-red-200 active:scale-[0.92]"
            aria-label="Cancel"
          >
            <Square className="h-3.5 w-3.5 fill-current" />
          </button>
        ) : (
          <button
            onClick={submit}
            disabled={!value.trim()}
            className={cn(
              'mb-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg transition-all',
              value.trim()
                ? 'bg-navy-900 text-white hover:bg-navy-700 active:scale-[0.92]'
                : 'cursor-not-allowed bg-gray-200 text-gray-400'
            )}
            aria-label="Send message"
          >
            <ArrowUp className="h-3.5 w-3.5" />
          </button>
        )}
      </div>

      <p className="mt-2 text-center text-[11px] text-gray-400">
        {isPending || isSubmitting
          ? (progressLabel ?? 'Processing...')
          : attachmentError ?? 'Enter to send · Shift+Enter for new line'}
      </p>
    </div>
  )
}
