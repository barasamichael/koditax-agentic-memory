# Validation Service

Deterministic standalone validation service for the supported 4A/4B backend. It validates governed structured return payloads, returns machine-consumable findings and rule results, records validation executions through a narrow replay-safe store, and now also supplies the shared governed validation envelopes consumed by supported forms, reports, and orchestration workflows.

## Description

The runtime in [services/validation/app/main.py](/c:/Users/Lenovo/kodi-backend/services/validation/app/main.py) exposes:

- `GET /healthz`
- `GET /readyz`
- `POST /validate/return`
- fail-closed scoped guard under `/v1/validation/{scope}/...`

`/validate/return` is an internal governed service boundary. Frontend clients
must not call it directly. Supported callers in the current backend are other
governed services such as forms, reports, and orchestration.

Current supported runtime scope is intentionally narrow and limited to supported 4A/4B domains:

- `income_tax`
  - supported modes: `draft`, `pre_submission`, `post_submission_integrity`
  - supported governed field checks:
    - `kra_pin` presence and format
    - `period_start` / `period_end` ISO-date validity and range consistency
    - `amount_total` numeric precision up to 2 decimal places
- `health_contribution`
  - supported modes: `draft`, `pre_submission`
  - `post_submission_integrity` remains fail-closed for this domain
  - supported governed field checks:
    - `regime_identifier`
    - `resolved_domain_path`
    - `historical_version_id`
    - `primary_effective_date`
    - `contribution_basis_kes`
    - `total_contribution_kes`
  - deterministic rule bundles:
    - supported lane detection
    - version binding consistency
    - effective-window consistency
    - contribution summary consistency

Unsupported validation domains remain fail-closed in this slice. This Phase
16.4 closeout does not claim 4C/4D/4E validation breadth.

## Dependencies

Python packages imported by the runtime:

- `fastapi`
- `pydantic`
- `psycopg`

Shared runtime dependencies:

- `shared/tracing/correlation.py`
- `shared/determinism/input_hash.py`

Persistence:

- development/test mode uses bounded in-memory execution storage
- production mode requires PostgreSQL and the `validation_executions` table created by [0025_validation_execution_baseline.sql](/c:/Users/Lenovo/kodi-backend/database/migrations/0025_validation_execution_baseline.sql)

Environment variables read by [services/validation/app/config.py](/c:/Users/Lenovo/kodi-backend/services/validation/app/config.py):

- `VALIDATION_RUNTIME_MODE=development|production`
- `DATABASE_URL`
- `VALIDATION_INTERNAL_API_KEY`

## API Endpoints

### `GET /healthz`

Returns:

- `status`
- `service`
- `version`
- `runtime_mode`
- `persistence_mode`
- `correlation_id`
- `trace_id`

`persistence_mode` is one of:

- `in_memory` in development/test mode
- `persistent` when the production database baseline is available
- `unavailable` when production readiness detects that persistence is required but not usable

### `GET /readyz`

Returns the same envelope family as `healthz`.

Runtime behavior:

- development/test mode returns `200 ready`
- production mode returns `503 validation_persistence_unavailable` if the required persistent store is unavailable

### `POST /validate/return`

Summary: validate one structured governed return payload and return deterministic findings.

Request fields:

- `return_id`
- `tax_domain`
- `mode`
- `fields`

Current request rules:

- `tax_domain` must be `income_tax` or `health_contribution`
- `mode` must be supported for the requested domain
- `fields` must be an object

Domain-specific mode rules:

- `income_tax`: `draft | pre_submission | post_submission_integrity`
- `health_contribution`: `draft | pre_submission`

Response shape:

- top level:
  - `status`
  - `service`
  - `correlation_id`
  - `trace_id`
  - `audit_evidence`
- `result`:
  - `validation_id`
  - `return_id`
  - `tax_domain`
  - `mode`
  - `validation_status`
  - `summary`
  - `issues`
  - `rule_results`

Validation status:

- `rejected` if any issue has severity `ERROR`
- `accepted` otherwise

Internal-boundary behavior:

- development/test mode allows direct local invocation without an internal key
- production mode requires `X-Validation-Internal-Key` to match
  `VALIDATION_INTERNAL_API_KEY`
- if the production boundary secret is missing, the runtime fails closed with
  `503 validation_internal_boundary_unavailable`
- if the production boundary secret is wrong or omitted, the runtime fails
  closed with `403 validation_internal_boundary_forbidden`

Rule result ordering is deterministic:

For `income_tax`:

1. `kra_pin_presence`
2. `kra_pin_format`
3. `period_range_consistency`
4. `amount_total_precision`

For `health_contribution`:

1. `health_contribution_supported_lane_detected`
2. `health_contribution_version_binding_consistent`
3. `health_contribution_effective_window_consistent`
4. `health_contribution_summary_consistent`

Issue codes currently emitted:

- `missing_kra_pin`
- `invalid_kra_pin_format`
- `invalid_date_format`
- `invalid_date_range`
- `invalid_amount_precision`
- `missing_health_regime_identifier`
- `missing_health_domain_path`
- `missing_health_historical_version_id`
- `unsupported_health_contribution_lane`
- `health_contribution_version_window_unsupported`
- `health_contribution_version_binding_inconsistent`
- `invalid_health_primary_effective_date`
- `health_contribution_effective_window_inconsistent`
- `missing_health_contribution_basis`
- `missing_health_total_contribution`
- `invalid_health_amount_precision`
- `health_contribution_summary_inconsistent`
- `validation_passed`

Example success response:

```json
{
  "status": "ok",
  "service": "validation",
  "correlation_id": "val-corr-001",
  "trace_id": "val-corr-001",
  "result": {
    "validation_id": "0d8b6901-8fea-5117-936c-e1f6c6a3ecaf",
    "return_id": "RET-001",
    "tax_domain": "income_tax",
    "mode": "pre_submission",
    "validation_status": "accepted",
    "summary": {
      "error_count": 0,
      "warning_count": 0,
      "info_count": 1,
      "total_issues": 1
    },
    "issues": [
      {
        "severity": "INFO",
        "code": "validation_passed",
        "message": "Validation checks passed.",
        "field": null
      }
    ],
    "rule_results": [
      {
        "rule_code": "kra_pin_presence",
        "outcome": "passed",
        "severity": "INFO",
        "message": "KRA PIN presence check passed.",
        "field": "kra_pin",
        "linked_issue_codes": []
      }
    ]
  }
}
```

## Failure Handling

Canonical request failures return:

- `detail.error_code`
- `detail.message`
- `detail.reason`
- `detail.reason_code`
- `detail.correlation_id`
- `detail.trace_id`

Canonical failure codes in this slice:

- `invalid_validation_request`
- `unsupported_validation_scope`
- `validation_persistence_unavailable`
- `validation_internal_boundary_unavailable`
- `validation_internal_boundary_forbidden`

HTTP behavior:

- `400` malformed request payload or invalid mode/field shape
- `403` unsupported direct/public boundary usage in production
- `404` unsupported validation domain or guarded scoped path
- `503` required production persistence unavailable
- `200` governed validation completed, even when `validation_status` is `rejected`

## Security and Compliance

- validation outcomes are deterministic and derived only from governed rule evaluation
- no LLM or probabilistic scoring is used
- correlation and trace identifiers are always included in success and error envelopes
- production persistence is fail-closed; if execution records cannot be stored, validation does not silently continue
- validation execution and validation rejection paths emit deterministic
  machine-consumable audit evidence
- production mode treats validation as an internal-only boundary and rejects
  unsupported direct/public callers canonically

## Persistence Baseline

Phase 16.1 adds a narrow execution-record baseline:

- table: `validation_executions`
- deterministic `validation_id` derived from canonical request fingerprint
- stored fields include request fingerprint, validation status, issues, rule results, correlation ID, and trace ID

Phase 16.4 completes the validation closeout baseline with deterministic
audit-event capture for:

- successful validation execution
- rejected validation execution
- unsupported-domain rejection
- invalid-request rejection
- persistence-unavailable failure

Current audit storage is append-only in behavior and correlation-linked. The
closeout claim for validation is limited to the supported 4A/4B backend and
does not imply broader 4C/4D/4E validation scope.

## Project Structure

```text
services/validation/app/
|- main.py                - FastAPI runtime and canonical error handling
|- config.py              - runtime mode and database configuration
|- audit_events.py        - deterministic validation audit-event construction
|- validation_rules.py    - deterministic supported-scope rule evaluation
|- validation_outcomes.py - canonical issue, rule-result, and summary models
`- validation_store.py    - in-memory and PostgreSQL execution-record storage
```
