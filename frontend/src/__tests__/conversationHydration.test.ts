import { render, screen } from '@testing-library/react'
import { createElement } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fetchConversations } from '@/api/orchestration.api'
import { ConversationList } from '@/components/chat/ConversationList'
import { useAuthStore } from '@/stores/authStore'
import { useChatStore } from '@/stores/chatStore'
import { useUIStore } from '@/stores/uiStore'

const mockApi = vi.hoisted(() => ({
  get: vi.fn(),
}))

vi.mock('@/api/client', () => ({
  getTrustedAuthHeaders: vi.fn(),
  orchestrationClient: { get: mockApi.get },
  withFlowCorrelation: vi.fn(() => ({ 'X-Correlation-ID': 'conversation-list' })),
}))

describe('conversation history hydration', () => {
  const userId = 'user-history-1'

  beforeEach(() => {
    vi.clearAllMocks()
    useChatStore.setState({
      userStates: {},
      activeUserId: null,
      conversationLoadStatus: 'idle',
      messages: [],
      conversationId: null,
      pendingAction: null,
      pendingDecision: null,
      promptChecksum: null,
      contextDocuments: [],
      activeComputationId: null,
    })
  })

  it('restores the same persisted conversation and ordered transcript after reinitialization', async () => {
    const conversationId = useChatStore.getState().createConversation(userId)
    const userMessage = {
      id: 'message-user-1',
      role: 'user' as const,
      content: 'What is VAT?',
      timestamp: '2026-08-02T10:00:00.000Z',
      type: 'text' as const,
    }
    const assistantMessage = {
      id: 'message-assistant-1',
      role: 'assistant' as const,
      content: 'VAT is a consumption tax.',
      timestamp: '2026-08-02T10:00:01.000Z',
      type: 'outcome' as const,
      metadata: { assistantState: 'completed' as const },
    }
    useChatStore.getState().appendMessage(userId, userMessage, conversationId)
    useChatStore.getState().appendMessage(userId, assistantMessage, conversationId)

    const visibleConversation = useChatStore
      .getState()
      .userStates[userId]?.conversations.find(
        (conversation) => conversation.conversationId === conversationId
      )
    expect(visibleConversation?.messages).toEqual([userMessage, assistantMessage])

    // This is the exact snake_case shape specified and returned by the history API.
    mockApi.get.mockResolvedValue({
      data: {
        status: 'listed',
        service: 'orchestration',
        correlation_id: 'corr-history-1',
        trace_id: 'trace-history-1',
        conversations: [
          {
            conversation_id: conversationId,
            title: 'What is VAT?',
            created_at: '2026-08-02T10:00:00.000Z',
            updated_at: '2026-08-02T10:00:01.000Z',
            status: 'active',
            messages: [userMessage, assistantMessage],
          },
        ],
      },
    })

    // A page refresh creates a new in-memory store before history hydration.
    useChatStore.setState({
      userStates: {},
      activeUserId: null,
      messages: [],
      conversationId: null,
    })
    useChatStore.getState().hydrateForUser(userId)
    const response = await fetchConversations()
    useChatStore.getState().setConversationsFromBackend(userId, response.conversations)

    const restored = useChatStore.getState()
    expect(restored.conversationId).toBe(conversationId)
    expect(restored.userStates[userId]?.activeConversationId).toBe(conversationId)
    expect(restored.messages).toEqual([userMessage, assistantMessage])
  })

  it('does not present a failed history request as an empty history', () => {
    useAuthStore.setState({ userId })
    useUIStore.setState({ chatHistoryOpen: false })
    useChatStore.getState().hydrateForUser(userId)
    useChatStore.getState().setConversationLoadStatus('failed', userId)

    render(createElement(ConversationList))

    expect(screen.getByRole('alert').textContent).toContain(
      'Could not load saved conversations. Refresh to try again.'
    )
    expect(screen.getByText('Saved conversations could not be loaded')).not.toBeNull()
  })
})
