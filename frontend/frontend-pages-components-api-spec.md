# Frontend Page, Component, and API Interaction Specification

Status: implementation-ready  
Date: 2026-04-23  
Scope: chat-first KODI frontend aligned to approved public backend boundaries

## 1. Product Direction

KODI frontend is chat-first and should feel like a modern AI workspace:

1. one primary conversation workspace
2. explicit context side panel for user-visible workflow state
3. deterministic action confirmations before side effects
4. traceability visible in outcomes where available

## 2. UX Principles

1. conversation-first navigation; do not bury chat behind dashboards
2. minimal cognitive load: one composer, one timeline, contextual right rail
3. deterministic state transitions with visible status chips
4. canonical error rendering with retry hints and reason codes
5. frontend must not force users to understand the internal microservice graph

## 3. Approved Frontend Runtime Alignment

Approved normal user-facing backend boundaries:

1. `auth`
2. `orchestration`
3. `document_ai` only when the product includes direct document workflows

Quarantined internal or admin-only backend surfaces:

1. `tax_core`
2. `forms`
3. `reports`
4. `validation`
5. `storage`
6. raw `knowledge` routes for standard end-user chat
7. `gateway` as a future ingress, not the primary frontend boundary yet

## 4. Primary Page Inventory

## P01. Auth Entry

Purpose:

1. account creation
2. login bootstrap
3. password reset
4. OTP verification handoff
5. protected-route recovery after auth interruption
6. session-expiry re-entry without losing the safe destination context

Approved backend integrations:

1. `/v1/auth/register`
2. `/v1/auth/login`
3. `/v1/auth/password-reset/initiate`
4. `/v1/auth/password-reset/confirm`
5. `/v1/auth/otp/challenges`
6. `/v1/auth/otp/verify`

## P02. Chat Workspace

Purpose:

1. prompt ingestion
2. deterministic route decision
3. route execution with governed final envelope
4. confirmation and trace rendering

Approved backend integrations:

1. `/v1/orchestration/prompt/ingest`
2. `/v1/orchestration/prompt/decide`
3. `/v1/orchestration/prompt/execute`

Core components:

1. `ConversationList`
2. `ConversationThread`
3. `MessageComposer`
4. `ActionApprovalCard`
5. `FinalOutcomeCard`
6. `TraceDrawer`

## P03. Document Center

Purpose:

1. upload sessions and completion
3. evidence and lifecycle visibility

Approved backend integrations:

1. `/v1/documents/upload-sessions`
2. `/v1/documents/{document_id}/upload-completion`
5. `/v1/documents`
6. `/v1/documents/{document_id}`

## P04. Account and Sessions

Purpose:

1. refresh/logout/session introspection
2. self-service account operations
3. phone-number change request and OTP confirmation
4. session health visibility with explicit re-auth handoff when needed

Approved backend integrations:

1. `/v1/auth/refresh`
2. `/v1/auth/logout`
3. `/v1/auth/sessions/{session_id}`
4. `/v1/auth/phone-change/*`
5. `/v1/auth/account-deletion/*`

## P05. Internal Knowledge Admin Workspace

Purpose:

1. governed knowledge ingestion review and publication workflow
2. governed source-version lifecycle inspection and archive control
3. governed source lineage and retention visibility for operators
4. bulk admin actions for supported lifecycle transitions

Boundary rule:

1. raw `knowledge` routes remain internal/admin-only
2. standard end-user guidance still goes through orchestration
3. this workspace must stay isolated from the normal customer-facing nav model
4. source and source-version visibility must feel like an operator console, not a customer search page

## 5. Shared Frontend Capability Modules

1. `PublicApiClientLayer`: typed public service adapters for `auth`,
   `orchestration`, and optional `document_ai`
2. `AuthContextAdapter`: endpoint-aware auth header strategy
3. `IdempotencyManager`: deterministic key generation for replay-sensitive writes
4. `CorrelationBinder`: one correlation ID per user flow
5. `CanonicalErrorNormalizer`: maps backend envelopes into UI-safe typed errors
6. `TraceabilityStore`: stores and renders trace/lineage references from outcomes

## 6. Implementation Slices

1. Slice A: auth entry plus chat workspace shell plus orchestration prompt flow
3. Slice C: conversation history and continuity
4. Slice D: streamed chat UX
5. Slice E: knowledge admin UI as a separate internal surface with lifecycle tabs and bulk actions
6. Slice F: cross-surface hardening and client-demo polish
   Chat continuity remains device-local for the signed-in account until a broader server-backed conversation catalog is introduced.

## 7. End-to-End Acceptance

Before the frontend is considered aligned:

1. auth flows pass through `auth`
2. chat prompt to decision to execution passes through `orchestration`
3. document workflows pass through `document_ai` when enabled
4. canonical error handling is consistent across the approved public surfaces
5. no standard user journey depends on direct `tax_core`, `forms`, `reports`, or
   raw `knowledge` calls

## 8. Guardrails

1. no ad hoc endpoint calls outside typed adapters
2. no hidden mutation of backend request bodies in components
3. no normal user-facing flow may force direct internal-service awareness
4. knowledge management must remain separate from standard end-user chat UX
5. gateway should be treated as a future consolidation target, not today's
   assumed frontend dependency
6. protected routes should preserve safe return paths after auth recovery but fail closed for admin-only surfaces
