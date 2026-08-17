import { renderHook, act } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useChat } from '@/hooks/useChat'

const mockChatState = {
  conversationId: 'conversation-1',
  appendMessage: vi.fn(),
  createConversation: vi.fn(() => 'conversation-1'),
  removeMessage: vi.fn(),
  setPendingAction: vi.fn(),
  setPendingDecision: vi.fn(),
  setConversationId: vi.fn(),
  setContextDocuments: vi.fn(),
  updateMessage: vi.fn(),
}

const mockAuthState = {
  userId: 'user-1',
}

const mockApi = vi.hoisted(() => ({
  ingestPrompt: vi.fn(),
  decidePrompt: vi.fn(),
  executePromptStream: vi.fn(),
  createDocumentBinding: vi.fn(),
}))

const mockDocumentHooks = vi.hoisted(() => ({
  useUploadDocument: vi.fn(),
}))

vi.mock('@/api/orchestration.api', () => ({
  ingestPrompt: mockApi.ingestPrompt,
  decidePrompt: mockApi.decidePrompt,
  executePromptStream: mockApi.executePromptStream,
}))

vi.mock('@/api/document.api', () => ({
  createDocumentBinding: mockApi.createDocumentBinding,
}))

vi.mock('@/hooks/useDocuments', () => ({
  useUploadDocument: mockDocumentHooks.useUploadDocument,
}))

vi.mock('@/stores/authStore', () => ({
  useAuthStore: (selector?: (state: typeof mockAuthState) => unknown) =>
    (typeof selector === 'function' ? selector(mockAuthState) : mockAuthState),
}))

vi.mock('@/stores/chatStore', () => ({
  useChatStore: (selector?: (state: typeof mockChatState) => unknown) =>
    (typeof selector === 'function' ? selector(mockChatState) : mockChatState),
}))

describe('useChat clarification handling', () => {
  const clarificationCases = [
    {
      reasonCode: 'missing_tax_year',
      message: 'Please tell me the tax year for this return.',
      requiredContextFields: ['tax_year'],
      candidateServiceFamilies: ['tax_core'],
    },
    {
      reasonCode: 'missing_tax_domain',
      message: 'Please tell me which tax type you mean.',
      requiredContextFields: ['tax_domain'],
      candidateServiceFamilies: ['tax_core', 'knowledge'],
    },
  ]

  beforeEach(() => {
    vi.clearAllMocks()
    mockDocumentHooks.useUploadDocument.mockReturnValue({
      status: 'idle',
      progress: null,
      uploadDocument: vi.fn(),
      cancelUpload: vi.fn(),
      resetUploadState: vi.fn(),
    })
    mockApi.ingestPrompt.mockResolvedValue({
      status: 'accepted',
      ingestion_id: 'ingestion-1',
      prompt_checksum: 'checksum-1',
      conversation_id: 'conversation-1',
      tenant_id: 'pilot_tenant_alpha',
    })
    mockApi.executePromptStream.mockReset()
    mockApi.createDocumentBinding.mockResolvedValue({
      status: 'ok',
      binding: {
        document_binding_id: 'binding-1',
        tenant_id: 'pilot_tenant_alpha',
        document_id: 'doc-1',
        document_version_id: null,
        resolved_document_version_id: null,
        binding_role: 'current_turn_attachment',
        conversation_id: 'conversation-1',
        turn_id: 'turn-1',
        workflow_id: null,
        attachment_order: 0,
        bound_by_user_id: 'user-1',
        bound_at: '2026-08-05T00:00:00.000Z',
        correlation_id: 'corr-1',
      },
    })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it.each(clarificationCases)(
    'surfaces the backend clarification message for %s instead of the stock fallback',
    async (caseData) => {
      mockApi.decidePrompt.mockResolvedValue({
        status: 'clarification_required',
        decision_id: 'decision-1',
        prompt_checksum: 'checksum-1',
        intent_class: 'clarification_required',
        tax_domain_hint: 'income_tax',
        gate_status: 'clarification_required',
        selected_route: null,
        plan: {
          plan_id: 'plan-1',
          plan_status: 'planned',
          planning_mode: 'clarification_required',
          execution_ready: false,
        },
        clarification: {
          reason_code: caseData.reasonCode,
          message: caseData.message,
          required_context_fields: caseData.requiredContextFields,
          candidate_service_families: caseData.candidateServiceFamilies,
        },
      })

      const { result } = renderHook(() => useChat())

      await act(async () => {
        await result.current.sendMessage('this year')
      })

      const clarificationUpdate = mockChatState.updateMessage.mock.calls.find(
        (call) => call[2]?.type === 'error' && call[2]?.content
      )

      expect(clarificationUpdate?.[2].content).toBe(caseData.message)
      expect(clarificationUpdate?.[2].content).not.toContain(
        'tax year or the type of income involved'
      )
    }
  )

  it('surfaces transition schema failures as system errors instead of clarification', async () => {
    mockApi.decidePrompt.mockRejectedValue({
      error_code: 'conversation_state_transition_invalid_schema',
      message: 'The request could not be processed because the response schema is invalid.',
      reason: 'invalid_transition_response_schema',
    })

    const { result } = renderHook(() => useChat())

    await act(async () => {
      await result.current.sendMessage('what is vat?')
    })

    const errorUpdate = mockChatState.updateMessage.mock.calls.find(
      (call) => call[2]?.type === 'error' && call[2]?.content
    )

    expect(errorUpdate?.[2].content).toContain('response schema is invalid')
  })

  it('does not collapse stream errors with reason_code into UNKNOWN', async () => {
    mockApi.decidePrompt.mockResolvedValue({
      status: 'resolved',
      decision_id: 'decision-1',
      prompt_checksum: 'checksum-1',
      intent_class: 'lookup_grounded_knowledge',
      tax_domain_hint: 'general_tax',
      gate_status: 'allowed',
      selected_route: {
        route_id: 'knowledge_search_route_v1',
        target_service: 'knowledge',
        target_operation: 'search_knowledge',
      },
      plan: {
        plan_id: 'plan-1',
        plan_status: 'planned',
        planning_mode: 'single_step',
        execution_ready: true,
      },
      clarification: null,
    })
    mockApi.executePromptStream.mockRejectedValue({
      error_code: 'unsupported_knowledge_scope',
      message: 'Deterministic knowledge grounding rejected selected orchestration route.',
      reason_code: 'unsupported_knowledge_scope',
    })

    const { result } = renderHook(() => useChat())

    await act(async () => {
      await result.current.sendMessage('VAT registration threshold around 5 million')
    })

    const errorUpdate = mockChatState.updateMessage.mock.calls.find(
      (call) => call[2]?.type === 'error' && call[2]?.content
    )

    expect(errorUpdate?.[2].content).toBe('unsupported_knowledge_scope')
    expect(errorUpdate?.[2].content).not.toBe('UNKNOWN')
  })

  it('keeps a completed streamed answer from being overwritten by a late flush', async () => {
    vi.useFakeTimers()

    mockApi.decidePrompt.mockResolvedValue({
      status: 'resolved',
      decision_id: 'decision-1',
      prompt_checksum: 'checksum-1',
      intent_class: 'lookup_grounded_knowledge',
      tax_domain_hint: 'general_tax',
      gate_status: 'allowed',
      selected_route: {
        route_id: 'knowledge_search_route_v1',
        target_service: 'knowledge',
        target_operation: 'search_knowledge',
      },
      plan: {
        plan_id: 'plan-1',
        plan_status: 'planned',
        planning_mode: 'single_step',
        execution_ready: true,
      },
      clarification: null,
    })
    mockApi.executePromptStream.mockImplementation(async (_params, handlers) => {
      handlers.onDelta?.('The tax filing deadline is ')
      return {
        execution_id: 'execution-1',
        decision_id: 'decision-1',
        selected_route: 'knowledge_search_route_v1',
        response: {
          status: 'generated',
          answer_text: 'The tax filing deadline is 30 June 2026.',
          warnings: [],
        },
        final_outcome: {
          message: 'Generated successfully.',
          result: {
            response: {
              answer_text: 'The tax filing deadline is 30 June 2026.',
            },
          },
          lineage_refs: [],
        },
        errors: [],
      }
    })

    const { result } = renderHook(() => useChat())

    await act(async () => {
      await result.current.sendMessage('When is the KRA filing deadline?')
    })

    await act(async () => {
      vi.runAllTimers()
    })

    const outcomeUpdates = mockChatState.updateMessage.mock.calls.filter(
      (call) => call[2]?.type === 'outcome' && call[2]?.content
    )
    const lastOutcomeUpdate = outcomeUpdates[outcomeUpdates.length - 1]

    expect(lastOutcomeUpdate?.[2]?.metadata?.assistantState).toBe('completed')
    expect(lastOutcomeUpdate?.[2]?.content).toContain('30 June 2026')
  })

  it('binds a freshly uploaded document to the current turn before execution', async () => {
    const uploadedDocument = {
      status: 'ok',
      document: {
        document_id: 'doc-1',
        state: 'validated',
        uploaded_at: '2026-08-05T00:00:00.000Z',
        computation_id: null,
        purge_eligible_at: null,
        purged_at: null,
        compliance_lock_until: null,
        display_name: 'Payslip.pdf',
        category: null,
        tags: [],
        description: null,
        revision: 1,
      },
      traceability: {
        trace_id: 'trace-1',
        correlation_id: 'corr-1',
      },
    }
    const uploadDocument = vi.fn().mockResolvedValue(uploadedDocument)
    mockDocumentHooks.useUploadDocument.mockReturnValue({
      status: 'idle',
      progress: null,
      uploadDocument,
      cancelUpload: vi.fn(),
      resetUploadState: vi.fn(),
    })
    mockApi.decidePrompt.mockResolvedValue({
      status: 'resolved',
      decision_id: 'decision-1',
      prompt_checksum: 'checksum-1',
      intent_class: 'lookup_grounded_knowledge',
      tax_domain_hint: 'general_tax',
      gate_status: 'allowed',
      selected_route: {
        route_id: 'knowledge_search_route_v1',
        target_service: 'knowledge',
        target_operation: 'search_knowledge',
      },
      plan: {
        plan_id: 'plan-1',
        plan_status: 'planned',
        planning_mode: 'single_step',
        execution_ready: true,
      },
      clarification: null,
    })
    mockApi.executePromptStream.mockResolvedValue({
      execution_id: 'execution-1',
      decision_id: 'decision-1',
      prompt_checksum: 'checksum-1',
      tax_domain_hint: 'general_tax',
      plan: {
        plan_id: 'plan-1',
        plan_status: 'planned',
        planning_mode: 'single_step',
        execution_ready: true,
        steps: [],
      },
      execution_status: 'resolved',
      mapped_result: { action_status: 'accepted', reason_code: null },
      adapter_response: null,
      response: {
        status: 'generated',
        answer_mode: 'grounded_knowledge',
        answer_text: 'All set.',
        citations: [],
        assumptions: [],
        warnings: [],
        integrity_signals: {
          verification_is_verified: true,
          verification_confidence: 1,
          unsupported_claims: [],
          contradictions_found: [],
          grounding_contradictions: [],
          unverified_or_contradicting_user_facts: [],
          synthesis_tool_iterations_used: 0,
          confidence_flag: 'high',
        },
      },
      final_outcome: {
        outcome_status: 'ok',
        message: 'All set.',
        result: {
          response: {
            answer_text: 'All set.',
          },
        },
        lineage_refs: {},
      },
      errors: null,
    })

    const { result } = renderHook(() => useChat())

    await act(async () => {
      await result.current.attachDocument(new File(['stub'], 'Payslip.pdf', { type: 'application/pdf' }))
    })

    await act(async () => {
      await result.current.sendMessage('Please review this payslip')
    })

    expect(uploadDocument).toHaveBeenCalledTimes(1)
    expect(mockApi.createDocumentBinding).toHaveBeenCalledTimes(1)
    expect(mockApi.createDocumentBinding.mock.calls[0][0]).toMatchObject({
      document_id: 'doc-1',
      binding_role: 'current_turn_attachment',
      conversation_id: 'conversation-1',
      attachment_order: 0,
    })
  })
})
