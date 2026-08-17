import { Trash2 } from 'lucide-react'
import { EmptyState } from '@/components/shared/EmptyState'

interface DocumentsTrashEmptyStateProps {
  onBackToDocuments: () => void
}

export function DocumentsTrashEmptyState({ onBackToDocuments }: DocumentsTrashEmptyStateProps) {
  return (
    <EmptyState
      title="Trash is empty"
      description="Documents you move to trash will appear here until you restore them."
      icon={<Trash2 className="h-10 w-10" aria-hidden="true" />}
      action={{ label: 'Back to documents', onClick: onBackToDocuments }}
    />
  )
}
