import type { DocumentViewModel } from '@/lib/documents/document-lifecycle'
import { DocumentListItem } from './DocumentListItem'

interface DocumentsListProps {
  documents: DocumentViewModel[]
}

export function DocumentsList({ documents }: DocumentsListProps) {
  return (
    <ul className="space-y-3">
      {documents.map((document) => (
        <DocumentListItem key={document.id} document={document} />
      ))}
    </ul>
  )
}
