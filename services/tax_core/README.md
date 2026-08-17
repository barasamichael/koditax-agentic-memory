# Tax Core Service

Tax Core is the deterministic computation service in `services/tax_core/app/`. The current FastAPI app exposes four POST endpoints from `main.py`:

- `/computations/execute`
- `/computations/replay`
- `/computations/finalize`
- `/computations/validate`

The implementation currently contains five governed income-tax execution bindings backed by modules in `services/tax_core/app/rules/income_tax/`, plus additional 2025 stub-oriented rule bindings in `rule_binding.py`.

## Verified Source Set

This README was written from these files:

- `services/tax_core/app/main.py`
- `services/tax_core/app/config.py`
- `services/tax_core/app/engine/execution_contract.py`
- `services/tax_core/app/engine/executor.py`
- `services/tax_core/app/engine/rule_binding.py`
- `services/tax_core/app/engine/replay.py`
- `services/tax_core/app/engine/finalization.py`
- `services/tax_core/app/engine/validation.py`
- `services/tax_core/app/persistence/materialization.py`
- `services/tax_core/app/rules/income_tax/*.py`
- `contracts/openapi/tax_core.yaml`
- `contracts/tools/schemas/income_tax_execution_request.schema.json`
- `contracts/tools/schemas/income_tax_result_payload.schema.json`
- `contracts/tools/schemas/income_tax_v1_execution_request.schema.json`
- `contracts/tools/schemas/income_tax_v1_result_payload.schema.json`
- `database/migrations/*.sql`
- `tests/test_tax_core_*.py`
- `tests/test_income_tax_*.py`
- `tests/test_input_hash_canonicalization.py`
- `docs/phase-4/income-tax/*.md`
- `eval/golden/tax_core/*.json`
- `shared/errors/codes.py`

`database/models/*.py` was not used.

## Runtime Dependencies

Third-party packages imported by the service code:

- `fastapi`
- `pydantic`
- `psycopg`

Internal shared modules used directly by this service:

- `shared.authz.rbac`
- `shared.determinism.input_hash`
- `shared.errors.envelope`
- `shared.idempotency.idempotency`
- `shared.tracing.correlation`

Environment variables loaded by `services/tax_core/app/config.py`:

| Variable | Required | Type | Default | Notes |
| --- | --- | --- | --- | --- |
| `DATABASE_URL` | yes | string | none | Required for persistence-backed execute/replay/finalize/validate flows. |
| `TAX_CORE_RETENTION_DAYS` | no | positive integer | `365` | Used for computation retention and default audit-event retention in tax-core flows. |
| `TAX_CORE_COMPLIANCE_LOCK_DAYS` | no | positive integer | `30` | Used when inserting `computations.compliance_lock_until`. |

Service-local installation commands are not defined in this directory. `services/tax_core/Dockerfile` is empty. The ASGI entrypoint is `services.tax_core.app.main:app`.

## HTTP Surface

### Auth and Request Context

The app has no router prefix. All paths below are exact.

Authentication and request metadata behavior in `main.py`:

- `X-Auth-Context` is the preferred header.
- `Authorization: Bearer <uuid>:<role>` is accepted only as a fallback when `X-Auth-Context` is absent.
- Allowed roles are `IndividualTaxpayer`, `TaxAgent`, and `Accountant`.
- Delegation is disabled by `build_authorized_principal_dependency(..., allow_delegation=False)`.
- `X-Auth-Context` tenant must match the default tenant enforced by `shared.authz.rbac` (`default_tenant`).
- `Idempotency-Key` is required on all four endpoints.
- `Idempotency-Key` is trimmed, must not be empty, and must be at most 128 characters.
- `X-Correlation-ID` is optional; `CorrelationIdMiddleware` generates and echoes one when absent or invalid.

Auth-context failures that can surface through the shared dependency used by tax-core:

- `auth_context_missing`
- `auth_context_malformed`
- `auth_context_invalid_claim`
- `auth_context_invalid_claim_type`
- `auth_context_invalid_role`
- `auth_context_invalid_session_id`
- `auth_context_missing_required_claim`
- `auth_context_invalid_delegation_context`
- `unsupported_auth_context_scope`
- `authorization_tenant_forbidden`
- `delegation_tenant_mismatch`
- `authorization_delegation_forbidden`
- `authorization_role_forbidden`

Contract note:

- `contracts/openapi/tax_core.yaml` documents `Authorization` on every endpoint.
- `services/tax_core/app/main.py` also supports `X-Auth-Context` and prefers it over `Authorization`.

### POST /computations/execute

Summary from `main.py`: `Execute one deterministic computation request.`

Request model from `ComputationExecutionRequest`:

| Field | Type | Required | Constraints |
| --- | --- | --- | --- |
| `tax_type` | string | yes | no extra validation at boundary |
| `regime_type` | string | yes | no extra validation at boundary |
| `regime_identifier` | string or null | no | omitted defaults to `null` |
| `tax_year` | integer | yes | `2000 <= tax_year <= 2100` |
| `rule_version` | string | yes | must be non-empty after stripping |
| `input_payload` | object | yes | top-level request model forbids unknown top-level fields, but `input_payload` itself is generic at this boundary |

Success response model from `MaterializedComputationExecutionResult`:

| Field | Type |
| --- | --- |
| `status` | `"ok"` |
| `computation_id` | UUID |
| `computation_result_id` | UUID |
| `audit_event_id` | UUID |
| `idempotency_key` | string |
| `correlation_id` | string |
| `tax_type` | string |
| `regime_type` | string |
| `tax_year` | integer |
| `rule_version` | string |
| `input_hash` | string |
| `result_payload` | object |

Execution behavior:

- `execute_computation()` canonicalizes `input_payload`, computes `input_hash`, binds a rule, runs the rule executor, and canonicalizes the result payload.
- `materialize_execution_result()` inserts one `computations` row, one `computation_results` row, and one `audit_events` row in one transaction.
- Reuse of an existing `Idempotency-Key` returns the existing materialized response only when both `user_id` and `input_hash` match.

Additional execute-path error codes after shared auth and idempotency validation:

- `400 invalid_computation_request`
- `400 invalid_rule_binding`
- `409 idempotency_key_conflict`
- `500 computation_materialization_failed`

`computation_materialization_failed` wraps `MaterializationError` reasons from `materialization.py`, including:

- `invalid_persistence_configuration`
- `invalid_retention_days`
- `invalid_compliance_lock_days`
- `database_error`
- `unexpected_materialization_error`

### POST /computations/replay

Summary from `main.py`: `Replay one persisted computation deterministically and verify stored output consistency.`

Request model from `ReplayVerificationRequest`:

| Field | Type | Required |
| --- | --- | --- |
| `computation_id` | UUID | yes |

Success response model from `ReplayVerificationResult`:

| Field | Type |
| --- | --- |
| `status` | `"ok"` |
| `verification_status` | `"matched"` |
| `computation_id` | UUID |
| `replay_audit_event_id` | UUID |
| `correlation_id` | string |
| `idempotency_key` | string |
| `tax_type` | string |
| `regime_type` | string |
| `tax_year` | integer |
| `rule_version` | string |
| `input_hash` | string |

Replay behavior:

- Loads the persisted computation and stored result payload from the database.
- Rebuilds a `ComputationExecutionRequest` from persisted fields.
- Re-runs `execute_computation()` through the same execution path as the original request.
- Fails with `404 computation_not_found` when the stored `user_id` does not match the caller.
- Writes a replay audit event for both match and mismatch outcomes.
- Compares canonical JSON for the stored public result payload and the replay result payload.

Replay-path error codes after shared auth and idempotency validation:

- `400 invalid_replay_request`
- `404 computation_not_found`
- `409 replay_input_hash_mismatch`
- `409 replay_result_mismatch`
- `500 invalid_persistence_configuration`
- `500 replay_verification_failed`

### POST /computations/finalize

Summary from `main.py`: `Finalize one persisted computation with deterministic idempotent behavior.`

Request model from `ComputationFinalizationRequest`:

| Field | Type | Required |
| --- | --- | --- |
| `computation_id` | UUID | yes |

Success response model from `ComputationFinalizationResult`:

| Field | Type |
| --- | --- |
| `status` | `"ok"` |
| `finalization_status` | `"finalized"` |
| `computation_id` | UUID |
| `finalized_at` | datetime |
| `finalized_audit_event_id` | UUID |
| `correlation_id` | string |
| `idempotency_key` | string |

Finalization behavior:

- Locks the computation row with `FOR UPDATE`.
- Returns the existing finalized state if `finalized_at` is already populated.
- Inserts a `computation.finalized` audit event and then updates `computations.finalized_at` and `computations.finalized_audit_event_id`.
- There is no `status` column and no draft/executed state machine in the database schema.

Finalization-path error codes after shared auth and idempotency validation:

- `400 invalid_finalization_request`
- `404 computation_not_found`
- `500 invalid_retention_days`
- `500 invalid_persistence_configuration`
- `500 invalid_finalization_state`
- `500 finalization_failed`

### POST /computations/validate

Summary from `main.py`: `Persist deterministic validation findings for one persisted computation.`

Request model from `ComputationValidationRequest`:

| Field | Type | Required |
| --- | --- | --- |
| `computation_id` | UUID | yes |

Success response model from `ComputationValidationResult`:

| Field | Type |
| --- | --- |
| `status` | `"ok"` |
| `validation_id` | UUID |
| `computation_id` | UUID |
| `validation_context` | string |
| `correlation_id` | string |
| `idempotency_key` | string |
| `tax_year` | integer |
| `rule_version` | string |
| `findings` | array of `ValidationFinding` |

Validation behavior:

- `DEFAULT_VALIDATION_CONTEXT` is `deterministic_post_computation_validation`.
- Validation loads a persisted computation and inserts a `validations` row plus a `computation.validated` audit event.
- Validation checks do not require the computation to be finalized.
- The first finding is always `computation_lineage_bound`.
- Additional findings come from `services/tax_core/app/rules/income_tax/validation_catalog.py`.

Validation-path error codes after shared auth and idempotency validation:

- `400 invalid_validation_request`
- `404 computation_not_found`
- `500 invalid_persistence_configuration`
- `500 validation_failed`

## Database Schema Used by Tax Core

The service directly uses `computations`, `computation_results`, `validations`, `audit_events`, and the `users` and `sessions` tables referenced by foreign keys and lineage triggers.

### computations

Created in `0001_core_schema_baseline.sql`, extended in `0013_computation_finalization_immutability.sql`.

| Column | Type | Constraints / notes |
| --- | --- | --- |
| `id` | UUID | primary key, default `gen_random_uuid()` |
| `user_id` | UUID | not null, FK to `users(id)` |
| `session_id` | UUID | nullable, FK to `sessions(id)` |
| `tax_type` | TEXT | not null |
| `regime_type` | TEXT | not null, check: `income_tax`, `health_tax`, `vat`, `other` |
| `regime_identifier` | TEXT | nullable |
| `tax_year` | INTEGER | not null, check `BETWEEN 2000 AND 2100` |
| `rule_version` | TEXT | not null |
| `input_hash` | TEXT | not null |
| `idempotency_key` | TEXT | not null, unique |
| `correlation_id` | TEXT | not null |
| `created_at` | TIMESTAMPTZ | not null, default `now()`, check not future |
| `retention_expires_at` | TIMESTAMPTZ | not null |
| `compliance_lock_until` | TIMESTAMPTZ | not null |
| `finalized_at` | TIMESTAMPTZ | nullable |
| `finalized_audit_event_id` | UUID | nullable, FK to `audit_events(id)` |

Additional computation constraints and triggers:

- `chk_computations_health_tax_regime_identifier`: `health_tax` requires non-null `regime_identifier`.
- `chk_computations_finalization_pair_consistency`: `finalized_at` and `finalized_audit_event_id` must both be null or both be set.
- `trg_computations_enforce_session_user_lineage`: `session_id`, when present, must belong to the same `user_id`.
- `trg_computations_enforce_finalization_immutability`: finalization cannot be reversed, and finalized fields become immutable.
- `fn_computations_enforce_retention_lock`: updates or deletes are blocked while `compliance_lock_until > now()` if protected fields change.

Indexes relevant to this service:

- `idx_computations_user_id`
- `idx_computations_session_id`
- `idx_computations_tax_year`

### computation_results

Created in `0001_core_schema_baseline.sql`, expanded in `0005_referential_ownership_enforcement.sql`.

| Column | Type | Constraints / notes |
| --- | --- | --- |
| `computation_id` | UUID | primary key, FK to `computations(id)` |
| `result_payload` | JSONB | not null |
| `created_at` | TIMESTAMPTZ | not null, default `now()`, check not future |
| `user_id` | UUID | not null after `0005`, FK to `users(id)` |

Tax-core-specific behavior:

- `materialize_execution_result()` stores the public result payload plus an internal replay context key: `_kodi_replay_context.normalized_input`.
- API responses strip `_kodi_replay_context` before returning `result_payload`.
- `trg_computation_results_enforce_computation_user_lineage` keeps `user_id` aligned with the owning computation.
- `trg_computation_results_prevent_mutation_if_finalized` blocks update/delete after computation finalization.

Indexes:

- `idx_computation_results_user_id`

### validations

Created in `0001_core_schema_baseline.sql`, expanded in `0005_referential_ownership_enforcement.sql`.

| Column | Type | Constraints / notes |
| --- | --- | --- |
| `id` | UUID | primary key, default `gen_random_uuid()` |
| `computation_id` | UUID | not null, FK to `computations(id)` |
| `validation_context` | TEXT | not null |
| `findings` | JSONB | not null |
| `validated_at` | TIMESTAMPTZ | not null, default `now()` |
| `created_at` | TIMESTAMPTZ | not null, default `now()`, check not future |
| `user_id` | UUID | not null after `0005`, FK to `users(id)` |

Tax-core-specific behavior:

- `insert_validation_row()` stores a JSON object with `tax_year`, `rule_version`, `input_hash`, and `findings`.
- `trg_validations_enforce_computation_user_lineage` keeps `user_id` aligned with the owning computation.
- `trg_validations_prevent_mutation_if_finalized` blocks update/delete after computation finalization.

Indexes:

- `idx_validations_computation_id`
- `idx_validations_user_id`

### audit_events

Created in `0001_core_schema_baseline.sql`, extended in `0004`, `0006`, `0011`, `0014`, and `0015`.

| Column | Type | Constraints / notes |
| --- | --- | --- |
| `id` | UUID | primary key, default `gen_random_uuid()` |
| `user_id` | UUID | not null, FK to `users(id)` |
| `role_at_time` | TEXT | not null |
| `event_type` | TEXT | not null |
| `resource_type` | TEXT | not null |
| `resource_id` | UUID | nullable |
| `correlation_id` | TEXT | not null |
| `request_id` | TEXT | nullable |
| `idempotency_key` | TEXT | nullable, unique when non-null via partial index |
| `details` | JSONB | not null, default `{}` |
| `previous_event_hash` | TEXT | nullable, enforced by hash-chain rules |
| `event_hash` | TEXT | not null, DB trigger computes it |
| `created_at` | TIMESTAMPTZ | not null, default `now()`, check not future |
| `event_timestamp` | TIMESTAMPTZ | not null, default `now()`, check not future |
| `retention_expires_at` | TIMESTAMPTZ | not null |
| `retention_policy_code` | TEXT | not null, default `event_store_default_retention` |
| `retention_days` | INTEGER | not null, default `3650`, check `> 0` |
| `idempotency_payload_fingerprint` | TEXT | nullable |

Audit invariants used by tax-core:

- `audit_events` is append-only: update and delete triggers both raise `audit_events is append-only`.
- `trg_audit_events_enforce_temporal_monotonicity` blocks event timestamp regression per `(user_id, resource_type, resource_id)`.
- `trg_audit_events_enforce_hash_chain` computes `event_hash` and enforces `previous_event_hash`.
- Hash-chain lookup index: `idx_audit_events_chain_lookup`.

Tax-core event types written by `materialization.py`:

- `computation.executed`
- `computation.validated`
- `computation.finalized`
- `computation.replay_verified`
- `computation.replay_mismatch`

## Computation Lifecycle Implemented in Code

There is no `status` column on `computations`. The lifecycle is represented by inserted rows and finalization fields:

1. Execute
   - inserts `computations`
   - inserts `computation_results`
   - inserts `audit_events` with `event_type = computation.executed`
2. Replay
   - reads persisted computation and result data
   - inserts `audit_events` with `event_type = computation.replay_verified` or `computation.replay_mismatch`
3. Finalize
   - inserts `audit_events` with `event_type = computation.finalized`
   - sets `computations.finalized_at`
   - sets `computations.finalized_audit_event_id`
4. Validate
   - inserts `validations`
   - inserts `audit_events` with `event_type = computation.validated`

Important implementation facts:

- Replay does not create a new computation row.
- Validation does not require finalization.
- Finalization is idempotent.
- Finalized computations make `computation_results` and `validations` immutable at the database level.

## Rule Binding

`services/tax_core/app/engine/executor.py` derives a `RuleSelectionKey` from:

- `tax_type`
- `regime_type`
- `regime_identifier`
- `tax_year`
- `rule_version`
- `input_payload.version_context.primary_effective_date`
- `input_payload.version_context.historical_version_id`
- `input_payload.taxpayer_context.resident_status_assertion`
- `income_category_signature`

`income_category_signature` is built from the presence of `employment`, `business`, `investment`, and `rental` mappings in `input_payload.income_sections`.

### Governed Implemented Bindings

| Binding ID | Window | Lane | Executor |
| --- | --- | --- | --- |
| `income_tax_resident_employment_v1_2021_01_01` | `2021-01-01` to `2021-06-30` | resident employment | `execute_resident_employment_2021_rule_pack` |
| `income_tax_non_resident_employment_v1_2021_01_01` | `2021-01-01` to `2021-06-30` | non-resident employment | `execute_non_resident_employment_2021_rule_pack` |
| `income_tax_resident_employment_v1_2023_07_01` | `2023-07-01` to `2023-08-31` | resident employment | `execute_resident_employment_rule_pack` |
| `income_tax_resident_employment_plus_qualifying_interest_v1_2023_07_01` | `2023-07-01` to `2023-08-31` | resident employment plus qualifying interest | `execute_resident_employment_plus_qualifying_interest_rule_pack` |
| `income_tax_non_resident_employment_v1_2023_07_01` | `2023-07-01` to `2023-08-31` | non-resident employment | `execute_non_resident_employment_rule_pack` |

### Additional Bindings Present in `rule_binding.py`

| Binding ID | Selection key | Execution behavior |
| --- | --- | --- |
| `income_tax_default_v1_2025` | `tax_type=income_tax`, `regime_type=income_tax`, `tax_year=2025`, `rule_version=v1`, `regime_identifier=null` | falls back to `deterministic_stub_rule_executor` |
| `health_contribution_v1_2025` | `tax_type=health_tax`, `regime_type=health_contribution`, `tax_year=2025`, `rule_version=v1`, non-null `regime_identifier` required | falls back to `deterministic_stub_rule_executor` |
| `income_tax_ambiguous_a_2025` and `income_tax_ambiguous_b_2025` | same key: `income_tax`, `income_tax`, `2025`, `v_ambiguous`, `regime_identifier=null` | intentionally raises `ambiguous_rule_binding` |

## Governed Income-Tax Contracts

Important contract split:

- The HTTP endpoint boundary uses `ComputationExecutionRequest` and only requires a generic object `input_payload`.
- The governed income-tax rule packs expect the richer JSON shape from `contracts/tools/schemas/income_tax_execution_request.schema.json`.
- The governed income-tax rule packs return payloads shaped like `contracts/tools/schemas/income_tax_result_payload.schema.json`.
- `docs/phase-4/income-tax/io_contract.md` explicitly marks `income_tax_v1_*` as narrow legacy drafts.

### Governing Request Schema: `income_tax_execution_request.schema.json`

Top-level fields:

| Field | Required in schema | Type / constraint |
| --- | --- | --- |
| `tax_type` | yes | constant `income_tax` |
| `regime_type` | yes | constant `income_tax` |
| `regime_identifier` | yes | `string` or `null`, `minLength: 1` when string |
| `tax_year` | yes | integer, `2004 <= tax_year <= 2100` |
| `rule_version` | yes | non-empty string |
| `input_payload` | yes | governed object |

`input_payload.version_context`:

- `primary_effective_date` required, date
- `assessment_period_start` optional, date
- `assessment_period_end` optional, date
- `version_selection_basis` required, one of `tax_year_end`, `receipt_date`, `payroll_period_end`, `filing_period_end`, `specific_event_date`
- `historical_version_id` optional, string or null
- `source_anchor_ids` optional, unique array of non-empty strings

`input_payload.taxpayer_context`:

- `taxpayer_kind` required, enum `individual`
- `resident_status_assertion` required, enum `resident`, `non_resident`, `undetermined`
- `citizenship_or_registration_context` optional, enum `not_provided`, `kenyan`, `foreign_national`, `mixed_or_other_status`
- `residence_reference_period_start` optional, date
- `residence_reference_period_end` optional, date
- `taxpayer_reference_id` optional, non-empty string

`input_payload.income_sections`:

- `employment` optional `EmploymentIncomeSection`
- `business` optional `BusinessIncomeSection`
- `investment` optional `InvestmentIncomeSection`
- `rental` optional `RentalIncomeSection`
- `classification_pending_items` optional array, `minItems: 1`

`EmploymentIncomeItem` fields:

- `income_subtype` required, enum `cash_emolument`, `allowance`, `benefit_in_kind`, `employer_loan_benefit`, `terminal_payment`, `other_taxable_employment_item`
- `amount_kes` required, money string pattern `^-?\d+\.\d{2}$`
- `event_date` required, date
- `employer_reference_id` optional
- `paye_withheld_kes` optional
- `prescribed_rate_notice_id` optional

`BusinessIncomeItem` fields:

- `income_subtype` required, enum `trade`, `profession`, `vocation`, `farm_or_agricultural`, `other_business_income`
- `gross_amount_kes` required
- `event_date` required
- `source_reference_id` optional
- `withholding_applied_kes` optional

`InvestmentIncomeItem` fields:

- `income_subtype` required, enum `interest`, `dividend`, `royalty`, `annuity`, `capital_gains_boundary_item`, `other_passive_income`
- `gross_amount_kes` required
- `event_date` required
- `withholding_applied_kes` optional
- `payer_reference_id` optional

`RentalIncomeItem` fields:

- `income_subtype` required, enum `residential_rental`, `commercial_property`, `mixed_use_property`, `other_property_income`
- `gross_amount_kes` required
- `event_date` required
- `property_reference_id` optional
- `withholding_applied_kes` optional

`PendingClassificationItem` fields:

- `gross_amount_kes` required
- `event_date` required
- `reason_code` required, enum `requires_domain_classification`, `awaiting_section_level_policy`, `adjacent_regime_boundary`, `insufficient_governed_mapping`
- `source_reference_id` optional

`input_payload.claims`:

- `relief_claims` required array of `ReliefClaim`
- `deduction_claims` required array of `DeductionClaim`
- `exemption_claims` required array of `ExemptionClaim`

`ReliefClaim`:

- `relief_type` required, enum `personal_relief`, `insurance_relief`
- `claim_reference_id` required
- `asserted_amount_kes` optional

`DeductionClaim`:

- `deduction_type` required, enum `charitable_donation`, `home_ownership_savings_plan`, `other_governed_deduction_pending_policy`
- `claim_reference_id` required
- `asserted_amount_kes` optional

`ExemptionClaim`:

- `exemption_type` required, enum `charitable_organization_exemption`, `other_governed_exemption_pending_policy`
- `claim_reference_id` required
- `asserted_amount_kes` optional

`input_payload.payment_pathways`:

- `withholding_events` required array
- `installment_tax_events` required array
- `advance_tax_events` required array

`WithholdingEvent`:

- `income_reference_id` required
- `amount_kes` required
- `event_date` required
- `treatment_assertion` optional, enum `final_tax`, `creditable_against_annual_liability`, `advance_payment`, `undetermined`

`InstallmentTaxEvent`:

- `amount_kes` required
- `event_date` required
- `basis_assertion` optional, enum `current_year_estimate`, `prior_year_basis`, `undetermined`

`AdvanceTaxEvent`:

- `amount_kes` required
- `event_date` required
- `advance_tax_subtype` optional, enum `motor_vehicle_advance_tax`, `other_governed_advance_tax`

`input_payload.traceability_context`:

- `source_record_ids` required, unique array, `minItems: 1`
- `evidence_reference_ids` optional unique array
- `preparation_profile` required, enum `manual_structured_entry`, `payroll_import_normalized`, `return_preparation_normalized`, `historical_reconstruction_normalized`
- `completeness_assertion` required, enum `complete`, `partial_but_governed`, `contains_unresolved_domains`

### Governing Result Schema: `income_tax_result_payload.schema.json`

Top-level required fields:

- `version_identity`
- `taxpayer_outcome`
- `domain_outcomes`
- `liability_summary`
- `treatment_decisions`
- `impact_summary`
- `unsupported_or_unresolved`
- `traceability`

`version_identity` fields:

- `historical_version_id`
- `tax_year`
- `rule_version`
- `effective_start`
- `effective_end`
- `version_selection_basis`
- `source_anchor_ids`

`taxpayer_outcome` fields:

- `taxpayer_kind`
- `resident_status`
- `classification_outcome`

`domain_outcomes` keys:

- `employment`
- `business`
- `investment`
- `rental`
- `withholding`
- `installment_tax`
- `advance_tax`
- `reliefs`
- `deductions_and_exemptions`
- `prescribed_rate_resolution`
- `adjacent_regime_interactions`

Every `DomainOutcome` uses:

- `status`
- `taxable_base_kes`
- `gross_tax_kes`
- `creditable_amount_kes`
- `final_tax_amount_kes`
- `decision_refs`

`liability_summary` fields:

- `assessable_income_kes`
- `chargeable_income_kes`
- `gross_tax_kes`
- `total_reliefs_kes`
- `creditable_withholding_kes`
- `installment_tax_credit_kes`
- `advance_tax_credit_kes`
- `net_income_tax_due_kes`
- `refund_due_kes`
- `final_tax_excluded_income_kes`

`treatment_decisions` fields:

- `withholding_treatments`
- `adjacent_regime_flags`

`WithholdingTreatment` fields:

- `income_reference_id`
- `treatment`
- `decision_ref`

`impact_summary` fields:

- `relief_impacts`
- `deduction_impacts`
- `exemption_impacts`

Every `ImpactEntry` uses:

- `impact_type`
- `claim_reference_id`
- `status`
- `impact_amount_kes`

`unsupported_or_unresolved` item fields:

- `domain_id`
- `reason_code`
- `decision_ref`
- `source_anchor_ids`

`traceability` fields:

- `input_hash`
- `applied_policy_ids`
- `source_anchor_ids`
- `validation_focus_domains`
- `computation_status`
- `replay_safe`

## Governed Income-Tax Lanes Implemented in Code

### Common Numeric Rules

Exact constants shared by the rule-pack code:

- money strings must match `^\d+\.\d{2}$` inside governed rule packs
- all final money outputs are quantized to two decimal places using `ROUND_HALF_UP`
- resident personal relief used in the current governed resident lanes: `28800.00`
- 2023 qualifying-interest final tax rate: `0.15`

### Resident Employment: `income_tax_resident_employment_v1_2023_07_01`

Source files:

- `resident_employment_rule_pack.py`
- `reliefs_and_credits.py`
- `deductions_and_exemptions.py`
- `resident_employment_numeric_policy_2023_07_01.md`
- `eval/golden/tax_core/income_tax_resident_employment_2023_07_01_case_001.json`

Exact implemented constraints:

- `tax_year` must be `2023`
- `version_context.version_selection_basis` must be `specific_event_date`
- `version_context.primary_effective_date` must be between `2023-07-01` and `2023-08-31`
- `historical_version_id`, when provided, must equal `KIT-VER-20230701-A`
- `taxpayer_kind` must be `individual`
- `resident_status_assertion` must be `resident`
- `residence_reference_period_start` must be `null` or `2023-01-01`
- `residence_reference_period_end` must be `null` or `2023-12-31`
- `income_sections.business`, `investment`, and `rental` must be `null`
- `classification_pending_items` must be empty
- `employment_items` must be present and non-empty
- every employment item must use `income_subtype = cash_emolument`
- `paye_withheld_kes` must be `null`
- `prescribed_rate_notice_id` must be `null`
- employment `amount_kes` must be non-negative
- `withholding_events`, `installment_tax_events`, and `advance_tax_events` must be empty
- `traceability_context.source_record_ids` must be non-empty
- `traceability_context.completeness_assertion` must be `complete`
- `deduction_claims` and `exemption_claims` must both be empty
- exactly one `personal_relief` claim is required
- if the personal-relief claim supplies `asserted_amount_kes`, it must equal `28800.00`
- `insurance_relief` claims are only accepted when `asserted_amount_kes` is present and equal to `0.00`

Exact tax constants:

- first `288000.00` at `10%`
- next `100000.00` at `25%`
- next `5612000.00` at `30%`
- next `3600000.00` at `32.5%`
- excess at `35%`

Golden example output:

- assessable income `960000.00`
- gross tax `225400.00`
- total reliefs `28800.00`
- net income tax due `196600.00`

### Non-Resident Employment: `income_tax_non_resident_employment_v1_2023_07_01`

Source files:

- `non_resident_employment_rule_pack.py`
- `reliefs_and_credits.py`
- `deductions_and_exemptions.py`
- `non_resident_employment_numeric_policy_2023_07_01.md`
- `eval/golden/tax_core/income_tax_non_resident_employment_2023_07_01_case_001.json`

Exact implemented constraints:

- same date window and version checks as the 2023 resident lane
- `resident_status_assertion` must be `non_resident`
- full-year scope is enforced with `2023-01-01` and `2023-12-31` when dates are provided
- `employment_items` must be present and non-empty
- every employment item must use `income_subtype = cash_emolument`
- `employer_reference_id` is required on every employment item
- `paye_withheld_kes` must be `null`
- `prescribed_rate_notice_id` must be `null`
- `relief_claims` must be empty
- `deduction_claims` and `exemption_claims` must be empty

Exact tax constants:

- uses the same 2023 band schedule as the 2023 resident lane
- total_reliefs is always `0.00`

Golden example output:

- assessable income `960000.00`
- gross tax `225400.00`
- total reliefs `0.00`
- net income tax due `225400.00`

### Resident Employment Plus Qualifying Interest: `income_tax_resident_employment_plus_qualifying_interest_v1_2023_07_01`

Source files:

- `mixed_income_computation.py`
- `reliefs_and_credits.py`
- `deductions_and_exemptions.py`
- `mixed_income_policy_resident_employment_plus_qualifying_interest_2023_07_01.md`
- `eval/golden/tax_core/income_tax_mixed_resident_employment_plus_qualifying_interest_2023_07_01_case_001.json`

Exact implemented constraints:

- same 2023 resident checks as the resident-employment lane
- `income_sections.business` and `income_sections.rental` must be `null`
- employment items follow the same `cash_emolument`-only rules as the resident lane
- `investment_items` must be present and non-empty
- every investment item must use `income_subtype = interest`
- every investment item must include `payer_reference_id`
- every investment `gross_amount_kes` must be non-negative
- every investment item must include `withholding_applied_kes`
- `withholding_applied_kes` must equal `15%` of `gross_amount_kes`
- `deduction_claims` and `exemption_claims` must be empty
- same resident relief rules as the resident-employment lane

Exact tax constants:

- employment component uses the 2023 resident-employment band schedule
- qualifying-interest final tax rate is `15%`

Golden example output:

- employment assessable income `960000.00`
- qualifying-interest gross amount `120000.00`
- employment gross tax `225400.00`
- qualifying-interest final tax `18000.00`
- aggregate gross tax `243400.00`
- total reliefs `28800.00`
- final-tax-excluded income `120000.00`
- net income tax due `214600.00`

### Resident Employment: `income_tax_resident_employment_v1_2021_01_01`

Source files:

- `resident_employment_rule_pack_2021.py`
- `deductions_and_exemptions.py`
- `historical_employment_numeric_policy_KIT-VER-20210101-A.md`
- `eval/golden/tax_core/income_tax_resident_employment_2021_01_01_case_001.json`

Exact implemented constraints:

- `tax_year` must be `2021`
- `version_context.primary_effective_date` must be between `2021-01-01` and `2021-06-30`
- `historical_version_id`, when provided, must equal `KIT-VER-20210101-A`
- resident/full-year/employment-only checks mirror the 2023 resident lane
- `deduction_claims` and `exemption_claims` must be empty
- exactly one `personal_relief` claim is required
- if the personal-relief claim supplies `asserted_amount_kes`, it must equal `28800.00`
- `insurance_relief` claims are only accepted when `asserted_amount_kes` is present and equal to `0.00`

Exact tax constants:

- first `288000.00` at `10%`
- next `100000.00` at `25%`
- excess at `30%`
- resident personal relief `28800.00`

Golden example output:

- assessable income `10000000.00`
- gross tax `2937400.00`
- total reliefs `28800.00`
- net income tax due `2908600.00`

### Non-Resident Employment: `income_tax_non_resident_employment_v1_2021_01_01`

Source files:

- `non_resident_employment_rule_pack_2021.py`
- `deductions_and_exemptions.py`
- `historical_employment_numeric_policy_KIT-VER-20210101-A.md`
- `eval/golden/tax_core/income_tax_non_resident_employment_2021_01_01_case_001.json`

Exact implemented constraints:

- same 2021 date-window and `historical_version_id` checks as the 2021 resident lane
- `resident_status_assertion` must be `non_resident`
- every employment item must use `income_subtype = cash_emolument`
- `employer_reference_id` is required on every employment item
- `prescribed_rate_notice_id` must be `null`
- `relief_claims` must be empty
- `deduction_claims` and `exemption_claims` must be empty

Exact tax constants:

- uses the same 2021 three-band schedule as the 2021 resident lane
- total_reliefs is always `0.00`

Golden example output:

- assessable income `10000000.00`
- gross tax `2937400.00`
- total reliefs `0.00`
- net income tax due `2937400.00`

## Validation Findings

Validation finding model from `execution_contract.py`:

- `code`
- `severity` (`info`, `warning`, `error`)
- `message`
- `details`

Findings currently emitted by `validation.py` and `validation_catalog.py`:

- `computation_lineage_bound`
- `income_tax_validation_scope_unsupported`
- `income_tax_result_payload_shape_invalid`
- `income_tax_supported_lane_detected`
- `income_tax_version_binding_consistent`
- `income_tax_version_binding_inconsistent`
- `income_tax_relief_treatment_consistent`
- `income_tax_relief_treatment_inconsistent`
- `income_tax_liability_summary_consistent`
- `income_tax_liability_summary_inconsistent`
- `income_tax_mixed_income_treatment_consistent`
- `income_tax_mixed_income_treatment_inconsistent`

Supported lane IDs inside the validation catalog:

- `resident_employment_2021_01_01`
- `non_resident_employment_2021_01_01`
- `resident_employment_2023_07_01`
- `resident_employment_plus_qualifying_interest_2023_07_01`
- `non_resident_employment_2023_07_01`

Validation catalog behavior:

- non-`income_tax` or non-`income_tax`/`income_tax` persisted computations return `income_tax_validation_scope_unsupported`
- missing `version_identity` in the stored result also returns `income_tax_validation_scope_unsupported`
- governed lanes are detected from `historical_version_id`, `resident_status`, and whether `domain_outcomes.investment.status` is `computed`

## Error Envelope Shapes

Request and processing errors built with `shared.errors.envelope.create_request_http_error()` return:

```json
{
  "detail": {
    "error_code": "string",
    "message": "string",
    "correlation_id": "string",
    "details": {}
  }
}
```

Auth-context authorization errors built in `shared.authz.rbac` return a different detail shape and include:

- `error_code`
- `message`
- `reason`
- `trace_id`
- `correlation_id`
- `details`

Error-code constants present in `shared/errors/codes.py` and used by tax-core dependencies:

- `missing_authorization_header`
- `invalid_authorization_scheme`
- `invalid_bearer_token`
- `missing_idempotency_key`
- `invalid_idempotency_key`

Tax-core-specific error code strings defined in `main.py`:

- `invalid_computation_request`
- `invalid_replay_request`
- `invalid_finalization_request`
- `invalid_validation_request`
- `invalid_rule_binding`
- `idempotency_key_conflict`
- `computation_materialization_failed`

Tax-core engine error reasons surfaced by replay/finalize/validate flows:

- `computation_not_found`
- `replay_input_hash_mismatch`
- `replay_result_mismatch`
- `replay_verification_failed`
- `invalid_persistence_configuration`
- `invalid_retention_days`
- `invalid_finalization_state`
- `finalization_failed`
- `validation_failed`

## Golden Fixtures

Golden computation fixtures in `eval/golden/tax_core/`:

- `income_tax_resident_employment_2023_07_01_case_001.json`
- `income_tax_non_resident_employment_2023_07_01_case_001.json`
- `income_tax_mixed_resident_employment_plus_qualifying_interest_2023_07_01_case_001.json`
- `income_tax_resident_employment_2021_01_01_case_001.json`
- `income_tax_non_resident_employment_2021_01_01_case_001.json`
- `income_tax_v1_case_001.json`

The golden-regression test suite treats the first five fixture IDs as the required governed-lane corpus.

## Project Structure

```text
services/tax_core/
|-- README.md
`-- app/
    |-- config.py
    |-- main.py
    |-- engine/
    |   |-- execution_contract.py
    |   |-- executor.py
    |   |-- finalization.py
    |   |-- replay.py
    |   |-- rule_binding.py
    |   `-- validation.py
    |-- persistence/
    |   `-- materialization.py
    `-- rules/
        `-- income_tax/
            |-- deductions_and_exemptions.py
            |-- mixed_income_computation.py
            |-- non_resident_employment_rule_pack.py
            |-- non_resident_employment_rule_pack_2021.py
            |-- reliefs_and_credits.py
            |-- resident_employment_rule_pack.py
            |-- resident_employment_rule_pack_2021.py
            `-- validation_catalog.py
```
