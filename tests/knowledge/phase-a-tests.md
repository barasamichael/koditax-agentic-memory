# Phase A Knowledge Test Plan

Status: Proposed  
Date: 2026-05-03  
Scope owner: Backend Engineering

## 1. Purpose

This plan defines the new Phase A test surface for the `knowledge` microservice.

It exists because the current root-level Knowledge tests are a mixed inventory:

1. some are still useful as coverage references
2. some are tied to older Phase 13 assumptions
3. some blend in adjacent-service concerns that should not define the Knowledge service test boundary

The goal of this plan is to build a clean `tests/knowledge/` suite that reflects the current implemented Knowledge runtime and only the Knowledge runtime.

## 2. Scope Rules

These rules are strict.

In scope:

1. protected retrieval routes:
   - `/knowledge/search`
   - `/knowledge/retrieve`
   - `/knowledge/timeline/search`
2. internal administrator-only Knowledge routes:
   - ingestion
   - review
   - approval
   - publication
   - metadata correction
   - supersession
   - archive
   - management listing and detail routes
3. repository-governed behavior that belongs to the Knowledge service itself:
   - publication-state gating
   - lineage and provenance validation
   - same-family supersession rules
   - deterministic sorting and envelopes
   - archive and retention posture
   - source-document-system restrictions
   - protected query guardrails
4. Knowledge runtime contract parity with `contracts/openapi/knowledge.yaml`
5. Knowledge-specific runbook and release-readiness artifacts where they assert current committed behavior

Out of scope:

1. orchestration planning, prompting, grounded explanation rendering, or fallback routing
2. `document_ai` upload lifecycle, extraction, retention, or access policy behavior
3. gateway edge enforcement beyond what Knowledge explicitly documents as an external dependency
4. auth service internals beyond the Knowledge runtime’s required `Administrator` boundary behavior
5. end-to-end cross-service tests whose primary assertion is not Knowledge-owned behavior
6. tests for future behavior that is not yet implemented in the current repository

## 3. Current Knowledge Test Inventory

Current root-level Knowledge-related files:

1. `tests/test_knowledge_service_smoke.py`
2. `tests/test_knowledge_openapi_contract.py`
3. `tests/test_phase13_knowledge_adversarial.py`
4. `tests/test_phase13_knowledge_bulk_ingestion.py`
5. `tests/test_phase13_knowledge_bulk_management.py`
6. `tests/test_phase13_knowledge_governance_baseline.py`
7. `tests/test_phase13_knowledge_hardening.py`
8. `tests/test_phase13_knowledge_ingestion_boundary.py`
9. `tests/test_phase13_knowledge_management_read_surfaces.py`
10. `tests/test_phase13_knowledge_orchestration_integration.py`
11. `tests/test_phase13_knowledge_performance_smoke.py`
12. `tests/test_phase13_knowledge_persistence_baseline.py`
13. `tests/test_phase13_knowledge_publication_workflow.py`
14. `tests/test_phase13_knowledge_supersession_archive.py`
15. `tests/test_phase13_knowledge_timeline_retrieval.py`

Support files already useful for reuse:

1. `tests/knowledge_db_test_support.py`
2. Knowledge-safe fixtures already housed under `tests/fixtures/` when they do not depend on orchestration behavior

## 4. Problem With The Current Layout

The current layout is behind the implementation for three reasons:

1. it is phase-labeled around older closure assumptions rather than the final Knowledge boundary
2. it mixes pure Knowledge tests with tests that are really orchestration-adjacent or historical milestone artifacts
3. it does not give contributors one obvious place to add or run the current canonical Knowledge suite

This plan fixes that by making `tests/knowledge/` the canonical Phase A suite location.

## 5. Target Folder Layout

The new suite will live under `tests/knowledge/`.

Planned files:

1. `tests/knowledge/test_runtime_smoke.py`
2. `tests/knowledge/test_openapi_contract.py`
3. `tests/knowledge/test_public_retrieval.py`
4. `tests/knowledge/test_timeline_retrieval.py`
5. `tests/knowledge/test_ingestion_boundaries.py`
6. `tests/knowledge/test_document_backed_ingestion.py`
7. `tests/knowledge/test_publication_workflow.py`
8. `tests/knowledge/test_supersession_and_archive.py`
9. `tests/knowledge/test_metadata_correction.py`
10. `tests/knowledge/test_management_surfaces.py`
11. `tests/knowledge/test_boundary_hardening.py`
12. `tests/knowledge/test_retention_and_lineage.py`
13. `tests/knowledge/test_runbook_artifacts.py`
14. `tests/knowledge/conftest.py` if local shared fixtures become necessary

Support reuse:

1. prefer reusing `tests/knowledge_db_test_support.py`
2. add new helpers only if they are Knowledge-specific and deterministic
3. do not move orchestration or cross-service helpers into this folder

## 6. Test Buckets

### 6.1 Runtime Smoke

Purpose:

1. prove the app boots
2. prove protected retrieval routes exist
3. prove protected mutation routes reject missing auth deterministically
4. prove `healthz` and `readyz` behave as documented

Candidate file:

1. `tests/knowledge/test_runtime_smoke.py`

### 6.2 Contract Parity

Purpose:

1. lock runtime route surface to `contracts/openapi/knowledge.yaml`
2. lock health-route versus protected-route classification
3. lock canonical error envelope fields and scoped fail-closed route behavior
4. lock current request-boundary rules such as protected query and identifier caps

Candidate file:

1. `tests/knowledge/test_openapi_contract.py`

### 6.3 Public Retrieval

Purpose:

1. verify `search`, `retrieve`, and `timeline/search`
2. assert only `published` and historically valid `superseded` records surface
3. assert deterministic ordering and stable response shape
4. assert public input guardrails fail closed

Candidate files:

1. `tests/knowledge/test_public_retrieval.py`
2. `tests/knowledge/test_timeline_retrieval.py`

### 6.4 Ingestion And Provenance

Purpose:

1. verify admin-only ingestion boundaries
2. verify `storage_registered` is the only valid new document-backed handoff
3. verify direct file ingestion remains legacy-import-only
4. verify URL ingestion remains governed and first-class
5. verify invalid lineage or provenance fails closed before publication

Candidate files:

1. `tests/knowledge/test_ingestion_boundaries.py`
2. `tests/knowledge/test_document_backed_ingestion.py`

### 6.5 Publication And Lifecycle Governance

Purpose:

1. verify canonical use of `approved`
2. verify distinct-actor review versus publish rule
3. verify same-family supersession and non-overlapping windows
4. verify metadata correction cannot mutate post-publication lineage
5. verify archive and retention summary semantics

Candidate files:

1. `tests/knowledge/test_publication_workflow.py`
2. `tests/knowledge/test_supersession_and_archive.py`
3. `tests/knowledge/test_metadata_correction.py`
4. `tests/knowledge/test_retention_and_lineage.py`

### 6.6 Boundary Hardening And Management Surfaces

Purpose:

1. verify public versus internal route boundary assumptions
2. verify management endpoints remain Administrator-only
3. verify source and anchor detail surfaces expose only governed management data
4. verify compatibility-mode lineage remains readable but not reusable as a new ingestion path

Candidate files:

1. `tests/knowledge/test_management_surfaces.py`
2. `tests/knowledge/test_boundary_hardening.py`

### 6.7 Docs And Operational Artifacts

Purpose:

1. verify committed runbooks exist
2. verify README claims match the final boundary posture
3. verify Knowledge operational posture remains explicit for `purge_supported = false`

Candidate file:

1. `tests/knowledge/test_runbook_artifacts.py`

## 7. Files To Treat As Legacy References, Not Canonical Phase A Targets

These files may still contain useful assertions, but they should be treated as source material to port or narrow, not as the final home for Knowledge Phase A coverage:

1. `tests/test_knowledge_service_smoke.py`
2. `tests/test_knowledge_openapi_contract.py`
3. all `tests/test_phase13_knowledge_*.py` files

Special note:

1. `tests/test_phase13_knowledge_orchestration_integration.py` is explicitly not part of the new pure Knowledge suite because its primary concern crosses the Knowledge boundary into orchestration behavior

## 8. Migration Strategy

The migration strategy is additive first, cleanup second.

Step 1:

1. create the new canonical tests under `tests/knowledge/`
2. keep them tightly scoped to current implemented Knowledge behavior
3. avoid importing orchestration-specific fixtures unless the assertion is still purely Knowledge-owned

Step 2:

1. compare old root-level Knowledge files against the new suite
2. port only still-valid assertions
3. drop or quarantine assertions that belong to:
   - orchestration
   - document AI
   - historical milestone artifacts
   - unimplemented behavior

Step 3:

1. once parity is reached, mark root-level legacy Knowledge tests for retirement or relocation
2. keep the new folder as the canonical execution target

## 9. Execution Order

The suite should be built in this order:

1. `test_runtime_smoke.py`
2. `test_openapi_contract.py`
3. `test_public_retrieval.py`
4. `test_timeline_retrieval.py`
5. `test_ingestion_boundaries.py`
6. `test_document_backed_ingestion.py`
7. `test_publication_workflow.py`
8. `test_supersession_and_archive.py`
9. `test_metadata_correction.py`
10. `test_management_surfaces.py`
11. `test_boundary_hardening.py`
12. `test_retention_and_lineage.py`
13. `test_runbook_artifacts.py`

Reason for this order:

1. it starts with the stable boundary and contract surface
2. it then moves through protected retrieval behavior before admin write workflows
3. it leaves docs and runbook artifact checks until the runtime semantics are already locked

## 10. Test Design Rules

All new tests under `tests/knowledge/` must follow these rules:

1. deterministic assertions only
2. no weakening assertions to match accidental runtime drift
3. no coverage for unsupported tax domains
4. no cross-service behavioral claims unless the Knowledge runtime itself owns the contract
5. no reliance on web/network access
6. no silent use of unrelated service fixtures
7. canonical error envelope checks must assert:
   - `error_code`
   - `message`
   - `reason`
8. protected-route tests must include missing-auth negative paths
9. published versus non-searchable state behavior must be asserted explicitly
10. timeline retrieval must assert chronology preservation, not just record presence

## 11. What “Uncorrupted” Means For This Suite

For this effort, an uncorrupted Knowledge test is one that:

1. asserts only Knowledge-owned behavior
2. matches the current Phase A implementation, not a superseded milestone assumption
3. does not depend on orchestration answer rendering to prove Knowledge correctness
4. does not depend on generic `document_ai` user-document behavior to prove shared-corpus ingestion correctness
5. does not assert future architecture that the repo does not yet implement

## 12. Initial Validation Target

When implementation begins, the first execution target should be the new folder only, not the entire repository.

Planned command shape:

```powershell
pytest -q tests/knowledge --timeout=30 --basetemp "$env:TEMP\kodi_pytest\<unique_run_id>"
```

Then run:

```powershell
ruff check tests/knowledge tests/knowledge_db_test_support.py
ruff format --check tests/knowledge tests/knowledge_db_test_support.py
```

No cross-service test file should be pulled into the initial Phase A Knowledge execution target.

## 13. Definition Of Done For The Test Migration

This test-plan effort is done when:

1. `tests/knowledge/` exists as the canonical Phase A Knowledge suite location
2. the suite covers only current Knowledge scope
3. the suite excludes orchestration-primary and document-AI-primary assertions
4. the suite reflects the final Phase A runtime, contract, and README posture
5. contributors can run the Knowledge suite without guessing which old root-level tests are still authoritative
