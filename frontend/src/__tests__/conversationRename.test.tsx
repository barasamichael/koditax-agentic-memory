import { render, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ConversationList } from '@/components/chat/ConversationList'
import { ChatToolbar } from '@/components/chat/ChatToolbar'
import { useAuthStore } from '@/stores/authStore'
import { useChatStore } from '@/stores/chatStore'
import { useUIStore } from '@/stores/uiStore'

const mockApi = vi.hoisted(() => ({
  deleteConversation: vi.fn(),
  bulkDeleteConversations: vi.fn(),
  renameConversation: vi.fn(),
}))

vi.mock('@/api/orchestration.api', () => ({
  deleteConversation: mockApi.deleteConversation,
  bulkDeleteConversations: mockApi.bulkDeleteConversations,
  renameConversation: mockApi.renameConversation,
}))

const userId = 'user-rename-1'
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

function ToolbarHarness() {
  const conversation = useChatStore((state) => {
    if (!state.activeUserId) return null
    return (
      state.userStates[state.activeUserId]?.conversations.find(
        (entry) => entry.conversationId === state.conversationId
      ) ?? null
    )
  })

  return <ChatToolbar conversation={conversation} />
}

describe('conversation rename', () => {
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

  it('renames a sidebar conversation through orchestration and updates the store title', async () => {
    mockApi.renameConversation.mockResolvedValue({
      status: 'renamed',
      service: 'orchestration',
      correlation_id: 'corr-rename-a',
      trace_id: 'trace-rename-a',
      conversation_id: conversationA.conversationId,
      conversation_title: 'Renamed alpha',
      updated_count: 1,
    })

    render(<ConversationList />)

    fireEvent.click(screen.getByRole('button', { name: `Rename ${conversationA.title}` }))
    const dialog = screen.getByRole('dialog', { name: /rename conversation/i })
    const modal = within(dialog)
    const input = modal.getByPlaceholderText('Enter a new conversation title')
    fireEvent.change(input, { target: { value: 'Renamed alpha' } })
    fireEvent.click(modal.getByRole('button', { name: /save title/i }))

    await waitFor(() => {
      expect(mockApi.renameConversation).toHaveBeenCalledWith(
        conversationA.conversationId,
        'Renamed alpha'
      )
    })

    const state = useChatStore.getState()
    expect(state.userStates[userId]?.conversations[0]?.title).toBe('Renamed alpha')
    expect(state.conversationId).toBe(conversationA.conversationId)
  })

  it('renames the active conversation from the toolbar and keeps the active thread selected', async () => {
    mockApi.renameConversation.mockResolvedValue({
      status: 'renamed',
      service: 'orchestration',
      correlation_id: 'corr-rename-b',
      trace_id: 'trace-rename-b',
      conversation_id: conversationA.conversationId,
      conversation_title: 'Renamed alpha from toolbar',
      updated_count: 1,
    })
    useChatStore.setState((state) => ({
      ...state,
      userStates: {
        ...state.userStates,
        [userId]: {
          conversations: [
            {
              ...conversationA,
              messages: [
                {
                  id: 'message-1',
                  role: 'user',
                  content: 'Hello',
                  timestamp: '2026-07-30T10:05:00.000Z',
                  type: 'text' as const,
                },
              ],
            },
            conversationB,
          ],
          activeConversationId: conversationA.conversationId,
        },
      },
      conversationId: conversationA.conversationId,
      messages: [
        {
          id: 'message-1',
          role: 'user',
          content: 'Hello',
          timestamp: '2026-07-30T10:05:00.000Z',
          type: 'text' as const,
        },
      ],
    }))

    render(<ToolbarHarness />)

    fireEvent.click(screen.getByRole('button', { name: /rename/i }))
    const dialog = screen.getByRole('dialog', { name: /rename conversation/i })
    const modal = within(dialog)
    const input = modal.getByPlaceholderText('Enter a new conversation title')
    fireEvent.change(input, { target: { value: 'Renamed alpha from toolbar' } })
    fireEvent.click(modal.getByRole('button', { name: /save title/i }))

    await waitFor(() => {
      expect(mockApi.renameConversation).toHaveBeenCalledWith(
        conversationA.conversationId,
        'Renamed alpha from toolbar'
      )
    })

    const state = useChatStore.getState()
    expect(state.userStates[userId]?.conversations[0]?.title).toBe(
      'Renamed alpha from toolbar'
    )
    expect(state.conversationId).toBe(conversationA.conversationId)
    expect(state.userStates[userId]?.activeConversationId).toBe(conversationA.conversationId)
  })
})
