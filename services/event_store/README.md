# Event Store Service

Append-only, tamper-evident audit event persistence service for the Kodi Solutions tax compliance platform. Provides durable audit event storage with SHA-256 hash-chain integrity, deterministic cursor-based pagination, retention lifecycle management, and scoped event replay — serving as the immutable audit backbone for all other microservices.

---

## Table of Contents

1. [Description](#description)
2. [Dependencies](#dependencies)
3. [Installation](#installation)
4. [API Endpoints](#api-endpoints)
5. [Business Rules & Constraints](#business-rules--constraints)
6. [Event Contracts](#event-contracts)
7. [Error Handling](#error-handling)
8. [Performance Metrics](#performance-metrics)
9. [Security & Compliance](#security--compliance)
10. [Integration Capabilities](#integration-capabilities)
11. [Project Structure](#project-structure)

---

## Description

The Event Store is the platform's immutable audit backbone. Every service that performs a security-sensitive or business-significant state change emits an audit event here. Events are append-only — they can never be deleted or mutated, only archived after retention expiry.

**Key capabilities:**

- **Append-only persistence**: Events written with idempotency fingerprinting; duplicate submissions detected and rejected with `409`
- **SHA-256 hash-chain integrity**: Each event carries a `previous_event_checksum` linking it to the prior event in the chain; tamper detection via `integrity/verify` endpoint
- **Retention lifecycle**: Events carry `retention_expires_at` based on policy; eligible events can be transitioned to archived state
- **Archival transition**: Archived events are moved to a secondary store; the primary store retains metadata for lineage
- **Scoped queries**: Events queried by `tenant_id` and optionally `user_id`; delegation context expands the visible user scope
- **Correlation replay**: All events for a given `correlation_id` replayed in deterministic order
- **Cursor-based pagination**: Base64url-encoded JSON cursors with `created_at` + `event_id` for stable pagination across pages
- **Delegation-aware access**: `TaxAgent` and `Accountant` principals can query events for their `principal_user_id`

**Architectural role:** Consumed by all services for audit persistence. The only cross-cutting infrastructure service in the platform — every state change with compliance significance is recorded here.

---

## Dependencies

### Python Packages

| Package | Purpose |
|---------|---------|
| `fastapi` | HTTP routing |
| `pydantic` | Request/response schema validation |
| `uvicorn` | ASGI runtime |

### Database

- **PostgreSQL**: Primary storage for `audit_events` table and `archived_audit_events` table
- Migration 14 adds: `idempotency_payload_fingerprint`, `retention_expires_at`, `retention_policy_code`, `retention_days`, `archived_at`, `archival_reason_code` columns
- Migration 15 adds: unique index on `idempotency_payload_fingerprint`

### Environment Variables

```bash
DATABASE_URL=postgresql://user:password@localhost:5432/kodi_audit
EVENT_STORE_RETENTION_POLICY_CODE=default
EVENT_STORE_RETENTION_DAYS=2555         # ~7 years (KRA compliance requirement)
```

---

## Installation

```bash
cd services/event_store

pip install -r requirements.txt

# Run migrations
alembic upgrade head

# Start service
uvicorn services.event_store.app.main:app --host 0.0.0.0 --port 8002 --reload
```

---

## API Endpoints

### Endpoint Summary

| Method | Path | Auth Required | Summary |
|--------|------|---------------|---------|
| POST | `/audit/append` | Bearer | Append immutable audit event |
| GET | `/audit/retention/eligible` | Bearer | List retention-eligible events |
| POST | `/audit/archival/mark` | Bearer | Mark event as archived |
| GET | `/audit/events` | Bearer | Query events with pagination |
| GET | `/audit/replay/{correlation_id}` | Bearer | Replay events by correlation ID |
| GET | `/audit/integrity/verify` | Bearer | Verify hash-chain integrity for a page |

**Allowed roles**: `IndividualTaxpayer`, `TaxAgent`, `Accountant`
**Allowed delegated roles**: `TaxAgent`, `Accountant`

---

### Append

#### POST /audit/append

**Summary**: Append one immutable audit event with idempotency guarantee

**Authentication**: Required (Bearer token)

**Headers**:
- `Authorization: Bearer {access_token}` (required)
- `X-Correlation-ID` (string, optional): Propagated correlation identifier

**Request Body**:
- **event_type** (`string`, required): Categorized event type (e.g., `auth.login.success`, `document.lifecycle.trash`)
- **user_id** (`string`, UUID, required): User whose action is being recorded
- **trace_id** (`string`, optional): Distributed trace identifier; falls back to request trace
- **correlation_id** (`string`, required): Correlation identifier linking related events across services
- **idempotency_key** (`string`, required): Deduplication key; max 128 chars, non-empty after stripping

**Response** (200 OK):
```json
{
  "event_id": "550e8400-e29b-41d4-a716-446655440000",
  "correlation_id": "req-20260412-001"
}
```

**Business Rules**:
- `idempotency_key` is hashed into `idempotency_payload_fingerprint`; duplicate fingerprints return `409`
- `role_at_time` is captured from the principal at the moment of append (immutable record of role)
- Delegation context (`is_delegated`, `principal_user_id`, `delegate_user_id`, `delegation_id`) captured from principal
- Events are timestamped at the moment of append server-side (`event_timestamp = datetime.now(UTC)`)
- Retention expiry calculated immediately at append time using the configured retention policy

**Error Responses**:
- `400` — `invalid_event_store_request` (malformed payload, empty idempotency key)
- `409` — `append_conflict` (duplicate idempotency fingerprint)
- `500` — `persistence_not_configured`, `retention_policy_invalid`
- `503` — Storage layer unavailable

**Example Request**:
```bash
curl -X POST https://api.kodi.solutions/audit/append \
  -H "Authorization: Bearer eyJ0eXAiOiJKV1Qi..." \
  -H "Content-Type: application/json" \
  -H "X-Correlation-ID: req-20260412-001" \
  -d '{
    "event_type": "auth.login.success",
    "user_id": "550e8400-e29b-41d4-a716-446655440001",
    "trace_id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
    "correlation_id": "req-20260412-001",
    "idempotency_key": "login-20260412-alice-001"
  }'
```

---

### Retention

#### GET /audit/retention/eligible

**Summary**: List events whose retention expiry has passed and are eligible for archival

**Authentication**: Required (Bearer token)

**Query Parameters**:
- **limit** (`integer`, optional): Max events to return, default 50
- **as_of** (`datetime`, optional): Evaluate eligibility as of this timestamp; defaults to `now()`

**Response** (200 OK):
```json
{
  "as_of": "2026-04-12T10:30:00Z",
  "events": [
    {
      "event_id": "550e8400-e29b-41d4-a716-446655440000",
      "user_id": "550e8400-e29b-41d4-a716-446655440001",
      "correlation_id": "req-20260412-001",
      "trace_id": "a1b2c3d4...",
      "created_at": "2019-04-12T10:30:00Z",
      "retention_expires_at": "2026-04-12T10:30:00Z",
      "retention_policy_code": "default",
      "retention_days": 2555
    }
  ]
}
```

**Business Rules**:
- Results scoped to the principal's `user_id` (and `principal_user_id` if delegated)
- Only events past `retention_expires_at` as of `as_of` timestamp are returned

**Error Responses**:
- `400` — `invalid_event_store_request`
- `403` — `archival_forbidden` (cross-tenant scope requested)
- `500` — `persistence_not_configured`

---

#### POST /audit/archival/mark

**Summary**: Mark one retention-eligible event as archived

**Authentication**: Required (Bearer token)

**Request Body**:
- **event_id** (`string`, UUID, required): Event to archive
- **reason_code** (`string`, required): Archival reason (e.g., `retention_expired`); defaults to `retention_expired` if empty
- **archived_at** (`datetime`, optional): Archival timestamp; defaults to `now()`

**Response** (200 OK):
```json
{
  "status": "archived",
  "event_id": "550e8400-e29b-41d4-a716-446655440000",
  "user_id": "550e8400-e29b-41d4-a716-446655440001",
  "correlation_id": "req-20260412-001",
  "archived_at": "2026-04-12T10:35:00Z",
  "archival_reason_code": "retention_expired"
}
```

**Error Responses**:
- `403` — `archival_forbidden` (event belongs to different user outside delegation scope)
- `404` — `archival_not_found` (event does not exist)
- `409` — `archival_ineligible` (retention period not yet expired)
- `500` — `persistence_not_configured`

---

### Query and Replay

#### GET /audit/events

**Summary**: Query audit events with stable cursor-based pagination

**Authentication**: Required (Bearer token)

**Query Parameters**:
- **tenant_id** (`string`, required): Must match principal's `tenant_id`
- **user_id** (`string`, UUID, optional): Filter to specific user; must be within principal's allowed scope
- **limit** (`integer`, optional): Page size, default 50
- **cursor** (`string`, optional): Base64url-encoded pagination cursor from previous response

**Response** (200 OK):
```json
{
  "tenant_id": "default_tenant",
  "user_id": "550e8400-e29b-41d4-a716-446655440001",
  "correlation_id": null,
  "limit": 50,
  "next_cursor": "eyJjcmVhdGVkX2F0IjoiMjAyNi0wNC0xMlQxMDozMDowMFoiLCJldmVudF9pZCI6IjU1MGU4NDAwLWUyOWItNDFkNC1hNzE2LTQ0NjY1NTQ0MDAwMCJ9",
  "events": [
    {
      "event_id": "550e8400-e29b-41d4-a716-446655440000",
      "event_type": "auth.login.success",
      "action_type": "auth.login",
      "user_id": "550e8400-e29b-41d4-a716-446655440001",
      "trace_id": "a1b2c3d4...",
      "correlation_id": "req-20260412-001",
      "idempotency_key": "login-20260412-alice-001",
      "created_at": "2026-04-12T10:30:00Z",
      "previous_event_checksum": "f1e2d3c4b5a6...",
      "event_checksum": "a1b2c3d4e5f6...",
      "is_delegated": false,
      "principal_user_id": null,
      "delegate_user_id": null,
      "delegation_id": null
    }
  ]
}
```

**Cursor Format**: Base64url-encoded JSON `{"created_at": "ISO8601Z", "event_id": "UUID"}`

**Access Control**: `tenant_id` must match principal's tenant. Cross-tenant queries return `403`.

#### GET /audit/replay/{correlation_id}

**Summary**: Replay all events for a given correlation ID in deterministic order

**Path Parameters**:
- **correlation_id** (`string`, required): Non-empty string; URL-encoded

**Query Parameters**: Same as `GET /audit/events` (tenant_id, user_id, limit, cursor)

**Response**: Same structure as `GET /audit/events`, filtered to the specified `correlation_id`

**Error Responses**:
- `400` — `invalid_event_store_request` (empty correlation ID or invalid cursor)
- `403` — `query_scope_forbidden` (tenant mismatch)

---

### Integrity Verification

#### GET /audit/integrity/verify

**Summary**: Verify SHA-256 hash-chain integrity for a paginated scope of events

**Authentication**: Required (Bearer token)

**Query Parameters**:
- **tenant_id** (`string`, required): Must match principal's tenant
- **user_id** (`string`, UUID, optional): Scope to specific user
- **correlation_id** (`string`, optional): Scope to correlation chain
- **limit** (`integer`, optional): Page size, default 50
- **cursor** (`string`, optional): Pagination cursor

**Response** (200 OK):
```json
{
  "tenant_id": "default_tenant",
  "user_id": "550e8400-e29b-41d4-a716-446655440001",
  "correlation_id": null,
  "limit": 50,
  "algorithm": "SHA-256",
  "verified_event_count": 42,
  "verified_through_event_id": "550e8400-e29b-41d4-a716-446655440099",
  "next_cursor": "eyJjcmVhdGVkX2F0IjoiMjAyNi0wNC0xMlQxMDozMDowMFoiLCJldmVudF9pZCI6IjU1MGU4NDAwIn0="
}
```

**Business Rules**:
- Each event's `event_checksum` is verified against `SHA256(event_payload)` 
- Each event's `previous_event_checksum` is verified against the preceding event's `event_checksum`
- A broken chain returns `409 integrity_check_failed`
- Verification is paginated; call repeatedly with `next_cursor` to verify entire chain

**Error Responses**:
- `400` — `invalid_event_store_request`, `query_cursor_invalid`
- `403` — `query_scope_forbidden`
- `409` — `integrity_check_failed` (hash-chain break detected)
- `500` — `persistence_not_configured`

---

## Business Rules & Constraints

### Append-Only Invariant

- Events can never be deleted from the active store
- Events can never be modified after creation
- Archival moves events to a secondary store but preserves metadata in primary
- The `_visible_events_floor` mechanism (test utility only) advances a cursor; it does not delete events

### Idempotency

- `idempotency_key` is SHA-256 hashed with event payload to produce `idempotency_payload_fingerprint`
- Unique index on fingerprint prevents duplicate appends
- Same `idempotency_key` with different payload → `409 append_conflict`
- Same `idempotency_key` with same payload → `409 append_conflict` (use correlation_id for dedup at caller)

### Hash-Chain Integrity

Each event stores:
- `event_checksum`: SHA-256 hash of the canonical event payload
- `previous_event_checksum`: `event_checksum` of the immediately preceding event in the user's chain

Chain break detection: integrity verify endpoint re-computes and cross-checks every event in the page.

### Delegation-Aware Scope

When a `TaxAgent` or `Accountant` principal acts on behalf of a client:
- Allowed user IDs = `{principal.user_id, principal.delegation_context.principal_user_id}`
- Events for both the delegate and the principal are within query scope
- Delegation metadata (`is_delegated`, `principal_user_id`, `delegate_user_id`, `delegation_id`) recorded at append time

### Retention Policy

- Retention days configurable per policy code (default: 2555 days ≈ 7 years, KRA statutory requirement)
- `retention_expires_at` computed at append: `created_at + timedelta(days=retention_days)`
- Events only eligible for archival when `retention_expires_at <= as_of`

---

## Event Contracts

The Event Store does not emit events itself — it **is** the event store. All other services emit events **to** the Event Store.

### Persisted Event Record Schema

```json
{
  "event_id": "UUID",
  "event_type": "string (e.g., auth.login.success)",
  "action_type": "string (derived from event_type prefix)",
  "user_id": "UUID",
  "role_at_time": "string (role captured at append)",
  "trace_id": "string (SHA-256 hex or trace identifier)",
  "correlation_id": "string",
  "idempotency_key": "string",
  "is_delegated": "boolean",
  "principal_user_id": "UUID | null",
  "delegate_user_id": "UUID | null",
  "delegation_id": "UUID | null",
  "created_at": "ISO8601 UTC string",
  "previous_event_checksum": "SHA-256 hex | null (null for first event in chain)",
  "event_checksum": "SHA-256 hex",
  "retention_expires_at": "ISO8601 UTC string",
  "retention_policy_code": "string",
  "retention_days": "integer",
  "archived_at": "ISO8601 UTC string | null",
  "archival_reason_code": "string | null"
}
```

---

## Error Handling

### Error Response Format

```json
{
  "detail": {
    "error_code": "specific_error_code",
    "message": "Human-readable description",
    "reason": "machine_reason_code",
    "reason_code": "machine_reason_code",
    "trace_id": "a1b2c3d4...",
    "correlation_id": "req-20260412-001",
    "details": {}
  }
}
```

### Error Code Catalog

| Error Code | HTTP Status | Description |
|-----------|------------|-------------|
| `invalid_event_store_request` | 400 | Malformed payload, empty idempotency key, invalid cursor |
| `query_cursor_invalid` | 400 | Pagination cursor cannot be decoded |
| `query_scope_forbidden` | 403 | Requested tenant does not match principal |
| `archival_forbidden` | 403 | Event belongs to user outside delegation scope |
| `archival_not_found` | 404 | Event ID does not exist |
| `append_conflict` | 409 | Duplicate idempotency fingerprint |
| `archival_ineligible` | 409 | Event retention period not yet expired |
| `integrity_check_failed` | 409 | Hash-chain break detected in page |
| `persistence_not_configured` | 500 | Repository backend not initialized |
| `retention_policy_invalid` | 500 | Retention policy configuration error |
| `unsupported_event_store_scope` | 404 | Requested path not supported |

---

## Performance Metrics

The Event Store does not currently expose self-metrics. Consumers emit latency and failure metrics from their side.

**Indexing**: The `audit_events` table has deterministic indexes on:
- `(user_id, created_at, event_id)` — primary query pattern
- `(correlation_id, created_at, event_id)` — replay pattern
- `idempotency_payload_fingerprint` (unique) — deduplication

---

## Security & Compliance

### Authentication

All endpoints require a valid Bearer token. Role verification:
- Allowed roles: `IndividualTaxpayer`, `TaxAgent`, `Accountant`
- Delegated roles: `TaxAgent`, `Accountant` (can act on behalf of `IndividualTaxpayer`)

### Audit Data Sensitivity

- Audit events contain `user_id`, `correlation_id`, and `trace_id` — never PII like email, phone, or financial amounts
- Event payload data from callers must be pre-redacted before appending
- The Event Store does not apply redaction itself — callers are responsible

### Retention Compliance

- Default retention: 2555 days (≈ 7 years) — aligns with KRA statutory audit requirement
- Archival is a soft delete: metadata remains; active record transitions to archived store
- Integrity hash-chain enables tamper evidence for regulatory audits

### CORS

- Allowed origins: `http://127.0.0.1:5173`, `http://127.0.0.1:5174`, `http://localhost:5173`, `http://localhost:5174`

---

## Integration Capabilities

### Inbound (Producers)

Every service in the platform appends audit events:

| Producer Service | Example Event Types |
|-----------------|---------------------|
| `auth` | `auth.login.success`, `auth.session.revoked`, `auth.account_deletion.executed` |
| `document_ai` | `document.lifecycle.trash`, `document.extraction.completed`, `document.compliance_override.approved` |
| `forms` | `form.artifact.generated`, `form.version.bound`, `form.submission.closed` |
| `orchestration` | `orchestration.plan.resolved`, `orchestration.action.dispatched` |
| `reports` | `report.generated`, `report.downloaded` |
| `tax_core` | `computation.executed`, `computation.finalized` |
| `gateway` | `gateway.tool_ping` |

### Database Operations

- **Append**: `INSERT INTO audit_events` with conflict detection on fingerprint
- **Query**: `SELECT` with user scope filtering, cursor-based pagination
- **Retention**: `SELECT WHERE retention_expires_at <= as_of`
- **Archival**: `INSERT INTO archived_audit_events` + `UPDATE audit_events SET archived_at`
- **Integrity**: `SELECT` ordered by `created_at, event_id`; in-memory hash-chain verification

---

## Project Structure

```
services/event_store/app/
├── main.py         # FastAPI app factory, all route handlers, cursor encode/decode, error handling
├── config.py       # Database URL and retention policy configuration
├── models.py       # Frozen dataclasses: PersistedAuditEvent, ArchivedAuditEvent,
│                   #   AuditEventQueryPage, AuditEventIntegrityVerification
└── repository.py   # EventStoreRepository: append, query, replay, retention, archival,
                    #   integrity verification; all PostgreSQL persistence logic
```
