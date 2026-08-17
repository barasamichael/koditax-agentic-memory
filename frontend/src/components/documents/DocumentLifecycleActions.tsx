import { useState } from 'react'
import { Trash2, RotateCcw } from 'lucide-react'
import { useToast } from '@/components/shared/Toast'
import { ConfirmModal } from '@/components/shared/ConfirmModal'
import { Spinner } from '@/components/shared/Spinner'
import { getDocumentErrorMessage } from '@/lib/documents/document-errors'
import type { DocumentViewModel } from '@/lib/documents/document-lifecycle'
import { useRestoreDocument, useTrashDocument } from '@/hooks/useDocuments'
import { cn } from '@/lib/utils'
import { DocumentPermanentDeleteModal } from '@/components/documents/DocumentPermanentDeleteModal'

interface DocumentLifecycleActionsProps {
  document: Pick<
    DocumentViewModel,
    'id' | 'displayName' | 'availableActions'
  >
  className?: string
  onDeleted?: () => void
}

export function DocumentLifecycleActions({
  document,
  className,
  onDeleted,
}: DocumentLifecycleActionsProps) {
  const toast = useToast()
  const trashMutation = useTrashDocument(document.id)
  const restoreMutation = useRestoreDocument(document.id)
  const [trashConfirmOpen, setTrashConfirmOpen] = useState(false)
  const [restoreConfirmOpen, setRestoreConfirmOpen] = useState(false)
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false)

  const handleTrash = async () => {
    try {
      await trashMutation.mutateAsync(undefined)
      setTrashConfirmOpen(false)
      toast.success('Document moved to trash.')
    } catch (error) {
      toast.error(getDocumentErrorMessage(error, { action: 'trash' }))
    }
  }

  const handleRestore = async () => {
    try {
      await restoreMutation.mutateAsync(undefined)
      setRestoreConfirmOpen(false)
      toast.success('Document restored.')
    } catch (error) {
      toast.error(getDocumentErrorMessage(error, { action: 'restore' }))
    }
  }

  const showTrashAction = document.availableActions.canMoveToTrash
  const showRestoreAction = document.availableActions.canRestore
  const showDeleteAction = document.availableActions.canDeletePermanently

  if (!showTrashAction && !showRestoreAction && !showDeleteAction) {
    return null
  }

  const buttonClassName = [
    'inline-flex items-center gap-2 rounded-lg border px-3 py-2 text-sm font-medium transition-all',
    'disabled:cursor-not-allowed disabled:opacity-60',
  ].join(' ')

  return (
    <div className={cn('flex flex-wrap items-center gap-2', className)}>
      {showTrashAction ? (
        <button
          type="button"
          onClick={() => setTrashConfirmOpen(true)}
          disabled={trashMutation.isPending}
          className={cn(
            buttonClassName,
            'border-gray-200 bg-white text-gray-700 hover:bg-gray-50',
          )}
        >
          {trashMutation.isPending ? (
            <Spinner size="sm" className="h-4 w-4" />
          ) : (
            <Trash2 className="h-4 w-4" aria-hidden="true" />
          )}
          Move to trash
        </button>
      ) : null}

      {showRestoreAction ? (
        <button
          type="button"
          onClick={() => setRestoreConfirmOpen(true)}
          disabled={restoreMutation.isPending}
          className={cn(
            buttonClassName,
            'border-navy-200 bg-navy-50 text-navy-800 hover:bg-navy-100',
          )}
        >
          {restoreMutation.isPending ? (
            <Spinner size="sm" className="h-4 w-4" />
          ) : (
            <RotateCcw className="h-4 w-4" aria-hidden="true" />
          )}
          Restore document
        </button>
      ) : null}

      {showDeleteAction ? (
        <button
          type="button"
          onClick={() => setDeleteConfirmOpen(true)}
          className={cn(
            buttonClassName,
            'border-red-200 bg-red-50 text-red-700 hover:bg-red-100',
          )}
        >
          <Trash2 className="h-4 w-4" aria-hidden="true" />
          Delete permanently
        </button>
      ) : null}

      <ConfirmModal
        open={trashConfirmOpen}
        onOpenChange={setTrashConfirmOpen}
        title="Move document to trash?"
        description="Moving a document to trash is reversible. You can restore it later from Trash."
        confirmLabel="Move to trash"
        variant="danger"
        loading={trashMutation.isPending}
        onConfirm={() => void handleTrash()}
      />

      <ConfirmModal
        open={restoreConfirmOpen}
        onOpenChange={setRestoreConfirmOpen}
        title="Restore document?"
        description="Restoring a document returns it to your ordinary Documents Library."
        confirmLabel="Restore"
        loading={restoreMutation.isPending}
        onConfirm={() => void handleRestore()}
      />

      <DocumentPermanentDeleteModal
        open={deleteConfirmOpen}
        onOpenChange={setDeleteConfirmOpen}
        documentId={document.id}
        displayName={document.displayName}
        onDeleted={onDeleted}
      />
    </div>
  )
}
