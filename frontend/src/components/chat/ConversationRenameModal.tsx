import { useEffect, useRef, useState } from 'react'
import { X } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Spinner } from '@/components/shared/Spinner'

interface ConversationRenameModalProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  currentTitle: string
  onRename: (title: string) => void | Promise<void>
  loading?: boolean
}

export function ConversationRenameModal({
  open,
  onOpenChange,
  currentTitle,
  onRename,
  loading = false,
}: ConversationRenameModalProps) {
  const [title, setTitle] = useState(currentTitle)
  const inputRef = useRef<HTMLInputElement>(null)
  const overlayRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (open) {
      setTitle(currentTitle)
    }
  }, [currentTitle, open])

  useEffect(() => {
    if (open) {
      setTimeout(() => inputRef.current?.focus(), 50)
    }
  }, [open])

  useEffect(() => {
    if (!open) return
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onOpenChange(false)
    }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [open, onOpenChange])

  if (!open) return null

  const trimmedTitle = title.trim()
  const canSubmit = !loading && trimmedTitle.length > 0 && trimmedTitle !== currentTitle

  const handleSubmit = async () => {
    if (!canSubmit) return
    await onRename(trimmedTitle)
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="conversation-rename-title"
      ref={overlayRef}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 backdrop-blur-[1px]"
      onMouseDown={(event) => {
        if (event.target === overlayRef.current) onOpenChange(false)
      }}
    >
      <div className="w-full max-w-md rounded-3xl border border-gray-200 bg-white p-6 shadow-2xl">
        <div className="flex items-start justify-between gap-4">
          <div>
            <p className="text-[10px] font-semibold uppercase tracking-[0.28em] text-navy-600">
              Rename chat
            </p>
            <h2 id="conversation-rename-title" className="mt-1 text-lg font-semibold text-gray-900">
              Rename conversation
            </h2>
          </div>
          <button
            onClick={() => onOpenChange(false)}
            className="rounded-lg p-1 text-gray-400 transition-colors hover:bg-gray-50 hover:text-gray-600"
            aria-label="Close"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <p className="mt-3 text-sm text-gray-500">
          Update the title shown in your sidebar and active conversation header.
        </p>

        <label className="mt-5 block">
          <span className="mb-2 block text-xs font-medium uppercase tracking-wide text-gray-500">
            Conversation title
          </span>
          <input
            ref={inputRef}
            type="text"
            value={title}
            maxLength={80}
            onChange={(event) => setTitle(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault()
                void handleSubmit()
              }
            }}
            className="w-full rounded-2xl border border-gray-200 bg-gray-50 px-4 py-3 text-sm text-gray-900 outline-none transition-colors placeholder:text-gray-400 focus:border-navy-400 focus:bg-white focus:ring-1 focus:ring-navy-400"
            placeholder="Enter a new conversation title"
          />
        </label>

        <div className="mt-6 flex items-center justify-end gap-3">
          <button
            onClick={() => onOpenChange(false)}
            disabled={loading}
            className="rounded-xl border border-gray-200 bg-white px-4 py-2 text-sm font-medium text-gray-700 transition-colors hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            onClick={() => void handleSubmit()}
            disabled={!canSubmit}
            className={cn(
              'inline-flex items-center gap-2 rounded-xl px-4 py-2 text-sm font-medium transition-colors',
              canSubmit
                ? 'bg-navy-900 text-white hover:bg-navy-700'
                : 'cursor-not-allowed bg-navy-200 text-white'
            )}
          >
            {loading && <Spinner size="sm" className="h-3.5 w-3.5" />}
            Save title
          </button>
        </div>
      </div>
    </div>
  )
}
