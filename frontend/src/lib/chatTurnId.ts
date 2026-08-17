const sha256Hex = async (value: string): Promise<string> => {
  const bytes = new TextEncoder().encode(value)
  const hashBuffer = await crypto.subtle.digest('SHA-256', bytes)
  return Array.from(new Uint8Array(hashBuffer))
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('')
}

export const buildConversationTurnId = async (params: {
  conversationId: string
  intentClass: string
  planId: string
  promptChecksum: string
  tenantId: string
}): Promise<string> => {
  // Mirrors the backend's JSON-deterministic hash inputs for the execution turn id.
  const canonicalPayload = JSON.stringify({
    conversation_id: params.conversationId,
    intent_class: params.intentClass,
    plan_id: params.planId,
    prompt_checksum: params.promptChecksum,
    tenant_id: params.tenantId,
  })
  return sha256Hex(canonicalPayload)
}
