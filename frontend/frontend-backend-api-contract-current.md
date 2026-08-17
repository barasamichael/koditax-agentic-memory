# Frontend-Backend API Handshake (Current Approved Frontend Surface)

Status: authoritative for current frontend integration boundaries  
Date: 2026-04-23  
Scope: chat-first frontend integration aligned to the root README public-surface rules

## 1. Integration Rules

1. `Content-Type: application/json` for JSON requests.
2. `X-Correlation-ID` should be sent by frontend on every request; backend will generate one if omitted.
3. Use idempotency keys on replay-sensitive writes where supported.
4. Frontend must normalize canonical error envelopes by reading:
   - `detail.error_code`
   - `detail.message`
   - `detail.reason`
   - `detail.reason_code` when present
   - `detail.context` when present
5. Endpoint-specific auth handling must remain adapter-driven rather than globally hardcoded.

## 2. Approved Public Frontend Boundaries

Normal end-user frontend integrations are:

1. `auth`
2. `orchestration`
3. `document_ai` only when the product includes direct document-management UX

These are the only standard user-facing backend surfaces the frontend should
depend on today.

## 3. Quarantined Internal Services

The following services are not approved as normal browser-facing integrations:

1. `tax_core`
2. `forms`
3. `reports`
4. `validation`
5. `storage`
6. `event_store`
7. raw `knowledge` routes for end-user chat

They may remain present in the repo for deferred internal/admin tooling, but
they are not the standard client boundary model.

## 4. Auth Header Nuances

1. auth uses endpoint-specific header semantics, including bearer-style session
   context on selected protected routes
2. document-ai uses its documented bearer fallback in the browser adapter
3. internal-only adapters may still use `X-Auth-Context`, but that does not make
   them approved end-user browser APIs
4. protected-route recovery should preserve safe destination context in the frontend, but must never bypass auth or role enforcement

## 5. Chat-First Core Endpoints

These are the current product-critical chat endpoints:

1. `POST /v1/orchestration/prompt/ingest`
2. `POST /v1/orchestration/prompt/decide`
3. `POST /v1/orchestration/prompt/execute`
4. `POST /v1/orchestration/income-tax/execute`

End-user tax assistance should go through orchestration, not through direct
internal-service selection in the browser.

## 6. Customer-Critical Frontend Flows Available Now

1. registration, login, refresh, logout, and session/account flows through `auth`
2. prompt-driven chat and governed workflow execution through `orchestration`
3. document upload, extraction, and verification through `document_ai` when the
   product includes document workflows
4. phone-number change remains an auth-scoped self-service flow, not a separate backend surface

## 7. Gateway Reality

1. gateway is still minimal and is not yet the primary frontend ingress
2. direct frontend use of `auth` and `orchestration` remains acceptable until
   gateway matures into the approved unified ingress
3. frontend architecture should still assume a future gateway consolidation path

## 8. Knowledge Boundary Reality

1. raw knowledge routes are not the normal user chat API
2. end-user legal/tax guidance should still go through orchestration
3. any knowledge-management UI is a separate admin/internal surface and must not
   be mixed into standard customer chat flows

## 9. Internal Knowledge Admin Surface

The frontend may use a separate internal/admin knowledge workspace for governed
operator workflows only. Current approved admin-only frontend usage is limited
to management routes such as:

1. `GET /knowledge/ingestion`
2. `GET /knowledge/ingestion/{ingestion_job_id}`
3. `POST /knowledge/ingestion/{ingestion_job_id}/review`
4. `POST /knowledge/ingestion/{ingestion_job_id}/approve`
5. `POST /knowledge/ingestion/{ingestion_job_id}/reject`
6. `POST /knowledge/ingestion/{ingestion_job_id}/publish`
7. `POST /knowledge/ingestion/bulk/reject`
8. `POST /knowledge/ingestion/bulk/publish`
9. `GET /knowledge/source-versions`
10. `GET /knowledge/source-versions/{source_version_id}`
11. `POST /knowledge/source-versions/{source_version_id}/archive`
12. `POST /knowledge/source-versions/bulk/archive`
13. `GET /knowledge/sources`
14. `GET /knowledge/sources/{source_id}`

These routes must remain:

1. admin-only
2. X-Auth-Context driven through internal adapters
3. clearly separate from the normal user chat/document/account product surface
4. aligned to the backend lifecycle contract rather than ad hoc UI-only states

## 10. Endpoint Manifest Reference

Machine-readable source of truth:

1. `frontend/frontend-endpoint-manifest.json`

The manifest now distinguishes:

1. approved public frontend services
2. quarantined internal services

## 11. Frontend Contract Discipline

1. do not treat quarantined internal services as normal user-facing APIs
2. keep standard user flows on typed adapters for `auth`, `orchestration`, and
   optional `document_ai`
3. attach correlation ID and idempotency keys consistently
4. keep request/response DTOs service-scoped and typed
5. treat non-2xx responses as canonical backend failures rather than ad hoc UI
   exceptions
6. preserve safe post-auth route recovery for standard user pages while failing closed for admin-only routes
