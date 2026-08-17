import type { DocumentRecord, DocumentState } from '@/types/document'
import { formatDocumentName, getFileExtension } from './document-formatters'

export type DocumentUiStatus =
  | 'checking_file'
  | 'getting_ready'
  | 'ready'
  | 'ready_with_limitations'
  | 'needs_attention'
  | 'updating'
  | 'in_trash'
  | 'deleting'

export type DocumentUiLifecycle =
  | 'checking'
  | 'preparing'
  | 'available'
  | 'limited'
  | 'action_required'
  | 'updating'
  | 'in_trash'
  | 'deleting'
  | 'removed'

export interface DocumentAvailableActions {
  canView: boolean
  canOpen: boolean
  canDownload: boolean
  canMoveToTrash: boolean
  canRestore: boolean
  canMarkEligibleForRemoval: boolean
  canDeletePermanently: boolean
  canPurge: boolean
  canRename: boolean
}

export interface DocumentViewModel {
  id: string
  displayName: string
  originalFileName?: string
  fileExtension?: string
  mediaType?: string
  sizeBytes?: number
  pageCount?: number
  addedAt?: string
  updatedAt?: string
  revision: number
  category?: string
  tags: string[]
  description?: string
  complianceLockUntil?: string
  status: DocumentUiStatus
  statusLabel: string
  statusDescription?: string
  lifecycle: DocumentUiLifecycle
  availableActions: DocumentAvailableActions
  isSaved: boolean
  requiresUserAction: boolean
}

interface DocumentPresentation {
  status: DocumentUiStatus
  statusLabel: string
  statusDescription: string
  lifecycle: DocumentUiLifecycle
  availableActions: DocumentAvailableActions
  isSaved: boolean
  requiresUserAction: boolean
}

const PRESENTATION_BY_STATE: Record<DocumentState, DocumentPresentation> = {
  uploaded: {
    status: 'checking_file',
    statusLabel: 'Checking file',
    statusDescription: 'The file has been saved and is being checked before it can be used.',
    lifecycle: 'checking',
    availableActions: {
      canView: true,
      canOpen: false,
      canDownload: false,
      canMoveToTrash: true,
      canRestore: false,
      canMarkEligibleForRemoval: true,
      canDeletePermanently: false,
      canPurge: false,
      canRename: true,
    },
    isSaved: true,
    requiresUserAction: false,
  },
  active: {
    status: 'ready',
    statusLabel: 'Ready',
    statusDescription: 'The document is saved and ready for normal use.',
    lifecycle: 'available',
    availableActions: {
      canView: true,
      canOpen: true,
      canDownload: true,
      canMoveToTrash: true,
      canRestore: false,
      canMarkEligibleForRemoval: true,
      canDeletePermanently: false,
      canPurge: false,
      canRename: true,
    },
    isSaved: true,
    requiresUserAction: false,
  },
  trashed: {
    status: 'in_trash',
    statusLabel: 'In trash',
    statusDescription: 'The document is in trash and can be restored later.',
    lifecycle: 'in_trash',
    availableActions: {
      canView: true,
      canOpen: false,
      canDownload: false,
      canMoveToTrash: false,
      canRestore: true,
      canMarkEligibleForRemoval: false,
      canDeletePermanently: true,
      canPurge: false,
      canRename: false,
    },
    isSaved: true,
    requiresUserAction: false,
  },
  purge_pending: {
    status: 'deleting',
    statusLabel: 'Deleting',
    statusDescription:
      'Permanent deletion has started and may continue after you leave this page.',
    lifecycle: 'deleting',
    availableActions: {
      canView: true,
      canOpen: false,
      canDownload: false,
      canMoveToTrash: false,
      canRestore: false,
      canMarkEligibleForRemoval: false,
      canDeletePermanently: false,
      canPurge: false,
      canRename: false,
    },
    isSaved: true,
    requiresUserAction: false,
  },
  processing: {
    status: 'getting_ready',
    statusLabel: 'Getting ready',
    statusDescription: 'The document is saved and is still being prepared.',
    lifecycle: 'preparing',
    availableActions: {
      canView: true,
      canOpen: false,
      canDownload: false,
      canMoveToTrash: true,
      canRestore: false,
      canMarkEligibleForRemoval: true,
      canDeletePermanently: false,
      canPurge: false,
      canRename: true,
    },
    isSaved: true,
    requiresUserAction: false,
  },
  validated: {
    status: 'ready_with_limitations',
    statusLabel: 'Ready with limitations',
    statusDescription:
      'The document is ready, but some content may still be limited or incomplete.',
    lifecycle: 'limited',
    availableActions: {
      canView: true,
      canOpen: true,
      canDownload: true,
      canMoveToTrash: true,
      canRestore: false,
      canMarkEligibleForRemoval: true,
      canDeletePermanently: false,
      canPurge: false,
      canRename: true,
    },
    isSaved: true,
    requiresUserAction: false,
  },
  eligible_for_purge: {
    status: 'needs_attention',
    statusLabel: 'Ready to delete',
    statusDescription:
      'The document is ready for permanent deletion. Restore it if you still need it.',
    lifecycle: 'action_required',
    availableActions: {
      canView: true,
      canOpen: false,
      canDownload: false,
      canMoveToTrash: false,
      canRestore: true,
      canMarkEligibleForRemoval: false,
      canDeletePermanently: true,
      canPurge: true,
      canRename: false,
    },
    isSaved: true,
    requiresUserAction: true,
  },
  purged: {
    status: 'in_trash',
    statusLabel: 'Removed',
    statusDescription: 'The document has been permanently removed.',
    lifecycle: 'removed',
    availableActions: {
      canView: false,
      canOpen: false,
      canDownload: false,
      canMoveToTrash: false,
      canRestore: false,
      canMarkEligibleForRemoval: false,
      canDeletePermanently: false,
      canPurge: false,
      canRename: false,
    },
    isSaved: false,
    requiresUserAction: false,
  },
}

export const resolveDocumentPresentation = (state: DocumentState): DocumentPresentation =>
  PRESENTATION_BY_STATE[state]

export const normalizeDocumentRecord = (record: DocumentRecord): DocumentViewModel => {
  const presentation = resolveDocumentPresentation(record.state)
  const displayName = formatDocumentName(record.display_name)
  const fileExtension = getFileExtension(record.display_name ?? undefined) ?? undefined

  return {
    id: record.document_id,
    displayName,
    fileExtension,
    addedAt: record.uploaded_at,
    revision: record.revision,
    status: presentation.status,
    statusLabel: presentation.statusLabel,
    statusDescription: presentation.statusDescription,
    category: record.category ?? undefined,
    tags: record.tags,
    description: record.description ?? undefined,
    complianceLockUntil: record.compliance_lock_until ?? undefined,
    lifecycle: presentation.lifecycle,
    availableActions: presentation.availableActions,
    isSaved: presentation.isSaved,
    requiresUserAction: presentation.requiresUserAction,
  }
}
