import { AlertCircle } from 'lucide-react'
import { EmptyState } from '@/components/shared/EmptyState'

interface DocumentsErrorStateProps {
  message: string
  onRetry: () => void
  title?: string
}

export function DocumentsErrorState({ message, onRetry, title = 'We could not load your documents' }: DocumentsErrorStateProps) {
  return (
    <EmptyState
      title={title}
      description={message}
      icon={<AlertCircle className="h-10 w-10" aria-hidden="true" />}
      action={{ label: 'Try again', onClick: onRetry }}
    />
  )
}
