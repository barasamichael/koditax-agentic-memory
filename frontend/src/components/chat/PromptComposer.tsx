import { useRef, KeyboardEvent } from 'react'
import { Send } from 'lucide-react'
import { cn } from '@/lib/utils'

interface PromptComposerProps {
  onSend: (message: string) => void
  isPending: boolean
  value: string
  onChange: (value: string) => void
}

export function PromptComposer({ onSend, isPending, value, onChange }: PromptComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  const adjustHeight = () => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    const lineHeight = 24
    const maxRows = 5
    const newHeight = Math.min(el.scrollHeight, lineHeight * maxRows + 24)
    el.style.height = `${newHeight}px`
  }

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      if (value.trim() && !isPending) {
        onSend(value.trim())
        onChange('')
        if (textareaRef.current) {
          textareaRef.current.style.height = 'auto'
        }
      }
    }
  }

  const handleSend = () => {
    if (value.trim() && !isPending) {
      onSend(value.trim())
      onChange('')
      if (textareaRef.current) {
        textareaRef.current.style.height = 'auto'
      }
    }
  }

  return (
    <div className="shrink-0 border-t border-gray-100 p-4">
      <div className="relative">
        <textarea
          ref={textareaRef}
          value={value}
          onChange={(e) => {
            onChange(e.target.value)
            adjustHeight()
          }}
          onKeyDown={handleKeyDown}
          disabled={isPending}
          placeholder="Ask Kodi about your taxes…"
          rows={1}
          className={cn(
            'w-full border border-gray-200 rounded-card p-3 pr-12 resize-none',
            'text-sm text-gray-800 placeholder-gray-400',
            'focus:outline-none focus-visible:ring-2 focus-visible:ring-navy-500 focus-visible:border-transparent',
            'transition-all overflow-hidden',
            isPending && 'opacity-60 cursor-not-allowed'
          )}
        />
        <button
          onClick={handleSend}
          disabled={!value.trim() || isPending}
          className={cn(
            'absolute right-3 bottom-3 w-8 h-8 rounded-lg flex items-center justify-center transition-all',
            value.trim() && !isPending
              ? 'bg-navy-900 text-white hover:bg-navy-700 active:scale-[0.95]'
              : 'bg-gray-100 text-gray-400 cursor-not-allowed'
          )}
          aria-label="Send message"
        >
          <Send className="w-4 h-4" />
        </button>
      </div>
    </div>
  )
}
