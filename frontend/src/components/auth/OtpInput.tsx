import { useRef, type ClipboardEvent, type KeyboardEvent } from 'react'
import { cn } from '@/lib/utils'

interface OtpInputProps {
  value: string
  onChange: (value: string) => void
  length?: number
  disabled?: boolean
}

export function OtpInput({
  value,
  onChange,
  length = 6,
  disabled = false,
}: OtpInputProps) {
  const inputRefs = useRef<(HTMLInputElement | null)[]>([])

  const digits = value.split('').concat(Array(length).fill('')).slice(0, length)

  const focusNext = (idx: number) => {
    if (idx < length - 1) inputRefs.current[idx + 1]?.focus()
  }

  const focusPrev = (idx: number) => {
    if (idx > 0) inputRefs.current[idx - 1]?.focus()
  }

  const handleChange = (idx: number, char: string) => {
    if (!/^\d$/.test(char)) return
    const next = [...digits]
    next[idx] = char
    onChange(next.join('').trimEnd())
    focusNext(idx)
  }

  const handleKeyDown = (idx: number, event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Backspace') {
      event.preventDefault()
      const next = [...digits]

      if (digits[idx]) {
        next[idx] = ''
        onChange(next.join('').trimEnd())
        return
      }

      if (idx > 0) {
        next[idx - 1] = ''
        onChange(next.join('').trimEnd())
        focusPrev(idx)
      }
      return
    }

    if (event.key === 'ArrowLeft') {
      event.preventDefault()
      focusPrev(idx)
    }

    if (event.key === 'ArrowRight') {
      event.preventDefault()
      focusNext(idx)
    }
  }

  const handlePaste = (event: ClipboardEvent<HTMLInputElement>) => {
    event.preventDefault()
    const pasted = event.clipboardData.getData('text').replace(/\D/g, '').slice(0, length)
    if (!pasted) return

    onChange(pasted)
    inputRefs.current[Math.min(pasted.length, length) - 1]?.focus()
  }

  return (
    <div className="space-y-2">
      <div className="flex gap-3">
        {digits.map((digit, idx) => (
          <input
            key={idx}
            ref={(element) => {
              inputRefs.current[idx] = element
            }}
            type="text"
            inputMode="numeric"
            maxLength={1}
            value={digit}
            disabled={disabled}
            onChange={(event) => handleChange(idx, event.target.value)}
            onKeyDown={(event) => handleKeyDown(idx, event)}
            onPaste={handlePaste}
            className={cn(
              'h-12 w-11 rounded-input border text-center text-body font-medium transition-all focus:outline-none focus-visible:border-transparent focus-visible:ring-2 focus-visible:ring-navy-500',
              digit ? 'border-navy-500 bg-navy-50' : 'border-gray-200 bg-white',
              disabled && 'cursor-not-allowed opacity-50'
            )}
          />
        ))}
      </div>
      <p className="text-small text-gray-400">Codes are numeric and usually arrive as 4 to 6 digits.</p>
    </div>
  )
}
