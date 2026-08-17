import { useEffect, useRef, useState } from 'react'
import { v4 as uuid } from 'uuid'
import { createDocumentBinding } from '@/api/document.api'
import * as orchestrationApi from '@/api/orchestration.api'
import { useUploadDocument } from '@/hooks/useDocuments'
import { buildConversationTurnId } from '@/lib/chatTurnId'
import { normalizeDocumentRecord } from '@/lib/documents/document-lifecycle'
import { useAuthStore } from '@/stores/authStore'
import { useChatStore } from '@/stores/chatStore'
import { resetFlowCorrelation } from '@/lib/correlation'
import { normalizeError } from '@/lib/errorNormalizer'
import type { ChatAttachment, ChatMessage, ChatSourceReference } from '@/types/chat'
import type { DocumentUploadProgress } from '@/hooks/useDocuments'

const ORCHESTRATION_ERROR_MESSAGES: Record<string, string> = {
  off_topic_prompt:
    'I can only help with Kenyan tax questions. Please ask something related to KRA, PAYE, VAT, or other tax topics.',
  pilot_tenant_not_allowed:
    'Your account is not yet enabled for this feature.',
  unsupported_prompt_scope: 'I cannot process that type of request yet.',
  unsafe_route_override: 'A security check failed. Please try again.',
  prompt_context_mismatch:
    'A session error occurred. Please start a new conversation.',
  route_selection_mismatch:
    'A session error occurred. Please start a new conversation.',
  clarification_required:
    'I need a bit more detail to answer accurately. Could you clarify the tax year or the type of income involved?',
  disambiguation_required:
    'Your request could relate to more than one tax area. Please specify whether this is about income tax, health contributions, or another domain.',
  conversation_state_transition_invalid_schema:
    'The request could not be processed because the response schema is invalid. Please try again after the service is fixed.',
  conversation_state_transition_api_error:
    'The request could not be processed because the transition service failed. Please try again.',
  conversation_state_transition_timeout:
    'The request took too long while checking the conversation state. Please try again.',
  conversation_state_transition_unavailable:
    'The conversation-state service is unavailable right now. Please try again later.',
}

const NON_RETRYABLE_ERROR_CODES = new Set([
  'off_topic_prompt',
  'pilot_tenant_not_allowed',
  'unsupported_prompt_scope',
  'unsafe_route_override',
  'prompt_context_mismatch',
  'route_selection_mismatch',
  'conversation_state_transition_invalid_schema',
  'conversation_state_transition_unavailable',
])

const resolveOrchestrationErrorMessage = (
  errorCode: string,
  fallback: string
): string => ORCHESTRATION_ERROR_MESSAGES[errorCode] ?? fallback

const resolveActionLabel = (intentClass: string, taxDomainHint: string): string => {
  if (intentClass === 'lookup_grounded_knowledge' || intentClass === 'retrieve_grounded_knowledge') {
    return 'Look up tax knowledge'
  }
  if (intentClass === 'compute_health_contribution') {
    return 'Compute health contribution'
  }
  if (taxDomainHint === 'health-contribution' || taxDomainHint === 'health_contribution') {
    return 'Compute health contribution'
  }
  return 'Compute income tax'
}

const resolveActionDescription = (intentClass: string): string => {
  if (intentClass === 'lookup_grounded_knowledge' || intentClass === 'retrieve_grounded_knowledge') {
    return "I'll search the governed tax knowledge base and return the relevant authority."
  }
  if (intentClass === 'compute_health_contribution') {
    return "I'll calculate your health contribution based on your income details."
  }
  return "I'll calculate your income tax based on your documents and details."
}

const isRetryableErrorCode = (errorCode: string): boolean =>
  !NON_RETRYABLE_ERROR_CODES.has(errorCode)

const isAbortError = (error: unknown): boolean => {
  if (error instanceof DOMException && error.name === 'AbortError') return true
  if (error && typeof error === 'object' && 'name' in error) {
    return (error as { name: unknown }).name === 'AbortError'
  }
  return false
}

const isKnowledgeLookupIntent = (intentClass: string): boolean =>
  intentClass === 'lookup_grounded_knowledge' ||
  intentClass === 'retrieve_grounded_knowledge'

const toAttachmentErrorMessage = (error: unknown): string => {
  const normalized = normalizeError(error)
  if (normalized.error_code === 'abort_error') {
    return 'Document upload was cancelled.'
  }
  return normalized.message || 'The document could not be attached.'
}

const toCanonicalError = (
  error: unknown
): { error_code: string; message: string; reason: string; reason_code?: string } => {
  if (
    error &&
    typeof error === 'object' &&
    'error_code' in error &&
    'message' in error &&
    ('reason' in error || 'reason_code' in error)
  ) {
    const normalized = error as {
      error_code: unknown
      message: unknown
      reason: unknown
      reason_code?: unknown
    }
    const reasonValue =
      normalized.reason ?? normalized.reason_code ?? normalized.error_code
    return {
      error_code: String(normalized.error_code),
      message: String(normalized.message),
      reason: String(reasonValue),
      reason_code:
        normalized.reason_code == null ? undefined : String(normalized.reason_code),
    }
  }
  return normalizeError(error)
}

const logChatError = (context: string, error: unknown, canonical?: { error_code: string; message: string; reason: string; reason_code?: string }) => {
  // Keep browser-side diagnostics available for stream and transport failures.
  console.error(`[chat] ${context}`, {
    error,
    canonical,
  })
}

export function useChat() {
  const [pendingCount, setPendingCount] = useState(0)
  const [draftAttachment, setDraftAttachment] = useState<ChatAttachment | null>(null)
  const [attachmentError, setAttachmentError] = useState<string | null>(null)
  const [attachmentProgress, setAttachmentProgress] =
    useState<DocumentUploadProgress | null>(null)
  const [attachmentStage, setAttachmentStage] = useState<
    'idle' | 'uploading' | 'ready' | 'binding'
  >('idle')
  const abortControllerRef = useRef<AbortController | null>(null)
  const streamGenerationRef = useRef(0)
  const uploadDocumentHook = useUploadDocument()
  const userId = useAuthStore((state) => state.userId ?? '')
  const tenantId = useAuthStore((state) => state.tenantId)
  const {
    appendMessage,
    createConversation,
    conversationId,
    removeMessage,
    setPendingAction,
    setPendingDecision,
    setConversationId,
    setContextDocuments,
    updateMessage,
  } = useChatStore()

  useEffect(() => {
    setAttachmentProgress(uploadDocumentHook.progress)
  }, [uploadDocumentHook.progress])

  const beginPending = () => {
    if (!abortControllerRef.current || abortControllerRef.current.signal.aborted) {
      abortControllerRef.current = new AbortController()
    }
    setPendingCount((count) => count + 1)
  }
  const endPending = () =>
    setPendingCount((count) => {
      const next = Math.max(0, count - 1)
      if (next === 0) abortControllerRef.current = null
      return next
    })

  const resetDraftAttachment = () => {
    uploadDocumentHook.cancelUpload()
    uploadDocumentHook.resetUploadState()
    setDraftAttachment(null)
    setAttachmentError(null)
    setAttachmentProgress(null)
    setAttachmentStage('idle')
    setContextDocuments([])
  }

  const attachDocument = async (file: File): Promise<boolean> => {
    if (pendingCount > 0 || attachmentStage === 'uploading' || attachmentStage === 'binding') {
      return false
    }

    setAttachmentError(null)
    setAttachmentProgress(null)
    setAttachmentStage('uploading')
    setDraftAttachment({
      id: 'pending-attachment',
      displayName: file.name,
      fileExtension: file.name.includes('.') ? file.name.split('.').pop() ?? undefined : undefined,
    })

    try {
      const result = await uploadDocumentHook.uploadDocument(file)
      if (!result) {
        resetDraftAttachment()
        return false
      }

      const normalized = normalizeDocumentRecord(result.document)
      const attachment: ChatAttachment = {
        id: normalized.id,
        displayName: normalized.displayName,
        fileExtension: normalized.fileExtension ?? undefined,
      }
      setDraftAttachment(attachment)
      setAttachmentStage('ready')
      setAttachmentProgress(null)
      setContextDocuments([
        {
          id: attachment.id,
          name: attachment.displayName,
          type: attachment.fileExtension ?? 'file',
        },
      ])
      return true
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') {
        resetDraftAttachment()
        return false
      }
      setAttachmentError(toAttachmentErrorMessage(error))
      setAttachmentStage('idle')
      setAttachmentProgress(null)
      return false
    }
  }

  const removeAttachment = () => {
    resetDraftAttachment()
  }

  const markAssistantMessage = (params: {
    conversationId: string
    messageId: string
    content: string
    assistantState: 'pending' | 'running' | 'completed' | 'failed'
    type?: 'text' | 'action_approval' | 'outcome' | 'error'
    progressLabel: string
    retryPrompt: string
    retryable?: boolean
    metadata?: Partial<NonNullable<ChatMessage['metadata']>>
  }) => {
    updateMessage(
      userId,
      params.messageId,
      {
        type: params.type ?? 'text',
        content: params.content,
        timestamp: new Date().toISOString(),
        metadata: {
          assistantState: params.assistantState,
          progressLabel: params.progressLabel,
          retryPrompt: params.retryPrompt,
          retryable: params.retryable ?? false,
          ...params.metadata,
        },
      },
      params.conversationId
    )
  }

  const runPromptFlow = async (params: {
    prompt: string
    flowConversationId: string
    assistantMessageId: string
    attachedDocument?: ChatAttachment | null
  }): Promise<boolean> => {
    const { prompt, flowConversationId, assistantMessageId, attachedDocument } = params
    beginPending()
    let submissionStarted = false

    try {
      markAssistantMessage({
        conversationId: flowConversationId,
        messageId: assistantMessageId,
        content: 'On it...',
        assistantState: 'pending',
        progressLabel: 'Reading your question.',
        retryPrompt: prompt,
      })

      await orchestrationApi.ingestPrompt(prompt, flowConversationId, abortControllerRef.current?.signal)

      markAssistantMessage({
        conversationId: flowConversationId,
        messageId: assistantMessageId,
        content: 'Looking into this for you...',
        assistantState: 'running',
        progressLabel: 'Checking what kind of question this is.',
        retryPrompt: prompt,
      })

      const decideResponse = await orchestrationApi.decidePrompt(
        prompt,
        flowConversationId,
        abortControllerRef.current?.signal
      )

      if (decideResponse.gate_status !== 'allowed') {
        const clarificationReasonCode = decideResponse.clarification?.reason_code
        const hasSystemClarificationCode =
          typeof clarificationReasonCode === 'string' &&
          (clarificationReasonCode.startsWith('conversation_state_transition_') ||
            clarificationReasonCode.startsWith('transition_'))
        const blockedContent =
          decideResponse.gate_status === 'clarification_required' && !hasSystemClarificationCode
            ? decideResponse.clarification?.message ??
              resolveOrchestrationErrorMessage(
                'clarification_required',
                'I need a bit more detail to help with that.'
              )
            : resolveOrchestrationErrorMessage(
                clarificationReasonCode ?? 'conversation_state_transition_api_error',
                "I'm not able to process that request right now."
              )
        const progressLabel =
          decideResponse.gate_status === 'clarification_required' && !hasSystemClarificationCode
            ? 'A bit more detail is needed.'
            : 'Something went wrong.'
        markAssistantMessage({
          conversationId: flowConversationId,
          messageId: assistantMessageId,
          type: 'error',
          content: blockedContent,
          assistantState: 'failed',
          progressLabel,
          retryPrompt: prompt,
          retryable:
            decideResponse.gate_status === 'clarification_required' && !hasSystemClarificationCode
              ? true
              : isRetryableErrorCode(clarificationReasonCode ?? 'conversation_state_transition_api_error'),
        })
        return false
      }

      setPendingDecision(decideResponse)
      const actionLabel = resolveActionLabel(
        decideResponse.intent_class,
        decideResponse.tax_domain_hint
      )
      const actionDescription = resolveActionDescription(decideResponse.intent_class)
      const actionConsequence =
        isKnowledgeLookupIntent(decideResponse.intent_class)
          ? 'This will search the governed tax knowledge base.'
          : decideResponse.intent_class === 'compute_health_contribution'
            ? 'This will compute your health contribution using your current data.'
            : 'This will run a tax computation using your current data.'

      const executeApprovedAction = async (): Promise<boolean> => {
        const streamGeneration = ++streamGenerationRef.current
        let streamSettled = false
        let flushTimeoutId: number | null = null

        const isCurrentStream = () =>
          streamGenerationRef.current === streamGeneration && !streamSettled

        const clearFlushTimeout = () => {
          if (flushTimeoutId === null) return
          window.clearTimeout(flushTimeoutId)
          flushTimeoutId = null
        }

        markAssistantMessage({
          conversationId: flowConversationId,
          messageId: assistantMessageId,
          content: isKnowledgeLookupIntent(decideResponse.intent_class)
            ? 'Searching the tax knowledge base...'
            : 'Got it. Working on your calculation...',
          assistantState: 'completed',
          progressLabel: isKnowledgeLookupIntent(decideResponse.intent_class)
            ? 'Looking up the relevant tax rules.'
            : 'Running your calculation.',
          retryPrompt: prompt,
        })

        const executionMessageId = uuid()

        appendMessage(
          userId,
          {
            id: executionMessageId,
            role: 'assistant',
            content: '',
            timestamp: new Date().toISOString(),
            type: 'outcome',
            metadata: {
              assistantState: 'running',
              progressLabel: 'Writing your answer.',
              retryPrompt: prompt,
              retryable: false,
            },
          },
          flowConversationId
        )

        beginPending()

        if (attachedDocument) {
          setAttachmentStage('binding')
          try {
            const turnId = await buildConversationTurnId({
              conversationId: flowConversationId,
              intentClass: decideResponse.intent_class,
              planId: decideResponse.plan.plan_id,
              promptChecksum: decideResponse.prompt_checksum,
              tenantId,
            })
            await createDocumentBinding({
              document_id: attachedDocument.id,
              binding_role: 'current_turn_attachment',
              conversation_id: flowConversationId,
              turn_id: turnId,
              attachment_order: 0,
            })
            submissionStarted = true
            setAttachmentStage('ready')
          } catch (error) {
            const canonical = toCanonicalError(error)
            logChatError('document binding failed', error, canonical)
            setAttachmentError(
              'The document was saved, but it could not be attached to this question. Please try again.'
            )
            setAttachmentStage('ready')
            markAssistantMessage({
              conversationId: flowConversationId,
              messageId: assistantMessageId,
              type: 'error',
              content:
                'The document was saved, but it could not be attached to this question. Please try again.',
              assistantState: 'failed',
              progressLabel: 'Could not attach the document.',
              retryPrompt: prompt,
              retryable: true,
            })
            endPending()
            return false
          }
        } else {
          submissionStarted = true
        }

        let streamedContent = ''
        try {
          const flushStreamedContent = (assistantState: 'running' | 'completed') => {
            if (!isCurrentStream()) return
            clearFlushTimeout()
            markAssistantMessage({
              conversationId: flowConversationId,
              messageId: executionMessageId,
              type: 'outcome',
              content: streamedContent,
              assistantState,
              progressLabel:
                assistantState === 'running'
                  ? 'Writing your answer.'
                  : 'Done.',
              retryPrompt: prompt,
            })
          }

          const scheduleFlush = () => {
            if (!isCurrentStream() || flushTimeoutId !== null) return
            flushTimeoutId = window.setTimeout(() => {
              flushTimeoutId = null
              flushStreamedContent('running')
            }, 32)
          }

          const executionResponse = await orchestrationApi.executePromptStream(
            {
              text: prompt,
              conversationId: flowConversationId,
              decide: decideResponse,
              actionContext: {
                risk_class: 'low',
                confirmation_state: 'confirmed',
                step_up_proof_state: 'not_required',
              },
            },
            {
              onDelta: (delta) => {
                streamedContent += delta
                scheduleFlush()
              },
              signal: abortControllerRef.current?.signal,
            }
          )

          if (flushTimeoutId !== null) {
            flushStreamedContent('running')
          }

          const outcome = executionResponse.final_outcome
          const responseStatus = executionResponse.response?.status
          const answerText =
            executionResponse.response?.answer_text ||
            outcome.result?.response?.answer_text ||
            null
          const sourceReferences: ChatSourceReference[] = (
            executionResponse.response?.source_references ??
            (outcome.result?.response as { source_references?: ChatSourceReference[] } | undefined)
              ?.source_references ??
            []
          ) as ChatSourceReference[]
          const synthesisWarning =
            executionResponse.response?.warnings?.find(Boolean) ?? null
          const firstError = executionResponse.errors?.[0]

          const displayContent: string =
            answerText ??
            (responseStatus !== 'generated'
              ? (
                  synthesisWarning ??
                  firstError?.message ??
                  'The answer could not be generated at this time. Please try again.'
                )
              : outcome.message)

          streamedContent = displayContent
          streamSettled = true
          clearFlushTimeout()

          markAssistantMessage({
            conversationId: flowConversationId,
            messageId: executionMessageId,
            type: 'outcome',
            content: displayContent,
            assistantState: 'completed',
            progressLabel: 'Done.',
            retryPrompt: prompt,
            metadata: {
              sourceReferences,
            },
          })
          setPendingDecision(null)
          return submissionStarted
        } catch (error) {
          if (isAbortError(error)) {
            markAssistantMessage({
              conversationId: flowConversationId,
              messageId: executionMessageId,
              type: 'error',
              content: streamedContent.trim() || 'Cancelled.',
              assistantState: 'failed',
              progressLabel: 'Cancelled.',
              retryPrompt: prompt,
              retryable: true,
            })
          } else {
            const canonical = toCanonicalError(error)
            logChatError('stream execution failed', error, canonical)
            streamSettled = true
            clearFlushTimeout()
            markAssistantMessage({
              conversationId: flowConversationId,
              messageId: executionMessageId,
              type: 'error',
              content: resolveOrchestrationErrorMessage(
                canonical.error_code,
                canonical.reason
              ),
              assistantState: 'failed',
              progressLabel: 'Something went wrong.',
              retryPrompt: prompt,
              retryable: isRetryableErrorCode(canonical.error_code),
            })
          }
          return submissionStarted
        } finally {
          streamSettled = true
          clearFlushTimeout()
          endPending()
        }
      }

      if (isKnowledgeLookupIntent(decideResponse.intent_class)) {
        markAssistantMessage({
          conversationId: flowConversationId,
          messageId: assistantMessageId,
          type: 'text',
          content: 'Searching the tax knowledge base...',
          assistantState: 'running',
          progressLabel: 'Looking up the relevant tax rules.',
          retryPrompt: prompt,
        })
        const executed = await executeApprovedAction()
        return executed
      }

      markAssistantMessage({
        conversationId: flowConversationId,
        messageId: assistantMessageId,
        type: 'action_approval',
        content: actionLabel,
        assistantState: 'completed',
        progressLabel: 'Please review and confirm below.',
        retryPrompt: prompt,
      })

      setPendingAction({
        id: assistantMessageId,
        conversationId: flowConversationId,
        label: actionLabel,
        description: actionDescription,
        consequence: actionConsequence,
        onConfirm: async () => {
          await executeApprovedAction()
        },
      })
      return true
    } catch (error) {
      if (isAbortError(error)) {
        markAssistantMessage({
          conversationId: flowConversationId,
          messageId: assistantMessageId,
          type: 'error',
          content: 'Cancelled.',
          assistantState: 'failed',
          progressLabel: 'Cancelled.',
          retryPrompt: prompt,
          retryable: true,
        })
      } else {
        const canonical = toCanonicalError(error)
        logChatError('prompt flow failed', error, canonical)
        markAssistantMessage({
          conversationId: flowConversationId,
          messageId: assistantMessageId,
          type: 'error',
          content: resolveOrchestrationErrorMessage(
            canonical.error_code,
            canonical.reason
          ),
          assistantState: 'failed',
          progressLabel: 'Could not complete your request.',
          retryPrompt: prompt,
          retryable: isRetryableErrorCode(canonical.error_code),
        })
      }
      return false
    } finally {
      endPending()
    }
  }

  const sendMessage = async (
    content: string,
    options?: { conversationId?: string; existingAssistantMessageId?: string }
  ): Promise<boolean> => {
    if (!userId) return false

    const flowConversationId =
      options?.conversationId ?? conversationId ?? createConversation(userId)
    const attachedDocument = draftAttachment
    const userMessageId = uuid()

    if (!conversationId) {
      setConversationId(userId, flowConversationId)
      resetFlowCorrelation(flowConversationId)
    }

    if (!options?.existingAssistantMessageId) {
      appendMessage(
        userId,
        {
          id: userMessageId,
          role: 'user',
          content,
          timestamp: new Date().toISOString(),
          type: 'text',
          metadata: attachedDocument
            ? {
                documentIds: [attachedDocument.id],
                documents: [attachedDocument],
              }
            : undefined,
        },
        flowConversationId
      )
    }

    const assistantMessageId =
      options?.existingAssistantMessageId ?? uuid()

    if (options?.existingAssistantMessageId) {
      markAssistantMessage({
        conversationId: flowConversationId,
        messageId: assistantMessageId,
        content: 'Trying again...',
        assistantState: 'pending',
        progressLabel: 'Picking up where we left off.',
        retryPrompt: content,
      })
    } else {
      appendMessage(
        userId,
        {
          id: assistantMessageId,
          role: 'assistant',
          content: 'On it...',
          timestamp: new Date().toISOString(),
          type: 'text',
          metadata: {
            assistantState: 'pending',
            progressLabel: 'Reading your question.',
            retryPrompt: content,
            retryable: false,
          },
        },
        flowConversationId
      )
    }

    const submitted = await runPromptFlow({
      prompt: content,
      flowConversationId,
      assistantMessageId,
      attachedDocument,
    })
    if (submitted) {
      resetDraftAttachment()
      return true
    }

    setPendingDecision(null)
    if (!options?.existingAssistantMessageId) {
      removeMessage(userId, assistantMessageId, flowConversationId)
      removeMessage(userId, userMessageId, flowConversationId)
    }
    return false
  }

  const retryMessage = async (
    prompt: string,
    flowConversationId: string,
    messageId: string
  ): Promise<boolean> => {
    if (!userId) return false
    return sendMessage(prompt, {
      conversationId: flowConversationId,
      existingAssistantMessageId: messageId,
    })
  }

  const dismissMessage = (messageId: string, flowConversationId?: string) => {
    if (!userId) return
    removeMessage(userId, messageId, flowConversationId ?? conversationId)
  }

  const cancelQuery = () => {
    if (attachmentStage === 'uploading' && pendingCount === 0) {
      resetDraftAttachment()
      return
    }
    abortControllerRef.current?.abort()
  }

  return {
    sendMessage,
    retryMessage,
    dismissMessage,
    cancelQuery,
    attachDocument,
    removeAttachment,
    attachment: draftAttachment,
    attachmentError,
    attachmentProgress,
    attachmentStage,
    isPending: pendingCount > 0 || attachmentStage === 'uploading' || attachmentStage === 'binding',
  }
}
