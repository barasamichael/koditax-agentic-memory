# Knowledge Service

Deterministic tax law knowledge catalog service for the Kodi Solutions tax compliance platform. It provides governed search, direct retrieval, and review-safe intake of authoritative Kenyan legal source materials so downstream surfaces can ground responses in verified legal sources.

---

## Table of Contents

1. [Description](#description)
2. [Dependencies](#dependencies)
3. [Installation](#installation)
4. [API Endpoints](#api-endpoints)
5. [Business Rules and Constraints](#business-rules-and-constraints)
6. [Error Handling](#error-handling)
7. [Security and Compliance](#security-and-compliance)
8. [Runtime and Configuration Notes](#runtime-and-configuration-notes)
9. [Integration Capabilities](#integration-capabilities)
10. [Project Structure](#project-structure)

---

## Description

The Knowledge service is a deterministic governed catalog of authoritative Kenyan tax law references. It currently exposes:

- keyword search with authority-ranked results
- direct retrieval by source ID or anchor ID
- timeline retrieval across governed effective windows
- single-item document-backed official-source intake for shared-corpus ingestion jobs
- bulk document-backed official-source intake for shared-corpus ingestion jobs
- single-item official-source intake for file-backed and URL-backed shared-corpus ingestion jobs
- bulk official-source intake for file-backed and URL-backed shared-corpus ingestion jobs
- single-item review, approval, rejection, and governed publication for ingested official-source jobs
- single-item supersession and archive transitions for published governed source versions

Key capabilities:

- Deterministic governed hybrid search: metadata and publication-state filters are applied first, then lexical matching and optional OpenAI embedding similarity rank only the already-governed candidate set
- Filter support: filter by `source_type`, `tax_domain`, and `effective_date`; effective date filtering enforces `effective_from <= query_date <= effective_to`
- Direct retrieval: fetch one or more records by `source_id` or `anchor_id`
- Governed timeline retrieval: fetch chronologically ordered published and historically valid superseded records across a requested date range without blending effective windows
- Governed document-backed ingestion boundary: accept registered upstream document provenance plus checksum and storage metadata into persistent `knowledge_ingestion_jobs` for file-backed shared-corpus publication
- Governed ingestion boundary: accept official-source files and URLs into persistent `knowledge_ingestion_jobs` with checksuming, deduplication, and replay-safe idempotency behavior
- Governed bulk ingestion boundary: accept multiple official-source files or URLs with deterministic per-item outcome reporting and canonical partial-failure semantics
- Governed review/publication workflow: fetch one ingestion job, record review-safe notes and metadata refinements, approve or reject deterministically, and materialize searchable knowledge records only after successful publication
- Governed lifecycle control: supersede one published source version with a same-family successor and archive governed source versions only through explicit lifecycle transitions
- Governed management visibility: list ingestion jobs and source-version lifecycle state through deterministic read-only management endpoints with stable pagination and sorting
- Governed bulk lifecycle control: bulk publish approved ingestion jobs, bulk reject reviewable ingestion jobs, and bulk archive eligible source versions through replay-safe deterministic outcome reporting
- Non-searchable intake safety: ingested jobs remain unpublished and do not enter `/knowledge/search` or `/knowledge/retrieve`
- Fail-closed scoping: unsupported paths return `404` with `unsupported_knowledge_scope`

Architectural role: governed legal retrieval and intake boundary consumed by orchestration and downstream explanation layers while preserving publication-state safety.

Product boundary note:

- end-user and chat-style frontend experiences should interact with knowledge-backed capability through `orchestration`, not by calling the knowledge service directly
- direct frontend use of `knowledge` is reserved for internal administrator-facing workflows such as ingestion, review, publication, and lifecycle management
- administrator-facing workflows should be presented as guided business operations rather than raw technical API exercises
- the same frontend application may host both user and admin surfaces, but authorization must determine which capability surface is available to the current user

Practical contributor mental model:

- protected retrieval surface: `/knowledge/search`, `/knowledge/retrieve`, and `/knowledge/timeline/search`
- internal governance surface: ingestion, review, approval, publication, metadata correction, lifecycle transitions, and management listing/detail routes
- primary file-backed publication path: registered document provenance first, then Knowledge ingestion review
- repository-backed runtime: FastAPI handlers stay thin and delegate deterministic storage, ranking, lifecycle, and validation rules to `repository.py`
- hybrid retrieval is optional and governance-first: lexical search always remains active; OpenAI embeddings only refine ranking inside an already-authorized candidate set
- product-facing rule: user-visible frontend retrieval should route through orchestration, while internal admin tooling may use protected knowledge routes directly

What this service does not do:

- it does not generate user-facing answers or legal explanations; orchestration and downstream presentation layers do that
- it does not make unpublished intake searchable before explicit review and publication
- it does not allow customer-uploaded documents to enter the shared authoritative corpus
- it does not expose destructive purge endpoints in the current runtime slice

Current implementation status:

- persistent governed storage for searchable knowledge records
- persisted chunk-embedding storage for governed hybrid lexical-plus-vector ranking
- persistent governed ingestion intake for official-source files and URLs
- persistent governed document-backed ingestion intake for official-source file provenance handoff
- deterministic governed bulk ingestion intake for official-source files and URLs
- deterministic single-item review and publication workflow for official-source ingestion jobs
- deterministic single-item supersession and archive workflow for published source versions
- deterministic read-only management listing for ingestion jobs and source versions
- deterministic bulk publish, bulk reject, and bulk archive behavior for governed operator workflows
- deterministic timeline retrieval for historical and multi-period legal search
- deterministic source and anchor management detail visibility with governance-safe chunk summaries
- deterministic pre-publication metadata correction for editable unpublished review-stage material
- internal auth-context protection for retrieval, management, and mutation routes
- deterministic audit-event coverage for ingestion, lifecycle mutation, and query operations
- retention-safe lineage visibility for source management without destructive purge support in this phase

Production target:

- explicit ingestion, review, approval, and publication workflow
- temporal versioning by `effective_from` and `effective_to`
- authority-ranked retrieval with metadata filters before lexical or vector ranking
- publication-state-aware management surfaces and audited governance controls

See [C:\Users\Lenovo\kodi-backend\docs\architecture\Knowledge Service Architecture.md](C:\Users\Lenovo\kodi-backend\docs\architecture\Knowledge%20Service%20Architecture.md) for the governing architecture.

---

## Dependencies

### Python Packages

| Package | Purpose |
|---------|---------|
| `fastapi` | HTTP routing |
| `uvicorn` | ASGI runtime |
| `psycopg` | Persistent governed knowledge storage access |
| `httpx` | OpenAI embeddings API client for governed hybrid retrieval |

Current runtime uses persistent governed storage for search, retrieve, timeline retrieval, single-item and bulk ingestion intake, source and anchor management visibility, pre-publication metadata correction, single-item review/publication, governed bulk lifecycle control, audit emission, and optional OpenAI-backed hybrid retrieval. The FastAPI app also installs `CorrelationIdMiddleware`, request-validation and repository exception handlers, and local-development CORS configuration inside `main.py`.

---

## Installation

```bash
cd services/knowledge
pip install -r requirements.txt
uvicorn services.knowledge.app.main:app --host 0.0.0.0 --port 8006 --reload
```

Important runtime notes:

- service name is `knowledge`
- current service version constant is `0.1.0`
- the app factory wires `app.state.knowledge_repository` to `get_default_knowledge_repository()` unless an explicit repository override is injected
- local-development CORS allows `http://127.0.0.1:5173`, `http://localhost:5173`, `http://127.0.0.1:5174`, and `http://localhost:5174`
- `GET /healthz` and `GET /readyz` are implemented directly in the runtime and return the standard service envelope fields including `correlation_id` and `trace_id`

---

## API Endpoints

### Endpoint Summary

| Method | Path | Auth Required | Summary |
|--------|------|---------------|---------|
| POST | `/knowledge/search` | Yes | Search knowledge catalog with filters and ranking |
| POST | `/knowledge/retrieve` | Yes | Retrieve records by source ID or anchor ID |
| POST | `/knowledge/timeline/search` | Yes | Retrieve governed knowledge records across multiple effective windows chronologically |
| POST | `/knowledge/ingestion/files` | Yes | Persist one governed legacy direct-import official-source file ingestion job |
| POST | `/knowledge/ingestion/files/bulk` | Yes | Persist multiple governed legacy direct-import official-source file ingestion jobs deterministically |
| POST | `/knowledge/ingestion/documents` | Yes | Persist one governed document-backed official-source ingestion job |
| POST | `/knowledge/ingestion/documents/bulk` | Yes | Persist multiple governed document-backed official-source ingestion jobs deterministically |
| POST | `/knowledge/ingestion/urls` | Yes | Persist one governed official-source URL ingestion job |
| POST | `/knowledge/ingestion/urls/bulk` | Yes | Persist multiple governed official-source URL ingestion jobs deterministically |
| GET | `/knowledge/ingestion` | Yes | List governed ingestion jobs with deterministic management filtering |
| POST | `/knowledge/ingestion/bulk/reject` | Yes | Reject multiple reviewable ingestion jobs with deterministic per-item outcomes |
| POST | `/knowledge/ingestion/bulk/publish` | Yes | Publish multiple approved ingestion jobs with deterministic per-item outcomes |
| GET | `/knowledge/ingestion/{ingestion_job_id}` | Yes | Fetch deterministic non-searchable ingestion metadata for review |
| POST | `/knowledge/ingestion/{ingestion_job_id}/review` | Yes | Record governed review notes and proposed metadata refinement |
| POST | `/knowledge/ingestion/{ingestion_job_id}/approve` | Yes | Approve one ingested item for publication-ready processing |
| POST | `/knowledge/ingestion/{ingestion_job_id}/reject` | Yes | Reject one ingested item and keep it non-searchable |
| POST | `/knowledge/ingestion/{ingestion_job_id}/publish` | Yes | Materialize one approved ingestion job into searchable governed records |
| POST | `/knowledge/ingestion/{ingestion_job_id}/metadata-correction` | Yes | Apply one narrow metadata correction for editable unpublished review-stage material |
| GET | `/knowledge/source-versions` | Yes | List governed source versions with deterministic management filtering |
| POST | `/knowledge/source-versions/bulk/archive` | Yes | Archive multiple eligible source versions with deterministic per-item outcomes |
| GET | `/knowledge/source-versions/{source_version_id}` | Yes | Fetch one governed source-version lifecycle record |
| POST | `/knowledge/source-versions/{source_version_id}/supersede` | Yes | Supersede one published source version with a governed successor |
| POST | `/knowledge/source-versions/{source_version_id}/archive` | Yes | Archive one governed published or superseded source version |
| GET | `/knowledge/sources` | Yes | List governed sources with deterministic management filtering |
| GET | `/knowledge/sources/{source_id}` | Yes | Fetch one governed source detail record with version and retention summaries |
| GET | `/knowledge/anchors/{anchor_id}` | Yes | Fetch one governed anchor detail record with chunk summaries only |
| GET | `/healthz` | No | Service liveness check |
| GET | `/readyz` | No | Service readiness check |

---

### POST /knowledge/search

Summary: search the governed knowledge catalog with keyword query, optional filters, and deterministic authority-ranked results. The runtime always applies governance filters first; when OpenAI embeddings are configured, stored chunk embeddings add a secondary vector score inside the already-filtered candidate set.

Request body:

- `query` (`string`, required): non-empty keyword query
- `source_type` (`string`, optional): governed source class filter
- `tax_domain` (`string`, optional): tax domain filter
- `effective_date` (`string`, optional): ISO date used for effective-window filtering

Response:

- stable `status`, `service`, `correlation_id`, `trace_id`
- `result.total`
- `result.items[]` with governed knowledge metadata only; content is not returned

Error responses:

- `400` for `invalid_knowledge_request`

---

### POST /knowledge/retrieve

Summary: retrieve governed knowledge records directly by `source_id` or `anchor_id`.

Request body:

- `source_ids` (`array[string]`, required)
- `anchor_ids` (`array[string]`, required)

Response:

- stable `status`, `service`, `correlation_id`, `trace_id`
- deterministic `result.items[]` sorted by `source_id` and `anchor_id`

Error responses:

- `400` for `invalid_knowledge_request`

---

### POST /knowledge/timeline/search

Summary: retrieve governed knowledge records across a requested effective-date range without collapsing chronology.

Request body:

- `query` (`string`, required)
- `tax_domain` (`string`, required)
- `source_type` (`string`, optional)
- `start_date` (`string`, required): ISO date
- `end_date` (`string`, required): ISO date

Response:

- stable `status`, `service`, `correlation_id`, `trace_id`
- `result.total`
- `result.items[]` ordered chronologically and carrying `source_id`, `source_version_id`, `anchor_id`, `authority_level`, `publication_state`, and `timeline_position`

Error responses:

- `400` for `invalid_knowledge_request`

Important:

- timeline retrieval returns only `published` and historically valid `superseded` records
- archived and unpublished records are excluded
- lexical and optional vector ranking may order records only inside the same governed temporal slice

---

### POST /knowledge/ingestion/documents

Summary: persist one governed official-source ingestion job from an already registered upstream document reference.

This is now the primary file-backed shared-corpus ingestion path.

Request body:

- `requested_by` (`string`, required): UUID for the requesting actor
- `idempotency_key` (`string`, required): replay-safe request key
- `document_id` (`string`, required): UUID of the already registered upstream document
- `storage_key` (`string`, required): upstream local storage key for the registered document; URL-style values are rejected
- `mime_type` (`string`, required): supported MIME type
- `payload_checksum_sha256` (`string`, required): canonical checksum for the registered document payload
- `source_document_system` (`string`, required): `storage_registered` only for new governed shared-corpus handoff
- `source_input_origin` (`string`, optional): when provided, must be `official_source_upload`
- `source_class` (`string`, optional): one of `tax_law`, `regulation`, `guidance`, `commentary`

Important:

- this path is for governed official-source publication only
- upstream customer documents remain outside the shared corpus
- the referenced document must already exist in upstream registration/storage provenance
- Knowledge validates the document reference and persists ingestion review state without creating a new searchable record
- new file-backed source references are normalized under the `official-source-upload://...` namespace rather than using service-local ad hoc identifiers

---

### POST /knowledge/ingestion/files

Summary: persist one governed legacy direct-import official-source file ingestion job into `knowledge_ingestion_jobs`.

This route is no longer the primary production file-backed ingestion path. It is reserved for explicit administrator-driven legacy import or bootstrap migration workflows.

Request body:

- `requested_by` (`string`, required): UUID for the requesting actor
- `idempotency_key` (`string`, required): replay-safe request key
- `filename` (`string`, required): original filename
- `mime_type` (`string`, required): supported MIME type
- `file_content_base64` (`string`, required): base64-encoded file payload
- `legacy_import_acknowledged` (`boolean`, required): must be `true`
- `source_input_origin` (`string`, optional): when provided, must be `official_source_upload`
- `source_class` (`string`, optional): one of `tax_law`, `regulation`, `guidance`, `commentary`

Supported MIME types:

- `application/pdf`
- `text/html`
- `text/plain`
- `text/markdown`
- `application/vnd.openxmlformats-officedocument.wordprocessingml.document`
- `application/xml`

Response:

- stable `status`, `service`, `correlation_id`, `trace_id`
- `result.ingestion_job_id`
- `result.document_id`
- `result.requested_by`
- `result.ingestion_state`
- `result.source_input_origin`
- `result.source_input_ref`
- `result.payload_checksum_sha256`
- `result.source_class`

Error responses:

- `400` for `invalid_knowledge_request`, `unsupported_source_input_origin`, `unsupported_source_class`
- `409` for `invalid_knowledge_lineage`, `knowledge_idempotency_conflict`

Important:

- direct file ingestion is legacy-only and must be explicitly acknowledged by the caller
- accepted ingestion remains unpublished and non-searchable
- this endpoint does not create searchable source versions, anchors, or chunks
- `customer_uploaded_document` is forbidden from shared-corpus ingestion semantics
- this and all other management or mutation routes require `X-Auth-Context` for an internal `Administrator` principal
- legacy direct-import file lineage remains supported for historical compatibility, but it is no longer the preferred publication provenance shape

---

### POST /knowledge/ingestion/urls

Summary: persist one governed official-source URL ingestion job into `knowledge_ingestion_jobs`.

Request body:

- `requested_by` (`string`, required): UUID for the requesting actor
- `idempotency_key` (`string`, required): replay-safe request key
- `url` (`string`, required): non-empty `http` or `https` URL
- `source_input_origin` (`string`, optional): when provided, must be `official_source_url`
- `source_class` (`string`, optional): one of `tax_law`, `regulation`, `guidance`, `commentary`

Response:

- stable ingestion envelope with the same fields as file ingestion

Error responses:

- `400` for `invalid_knowledge_request`, `unsupported_source_input_origin`, `unsupported_source_class`
- `409` for `invalid_knowledge_lineage`, `knowledge_idempotency_conflict`

Important:

- URLs are normalized deterministically before provenance and checksuming
- accepted ingestion remains unpublished and non-searchable
- `customer_uploaded_document` is forbidden from shared-corpus ingestion semantics

---

### Internal Authorization

Protected routes require `X-Auth-Context` carrying the shared canonical auth-context JSON envelope. In the current runtime:

- search, retrieve, timeline retrieval, ingestion, review, publication, metadata correction, source and anchor management, supersession, archive, and bulk lifecycle routes require an internal `Administrator` principal
- missing auth context fails closed with `401 auth_context_missing`
- invalid or forbidden role or tenant context fails closed with canonical shared authz errors
- app startup verifies that intended protected routes still require the internal `Administrator` dependency and that only `/healthz`, `/readyz`, and unsupported-scope catch-all routes remain outside that protected boundary

This slice intentionally reuses the shared internal auth-context boundary instead of introducing a separate auth subsystem inside the knowledge service.

---

### Management and Hardening Notes

The management surface is deterministic and governance-safe:

- `/knowledge/ingestion` lists non-searchable ingestion jobs with stable pagination and sorting
- `/knowledge/source-versions` lists lifecycle state for governed source versions
- `/knowledge/sources` and `/knowledge/sources/{source_id}` expose summary and detail visibility for shared-corpus source records
- `/knowledge/anchors/{anchor_id}` exposes anchor and chunk summary visibility only
- raw unpublished chunk bodies are not exposed through management detail routes
- `/knowledge/ingestion/{ingestion_job_id}/metadata-correction` is limited to editable unpublished review-stage ingestion jobs and only for a narrow set of mutable publication fields
- protected retrieval requests fail closed when query or identifier inputs exceed deterministic boundary limits

The runtime now emits deterministic audit events for:

- file and URL ingestion
- bulk file and URL ingestion
- review, approve, reject, and publish
- bulk reject, bulk publish, and bulk archive
- supersede and archive
- search, retrieve, and timeline retrieval

Retention behavior in this phase is intentionally conservative:

- published lineage required for governed retrieval and audit is preserved
- source detail exposes a `retention_summary` describing whether lineage is preserved and whether document lineage exists or has already been purged
- source detail also distinguishes:
  - historical compatibility lineage from older pre-normalized document refs
  - legacy direct-import lineage
  - URL-backed lineage
- destructive purge actions are not exposed by the runtime in this phase
- `purge_supported` is currently `false`; older published lineage remains visible through metadata even when source-document purge has occurred outside the service

Operational boundary notes for protected retrieval:

- retrieval routes are protected internal surfaces and are not approved as public browser-facing APIs
- runtime defensive-query limits are deterministic: `query` fields are capped at `512` characters and retrieval identifier arrays are capped at `50` items with per-item length caps
- unsupported scope paths continue to fail closed with `404 unsupported_knowledge_scope`
- retrieval ranking stays governance-first and does not expose unpublished, archived, lineage-broken, or customer-document-derived material

Hybrid retrieval remains governance-first:

- metadata, lineage, publication-state, and temporal filters gate the candidate set before ranking
- lexical ranking remains active even when embeddings are unavailable
- OpenAI-backed vector similarity is secondary and cannot bypass authority, lineage, publication-state, or temporal governance
- older published chunks created before embeddings were enabled continue to rank lexically until republished or backfilled

---

### GET /knowledge/ingestion/{ingestion_job_id}

Summary: fetch deterministic non-searchable ingestion metadata for one persisted official-source job.

Response:

- stable `status`, `service`, `correlation_id`, `trace_id`
- `result.ingestion_job_id`
- `result.document_id`
- `result.requested_by`
- `result.ingestion_state`
- `result.source_input_origin`
- `result.source_input_ref`
- `result.payload_checksum_sha256`
- `result.source_class`
- `result.extracted_metadata`
- `result.proposed_source_record`
- `result.review_notes`
- `result.completed_at`

Important:

- fetch is review-safe only and does not expose searchable publication artifacts unless the job has already been published
- the returned ingestion record itself is never a searchable retrieval artifact

---

### POST /knowledge/ingestion/{ingestion_job_id}/review

Summary: transition one ingested job into `review_pending` and persist governed review notes or proposed metadata refinement without publishing.

Request body:

- `reviewed_by` (`string`, required): UUID for the reviewing actor
- `review_notes` (`array[object]`, optional): deterministic structured review notes
- `proposed_source_updates` (`object`, optional): publication-safe metadata refinements

Error responses:

- `400` for `invalid_knowledge_request`
- `409` for `invalid_publication_state_transition`

---

### POST /knowledge/ingestion/{ingestion_job_id}/approve

Summary: validate one reviewed ingestion job for publication readiness and transition it into `approved`.

Request body:

- `reviewed_by` (`string`, required): UUID for the approving actor
- `review_notes` (`array[object]`, optional): deterministic structured approval notes
- `publication_payload` (`object`, required): governed publication-ready source, temporal, anchor, and chunk metadata

Error responses:

- `400` for `invalid_knowledge_request`, `invalid_authority_source_class_binding`, `invalid_effective_window_metadata`
- `409` for `invalid_knowledge_lineage`, `knowledge_publication_safety_rejected`, `invalid_publication_state_transition`

---

### POST /knowledge/ingestion/{ingestion_job_id}/reject

Summary: transition one reviewed or approved unpublished ingestion job into `rejected` while preserving fail-closed non-searchable behavior.

Request body:

- `reviewed_by` (`string`, required): UUID for the rejecting actor
- `review_notes` (`array[object]`, optional): deterministic structured rejection notes

Error responses:

- `400` for `invalid_knowledge_request`
- `409` for `invalid_publication_state_transition`

---

### POST /knowledge/ingestion/{ingestion_job_id}/publish

Summary: materialize one approved ingestion job into governed searchable `knowledge_sources`, `knowledge_source_versions`, `knowledge_anchors`, and `knowledge_chunks`.

Request body:

- `published_by` (`string`, required): UUID for the publishing actor

Error responses:

- `400` for `invalid_knowledge_request`, `invalid_authority_source_class_binding`, `invalid_effective_window_metadata`
- `409` for `invalid_knowledge_lineage`, `knowledge_publication_safety_rejected`, `invalid_publication_state_transition`

Important:

- publish requires an approved ingestion job and governed official-source lineage
- the publishing actor must be distinct from the approving reviewer
- repeated publish of the same already-published job is deterministic and replay-safe
- only successfully published items become visible to `/knowledge/search` and `/knowledge/retrieve`

---

### POST /knowledge/source-versions/{source_version_id}/supersede

Summary: transition one published source version into `superseded` using a governed successor from the same source family.

Request body:

- `successor_source_version_id` (`string`, required): UUID of the already-published successor version
- `superseded_by` (`string`, required): UUID for the acting user

Error responses:

- `400` for `invalid_knowledge_request`
- `409` for `knowledge_supersession_conflict`, `knowledge_temporal_scope_mismatch`, `knowledge_record_not_published`, `invalid_knowledge_lineage`, `invalid_publication_state_transition`

Important:

- predecessor and successor must share the governed source family under the current schema
- predecessor and successor effective windows must not overlap
- supersession is deterministic and replay-safe for the same predecessor/successor pair
- superseded records remain historically searchable only inside their own governed effective window

---

### POST /knowledge/source-versions/{source_version_id}/archive

Summary: transition one governed published or superseded source version into `archived`.

Request body:

- `archived_by` (`string`, required): UUID for the acting user

Error responses:

- `400` for `invalid_knowledge_request`
- `409` for `knowledge_record_not_published`, `invalid_publication_state_transition`, `invalid_knowledge_lineage`

Important:

- archived records are retained for governance and audit only
- archived records never appear in `/knowledge/search` or `/knowledge/retrieve`
- repeated archive of the same already-archived version is deterministic and replay-safe

---

## Business Rules and Constraints

### Searchable Records and Intake Jobs

- `/knowledge/search` and `/knowledge/retrieve` operate only on governed searchable records in persistent storage
- searchable states are restricted to `published` and historically valid `superseded` source versions
- `archived` source versions are never searchable
- `/knowledge/ingestion/files` and `/knowledge/ingestion/urls` create persistent ingestion jobs only
- `/knowledge/ingestion/{ingestion_job_id}` and its review mutation routes operate on non-searchable ingestion jobs, not retrieval artifacts
- ingestion jobs do not become searchable until successful governed publication materializes searchable source/version/anchor/chunk records
- rejected jobs remain permanently non-searchable unless later re-entered through a governed draft/review transition outside this service slice
- a later same-family effective version suppresses an earlier `superseded` version for present-effective search while preserving governed historical retrieval by effective date
- when the same governed family and effective window exist in both forms, `point_in_time_consolidation` is the preferred explanation and evidence form; `as_issued` remains the fallback when no consolidation exists
- timeline retrieval returns materially distinct effective windows chronologically instead of suppressing older historically applicable versions from multi-period results
- optional vector similarity never bypasses publication-state, lineage, tax-domain, authority, or effective-window controls
- OpenAI embeddings are stored per published chunk in `knowledge_chunk_embeddings`; older published chunks without stored embeddings fall back to lexical-only ranking until republished or backfilled

### Authority Rank

| Authority Level | Rank |
|----------------|------|
| `statute` | 0 |
| `regulation` | 1 |
| `guidance` | 2 |
| `commentary` | 3 |

### Effective Date Filtering

A searchable record is included when:

- `effective_from <= query_date`
- and `effective_to is null` or `query_date <= effective_to`

### Fail-Closed Scope Guard

Any request to `/v1/knowledge/{scope}/...` that does not match a registered route returns `404 unsupported_knowledge_scope`.

---

## Error Handling

### Error Response Format

```json
{
  "detail": {
    "error_code": "invalid_knowledge_request",
    "message": "Human-readable description",
    "reason": "invalid_knowledge_request",
    "reason_code": "invalid_knowledge_request",
    "correlation_id": "req-20260412-001",
    "trace_id": "a1b2c3d4"
  }
}
```

### HTTP Status Code Usage

| Code | When Used |
|------|-----------|
| `400` | Missing required fields, malformed filters, malformed URLs, unsupported MIME types, forbidden source origin for endpoint, invalid source class |
| `404` | Unsupported knowledge scope path |
| `409` | Ingestion lineage conflict, invalid lifecycle state transition, supersession conflict, temporal applicability conflict, publication safety rejection, or idempotency-key payload conflict |

---

## Security and Compliance

The Knowledge service is deterministic and fail-closed, and it should not be treated as a public frontend API surface.

Current runtime authorization model:

- `POST /knowledge/search`, `POST /knowledge/retrieve`, and `POST /knowledge/timeline/search` require the same internal `Administrator` authorization boundary as the rest of the governed service surface
- those retrieval routes are available for protected internal admin testing and governed operator workflows, not for public frontend consumption; user-facing knowledge access should go through `orchestration`
- ingestion, review, approval, rejection, publication, metadata correction, supersession, archive, management listing, and management detail routes require `X-Auth-Context`
- protected routes use the shared internal auth-context dependency with `allowed_roles={"Administrator"}` and `allow_delegation=False`
- missing auth context fails closed with canonical shared auth errors; invalid role or tenant context also fails closed
- administrator-facing write paths include the document-backed ingestion handoff and all shared-corpus lifecycle mutation routes
- internal admin tooling may expose protected knowledge workflows directly, but those experiences should be guided and non-technical for operators
- frontend route guards and hidden navigation are defense-in-depth only; backend `Administrator` authorization remains the authoritative enforcement boundary

Governance and data-handling constraints:

- ingestion stores provenance-safe intake records and keeps them non-searchable until explicit publication succeeds
- searchable publication states remain limited to `published` and historically valid `superseded`
- `archived`, `draft`, `review_pending`, `approved`, and `rejected` records do not surface through protected retrieval
- raw unpublished chunk bodies are not exposed through management detail routes
- destructive purge endpoints are intentionally absent in this runtime slice

CORS is configured for local development origins only.

---

## Runtime and Configuration Notes

### Core Runtime Shape

- `main.py` owns HTTP route registration, app construction, middleware, and canonical exception translation
- `repository.py` owns persistence, validation, lifecycle state transitions, search filtering, pagination, ranking inputs, and deterministic response shaping
- `embeddings.py` owns the OpenAI embedding provider plus cosine-similarity helpers used by governed hybrid retrieval
- `config.py` owns service constants and embedding-related environment-variable parsing
- file-backed shared-corpus publication should use document-backed ingestion handoff; raw file upload ingestion remains legacy-only

### OpenAI Embeddings and Hybrid Retrieval

Hybrid retrieval is optional. The service always keeps lexical search active and only adds vector scoring when an embedding provider can be built successfully.

Configured environment variables:

- `OPENAI_API_KEY`: required before the default OpenAI embedding provider can be constructed
- `OPENAI_BASE_URL`: defaults to `https://api.openai.com/v1`
- `KNOWLEDGE_OPENAI_EMBEDDING_MODEL`: defaults to `text-embedding-3-large`
- `KNOWLEDGE_OPENAI_EMBEDDING_TIMEOUT_SECONDS`: defaults to `15.0`
- `KNOWLEDGE_OPENAI_EMBEDDING_DIMENSIONS`: optional positive integer override when the upstream model supports it
- `KNOWLEDGE_HYBRID_VECTOR_WEIGHT`: defaults to `0.55`
- `KNOWLEDGE_HYBRID_LEXICAL_WEIGHT`: defaults to `0.45`
- `KNOWLEDGE_HYBRID_MIN_VECTOR_SIMILARITY`: defaults to `0.2`

Operational notes:

- if `OPENAI_API_KEY` is absent, the default embedding provider is not usable and the runtime falls back to governed lexical-only ranking
- vector scoring never bypasses metadata filters, publication-state gating, lineage checks, authority rank, or temporal filtering
- older published chunks without stored embeddings continue to rank lexically until republished or backfilled

### Canonical Repository Rules Contributors Should Know

- supported ingestion MIME types are `application/pdf`, `text/html`, `text/plain`, `text/markdown`, `application/vnd.openxmlformats-officedocument.wordprocessingml.document`, and `application/xml`
- supported source classes are `tax_law`, `regulation`, `guidance`, and `commentary`
- supported source document systems for new document-backed handoff are `storage_registered` only
- source-class-to-authority binding is fixed: `tax_law -> statute`, `regulation -> regulation`, `guidance -> guidance`, `commentary -> commentary`
- metadata correction is intentionally narrow and only allows editable unpublished review-stage fields such as `title`, `issuing_authority`, `point_in_time_url`, and `tax_year`
- approval is the canonical unpublished pre-publication state; older persisted `approved_for_publication` rows remain readable through compatibility normalization but new runtime output uses `approved`
- searchable publication states are fixed in the repository as `published` and `superseded`
- document-backed `storage_key` values must remain local storage keys rather than `http://`, `https://`, or other URL-style references
- new shared-corpus document-backed handoff intentionally excludes generic `document_ai` uploads because the shared `documents` table does not yet carry a trustworthy official-source-versus-customer-document classification marker
- historical compatibility rule: older published records using `document_ai://documents/{document_id}` remain readable and governable, but new file-backed ingestion must emit normalized `official-source-upload://...` references

### Data Classification and Logging Rules

- protected retrieval-safe fields: `source_id`, `source_version_id`, `anchor_id`, `title`, `url`, `authority_level`, `tax_domain`, `effective_from`, `effective_to`, `publication_state`
- internal operator-only provenance fields: `source_input_ref`, `payload_checksum_sha256`, `storage_key`, `document_id`, `source_document_system`, review notes, and lifecycle audit linkage
- restricted values that must not be emitted in public-facing logs or responses: raw file payloads, unpublished chunk bodies, raw auth-context headers, and any tenant-private document lineage
- audit events remain deterministic, but provenance identifiers and checksum-bearing metadata should be treated as internal governance evidence rather than user-facing display fields

### Contributor Workflow

When updating this service, read these files first:

- [main.py](/c:/Users/Lenovo/kodi-backend/services/knowledge/app/main.py)
- [repository.py](/c:/Users/Lenovo/kodi-backend/services/knowledge/app/repository.py)
- [embeddings.py](/c:/Users/Lenovo/kodi-backend/services/knowledge/app/embeddings.py)
- [config.py](/c:/Users/Lenovo/kodi-backend/services/knowledge/app/config.py)
- [knowledge.yaml](/c:/Users/Lenovo/kodi-backend/contracts/openapi/knowledge.yaml)

Most service behavior changes belong in the repository layer, not in the FastAPI handlers. Keep protected retrieval, internal governance workflows, and canonical error-envelope semantics aligned when making updates.

Operational source-of-truth documents for production ownership:

- [Knowledge Service Deployment and Recovery Runbook](/c:/Users/Lenovo/kodi-backend/docs/runbooks/knowledge-service-deployment-and-recovery.md)
- [Knowledge Service Data Operations Runbook](/c:/Users/Lenovo/kodi-backend/docs/runbooks/knowledge-service-data-operations.md)
- [Knowledge Service Retention and Archive Runbook](/c:/Users/Lenovo/kodi-backend/docs/runbooks/knowledge-service-retention-and-archive.md)
- [Knowledge Service Release Gates and Ownership](/c:/Users/Lenovo/kodi-backend/docs/runbooks/knowledge-service-release-gates-and-ownership.md)

---

## Integration Capabilities

### Consumers

| Service | Usage |
|---------|-------|
| `orchestration` | Grounds computation explanations in statutory authority |
| internal admin frontend | Supports guided administrator ingestion, review, publication, and lifecycle workflows |

### Current boundary limits

- no orchestration-triggered mutation workflow
- no approved direct public frontend integration target; end-user knowledge-backed flows should be mediated by orchestration
- no chunk-body detail endpoint for raw unpublished content exposure
- no anonymous public retrieval surface; protected admin retrieval remains subject to gateway, edge, and backend authorization controls

---

## Project Structure

```text
services/knowledge/app/
|-- main.py        # FastAPI app factory, search/retrieve/ingestion/review/publication/lifecycle handlers, health routes
|-- embeddings.py  # OpenAI embedding provider and cosine-similarity helpers for hybrid retrieval
|-- repository.py  # Persistent governed search/retrieve/ingestion/review/publication/lifecycle repository
`-- config.py      # Service constants and embedding/hybrid-retrieval environment parsing
```
