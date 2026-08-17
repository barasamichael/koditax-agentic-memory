# Reports Service

Policy-governed tax report generation, export, and audit packaging service for the Kodi Solutions tax compliance platform. Produces structured report artifacts (PDF, CSV, Excel) from finalized computation and form lineage, manages download capabilities via the storage service, enforces retention and expiry policies, and emits immutable audit events for every report lifecycle operation.

Current runtime note: the implemented FastAPI boundary in [services/reports/app/main.py](/c:/Users/Lenovo/kodi-backend/services/reports/app/main.py) exposes `POST /v1/reports/income-tax/artifacts`, `POST /v1/reports/health-contribution/artifacts`, metadata retrieval routes for those artifacts, `GET /v1/reports/income-tax/history`, `GET /v1/reports/income-tax/exports/{export_package_id}/metadata`, plus `GET /healthz` and `GET /readyz`. Supported generation requests are now pre-screened by governed validation before report creation; rejected requests fail closed canonically with `409 invalid_report_request` and include `context.governed_validation` in the error envelope.

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

The Reports service generates exportable tax computation and filing reports from finalized computation lineage. The boundary is tax-domain-aware, but the currently implemented downstream report generation and export workflow remains income-tax only. Reports are authoritative, signed artifacts consumed by users for KRA filing, record-keeping, and audits.

**Key capabilities:**

- **Multi-format generation**: Produces PDF, CSV, and Excel (XLSX) report artifacts from finalized computation lineage
- **Lineage-bound**: Reports are generated from a specific, immutable lineage snapshot — same lineage always produces the same report content
- **Download capability issuance**: Integrates with `storage` service to issue time-limited download URLs; expired capabilities rejected with `410`
- **Audit package**: Generates ZIP bundles of audit documentation accompanying a report
- **Access control**: Role-based authorization enforced per operation (`authz.py`)
- **Structured logging**: Every request logged with correlation_id, tenant_id, report_id, and reason_code via logging middleware
- **Retention and expiry**: Reports carry expiry metadata; download of expired reports rejected at storage layer
- **Metrics**: Lifecycle metrics emitted for generation, download, and expiry events

**Architectural role:** Consumer of `tax_core` finalized computation lineage; producer of filing-ready export artifacts. The service boundary recognizes multiple tax domains and fails closed canonically for domains without implemented report-generation lanes.

---

## Dependencies

### Python Packages

| Package | Purpose |
|---------|---------|
| `fastapi` | HTTP routing |
| `pydantic` | Schema validation |
| `reportlab` (or equivalent) | PDF rendering |
| `openpyxl` | Excel/XLSX rendering |
| `zipfile` | Audit package ZIP creation |
| `uvicorn` | ASGI runtime |

### External Services

| Service | Purpose |
|---------|---------|
| `storage` | Download capability issuance and object persistence |
| `tax_core` | Source of finalized computation lineage |
| `event_store` | Audit event emission |

---

## Installation

```bash
cd services/reports

pip install -r requirements.txt

alembic upgrade head

uvicorn services.reports.app.main:app --host 0.0.0.0 --port 8007 --reload
```

---

## API Endpoints

### Endpoint Summary

| Method | Path | Auth Required | Summary |
|--------|------|---------------|---------|
| POST | `/v1/reports/income-tax/generate` | Bearer | Generate a report from finalized computation lineage |
| GET | `/v1/reports/income-tax/artifacts/{report_id}` | Bearer | Retrieve report artifact metadata |
| GET | `/v1/reports/income-tax/artifacts/{report_id}/metadata` | Bearer | Retrieve report metadata (alias) |
| POST | `/v1/reports/income-tax/artifacts/{report_id}/download` | Bearer | Issue download capability for report artifact |
| POST | `/v1/reports/income-tax/artifacts/{report_id}/audit-package` | Bearer | Generate audit documentation ZIP |

---

### Report Generation

#### POST /v1/reports/income-tax/generate

**Summary**: Generate a report artifact from a finalized income-tax computation lineage

**Authentication**: Required (Bearer token)

**Headers**:
- `X-Tenant-ID` (`string`, optional): Tenant context for structured logging

**Request Body**:
- **computation_id** (`string`, UUID, required): Finalized computation to report from
- **report_format** (`string`, required): Output format
  - Enum: `pdf`, `csv`, `excel`
- **tax_year** (`integer`, required): Tax year of the computation
- **tenant_id** (`string`, required): Tenant scope
- **user_id** (`string`, UUID, required): Requesting user
- **lineage_ref** (`string`, required): Immutable lineage reference from finalized computation
- **report_type** (`string`, optional): Specific report type
  - Enum: `computation_summary`, `income_tax_return`, `audit_trail`
  - Default: `computation_summary`

**Response** (201 Created):
```json
{
  "status": "ok",
  "report_id": "550e8400-e29b-41d4-a716-446655440020",
  "computation_id": "550e8400-e29b-41d4-a716-446655440000",
  "lineage_ref": "lin-20260412-001",
  "report_format": "pdf",
  "report_type": "computation_summary",
  "tax_year": 2023,
  "generated_at": "2026-04-12T10:50:00Z",
  "expires_at": "2027-04-12T10:50:00Z",
  "storage_key": "default_tenant/reports/550e8400.pdf",
  "correlation_id": "req-20260412-001"
}
```

**Business Rules**:
- Computation must be `finalized`; draft computations rejected
- Report is deterministic: same `computation_id` + `report_format` + `lineage_ref` → same content
- Idempotent: duplicate generation request for same lineage returns existing report
- Retention: default 1 year from `generated_at`; governed by Phase 9 retention policy

**Error Responses**:
- `400` — `invalid_report_request` (missing/invalid fields)
- `403` — `report_access_forbidden` (role not permitted)
- `404` — `report_not_found` (computation not found)
- `409` — Report already generated for this lineage
- `422` — `computation_not_finalized`
- `503` — Storage service unavailable

**Example Request**:
```bash
curl -X POST https://api.kodi.solutions/v1/reports/income-tax/generate \
  -H "Authorization: Bearer eyJ0eXAiOiJKV1Qi..." \
  -H "Content-Type: application/json" \
  -H "X-Correlation-ID: rep-20260412-001" \
  -H "X-Tenant-ID: default_tenant" \
  -d '{
    "computation_id": "550e8400-e29b-41d4-a716-446655440000",
    "report_format": "pdf",
    "tax_year": 2023,
    "tenant_id": "default_tenant",
    "user_id": "550e8400-e29b-41d4-a716-446655440001",
    "lineage_ref": "lin-20260412-001",
    "report_type": "computation_summary"
  }'
```

---

### Report Retrieval

#### GET /v1/reports/income-tax/artifacts/{report_id}

**Summary**: Retrieve report artifact metadata

**Authentication**: Required (Bearer token)

**Path Parameters**:
- **report_id** (`string`, UUID, required)

**Headers**:
- `X-Tenant-ID` (`string`, optional)

**Response** (200 OK):
```json
{
  "status": "ok",
  "report_id": "550e8400-e29b-41d4-a716-446655440020",
  "computation_id": "550e8400-e29b-41d4-a716-446655440000",
  "report_format": "pdf",
  "report_type": "computation_summary",
  "tax_year": 2023,
  "generated_at": "2026-04-12T10:50:00Z",
  "expires_at": "2027-04-12T10:50:00Z",
  "storage_key": "default_tenant/reports/550e8400.pdf",
  "lineage_ref": "lin-20260412-001",
  "state": "active"
}
```

**Error Responses**:
- `403` — `report_access_forbidden`
- `404` — `report_not_found`

---

### Download Capability

#### POST /v1/reports/income-tax/artifacts/{report_id}/download

**Summary**: Issue a time-limited download capability for a report artifact via the storage service

**Authentication**: Required (Bearer token)

**Path Parameters**:
- **report_id** (`string`, UUID, required)

**Response** (201 Created):
```json
{
  "status": "ok",
  "report_id": "550e8400-e29b-41d4-a716-446655440020",
  "capability_id": "b2c3d4e5f6a1...",
  "download_url": "https://storage.kodi.solutions/reports/...",
  "expires_at": "2026-04-12T11:05:00Z",
  "method": "GET",
  "correlation_id": "req-20260412-001"
}
```

**Business Rules**:
- Report must be within retention window (`expires_at > now()`)
- Expired reports: `410` — `storage_capability_expired`; metrics emitted via `REPORTS_DOWNLOAD_EXPIRY_REJECT_TOTAL`
- Download capability TTL: governed by `storage` service capability configuration
- Structured log emitted for both success and failure via `emit_report_structured_log`

**Error Responses**:
- `403` — `report_access_forbidden`
- `404` — `report_not_found`, `storage_capability_not_found`
- `410` — `storage_capability_expired` (report past retention)
- `503` — Storage service unavailable

---

### Audit Package

#### POST /v1/reports/income-tax/artifacts/{report_id}/audit-package

**Summary**: Generate a ZIP bundle containing the report artifact plus associated audit documentation

**Authentication**: Required (Bearer token)

**Response** (201 Created):
```json
{
  "status": "ok",
  "report_id": "550e8400-e29b-41d4-a716-446655440020",
  "audit_package_id": "pkg-20260412-001",
  "package_url": "https://storage.kodi.solutions/audit-packages/...",
  "expires_at": "2026-04-12T11:05:00Z",
  "contents": [
    "computation_summary.pdf",
    "audit_trail.json",
    "lineage_manifest.json"
  ]
}
```

**Audit Package Contents**:
- Primary report artifact
- Audit trail JSON with full event lineage
- Lineage manifest with computation_id, lineage_ref, report_id, generation timestamps
- Integrity checksums for each included file

---

## Business Rules & Constraints

### Report Lifecycle

```
generation_requested ──▶ generating ──▶ active ──expires──▶ expired
                                           │
                                     retention_eligible──▶ purged
```

### Lineage Contract (Phase 9)

Reports are bound to a lineage snapshot at generation time:
- `lineage_ref` uniquely identifies the computation snapshot
- Same lineage always produces bit-identical report content
- Lineage cannot be modified after report generation — immutable
- `computation_id` and `lineage_ref` together constitute the primary lineage key

### Retention Policy (Phase 9)

| Report Type | Retention Period | Policy Code |
|-------------|-----------------|-------------|
| `computation_summary` | 365 days | `standard_report_retention` |
| `income_tax_return` | 2555 days (7 years) | `statutory_report_retention` |
| `audit_trail` | 2555 days (7 years) | `statutory_report_retention` |

Expired reports:
- Cannot be downloaded (`410 storage_capability_expired`)
- Eligible for purge from storage
- Audit events remain in event_store per statutory retention

### Access Control (authz.py)

| Operation | Required Role | Ownership Check |
|-----------|--------------|-----------------|
| Generate report | `IndividualTaxpayer`, `TaxAgent`, `Accountant` | Owner or delegated |
| Retrieve metadata | `IndividualTaxpayer`, `TaxAgent`, `Accountant` | Owner or delegated |
| Download | `IndividualTaxpayer`, `TaxAgent`, `Accountant` | Owner or delegated |
| Audit package | `TaxAgent`, `Accountant`, `Administrator` | Extended access |

### Structured Logging Middleware

Every HTTP request is wrapped by `_reports_structured_logging_middleware`:
- Extracts `correlation_id` from `X-Correlation-ID` header
- Extracts `tenant_id` from `X-Tenant-ID` header
- Infers `report_id` from URL path pattern `^/v1/reports/income-tax/artifacts/([0-9a-f-]{36})`
- Logs `reports_request_succeeded` (< 400) or `reports_request_failed` (≥ 400)
- Extracts `reason_code` from response JSON `detail.reason_code`

---

## Event Contracts

### Events Emitted (via audit.py ReportsAuditEmitter)

| Event Type | Trigger | Key Fields |
|-----------|---------|-----------|
| `report.generated` | Report artifact created | `report_id`, `computation_id`, `report_format`, `lineage_ref` |
| `report.generation_failed` | Generation failure | `reason_code`, `computation_id` |
| `report.downloaded` | Download capability issued | `report_id`, `capability_id` |
| `report.download_expired` | Download rejected due to expiry | `report_id`, `expires_at` |
| `report.audit_package_generated` | Audit ZIP created | `report_id`, `audit_package_id` |

### Audit Evidence Schema

```json
{
  "audit_evidence_id": "SHA-256 of event payload",
  "event_type": "report.generated",
  "report_id": "550e8400-e29b-41d4-a716-446655440020",
  "computation_id": "550e8400-e29b-41d4-a716-446655440000",
  "tenant_id": "default_tenant",
  "user_id": "550e8400-e29b-41d4-a716-446655440001",
  "occurred_at": "2026-04-12T10:50:00Z",
  "correlation_id": "req-20260412-001"
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
    "correlation_id": "req-20260412-001",
    "context": {}
  }
}
```

All reason codes are validated against `REPORTS_REASON_CODES`; unknown codes are normalized to `reports_contract_violation`.

### Error Code Catalog

| Error Code | HTTP Status | Description |
|-----------|------------|-------------|
| `invalid_report_request` | 400 | Malformed payload or missing required field |
| `computation_not_finalized` | 422 | Source computation not in finalized state |
| `report_access_forbidden` | 403 | Role not permitted for operation |
| `report_not_found` | 404 | Report ID does not exist |
| `storage_capability_not_found` | 404 | Object not found in storage |
| `storage_capability_expired` | 410 | Report past retention window |
| `reports_contract_violation` | 500 | Internal invariant broken |
| `storage_unavailable` | 503 | Storage service not reachable |

---

## Performance Metrics

### Metrics Emitted

| Metric Name | Type | Dimensions |
|-------------|------|-----------|
| `reports_generation_requests_total` | Counter | `report_format`, `status` |
| `reports_generation_failures_total` | Counter | `report_format`, `reason_code` |
| `reports_generation_latency_ms` | Histogram | `report_format` |
| `reports_download_issued_total` | Counter | `report_format` |
| `REPORTS_DOWNLOAD_EXPIRY_REJECT_TOTAL` | Counter | `event_type`, `reason_code` |

The `REPORTS_DOWNLOAD_EXPIRY_REJECT_TOTAL` counter is incremented by the storage service directly when a download capability resolution fails due to expiry, via the shared `get_default_reports_metrics_emitter()`.

---

## Security & Compliance

### Logging Redaction (logging_policy.py)

Sensitive fields redacted from structured logs:
- `authorization`, `token`, `secret`, `api_key`
- Field values matching Bearer token patterns

### Retention Compliance

- `income_tax_return` and `audit_trail` reports retained for 7 years (KRA statutory requirement)
- `computation_summary` reports retained for 1 year
- Expired report metadata preserved in the repository even after storage purge

---

## Integration Capabilities

### Service Dependencies

| Service | Operation | Direction |
|---------|-----------|-----------|
| `tax_core` | Read finalized computation lineage | Outbound (read) |
| `storage` | Upload report artifact; issue download capability | Outbound |
| `event_store` | Audit evidence persistence | Outbound |

---

## Project Structure

```
services/reports/app/
├── main.py              # FastAPI app factory, structured logging middleware, error handlers
├── config.py            # REPORTS_SERVICE_NAME and version constants; download TTL settings
├── routes.py            # ROUTER with all report route handlers
├── models.py            # Request/response Pydantic models for report generation and metadata
├── repository.py        # Report record persistence (get, create, list, update state)
├── generation.py        # Report generation flow: lineage consumption → renderer dispatch → storage
├── audit.py             # ReportsAuditEmitter: append-only audit event builder and emitter
├── audit_package.py     # ZIP audit package builder (report + audit trail + lineage manifest)
├── authz.py             # Access context and role-based authorization helpers
├── csv_renderer.py      # CSV format renderer for computation report artifacts
├── excel_renderer.py    # Excel/XLSX format renderer
├── pdf_renderer.py      # PDF format renderer
├── download_links.py    # Storage download capability integration with expiry handling
├── errors.py            # REPORTS_REASON_CODES, canonical error envelope builder
├── logging_policy.py    # Structured log emission with redaction
└── metrics.py           # Metric emitter: generation, download, expiry counters and histograms
```
