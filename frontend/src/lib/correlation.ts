import { v4 as uuid } from 'uuid'

// Per-flow: reuse same ID across a multi-step flow (e.g. orchestration ingest→decide→execute)
const flowStore = new Map<string, string>()

export const getFlowCorrelationId = (flowKey: string): string => {
  if (!flowStore.has(flowKey)) flowStore.set(flowKey, uuid())
  return flowStore.get(flowKey)!
}

export const resetFlowCorrelation = (flowKey: string) => flowStore.delete(flowKey)

// Per-request: fresh UUID for each independent request (auth, documents, forms, tax_core)
export const getRequestCorrelationId = (): string => uuid()

// Legacy alias — kept so existing callers compile without change
export const getCorrelationId = getFlowCorrelationId
export const resetCorrelation = resetFlowCorrelation
