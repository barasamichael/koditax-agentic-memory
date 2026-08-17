import { useState, useEffect, useRef } from 'react'
import { X } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Spinner } from './Spinner'

interface ConfirmModalProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description: string
  confirmLabel?: string
  cancelLabel?: string
  variant?: 'default' | 'danger'
  confirmInput?: string
  onConfirm: () => void | Promise<void>
  loading?: boolean
}

export function ConfirmModal({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  variant = 'default',
  confirmInput,
  onConfirm,
  loading = false,
}: ConfirmModalProps) {
  const [inputValue, setInputValue] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)
  const overlayRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) setInputValue('')
  }, [open])

  useEffect(() => {
    if (open && confirmInput) {
      setTimeout(() => inputRef.current?.focus(), 50)
    }
  }, [open, confirmInput])

  useEffect(() => {
    if (!open) return
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onOpenChange(false)
    }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [open, onOpenChange])

  if (!open) return null

  const confirmEnabled = !loading && (!confirmInput || inputValue === confirmInput)

  const handleConfirm = async () => {
    if (!confirmEnabled) return
    await onConfirm()
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="confirm-modal-title"
      ref={overlayRef}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
      onMouseDown={(e) => {
        if (e.target === overlayRef.current) onOpenChange(false)
      }}
    >
      <div className="bg-white rounded-xl shadow-xl w-full max-w-md mx-4 p-6">
        <div className="flex items-start justify-between mb-4">
          <h2 id="confirm-modal-title" className="text-base font-semibold text-gray-900">
            {title}
          </h2>
          <button
            onClick={() => onOpenChange(false)}
            className="text-gray-400 hover:text-gray-600 transition-colors ml-4"
            aria-label="Close"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <p className="text-sm text-gray-600 mb-4">{description}</p>

        {confirmInput && (
          <div className="mb-4">
            <label className="block text-xs text-gray-500 mb-1.5">
              Type <span className="font-mono font-medium text-gray-800">{confirmInput}</span> to confirm
            </label>
            <input
              ref={inputRef}
              type="text"
              value={inputValue}
              onChange={(e) => setInputValue(e.target.value)}
              placeholder={`Type "${confirmInput}" to confirm`}
              className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm outline-none focus:border-navy-500 focus:ring-1 focus:ring-navy-500"
            />
          </div>
        )}

        <div className="flex items-center justify-end gap-3">
          <button
            onClick={() => onOpenChange(false)}
            className="px-4 py-2 text-sm text-gray-600 border border-gray-200 rounded-lg hover:bg-gray-50 transition-all"
            disabled={loading}
          >
            {cancelLabel}
          </button>
          <button
            onClick={handleConfirm}
            disabled={!confirmEnabled}
            className={cn(
              'px-4 py-2 text-sm font-medium rounded-lg transition-all flex items-center gap-2',
              variant === 'danger'
                ? 'bg-red-600 hover:bg-red-700 text-white disabled:bg-red-300'
                : 'bg-navy-900 hover:bg-navy-800 text-white disabled:bg-navy-300',
              !confirmEnabled && 'cursor-not-allowed'
            )}
          >
            {loading && <Spinner size="sm" className="w-3.5 h-3.5" />}
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
