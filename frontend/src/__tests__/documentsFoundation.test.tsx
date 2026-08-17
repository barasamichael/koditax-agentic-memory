import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import DocumentsPage from '@/pages/DocumentsPage'
import { formatDocumentName, formatFileTypeLabel } from '@/lib/documents/document-formatters'
import { documentQueryKeys } from '@/lib/documents/document-query-keys'
import { getDocumentErrorMessage } from '@/lib/documents/document-errors'
import { normalizeDocumentRecord } from '@/lib/documents/document-lifecycle'

const documentRecord = {
  document_id: 'doc-123',
  state: 'validated' as const,
  uploaded_at: '2026-08-01T10:00:00.000Z',
  computation_id: null,
  purge_eligible_at: null,
  purged_at: null,
  compliance_lock_until: '2026-08-15T00:00:00.000Z',
  display_name: 'Quarterly return.pdf',
  category: 'tax',
  tags: ['income', 'draft'],
  description: 'Prepared for filing.',
  revision: 3,
}

const mockDocumentsHook = vi.hoisted(() => ({
  useDocumentViewModels: vi.fn(),
  useUploadDocument: vi.fn(),
  useDocument: vi.fn(),
  useRenameDocument: vi.fn(),
  useTrashDocument: vi.fn(),
  useRestoreDocument: vi.fn(),
  usePurgeDryRun: vi.fn(),
  usePurgeDocument: vi.fn(),
}))

const mockDocumentAccess = vi.hoisted(() => ({
  openDocumentPreview: vi.fn(),
  downloadDocumentOriginal: vi.fn(),
}))

vi.mock('@/hooks/useDocuments', () => ({
  useDocumentViewModels: mockDocumentsHook.useDocumentViewModels,
  useUploadDocument: mockDocumentsHook.useUploadDocument,
  useDocument: mockDocumentsHook.useDocument,
  useRenameDocument: mockDocumentsHook.useRenameDocument,
  useTrashDocument: mockDocumentsHook.useTrashDocument,
  useRestoreDocument: mockDocumentsHook.useRestoreDocument,
  usePurgeDryRun: mockDocumentsHook.usePurgeDryRun,
  usePurgeDocument: mockDocumentsHook.usePurgeDocument,
}))

vi.mock('@/lib/documents/document-access', () => ({
  openDocumentPreview: mockDocumentAccess.openDocumentPreview,
  downloadDocumentOriginal: mockDocumentAccess.downloadDocumentOriginal,
}))

function createQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  })
}

function renderWithProviders(
  ui: ReactElement,
  initialEntries: string[] = ['/'],
  routePath?: string,
) {
  const queryClient = createQueryClient()

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={initialEntries}>
        {routePath ? (
          <Routes>
            <Route path={routePath} element={ui} />
          </Routes>
        ) : (
          ui
        )}
      </MemoryRouter>
    </QueryClientProvider>
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  mockDocumentsHook.useDocumentViewModels.mockReturnValue({
    documentViews: [normalizeDocumentRecord(documentRecord)],
    documents: [documentRecord],
    isLoading: false,
    isFetching: false,
    isError: false,
    error: null,
    refetch: vi.fn(),
  })
  mockDocumentsHook.useUploadDocument.mockReturnValue({
    status: 'idle',
    progress: null,
    uploadDocument: vi.fn(),
    cancelUpload: vi.fn(),
    resetUploadState: vi.fn(),
  })
  mockDocumentsHook.useDocument.mockReturnValue({
    data: null,
    isPending: false,
    isError: false,
    error: null,
    refetch: vi.fn(),
  })
  mockDocumentsHook.useRenameDocument.mockReturnValue({
    mutateAsync: vi.fn(),
    isPending: false,
  })
  mockDocumentsHook.useTrashDocument.mockReturnValue({
    mutateAsync: vi.fn().mockResolvedValue({
      status: 'ok',
      document: { ...documentRecord, state: 'trashed' },
      traceability: { trace_id: null, correlation_id: null },
    }),
    isPending: false,
  })
  mockDocumentsHook.useRestoreDocument.mockReturnValue({
    mutateAsync: vi.fn().mockResolvedValue({
      status: 'ok',
      document: { ...documentRecord, state: 'active' },
      traceability: { trace_id: null, correlation_id: null },
    }),
    isPending: false,
  })
  mockDocumentsHook.usePurgeDryRun.mockReturnValue({
    data: null,
    isLoading: false,
    isFetching: false,
    isError: false,
    error: null,
    refetch: vi.fn(),
  })
  mockDocumentsHook.usePurgeDocument.mockReturnValue({
    mutateAsync: vi.fn().mockResolvedValue({
      status: 'ok',
      document: { ...documentRecord, state: 'purged' },
      traceability: { trace_id: null, correlation_id: null },
    }),
    isPending: false,
  })
  mockDocumentAccess.openDocumentPreview.mockResolvedValue(undefined)
  mockDocumentAccess.downloadDocumentOriginal.mockResolvedValue(undefined)
})

describe('document foundation', () => {
  it('normalizes backend records into a user-facing view model', () => {
    const viewModel = normalizeDocumentRecord(documentRecord)

    expect(viewModel.id).toBe(documentRecord.document_id)
    expect(viewModel.displayName).toBe('Quarterly return.pdf')
    expect(viewModel.fileExtension).toBe('pdf')
    expect(viewModel.status).toBe('ready_with_limitations')
    expect(viewModel.statusLabel).toBe('Ready with limitations')
    expect(viewModel.availableActions.canOpen).toBe(true)
    expect(viewModel.availableActions.canDownload).toBe(true)
    expect(viewModel.availableActions.canPurge).toBe(false)
    expect(viewModel.availableActions.canDeletePermanently).toBe(false)
    expect(viewModel.availableActions.canRename).toBe(true)
    expect(viewModel.requiresUserAction).toBe(false)
    expect(viewModel.category).toBe('tax')
    expect(viewModel.tags).toEqual(['income', 'draft'])
  })

  it('formats file names and type labels for empty document metadata', () => {
    expect(formatDocumentName('')).toBe('Untitled document')
    expect(formatDocumentName('  ')).toBe('Untitled document')
    expect(formatFileTypeLabel(null)).toBe('File')
    expect(formatFileTypeLabel('pdf')).toBe('PDF')
    expect(formatFileTypeLabel('xlsx')).toBe('XLSX')
  })

  it('keeps query keys stable and scoped to document filters', () => {
    expect(
      documentQueryKeys.list({
        state: 'validated',
        uploaded_from: '2026-08-01T00:00:00.000Z',
        uploaded_to: '2026-08-31T23:59:59.000Z',
        computation_id: 'comp-123',
      })
    ).toEqual([
      'documents',
      'list',
      'validated',
      '2026-08-01T00:00:00.000Z',
      '2026-08-31T23:59:59.000Z',
      'comp-123',
    ])
    expect(documentQueryKeys.detail('doc-123')).toEqual(['documents', 'detail', 'doc-123'])
    expect(documentQueryKeys.trashList()).toEqual([
      'documents',
      'list',
      'trashed',
      null,
      null,
      null,
    ])
    expect(documentQueryKeys.openCapability('doc-123')).toEqual([
      'documents',
      'open-capability',
      'doc-123',
    ])
    expect(documentQueryKeys.purgeDryRun('doc-123')).toEqual([
      'documents',
      'purge-dry-run',
      'doc-123',
    ])
  })

  it('formats lifecycle action errors with trash and restore context', () => {
    const backendError = {
      isAxiosError: true,
      response: {
        data: {
          detail: {
            error_code: 'invalid_document_state_transition',
            message: 'Document state does not support action.',
            reason: 'invalid_document_state_transition',
          },
        },
      },
    }

    expect(getDocumentErrorMessage(backendError, { action: 'trash' })).toBe(
      'This document cannot be moved to trash in its current state.',
    )
    expect(getDocumentErrorMessage(backendError, { action: 'restore' })).toBe(
      'This document cannot be restored in its current state.',
    )
    expect(getDocumentErrorMessage(backendError, { action: 'delete_permanently' })).toBe(
      'This document cannot be deleted permanently in its current state.',
    )
  })

  it('renders the document library without leaking backend identifiers', async () => {
    mockDocumentsHook.useDocumentViewModels.mockReturnValue({
      documentViews: [normalizeDocumentRecord(documentRecord)],
      documents: [documentRecord],
      isLoading: false,
      isFetching: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    })

    renderWithProviders(<DocumentsPage />)

    expect(await screen.findByRole('heading', { name: 'Documents' })).toBeTruthy()
    expect(screen.getByText('Quarterly return.pdf')).toBeTruthy()
    expect(screen.getByText('Ready with limitations')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Open Quarterly return.pdf' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Download Quarterly return.pdf' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Move to trash' })).toBeTruthy()
    expect(screen.queryByText('doc-123')).toBeNull()
    expect(screen.queryByText(/document id/i)).toBeNull()
    expect(screen.queryByText(/storage key/i)).toBeNull()
  })

  it('shows delete permanently for trashed documents', async () => {
    mockDocumentsHook.useDocumentViewModels.mockReturnValue({
      documentViews: [normalizeDocumentRecord({ ...documentRecord, state: 'trashed' })],
      documents: [{ ...documentRecord, state: 'trashed' }],
      isLoading: false,
      isFetching: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    })

    renderWithProviders(<DocumentsPage />)

    expect(await screen.findByRole('button', { name: 'Delete permanently' })).toBeTruthy()
    expect(screen.queryByText('Remove permanently')).toBeNull()
  })

  it('opens and downloads a document through the shared access flow', async () => {
    renderWithProviders(<DocumentsPage />)

    fireEvent.click(screen.getByRole('button', { name: 'Open Quarterly return.pdf' }))
    await waitFor(() => {
      expect(mockDocumentAccess.openDocumentPreview).toHaveBeenCalledWith({
        documentId: 'doc-123',
        displayName: 'Quarterly return.pdf',
      })
    })
    fireEvent.click(screen.getByRole('button', { name: 'Download Quarterly return.pdf' }))
    expect(mockDocumentAccess.downloadDocumentOriginal).toHaveBeenCalledWith({
      documentId: 'doc-123',
      displayName: 'Quarterly return.pdf',
    })
  })

  it('expands a document row to show the friendly metadata summary', async () => {
    mockDocumentsHook.useDocumentViewModels.mockReturnValue({
      documentViews: [normalizeDocumentRecord(documentRecord)],
      documents: [documentRecord],
      isLoading: false,
      isFetching: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    })

    renderWithProviders(<DocumentsPage />)

    fireEvent.click(screen.getByRole('button', { name: 'Expand Quarterly return.pdf' }))

    expect(screen.getByText('Category')).toBeTruthy()
    expect(screen.getByText('tax')).toBeTruthy()
    expect(screen.getByText('Locked until')).toBeTruthy()
    expect(screen.getByText('Tags')).toBeTruthy()
    expect(screen.getByText('income')).toBeTruthy()
    expect(screen.getByText('Prepared for filing.')).toBeTruthy()
  })

  it('renders document details from the route and opens rename controls', () => {
    mockDocumentsHook.useDocument.mockReturnValue({
      data: {
        status: 'ok',
        document: documentRecord,
        traceability: { trace_id: null, correlation_id: null },
      },
      isPending: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    })
    const mutateAsync = vi.fn().mockResolvedValue({
      status: 'ok',
      document: documentRecord,
      traceability: { trace_id: null, correlation_id: null },
    })
    mockDocumentsHook.useRenameDocument.mockReturnValue({
      mutateAsync,
      isPending: false,
    })

    renderWithProviders(<DocumentsPage />, ['/documents/doc-123'], '/documents/:documentId')

    expect(screen.getByRole('heading', { name: 'Quarterly return.pdf' })).toBeTruthy()
    expect(screen.getByText('Document details')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Open Quarterly return.pdf' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Download Quarterly return.pdf' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Move to trash' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Rename' }))
    expect(screen.getByRole('dialog', { name: 'Rename document' })).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Document name'), {
      target: { value: 'Quarterly return updated.pdf' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save name' }))
    expect(mutateAsync).toHaveBeenCalledWith({
      display_name: 'Quarterly return updated.pdf',
      expected_revision: 3,
    })
  })

  it('shows a safe error page for inaccessible documents', () => {
    mockDocumentsHook.useDocument.mockReturnValue({
      data: null,
      isPending: false,
      isError: true,
      error: new Error('Document was not found.'),
      refetch: vi.fn(),
    })

    renderWithProviders(<DocumentsPage />, ['/documents/doc-404'], '/documents/:documentId')

    expect(screen.getByText('Document not found')).toBeTruthy()
    expect(screen.getByText('This document may have been removed or is no longer available.')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Back to documents' })).toBeTruthy()
  })

  it('renders the trash view and restores a trashed document through the shared lifecycle flow', async () => {
    mockDocumentsHook.useDocumentViewModels.mockReturnValue({
      documentViews: [normalizeDocumentRecord({ ...documentRecord, state: 'trashed' })],
      documents: [{ ...documentRecord, state: 'trashed' }],
      isLoading: false,
      isFetching: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    })

    const restoreMutation = vi.fn().mockResolvedValue({
      status: 'ok',
      document: { ...documentRecord, state: 'active' },
      traceability: { trace_id: null, correlation_id: null },
    })
    mockDocumentsHook.useRestoreDocument.mockReturnValue({
      mutateAsync: restoreMutation,
      isPending: false,
    })

    renderWithProviders(<DocumentsPage />, ['/documents/trash'], '/documents/trash')

    expect(await screen.findByRole('heading', { name: 'Trash' })).toBeTruthy()
    expect(mockDocumentsHook.useDocumentViewModels).toHaveBeenCalledWith({ state: 'trashed' })
    expect(screen.getByText('Quarterly return.pdf')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Restore document' }))
    const restoreDialog = screen.getByRole('dialog', { name: 'Restore document?' })
    expect(restoreDialog).toBeTruthy()
    fireEvent.click(within(restoreDialog).getByRole('button', { name: 'Restore' }))
    await waitFor(() => {
      expect(restoreMutation).toHaveBeenCalled()
    })
  })

  it('allows moving a document to trash from the library through confirmation', async () => {
    const trashMutation = vi.fn().mockResolvedValue({
      status: 'ok',
      document: { ...documentRecord, state: 'trashed' },
      traceability: { trace_id: null, correlation_id: null },
    })
    mockDocumentsHook.useTrashDocument.mockReturnValue({
      mutateAsync: trashMutation,
      isPending: false,
    })

    renderWithProviders(<DocumentsPage />)

    fireEvent.click(screen.getByRole('button', { name: 'Move to trash' }))
    const trashDialog = screen.getByRole('dialog', { name: 'Move document to trash?' })
    expect(trashDialog).toBeTruthy()
    fireEvent.click(within(trashDialog).getByRole('button', { name: 'Move to trash' }))
    await waitFor(() => {
      expect(trashMutation).toHaveBeenCalled()
    })
  })

  it('shows a loading state before documents arrive', () => {
    mockDocumentsHook.useDocumentViewModels.mockReturnValue({
      documentViews: [],
      documents: [],
      isLoading: true,
      isFetching: true,
      isError: false,
      error: null,
      refetch: vi.fn(),
    })

    renderWithProviders(<DocumentsPage />)

    expect(screen.getByRole('status', { name: 'Loading documents' })).toBeTruthy()
    expect(screen.getByText('Loading documents…')).toBeTruthy()
  })

  it('translates every backend lifecycle state into the shared readiness model', () => {
    const states = [
      {
        state: 'uploaded' as const,
        status: 'checking_file',
        label: 'Checking file',
        canOpen: false,
        canDownload: false,
        canRestore: false,
        canPurge: false,
        canDeletePermanently: false,
        requiresUserAction: false,
        isSaved: true,
      },
      {
        state: 'processing' as const,
        status: 'getting_ready',
        label: 'Getting ready',
        canOpen: false,
        canDownload: false,
        canRestore: false,
        canPurge: false,
        canDeletePermanently: false,
        requiresUserAction: false,
        isSaved: true,
      },
      {
        state: 'validated' as const,
        status: 'ready_with_limitations',
        label: 'Ready with limitations',
        canOpen: true,
        canDownload: true,
        canRestore: false,
        canPurge: false,
        canDeletePermanently: false,
        requiresUserAction: false,
        isSaved: true,
      },
      {
        state: 'active' as const,
        status: 'ready',
        label: 'Ready',
        canOpen: true,
        canDownload: true,
        canRestore: false,
        canPurge: false,
        canDeletePermanently: false,
        requiresUserAction: false,
        isSaved: true,
      },
      {
        state: 'eligible_for_purge' as const,
        status: 'needs_attention',
        label: 'Ready to delete',
        canOpen: false,
        canDownload: false,
        canRestore: true,
        canPurge: true,
        canDeletePermanently: true,
        requiresUserAction: true,
        isSaved: true,
      },
      {
        state: 'trashed' as const,
        status: 'in_trash',
        label: 'In trash',
        canOpen: false,
        canDownload: false,
        canRestore: true,
        canPurge: false,
        canDeletePermanently: true,
        requiresUserAction: false,
        isSaved: true,
      },
      {
        state: 'purge_pending' as const,
        status: 'deleting',
        label: 'Deleting',
        canOpen: false,
        canDownload: false,
        canRestore: false,
        canPurge: false,
        canDeletePermanently: false,
        requiresUserAction: false,
        isSaved: true,
      },
      {
        state: 'purged' as const,
        status: 'in_trash',
        label: 'Removed',
        canOpen: false,
        canDownload: false,
        canRestore: false,
        canPurge: false,
        canDeletePermanently: false,
        requiresUserAction: false,
        isSaved: false,
      },
    ] as const

    for (const entry of states) {
      const viewModel = normalizeDocumentRecord({ ...documentRecord, state: entry.state })
      expect(viewModel.status).toBe(entry.status)
      expect(viewModel.statusLabel).toBe(entry.label)
      expect(viewModel.availableActions.canOpen).toBe(entry.canOpen)
      expect(viewModel.availableActions.canDownload).toBe(entry.canDownload)
      expect(viewModel.availableActions.canRestore).toBe(entry.canRestore)
      expect(viewModel.availableActions.canPurge).toBe(entry.canPurge)
      expect(viewModel.availableActions.canDeletePermanently).toBe(entry.canDeletePermanently)
      expect(viewModel.requiresUserAction).toBe(entry.requiresUserAction)
      expect(viewModel.isSaved).toBe(entry.isSaved)
    }
  })

  it('shows the empty state when no documents are available', () => {
    mockDocumentsHook.useDocumentViewModels.mockReturnValue({
      documentViews: [],
      documents: [],
      isLoading: false,
      isFetching: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    })

    renderWithProviders(<DocumentsPage />)

    expect(screen.getByText('No documents yet')).toBeTruthy()
    expect(screen.getByText('Documents you add will appear here after they are saved.')).toBeTruthy()
  })

  it('opens the add document dialog from the library action', () => {
    mockDocumentsHook.useDocumentViewModels.mockReturnValue({
      documentViews: [],
      documents: [],
      isLoading: false,
      isFetching: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    })

    renderWithProviders(<DocumentsPage />)

    fireEvent.click(screen.getAllByRole('button', { name: 'Add document' })[0])

    expect(screen.getByRole('dialog', { name: 'Add document' })).toBeTruthy()
    expect(screen.getByText('Before you choose a file')).toBeTruthy()
    expect(screen.getByText('maximum 200 MB')).toBeTruthy()
  })

  it('shows an error state and retry action when the list fails to load', () => {
    const refetch = vi.fn()
    mockDocumentsHook.useDocumentViewModels.mockReturnValue({
      documentViews: [],
      documents: [],
      isLoading: false,
      isFetching: false,
      isError: true,
      error: new Error('Request failed'),
      refetch,
    })

    renderWithProviders(<DocumentsPage />)

    expect(screen.getByText('We could not load your documents')).toBeTruthy()
    expect(screen.getByText('Request failed')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(refetch).toHaveBeenCalled()
  })
})
