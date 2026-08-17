import { getTrustedAuthHeaders, orchestrationClient, withFlowCorrelation } from './client'
import { generateUniqueIdempotencyKey } from '@/lib/idempotency'
import { DEFAULT_TENANT_ID, PUBLIC_FRONTEND_BASE_URLS } from '@/lib/constants'
import type { ChatConversation, ChatMessage } from '@/types/chat'

// Approved public frontend adapter: orchestration is the default user-facing
// workflow surface for chat and governed tax execution.

// ─── Shared prompt payload ────────────────────────────────────────────────────

interface PromptPayload {
  tenant_id: string
  conversation_id: string
  channel: 'chat'
  prompt: {
    text: string
    format: 'plain_text'
  }
}

const buildPromptPayload = (text: string, conversationId: string): PromptPayload => ({
  tenant_id: DEFAULT_TENANT_ID,
  conversation_id: conversationId,
  channel: 'chat',
  prompt: { text, format: 'plain_text' },
})

// ─── Ingest ───────────────────────────────────────────────────────────────────

export interface IngestResponse {
  status: 'accepted'
  ingestion_id: string
  prompt_checksum: string
  conversation_id: string
  tenant_id: string
}

export const ingestPrompt = async (
  text: string,
  conversationId: string,
  signal?: AbortSignal
): Promise<IngestResponse> => {
  const res = await orchestrationClient.post<IngestResponse>(
    '/v1/orchestration/prompt/ingest',
    buildPromptPayload(text, conversationId),
    { headers: withFlowCorrelation(conversationId), signal }
  )
  return res.data
}

// ─── Conversation deletion ──────────────────────────────────────────────────

export interface ConversationDeleteResponse {
  status: 'deleted'
  service: string
  correlation_id: string
  trace_id: string
  conversation_id: string
  deleted_count: number
}

export interface ConversationRenameRequest {
  conversation_title: string
}

export interface ConversationRenameResponse {
  status: 'renamed'
  service: string
  correlation_id: string
  trace_id: string
  conversation_id: string
  conversation_title: string
  updated_count: number
}

export interface BulkConversationDeleteRequest {
  conversation_ids: string[]
}

export interface BulkConversationDeleteResponse {
  status: 'deleted'
  service: string
  correlation_id: string
  trace_id: string
  requested_conversation_ids: string[]
  deleted_conversation_ids: string[]
  deleted_count: number
}

export interface ConversationListResponse {
  status: 'listed'
  service: string
  correlation_id: string
  trace_id: string
  conversations: ChatConversation[]
}

interface ConversationHistoryMessageResponse extends Omit<ChatMessage, 'metadata'> {
  metadata?: ChatMessage['metadata'] | null
}

interface ConversationHistoryConversationResponse {
  conversation_id: string
  title: string
  created_at: string
  updated_at: string
  status: ChatConversation['status']
  messages: ConversationHistoryMessageResponse[]
}

interface ConversationHistoryListResponse {
  status: 'listed'
  service: string
  correlation_id: string
  trace_id: string
  conversations: ConversationHistoryConversationResponse[]
}

const toChatConversation = (
  conversation: ConversationHistoryConversationResponse
): ChatConversation => ({
  conversationId: conversation.conversation_id,
  title: conversation.title,
  createdAt: conversation.created_at,
  updatedAt: conversation.updated_at,
  status: conversation.status,
  messages: conversation.messages.map(({ metadata, ...message }) =>
    metadata == null ? message : { ...message, metadata }
  ),
})

export const fetchConversations = async (
  signal?: AbortSignal
): Promise<ConversationListResponse> => {
  const res = await orchestrationClient.get<ConversationHistoryListResponse>(
    '/v1/orchestration/conversations',
    { headers: withFlowCorrelation('conversation-list'), signal }
  )
  return {
    ...res.data,
    conversations: res.data.conversations.map(toChatConversation),
  }
}

export const deleteConversation = async (
  conversationId: string,
  signal?: AbortSignal
): Promise<ConversationDeleteResponse> => {
  const res = await orchestrationClient.delete<ConversationDeleteResponse>(
    `/v1/orchestration/conversations/${encodeURIComponent(conversationId)}`,
    { headers: withFlowCorrelation(`delete:${conversationId}`), signal }
  )
  return res.data
}

export const renameConversation = async (
  conversationId: string,
  conversationTitle: string,
  signal?: AbortSignal
): Promise<ConversationRenameResponse> => {
  const res = await orchestrationClient.patch<ConversationRenameResponse>(
    `/v1/orchestration/conversations/${encodeURIComponent(conversationId)}`,
    {
      conversation_title: conversationTitle,
    } satisfies ConversationRenameRequest,
    { headers: withFlowCorrelation(`rename:${conversationId}`), signal }
  )
  return res.data
}

export const bulkDeleteConversations = async (
  conversationIds: string[],
  signal?: AbortSignal
): Promise<BulkConversationDeleteResponse> => {
  const sortedIds = [...conversationIds].sort()
  const res = await orchestrationClient.post<BulkConversationDeleteResponse>(
    '/v1/orchestration/conversations/bulk-delete',
    { conversation_ids: conversationIds } satisfies BulkConversationDeleteRequest,
    { headers: withFlowCorrelation(`bulk-delete:${sortedIds.join('|')}`), signal }
  )
  return res.data
}

// ─── Decide ───────────────────────────────────────────────────────────────────

export interface SelectedRoute {
  route_id: string
  target_service: string
  target_operation: string
}

export interface UnifiedAnswerSourceLocation {
  location_kind: 'page' | 'slide' | 'sheet' | 'line' | 'section' | 'cell' | 'image' | 'unknown'
  location_label: string
  location_status: 'exact' | 'approximate' | 'partial' | 'unavailable'
  page_number?: number | null
  slide_number?: number | null
  sheet_name?: string | null
  line_start?: number | null
  line_end?: number | null
  cell_reference?: string | null
  section_name?: string | null
}

export interface UnifiedAnswerSourceReference {
  document_id: string
  document_label: string
  document_status: 'available' | 'partial' | 'unavailable' | string
  source_location: UnifiedAnswerSourceLocation
  openable: boolean
  accessibility_label?: string | null
}

export interface DecideResponse {
  status: 'resolved' | 'clarification_required'
  decision_id: string
  prompt_checksum: string
  intent_class: string
  tax_domain_hint: string
  gate_status: 'allowed' | 'plan_only' | 'clarification_required'
  selected_route: SelectedRoute | null
  plan: {
    plan_id: string
    plan_status: string
    planning_mode: string
    execution_ready: boolean
  }
  clarification?: {
    reason_code: string
    message: string
    required_context_fields: string[]
    candidate_service_families: string[]
  } | null
}

export const decidePrompt = async (
  text: string,
  conversationId: string,
  signal?: AbortSignal
): Promise<DecideResponse> => {
  const res = await orchestrationClient.post<DecideResponse>(
    '/v1/orchestration/prompt/decide',
    buildPromptPayload(text, conversationId),
    { headers: withFlowCorrelation(conversationId), signal }
  )
  return res.data
}

// ─── Execute ──────────────────────────────────────────────────────────────────

interface ActionContext {
  risk_class: 'low' | 'high'
  confirmation_state: 'confirmed' | 'pending' | 'unknown'
  step_up_proof_state: 'bound' | 'unbound' | 'not_required'
}

interface ExecuteRequest extends PromptPayload {
  idempotency_key: string
  intent_class: string
  tax_domain_hint: string
  decision_id: string
  selected_route: SelectedRoute | null
  action_context?: ActionContext
}

export interface UnifiedAnswerResponse {
  status: 'generated' | 'failed' | string
  answer_mode: string
  answer_text: string | null
  citations: Array<{
    citation_id: string
    title: string
    authority_level: string
    temporal_applicability: string
    source_url: string | null
  }>
  source_references: UnifiedAnswerSourceReference[]
  assumptions: string[]
  warnings: string[]
  integrity_signals: ResponseIntegritySignals
}

interface ContradictionFinding {
  claim_topic: string
  source_a_id: string
  source_a_value: string
  source_b_id: string
  source_b_value: string
}

interface FactMismatch {
  field: string
  prior_value: unknown
  prior_execution_id: string
  current_value: unknown
}

interface ResponseIntegritySignals {
  verification_is_verified: boolean
  verification_confidence: number
  unsupported_claims: string[]
  contradictions_found: string[]
  grounding_contradictions: ContradictionFinding[]
  unverified_or_contradicting_user_facts: Array<FactMismatch | string>
  synthesis_tool_iterations_used: number
  confidence_flag: 'high' | 'medium' | 'low'
}

export interface ExecuteResponse {
  status: 'executed'
  execution_id: string
  decision_id: string
  prompt_checksum: string
  selected_route: SelectedRoute
  execution_status: string
  mapped_result: { action_status: string; reason_code: string | null }
  adapter_response: Record<string, unknown>
  response: UnifiedAnswerResponse
  source_references: UnifiedAnswerSourceReference[]
  errors: Array<{ error_code: string; message: string; reason_code: string }> | null
  final_outcome: {
    outcome_status: string
    message: string
    result: {
      response?: UnifiedAnswerResponse
      verification_confidence?: number
      verification_is_verified?: boolean
      grounding_status?: string
      grounded_evidence?: Array<Record<string, unknown>>
      citations?: Array<Record<string, unknown>>
      [key: string]: unknown
    }
    lineage_refs: Record<string, unknown>
  }
}

interface StreamExecutionHandlers {
  onDelta?: (delta: string) => void
  signal?: AbortSignal
}

interface StreamTransportError {
  error_code: string
  message: string
  reason: string
  reason_code?: string
}

const buildExecuteRequest = (params: {
  text: string
  conversationId: string
  decide: DecideResponse
  actionContext?: ActionContext
  idempotencyKey: string
}): ExecuteRequest => ({
  ...buildPromptPayload(params.text, params.conversationId),
  idempotency_key: params.idempotencyKey,
  intent_class: params.decide.intent_class,
  tax_domain_hint: params.decide.tax_domain_hint,
  decision_id: params.decide.decision_id,
  selected_route: params.decide.selected_route ?? null,
  action_context: params.actionContext ?? {
    risk_class: 'low',
    confirmation_state: 'unknown',
    step_up_proof_state: 'not_required',
  },
})

const toStreamTransportError = (payload: unknown): StreamTransportError => {
  if (payload && typeof payload === 'object') {
    const normalized = payload as {
      error_code?: unknown
      message?: unknown
      reason?: unknown
      reason_code?: unknown
      detail?: {
        error_code?: unknown
        message?: unknown
        reason?: unknown
        reason_code?: unknown
      }
    }
    const detail = normalized.detail
    if (detail && typeof detail === 'object') {
      const reasonCode = detail.reason_code ?? detail.reason ?? detail.error_code
      return {
        error_code: String(detail.error_code ?? 'UNKNOWN'),
        message: String(detail.message ?? 'Something went wrong.'),
        reason: String(reasonCode ?? detail.error_code ?? 'UNKNOWN'),
        reason_code: reasonCode == null ? undefined : String(reasonCode),
      }
    }
    const reasonCode = normalized.reason_code ?? normalized.reason ?? normalized.error_code
    return {
      error_code: String(normalized.error_code ?? 'UNKNOWN'),
      message: String(normalized.message ?? 'Something went wrong.'),
      reason: String(reasonCode ?? normalized.error_code ?? 'UNKNOWN'),
      reason_code: reasonCode == null ? undefined : String(reasonCode),
    }
  }
  return {
    error_code: 'UNKNOWN',
    message: 'Something went wrong.',
    reason: 'UNKNOWN',
    reason_code: 'UNKNOWN',
  }
}

const parseSseEvent = (
  rawEvent: string
): { event: string; data: unknown } | null => {
  const lines = rawEvent.replace(/\r\n/g, '\n').split('\n')
  let eventName = 'message'
  const dataLines: string[] = []

  for (const line of lines) {
    if (line.startsWith('event:')) {
      eventName = line.slice('event:'.length).trim()
      continue
    }
    if (line.startsWith('data:')) {
      dataLines.push(line.slice('data:'.length).trim())
    }
  }

  if (dataLines.length === 0) {
    return null
  }

  const dataText = dataLines.join('\n')
  try {
    return { event: eventName, data: JSON.parse(dataText) }
  } catch {
    return { event: eventName, data: dataText }
  }
}

const logStreamTransportError = (
  context: string,
  payload: unknown,
  extra?: Record<string, unknown>
): void => {
  // Keep the browser console noisy only when the transport path is already failing.
  console.error(`[orchestration-stream] ${context}`, {
    payload,
    ...extra,
  })
}

export const executePrompt = async (params: {
  text: string
  conversationId: string
  decide: DecideResponse
  actionContext?: ActionContext
}): Promise<ExecuteResponse> => {
  const idempotencyKey = generateUniqueIdempotencyKey('orchestration-execute')
  const body = buildExecuteRequest({ ...params, idempotencyKey })
  const res = await orchestrationClient.post<ExecuteResponse>(
    '/v1/orchestration/prompt/execute',
    body,
    {
      headers: {
        ...withFlowCorrelation(params.conversationId),
        'Idempotency-Key': idempotencyKey,
      },
    }
  )
  return res.data
}

export const executePromptStream = async (
  params: {
    text: string
    conversationId: string
    decide: DecideResponse
    actionContext?: ActionContext
  },
  handlers: StreamExecutionHandlers = {}
): Promise<ExecuteResponse> => {
  const idempotencyKey = generateUniqueIdempotencyKey('orchestration-execute')
  const response = await fetch(
    `${PUBLIC_FRONTEND_BASE_URLS.orchestration}v1/orchestration/prompt/execute/stream`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
        ...withFlowCorrelation(params.conversationId),
        ...getTrustedAuthHeaders('/v1/orchestration/prompt/execute/stream'),
        'Idempotency-Key': idempotencyKey,
      },
      body: JSON.stringify(buildExecuteRequest({ ...params, idempotencyKey })),
      signal: handlers.signal,
    }
  )

  if (!response.ok) {
    let payload: unknown = null
    try {
      payload = await response.json()
    } catch {
      payload = null
    }
    logStreamTransportError('non-ok response', payload, {
      status: response.status,
      statusText: response.statusText,
    })
    throw toStreamTransportError(payload)
  }

  if (!response.body) {
    throw {
      error_code: 'stream_unavailable',
      message: 'Streaming response body is unavailable.',
      reason: 'stream_unavailable',
    } satisfies StreamTransportError
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let finalResponse: ExecuteResponse | null = null

  while (true) {
    const { value, done } = await reader.read()
    buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done })

    let boundaryIndex = buffer.indexOf('\n\n')
    while (boundaryIndex !== -1) {
      const rawEvent = buffer.slice(0, boundaryIndex)
      buffer = buffer.slice(boundaryIndex + 2)
      boundaryIndex = buffer.indexOf('\n\n')

      const parsedEvent = parseSseEvent(rawEvent)
      if (!parsedEvent) continue

      if (parsedEvent.event === 'error') {
        logStreamTransportError('sse error event', parsedEvent.data, {
          conversationId: params.conversationId,
        })
      }

      if (parsedEvent.event === 'delta') {
        const payload = parsedEvent.data as { delta?: unknown }
        if (typeof payload?.delta === 'string' && payload.delta) {
          handlers.onDelta?.(payload.delta)
        }
        continue
      }

      if (parsedEvent.event === 'final') {
        finalResponse = parsedEvent.data as ExecuteResponse
        continue
      }

      if (parsedEvent.event === 'error') {
        throw toStreamTransportError(parsedEvent.data)
      }
    }

    if (done) {
      break
    }
  }

  if (finalResponse === null) {
    logStreamTransportError('stream terminated before final event', {
      conversationId: params.conversationId,
      decisionId: params.decide.decision_id,
    })
    throw {
      error_code: 'stream_terminated',
      message: 'Streaming response ended before the final orchestration payload arrived.',
      reason: 'stream_terminated',
    } satisfies StreamTransportError
  }

  return finalResponse
}
