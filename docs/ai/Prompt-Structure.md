# Kodi Solutions AI Platform Prompt Structure

**Status:** Active prompt-writing standard

## Purpose

This document defines the structure for prompts that direct implementation work
in this repository. A good prompt makes the affected boundary, requirement,
evidence, and exclusions explicit before code changes begin. It must reflect
the repository's current code and checked-in contracts, not a planning phase or
an assumed future design.

## Required Prompt Sections

Use these sections in this order for implementation prompts. Combine sections
only for small documentation-only changes where a separate implementation plan
would add no useful precision.

### 1. Header

Name the Kodi work item and its outcome in one specific line.

```text
HEADER:
Kodi Solutions AI Platform - add idempotent report artifact download handling.
```

Avoid vague instructions such as "improve reports".

### 2. Requirement and Ownership

Identify the requirement or governance control, the owning service, and the
affected public or internal boundary. Do not use a roadmap or milestone as
completion evidence.

```text
REQUIREMENT AND OWNERSHIP:
Requirement/control: FR-RPT-005
Owning service: reports
Caller boundary: orchestration to reports
Contracts: contracts/openapi/reports.yaml
```

Use `Not applicable` only when the work genuinely has no requirement or
governance mapping, such as a narrowly scoped build repair.

### 3. Scope

State exactly what the prompt implements and its exclusions.

```text
SCOPE:
Implement only idempotent download-link issuance for an existing report
artifact.

In scope:
- Preserve the current report artifact metadata contract.
- Add focused service and contract coverage.

Out of scope:
- New report formats.
- Frontend UI changes.
- Changes to tax calculation policy.
```

Keep prompts small enough for one coherent change and one verification story.

### 4. Context to Read

List exact files the implementer must read before editing. Start with the
relevant service README, route or entry point, contract, shared control, and
nearest tests. Include the requirement or governance document when it defines
the behavior.

```text
CONTEXT TO READ:
- README.md
- services/reports/README.md
- services/reports/app/routes.py
- contracts/openapi/reports.yaml
- tests/test_reports_download_link_issuance.py
```

The code and checked-in contracts are the source of truth. Report a material
contradiction before changing behavior.

### 5. Non-Negotiable Constraints

State the constraints that protect Kodi's architecture and data.

```text
NON-NEGOTIABLE CONSTRAINTS:
- Start by inspecting `git status --short` and preserve unrelated changes.
- Keep browser clients on approved public boundaries.
- Preserve authorization, tenant isolation, correlation, audit, and retention
  controls.
- Preserve deterministic, version-bound tax results and replay behavior.
- Use the existing shared error, tracing, authorization, and idempotency
  helpers where applicable.
- Update the affected contract when an observable boundary changes.
```

Add controls specific to the work, such as a capability gate, knowledge source
lineage, document confidence requirement, or migration invariant.

### 6. Goal and Invariants

Describe the system-level outcome, then give independently testable invariants.
Each invariant should map to the named requirement or control.

```text
GOAL:
An authorized caller can obtain a stable download capability for an existing
report artifact without creating duplicate artifact state.

INVARIANTS:
1. An authorized request returns the established download capability envelope.
2. Repeating the same idempotent request does not create duplicate state.
3. A caller without the required principal receives the standard error envelope.
4. The response carries the request correlation identifier on failure.
5. Existing report generation behavior remains unchanged.
```

Use only invariants that can be proved by tests, contracts, or an explicit
operational check.

### 7. Files and Change Boundaries

List files expected to change and why. Separate optional files so scope growth
is visible.

```text
FILES EXPECTED TO CHANGE:
- services/reports/app/routes.py: enforce existing idempotency behavior.
- tests/test_reports_download_link_issuance.py: cover success and rejection.

FILES ONLY IF REQUIRED:
- contracts/openapi/reports.yaml: align an externally observable contract.
```

If a necessary file is not listed, inspect the reason before touching it. Add
it only when it is directly required to complete the stated scope, and explain
the change in the handoff.

### 8. Implementation Requirements

Group precise behavior by concern. State what must remain true; mandate an
implementation mechanism only when Kodi already requires one.

```text
IMPLEMENTATION REQUIREMENTS:

AUTHORIZATION:
- Resolve the principal through the established reports authorization boundary.
- Reject unauthorized access with the shared error envelope.

IDEMPOTENCY:
- Reuse the established idempotency contract for repeated requests.
- Preserve the original artifact and audit lineage.
```

Do not use soft language such as "try", "consider", or "where possible".
Do not direct an implementer to bypass a safety control for local convenience.

### 9. Tests and Validation

Specify observable positive, negative, determinism, and regression evidence.
Run focused checks first, then broader checks when the change affects a shared,
public, cross-service, migration, or policy boundary.

```text
TESTS AND VALIDATION:

Positive:
- An authorized request produces the expected existing envelope.

Negative:
- A missing or unauthorized principal is rejected with a stable error code.

Determinism:
- A repeated idempotent request produces no duplicate persisted state.

Regression:
- Existing report artifact generation tests continue to pass.

Commands:
- ruff check services/reports tests/test_reports_download_link_issuance.py
- ruff format --check services/reports tests/test_reports_download_link_issuance.py
- pyright
- pytest tests/test_reports_download_link_issuance.py
- git diff --check
- git status --short
```

Use `scripts/validate_contracts.sh` for contract changes. For deterministic tax
or orchestration changes, name the golden fixture under `eval/golden/` that must
remain stable or explain its intentional update.

### 10. Commit and Success Criteria

State the intended commit scope and the conditions for completion.

```text
COMMIT:
Stage the intended files explicitly, inspect `git diff --cached --name-only`,
and create one coherent commit.

SUCCESS CRITERIA:
- Every invariant is demonstrated by the listed evidence.
- No internal service becomes a normal browser-facing dependency.
- Focused validation passes and unrelated workspace changes are preserved.
- The commit uses the required subject, blank line, and wrapped body format.
```

Every commit message must follow
`docs/architecture/Technical-Specification.md`. Use the configured Git identity
without override and never include agent, AI-assistance, co-author, or tooling
authorship references.

## Prompt Quality Checklist

Before using a prompt, confirm that it names a specific requirement or control,
owning service, exact context files, explicit scope exclusions, testable
invariants, expected file boundaries, focused validation, and one coherent
commit. A prompt is incomplete when it leaves the public boundary, policy
version, authorization, audit evidence, or verification method ambiguous.
