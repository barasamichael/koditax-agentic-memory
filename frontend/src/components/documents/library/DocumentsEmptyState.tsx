import { FileText } from 'lucide-react'
import { EmptyState } from '@/components/shared/EmptyState'

interface DocumentsEmptyStateProps {
  onAddDocument: () => void
}

export function DocumentsEmptyState({ onAddDocument }: DocumentsEmptyStateProps) {
  return (
    <EmptyState
      title="No documents yet"
      description="Documents you add will appear here after they are saved."
      icon={<FileText className="h-10 w-10" aria-hidden="true" />}
      action={{ label: 'Add document', onClick: onAddDocument }}
    />
  )
}
