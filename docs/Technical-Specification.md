# Kodi Solutions AI Platform Technical Specification

**Status:** Maintained implementation standard

## 1. Purpose and Scope

This specification defines the working conventions for the Kodi Solutions AI
Platform repository. It supplements the system and security architecture with
the standards used to implement, test, document, and commit changes.

The platform is a Python service monorepo with a TypeScript React frontend. It
provides governed tax workflows, deterministic policy computation, knowledge
retrieval, document processing, form and report generation, and auditable
evidence. It is not a collection of independent public APIs: `auth` and
`orchestration` are the normal client boundaries.

## 2. Repository Architecture

| Location | Responsibility |
| --- | --- |
| `services/` | FastAPI service implementations and service-specific runtime code |
| `shared/` | Reusable authorization, errors, tracing, logging, validation, and determinism controls |
| `database/` | SQL schema baseline, migrations, and persistence models |
| `contracts/` | OpenAPI, event, capability, tool, and UAT contracts |
| `tests/` | Unit, integration, contract, and end-to-end regression coverage |
| `eval/` | Golden deterministic and workflow evaluation fixtures |
| `frontend/` | React and TypeScript client application |
| `docs/` | Architecture, requirements, governance, operations, and release evidence |

Services are `gateway`, `auth`, `orchestration`, `tax_core`, `knowledge`,
`document_ai`, `forms`, `reports`, `storage`, `event_store`, and
`validation`.
Read the root README and the relevant service README before changing a service.

### 2.1 Local PostgreSQL for Development

Local development uses a Dockerized PostgreSQL instance named `kodi-postgres`
with the database `kodi_dev` exposed on host port `54329`. Start it with:

```bash
docker run -d \
  --name kodi-postgres \
  -e POSTGRES_USER=<db-user> \
  -e POSTGRES_PASSWORD=<db-password> \
  -e POSTGRES_DB=kodi_dev \
  -p 54329:5432 \
  postgres:15-alpine
```

If the container already exists, reuse it with:

```bash
docker start kodi-postgres
```

Stop it with:

```bash
docker stop kodi-postgres
```

Verify that it is running with:

```bash
docker ps --filter name=kodi-postgres
```

The project expects local database credentials from the shell environment or a
local `.env` file. Apply the migration stack with:

```bash
export DATABASE_URL="postgresql://<db-user>:<db-password>@localhost:54329/kodi_dev"
python shared/validation/db_migrate.py
```

## 3. Implementation Standards

### 3.1 Python

- Target Python 3.11 and use strict Pyright checking.
- Keep lines at or below 100 characters unless a generated or external format
  requires otherwise.
- Use Ruff with the repository configuration. Imports are single-name imports,
  sorted within their groups, and grouped as standard library, third-party, and
  local project imports.
- Use complete type annotations. Prefer Pydantic models, TypedDicts, protocols,
  and explicit domain types over unstructured dictionaries at boundaries.
- Keep each change within the owning service and use `shared/` helpers for
  common behavior. Do not duplicate authorization, error, correlation, logging,
  idempotency, or determinism logic in a service.
- Validate request inputs at the boundary. Fail safely with explicit,
  machine-readable error codes and the standard error envelope.
- Catch narrow exceptions. Re-raise, transform into a structured error, or log
  with useful context; never silently suppress an exception.
- Keep external effects idempotent where the contract requires it and preserve
  replayability for deterministic computation paths.

### 3.2 FastAPI and Service Boundaries

- Define request and response schemas explicitly. Reject unrecognized fields
  where the established Pydantic model uses `extra="forbid"`.
- Use shared principal and authorization helpers. Server-side checks are the
  only authority for protected operations.
- Preserve correlation IDs across service calls and return them in error
  envelopes using `shared/tracing` and `shared/errors`.
- Do not route browser clients directly to internal services. Keep `gateway`
  thin and keep workflow decisions in `orchestration`.
- Change the appropriate OpenAPI, event, or JSON schema contract with any
  externally observable boundary change.
- Add migrations rather than rewriting an applied migration. Keep database
  invariants enforced in the database when the existing model does so.

### 3.3 Tax, Knowledge, and AI Controls

- Tax calculations and validations belong in `tax_core` and must bind to the
  approved policy version and effective date.
- LLMs may classify, plan, or synthesize only inside governed orchestration.
  They cannot manufacture legal authority, tax results, provenance, or final
  workflow state.
- Production conversational semantics must never depend on keyword lists,
  regex heuristics, pronoun rewriting, or other brittle text shortcuts. If a
  conversational follow-up or classification path fails, fix the backend
  semantic implementation instead of reintroducing keyword-based logic.
- Knowledge responses require governed source lineage and must surface conflicts
  or uncertainty instead of choosing unsupported authority.
- Document extraction must retain confidence, verification, evidence linkage,
  lifecycle, and access controls.
- Capability gates, tenant allowlists, and kill switches are safety controls;
  do not bypass them in application code or tests.

### 3.4 Frontend

- Use TypeScript, React, and the existing API, hooks, store, type, and style
  conventions under `frontend/src`.
- Treat `auth` and `orchestration` as the default client APIs. Do not expose
  backend secrets or make internal service URLs production frontend dependencies.
- Normalize API errors using the established client utilities and preserve
  correlation information for support and diagnosis.
- Add focused Vitest coverage for changed user-visible behavior.

## 4. Testing and Validation

Run the narrowest relevant checks while developing, then expand validation for
shared, public, cross-service, migration, or policy changes.

```bash
ruff check .
ruff format --check .
pyright
pytest tests
```

Use `scripts/validate_contracts.sh` for contract changes. Preserve or update
the relevant golden fixture in `eval/golden/` when a deterministic tax or
orchestration outcome intentionally changes. Do not rewrite a golden result to
hide a regression; document the requirement or policy decision that warrants it.

When a production integration has both a live provider path and a mock-backed
unit path, keep the live success-path test in a dedicated integration test and
keep mock tests only for transport, failure, or invariant branches. Do not
retain a duplicate mock success test once the live integration coverage exists.

Tests are a contract for the backend, not a target to force with fixture edits.
When a test reveals a real behavioral mismatch, adjust the tested code or the
explicit contract, then update the test to reflect the corrected behavior. Do
not "fix" a failing test by masking the backend issue with keyword shortcuts,
forced mocks, or other changes that preserve the wrong behavior.

## 5. Documentation and Traceability

Keep documents in the folder matching their purpose: architecture,
requirements, governance, operations, integration, product, release,
remediation, tax policy, or UAT. Update references when moving a document.

Use the established functional requirement IDs in commit bodies when a change
closes or addresses a requirement. Implementation status is demonstrated by
code, contracts, migrations, and tests, not planning documents alone.

## 6. Commit Messages

Every commit must use this exact structure:

```text
type(scope): short description in present tense

Body explaining what changed and why. Not what the code does line by line,
but why the change was made, what problem it solves, what was broken before,
or what decision was taken. Use as many lines as needed. Wrap at 72
characters per line.
```

Allowed types are `feat`, `fix`, `test`, `refactor`, `docs`, and `chore`.
Scopes are lowercase, hyphenated ownership areas such as `auth`, `tax-core`,
`document-ai`, `orchestration`, `knowledge`, `forms`, or `reports`. The subject
is lowercase after the scope, uses present tense, has no final stop, and is at
most 72 characters. The body is mandatory, follows exactly one blank line, and
uses lines of at most 72 characters.

When a commit closes or addresses a requirement, include its identifier in the
body, for example `closes FR-CALC-001`. Commit messages must not contain agent,
AI-assistance, co-author, or authorship-tool references. Use the configured Git
identity and do not override it.
