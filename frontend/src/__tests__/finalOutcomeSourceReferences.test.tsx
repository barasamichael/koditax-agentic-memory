import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { FinalOutcomeCard } from '@/components/chat/FinalOutcomeCard'
import type { ChatMessage } from '@/types/chat'

const mockOpenDocumentPreview = vi.hoisted(() => vi.fn())
const mockToast = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
}))

vi.mock('@/lib/documents/document-access', () => ({
  openDocumentPreview: mockOpenDocumentPreview,
}))

vi.mock('@/components/shared/Toast', () => ({
  useToast: () => mockToast,
}))

describe('FinalOutcomeCard source references', () => {
  it('renders document sources with secure open actions', async () => {
    const message: ChatMessage = {
      id: 'message-1',
      role: 'assistant',
      content: 'The document shows the required filing detail.',
      timestamp: '2026-08-05T12:00:00.000Z',
      type: 'outcome',
      metadata: {
        assistantState: 'completed',
        sourceReferences: [
          {
            document_id: 'doc-123',
            document_label: 'Quarterly return.pdf',
            document_status: 'available',
            openable: true,
            accessibility_label: 'Source available',
            source_location: {
              location_kind: 'page',
              location_label: 'Page 3',
              location_status: 'partial',
              page_number: 3,
            },
          },
        ],
      },
    }

    render(<FinalOutcomeCard message={message} />)

    expect(screen.getByText('Sources')).toBeTruthy()
    expect(screen.getByText('Quarterly return.pdf')).toBeTruthy()
    expect(screen.getByText('Page 3')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Open source' }))

    await waitFor(() => {
      expect(mockOpenDocumentPreview).toHaveBeenCalledWith({
        documentId: 'doc-123',
        displayName: 'Quarterly return.pdf',
      })
    })
    expect(mockToast.success).toHaveBeenCalledWith('Source opened in a new tab.')
  })
})
