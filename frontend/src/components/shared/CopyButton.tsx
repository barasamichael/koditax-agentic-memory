import { useState } from 'react'
import { Copy, Check } from 'lucide-react'
import { cn } from '@/lib/utils'

interface CopyButtonProps {
  value: string
  size?: number
  className?: string
}

export function CopyButton({ value, size = 14, className }: CopyButtonProps) {
  const [copied, setCopied] = useState(false)

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      // Clipboard API not available — silent fail
    }
  }

  const Icon = copied ? Check : Copy

  return (
    <button
      onClick={handleCopy}
      className={cn(
        'text-gray-400 hover:text-gray-600 transition-colors',
        copied && 'text-green-600',
        className
      )}
      aria-label={copied ? 'Copied' : 'Copy to clipboard'}
      title={copied ? 'Copied!' : 'Copy'}
    >
      <Icon style={{ width: size, height: size }} />
    </button>
  )
}
