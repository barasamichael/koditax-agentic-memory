import { v4 as uuid } from 'uuid'
import { create } from 'zustand'
import type { DecideResponse } from '@/api/orchestration.api'
import type { ChatConversation, ChatMessage, PendingAction } from '@/types/chat'

interface ContextDocument {
  id: string
  name: string
  type: string
}

interface PersistedUserChatState {
  conversations: ChatConversation[]
  activeConversationId: string | null
}

interface PersistedChatState {
  userStates: Record<string, PersistedUserChatState>
}

interface ChatState extends PersistedChatState {
  activeUserId: string | null
  conversationLoadStatus: 'idle' | 'loading' | 'loaded' | 'failed'
  messages: ChatMessage[]
  conversationId: string | null
  pendingAction: PendingAction | null
  pendingDecision: DecideResponse | null
  promptChecksum: string | null
  contextDocuments: ContextDocument[]
  activeComputationId: string | null
  hydrateForUser: (userId: string) => void
  setConversationLoadStatus: (
    status: 'loading' | 'failed',
    userId: string
  ) => void
  setConversationsFromBackend: (userId: string, conversations: ChatConversation[]) => void
  createConversation: (userId: string) => string
  selectConversation: (userId: string, conversationId: string) => void
  appendMessage: (userId: string, msg: ChatMessage, conversationId?: string | null) => void
  updateMessage: (
    userId: string,
    messageId: string,
    updates: Partial<ChatMessage>,
    conversationId?: string | null
  ) => void
  removeMessage: (userId: string, messageId: string, conversationId?: string | null) => void
  setPendingAction: (
    action: PendingAction | null,
    userId?: string | null,
    conversationId?: string | null
  ) => void
  setPendingDecision: (decision: DecideResponse | null) => void
  clearConversation: (userId: string) => void
  deleteConversation: (userId: string, conversationId: string) => void
  deleteConversations: (userId: string, conversationIds: string[]) => void
  renameConversation: (userId: string, conversationId: string, title: string) => void
  setConversationId: (userId: string, id: string) => void
  setContextDocuments: (docs: ContextDocument[]) => void
  setActiveComputationId: (id: string | null) => void
}

const DEFAULT_CONVERSATION_TITLE = 'New chat'
const MAX_PERSISTED_RETRY_PROMPT_CHARS = 800

const nowIso = (): string => new Date().toISOString()

const normalizeTitle = (content: string): string => {
  const compact = content.replace(/\s+/g, ' ').trim()
  if (!compact) return DEFAULT_CONVERSATION_TITLE
  return compact.length <= 48 ? compact : `${compact.slice(0, 48).trimEnd()}...`
}

const deriveConversationTitle = (messages: ChatMessage[]): string => {
  const firstUserMessage = messages.find((message) => message.role === 'user')
  if (!firstUserMessage) return DEFAULT_CONVERSATION_TITLE
  return normalizeTitle(firstUserMessage.content)
}

const sortConversations = (
  conversations: ChatConversation[]
): ChatConversation[] =>
  [...conversations].sort((left, right) =>
    right.updatedAt.localeCompare(left.updatedAt)
  )

const createEmptyConversation = (): ChatConversation => {
  const timestamp = nowIso()
  return {
    conversationId: uuid(),
    title: DEFAULT_CONVERSATION_TITLE,
    createdAt: timestamp,
    updatedAt: timestamp,
    status: 'draft',
    messages: [],
  }
}

const syncActiveConversation = (
  userState?: PersistedUserChatState | null
): Pick<
  ChatState,
  | 'messages'
  | 'conversationId'
  | 'promptChecksum'
  | 'contextDocuments'
  | 'activeComputationId'
> => {
  if (!userState?.activeConversationId) {
    return {
      messages: [],
      conversationId: null,
      promptChecksum: null,
      contextDocuments: [],
      activeComputationId: null,
    }
  }

  const activeConversation = userState.conversations.find(
    (conversation) => conversation.conversationId === userState.activeConversationId
  )

  if (!activeConversation) {
    return {
      messages: [],
      conversationId: null,
      promptChecksum: null,
      contextDocuments: [],
      activeComputationId: null,
    }
  }

  return {
    messages: activeConversation.messages,
    conversationId: activeConversation.conversationId,
    promptChecksum: null,
    contextDocuments: [],
    activeComputationId: null,
  }
}

const ensureUserState = (
  userStates: Record<string, PersistedUserChatState>,
  userId: string
): PersistedUserChatState => {
  const existingState = userStates[userId]
  if (existingState && existingState.conversations.length > 0) {
    const conversations = sortConversations(existingState.conversations)
    const activeConversationId =
      existingState.activeConversationId &&
      conversations.some(
        (conversation) => conversation.conversationId === existingState.activeConversationId
      )
        ? existingState.activeConversationId
        : conversations[0]?.conversationId ?? null

    return {
      conversations,
      activeConversationId,
    }
  }

  const initialConversation = createEmptyConversation()
  return {
    conversations: [initialConversation],
    activeConversationId: initialConversation.conversationId,
  }
}

const replaceConversation = (
  conversations: ChatConversation[],
  replacement: ChatConversation
): ChatConversation[] =>
  sortConversations(
    conversations.map((conversation) =>
      conversation.conversationId === replacement.conversationId
        ? replacement
        : conversation
    )
  )

const deleteConversationsFromState = (
  userState: PersistedUserChatState,
  conversationIds: string[]
): PersistedUserChatState => {
  const ids = new Set(conversationIds)
  const remainingConversations = userState.conversations.filter(
    (conversation) => !ids.has(conversation.conversationId)
  )

  if (remainingConversations.length === 0) {
    const replacement = createEmptyConversation()
    return {
      conversations: [replacement],
      activeConversationId: replacement.conversationId,
    }
  }

  const activeConversationStillExists =
    userState.activeConversationId != null &&
    remainingConversations.some(
      (conversation) => conversation.conversationId === userState.activeConversationId
    )
  const nextActiveConversationId =
    activeConversationStillExists
      ? userState.activeConversationId
      : remainingConversations[0].conversationId

  return {
    conversations: remainingConversations,
    activeConversationId: nextActiveConversationId,
  }
}

const renameConversationInState = (
  userState: PersistedUserChatState,
  conversationId: string,
  title: string
): PersistedUserChatState => {
  let updated = false
  const conversations = userState.conversations.map((conversation) => {
    if (conversation.conversationId !== conversationId) {
      return conversation
    }
    if (conversation.title === title) {
      return conversation
    }
    updated = true
    return {
      ...conversation,
      title,
      updatedAt: nowIso(),
    }
  })

  if (!updated) return userState

  return {
    conversations,
    activeConversationId: userState.activeConversationId,
  }
}

const trimPersistedText = (text: string | undefined, limit: number): string | undefined => {
  if (text == null) return text
  return text.length <= limit ? text : text.slice(0, limit)
}

const sanitizeMetadataForPersistence = (
  metadata: ChatMessage['metadata']
): ChatMessage['metadata'] | undefined => {
  if (!metadata) return undefined

  return {
    assistantState: metadata.assistantState,
    sourceReferences: metadata.sourceReferences,
    retryPrompt: trimPersistedText(
      metadata.retryPrompt,
      MAX_PERSISTED_RETRY_PROMPT_CHARS
    ),
    retryable: metadata.retryable,
  }
}

const normalizeConversationForHydration = (
  conversation: ChatConversation
): ChatConversation => {
  const messages = conversation.messages.map((message) => {
    const sanitizedMetadata = sanitizeMetadataForPersistence(message.metadata)
    const assistantState = message.metadata?.assistantState

    // Action approval cards lose their pendingAction on reload — convert to a
    // neutral text message so the user sees what was proposed without a broken
    // confirm button.
    if (message.type === 'action_approval') {
      return {
        ...message,
        type: 'text' as const,
        metadata: {
          ...sanitizedMetadata,
          assistantState: 'completed' as const,
        },
      }
    }

    if (assistantState !== 'pending' && assistantState !== 'running') {
      return message
    }

    return {
      ...message,
      type: 'error' as const,
      content:
        message.content.trim() ||
        'This response was interrupted before completion. Retry to continue this conversation.',
      metadata: {
        ...sanitizedMetadata,
        assistantState: 'failed' as const,
        retryable: true,
      },
    }
  })

  return {
    ...conversation,
    messages,
    status:
      messages.length === 0
        ? 'draft'
        : messages.some(
            (message) =>
              message.type === 'error' ||
              message.metadata?.assistantState === 'failed'
          )
          ? 'attention'
          : 'active',
  }
}

const applyConversationMutation = (
  state: ChatState,
  userId: string,
  mutate: (userState: PersistedUserChatState) => PersistedUserChatState
): Partial<ChatState> => {
  const nextUserState = mutate(ensureUserState(state.userStates, userId))
  const userStates = {
    ...state.userStates,
    [userId]: {
      ...nextUserState,
      conversations: sortConversations(nextUserState.conversations),
    },
  }
  const activeSlice = syncActiveConversation(userStates[userId])

  return {
    userStates,
    activeUserId: userId,
    ...activeSlice,
  }
}

export const useChatStore = create<ChatState>()((set) => ({
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

  hydrateForUser: (userId) =>
    set((state) => {
      const existingState = state.userStates[userId]
      if (!existingState) {
        return {
          userStates: {
            ...state.userStates,
            [userId]: {
              conversations: [],
              activeConversationId: null,
            },
          },
          activeUserId: userId,
          conversationLoadStatus: 'idle',
          messages: [],
          conversationId: null,
          promptChecksum: null,
          contextDocuments: [],
          activeComputationId: null,
          pendingAction: null,
          pendingDecision: null,
        }
      }

      return {
        activeUserId: userId,
        conversationLoadStatus:
          state.activeUserId === userId ? state.conversationLoadStatus : 'idle',
        ...syncActiveConversation(existingState),
        pendingAction: null,
        pendingDecision: null,
      }
    }),

  setConversationLoadStatus: (status, userId) =>
    set((state) =>
      state.activeUserId === userId
        ? { conversationLoadStatus: status }
        : {}
    ),

  setConversationsFromBackend: (userId, conversations) =>
    set((state) => {
      const normalizedConversations = sortConversations(
        conversations.map(normalizeConversationForHydration)
      )
      const nextUserState: PersistedUserChatState = {
        conversations: normalizedConversations,
        activeConversationId: normalizedConversations[0]?.conversationId ?? null,
      }
      const userStates = {
        ...state.userStates,
        [userId]: nextUserState,
      }
      return {
        userStates,
        activeUserId: userId,
        conversationLoadStatus: 'loaded',
        ...syncActiveConversation(nextUserState),
        pendingAction: null,
        pendingDecision: null,
        promptChecksum: null,
        contextDocuments: [],
        activeComputationId: null,
      }
    }),

  createConversation: (userId) => {
    let conversationId = ''
    set((state) => {
      const userState = ensureUserState(state.userStates, userId)
      const activeConversation = userState.conversations.find(
        (conversation) => conversation.conversationId === userState.activeConversationId
      )

      if (
        activeConversation &&
        activeConversation.messages.length === 0 &&
        activeConversation.title === DEFAULT_CONVERSATION_TITLE
      ) {
        conversationId = activeConversation.conversationId
        return applyConversationMutation(state, userId, () => userState)
      }

      const conversation = createEmptyConversation()
      conversationId = conversation.conversationId
      return applyConversationMutation(state, userId, (nextUserState) => ({
        conversations: [conversation, ...nextUserState.conversations],
        activeConversationId: conversation.conversationId,
      }))
    })
    return conversationId
  },

      selectConversation: (userId, conversationId) =>
        set((state) =>
          applyConversationMutation(state, userId, (userState) => ({
            conversations: userState.conversations,
            activeConversationId: conversationId,
          }))
        ),

      appendMessage: (userId, msg, conversationId) =>
        set((state) => {
          const targetConversationId =
            conversationId ??
            state.userStates[userId]?.activeConversationId ??
            state.conversationId
          const fallbackConversation =
            targetConversationId === null
              ? createEmptyConversation()
              : null

          return applyConversationMutation(state, userId, (userState) => {
            const resolvedConversationId =
              targetConversationId ?? fallbackConversation!.conversationId
            const baseConversations = fallbackConversation
              ? [fallbackConversation, ...userState.conversations]
              : userState.conversations

            const currentConversation = baseConversations.find(
              (conversation) => conversation.conversationId === resolvedConversationId
            )

            if (!currentConversation) {
              return userState
            }

            const messages = [...currentConversation.messages, msg]
            const replacement: ChatConversation = {
              ...currentConversation,
              messages,
              updatedAt: msg.timestamp,
              title:
                currentConversation.title === DEFAULT_CONVERSATION_TITLE ||
                currentConversation.messages.length === 0
                  ? deriveConversationTitle(messages)
                  : currentConversation.title,
              status: msg.type === 'error' ? 'attention' : 'active',
            }

            return {
              conversations: replaceConversation(baseConversations, replacement),
              activeConversationId:
                conversationId === undefined
                  ? resolvedConversationId
                  : userState.activeConversationId ?? resolvedConversationId,
            }
          })
        }),

      updateMessage: (userId, messageId, updates, conversationId) =>
        set((state) =>
          applyConversationMutation(state, userId, (userState) => {
            const resolvedConversationId =
              conversationId ?? userState.activeConversationId
            if (!resolvedConversationId) return userState

            const conversation = userState.conversations.find(
              (entry) => entry.conversationId === resolvedConversationId
            )
            if (!conversation) return userState

            const messages = conversation.messages.map((message) =>
              message.id === messageId
                ? {
                    ...message,
                    ...updates,
                    metadata: {
                      ...message.metadata,
                      ...updates.metadata,
                    },
                  }
                : message
            )

            const replacement: ChatConversation = {
              ...conversation,
              messages,
              updatedAt: nowIso(),
              status:
                messages.length === 0
                  ? 'draft'
                  : messages.some(
                      (message) =>
                        message.type === 'error' ||
                        message.metadata?.assistantState === 'failed'
                    )
                    ? 'attention'
                    : 'active',
            }

            return {
              conversations: replaceConversation(userState.conversations, replacement),
              activeConversationId:
                conversationId === undefined
                  ? resolvedConversationId
                  : userState.activeConversationId ?? resolvedConversationId,
            }
          })
        ),

      removeMessage: (userId, messageId, conversationId) =>
        set((state) =>
          applyConversationMutation(state, userId, (userState) => {
            const resolvedConversationId =
              conversationId ?? userState.activeConversationId
            if (!resolvedConversationId) return userState

            const conversation = userState.conversations.find(
              (entry) => entry.conversationId === resolvedConversationId
            )
            if (!conversation) return userState

            const messages = conversation.messages.filter(
              (message) => message.id !== messageId
            )
            const replacement: ChatConversation = {
              ...conversation,
              messages,
              updatedAt: nowIso(),
              title: deriveConversationTitle(messages),
              status: messages.length === 0 ? 'draft' : conversation.status,
            }

            return {
              conversations: replaceConversation(userState.conversations, replacement),
              activeConversationId:
                conversationId === undefined
                  ? resolvedConversationId
                  : userState.activeConversationId ?? resolvedConversationId,
            }
          })
        ),

      setPendingAction: (action) => set({ pendingAction: action }),

      setPendingDecision: (decision) => set({ pendingDecision: decision }),

      clearConversation: (userId) =>
        set((state) =>
          applyConversationMutation(state, userId, (userState) => {
            const activeConversationId = userState.activeConversationId
            if (!activeConversationId) return userState

            const conversation = userState.conversations.find(
              (entry) => entry.conversationId === activeConversationId
            )
            if (!conversation) return userState

            const replacement: ChatConversation = {
              ...conversation,
              title: DEFAULT_CONVERSATION_TITLE,
              updatedAt: nowIso(),
              status: 'draft',
              messages: [],
            }

            return {
              conversations: replaceConversation(userState.conversations, replacement),
              activeConversationId,
            }
          })
        ),

      deleteConversation: (userId, conversationId) =>
        set((state) =>
          applyConversationMutation(state, userId, (userState) => {
            const conversationExists = userState.conversations.some(
              (entry) => entry.conversationId === conversationId
            )
            if (!conversationExists) return userState
            return deleteConversationsFromState(userState, [conversationId])
          })
        ),

      deleteConversations: (userId, conversationIds) =>
        set((state) =>
          applyConversationMutation(state, userId, (userState) => {
            const filteredIds = conversationIds.filter((conversationId) =>
              userState.conversations.some(
                (entry) => entry.conversationId === conversationId
              )
            )
            if (filteredIds.length === 0) return userState
            return deleteConversationsFromState(userState, filteredIds)
          })
        ),

      renameConversation: (userId, conversationId, title) =>
        set((state) =>
          applyConversationMutation(state, userId, (userState) => {
            const conversationExists = userState.conversations.some(
              (entry) => entry.conversationId === conversationId
            )
            if (!conversationExists) return userState
            return renameConversationInState(userState, conversationId, title)
          })
        ),

      setConversationId: (userId, id) =>
        set((state) =>
          applyConversationMutation(state, userId, (userState) => ({
            conversations: userState.conversations,
            activeConversationId: id,
          }))
        ),

      setContextDocuments: (docs) => set({ contextDocuments: docs }),

  setActiveComputationId: (id) => set({ activeComputationId: id }),
}))
