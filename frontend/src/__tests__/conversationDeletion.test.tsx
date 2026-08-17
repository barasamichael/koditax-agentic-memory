import { render, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ConversationList } from '@/components/chat/ConversationList'
import { useAuthStore } from '@/stores/authStore'
import { useChatStore } from '@/stores/chatStore'
import { useUIStore } from '@/stores/uiStore'

const mockApi = vi.hoisted(() => ({
  deleteConversation: vi.fn(),
  bulkDeleteConversations: vi.fn(),
}))

vi.mock('@/api/orchestration.api', () => ({
  deleteConversation: mockApi.deleteConversation,
  bulkDeleteConversations: mockApi.bulkDeleteConversations,
}))

const userId = 'user-1'
const conversationA = {
  conversationId: 'conversation-a',
  title: 'Alpha chat',
  createdAt: '2026-07-30T10:00:00.000Z',
  updatedAt: '2026-07-30T10:00:00.000Z',
  status: 'active' as const,
  messages: [],
}
const conversationB = {
  conversationId: 'conversation-b',
  title: 'Beta chat',
  createdAt: '2026-07-30T11:00:00.000Z',
  updatedAt: '2026-07-30T11:00:00.000Z',
  status: 'active' as const,
  messages: [],
}

describe('conversation deletion', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    sessionStorage.clear()
    useAuthStore.setState({ userId })
    useUIStore.setState({ chatHistoryOpen: false })
    useChatStore.setState({
      activeUserId: userId,
      userStates: {
        [userId]: {
          conversations: [conversationA, conversationB],
          activeConversationId: conversationA.conversationId,
        },
      },
      messages: [],
      conversationId: conversationA.conversationId,
      pendingAction: null,
      pendingDecision: null,
      promptChecksum: null,
      contextDocuments: [],
      activeComputationId: null,
    })
  })

  it('deletes a single chat through orchestration and falls back to the next active conversation', async () => {
    mockApi.deleteConversation.mockResolvedValue({
      status: 'deleted',
      service: 'orchestration',
      correlation_id: 'corr-delete-a',
      trace_id: 'trace-delete-a',
      conversation_id: conversationA.conversationId,
      deleted_count: 1,
    })

    render(<ConversationList />)

    fireEvent.click(screen.getByRole('button', { name: `Delete ${conversationA.title}` }))
    const confirmationDialog = screen.getByRole('dialog', { name: /delete conversation/i })
    const confirmActions = within(confirmationDialog)
    fireEvent.click(confirmActions.getByRole('button', { name: /delete chat/i }))

    await waitFor(() => {
      expect(mockApi.deleteConversation).toHaveBeenCalledWith(conversationA.conversationId)
    })

    const state = useChatStore.getState()
    expect(state.userStates[userId]?.conversations.map((conversation) => conversation.conversationId)).toEqual([
      conversationB.conversationId,
    ])
    expect(state.conversationId).toBe(conversationB.conversationId)
    expect(state.userStates[userId]?.activeConversationId).toBe(conversationB.conversationId)
  })

  it('renders the bulk delete modal with search UI and deletes the selected conversations', async () => {
    mockApi.bulkDeleteConversations.mockResolvedValue({
      status: 'deleted',
      service: 'orchestration',
      correlation_id: 'corr-bulk-delete',
      trace_id: 'trace-bulk-delete',
      requested_conversation_ids: [conversationA.conversationId, conversationB.conversationId],
      deleted_conversation_ids: [conversationA.conversationId, conversationB.conversationId],
      deleted_count: 2,
    })

    render(<ConversationList />)

    fireEvent.click(screen.getByRole('button', { name: /manage chats/i }))

    const dialog = screen.getByRole('dialog', { name: /remove selected conversations/i })
    expect(dialog).toBeTruthy()
    const modal = within(dialog)
    expect(modal.getByPlaceholderText('Search chat titles')).toBeTruthy()

    fireEvent.change(modal.getByPlaceholderText('Search chat titles'), {
      target: { value: 'alpha' },
    })

    expect(modal.getByText('Alpha chat')).toBeTruthy()
    expect(modal.getByText('Beta chat')).toBeTruthy()

    fireEvent.click(modal.getByRole('button', { name: /select all/i }))
    fireEvent.click(modal.getByRole('button', { name: /delete selected/i }))

    await waitFor(() => {
      expect(mockApi.bulkDeleteConversations).toHaveBeenCalledWith([
        conversationA.conversationId,
        conversationB.conversationId,
      ])
    })

    const state = useChatStore.getState()
    expect(state.userStates[userId]?.conversations).toHaveLength(1)
    expect(state.userStates[userId]?.conversations[0]?.conversationId).not.toBe(conversationA.conversationId)
    expect(screen.queryByRole('dialog', { name: /remove selected conversations/i })).toBeNull()
  })

  it('keeps the active conversation valid when multiple chats are deleted through the store', () => {
    useChatStore.getState().deleteConversations(userId, [
      conversationA.conversationId,
      conversationB.conversationId,
    ])

    const state = useChatStore.getState()
    expect(state.userStates[userId]?.conversations).toHaveLength(1)
    expect(state.userStates[userId]?.conversations[0]?.title).toBe('New chat')
    expect(state.conversationId).toBe(state.userStates[userId]?.activeConversationId)
  })
})
