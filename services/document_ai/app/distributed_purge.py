"""Durable, replayable deletion of every document representation.

The database is the purge ledger; providers only execute individual, idempotent
targets.  A document is never marked purged until the complete manifest has
been reconciled successfully.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID
from uuid import uuid4
from typing import Protocol
from dataclasses import replace
from dataclasses import dataclass


class PurgeTargetKind(StrEnum):
    DOCUMENT_STATE = "document_state"
    VERSIONS = "versions"
    R2_ORIGINAL = "r2_original"
    R2_DERIVED = "r2_derived"
    CANONICAL_CONTENT = "canonical_content"
    CHUNKS = "chunks"
    VECTORS = "vectors"
    EVIDENCE = "evidence"
    PROJECTIONS = "projections"
    CACHES = "caches"
    PROVIDER_FILES = "provider_files"
    TEMPORARY_ARTIFACTS = "temporary_artifacts"
    MIGRATION_COPIES = "migration_copies"


REQUIRED_PURGE_TARGET_KINDS = frozenset(PurgeTargetKind)


class PurgeTargetState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class PurgeTarget:
    kind: PurgeTargetKind
    reference: str
    state: PurgeTargetState = PurgeTargetState.PENDING
    failure_detail: str | None = None


@dataclass(frozen=True)
class PurgeOperation:
    purge_operation_id: UUID
    tenant_id: str
    document_id: UUID
    state: str
    targets: tuple[PurgeTarget, ...]


class PurgeError(ValueError):
    """A purge request or completion violates a lifecycle invariant."""


class PurgeTargetExecutor(Protocol):
    def delete(self, *, tenant_id: str, document_id: UUID, target: PurgeTarget) -> None:
        """Delete or invalidate one target; absence must be accepted."""

    def is_resolved(self, *, tenant_id: str, document_id: UUID, target: PurgeTarget) -> bool:
        """Verify deletion independently of a provider success response."""

        ...


def create_purge_operation(
    *, tenant_id: str, document_id: UUID, targets: tuple[PurgeTarget, ...]
) -> PurgeOperation:
    """Create a complete manifest before any destructive provider call."""

    kinds = {target.kind for target in targets}
    missing = REQUIRED_PURGE_TARGET_KINDS - kinds
    if missing:
        raise PurgeError("purge_manifest_missing_required_targets")
    if len({(target.kind, target.reference) for target in targets}) != len(targets):
        raise PurgeError("purge_manifest_duplicate_target")
    return PurgeOperation(uuid4(), tenant_id, document_id, "requested", targets)


def reconcile_purge(
    *, operation: PurgeOperation, executor: PurgeTargetExecutor
) -> PurgeOperation:
    """Run all unresolved targets and complete only after verified resolution.

    A failed target remains durable and retryable.  This makes database restore
    safe: replaying the same manifest simply verifies or deletes again.
    """

    if operation.state == "completed":
        return operation
    reconciled: list[PurgeTarget] = []
    for target in operation.targets:
        if target.state is PurgeTargetState.COMPLETED:
            reconciled.append(target)
            continue
        try:
            executor.delete(
                tenant_id=operation.tenant_id,
                document_id=operation.document_id,
                target=target,
            )
            if not executor.is_resolved(
                tenant_id=operation.tenant_id, document_id=operation.document_id, target=target
            ):
                raise PurgeError("purge_target_not_verified")
        except Exception as error:  # provider faults must retain retry state
            reconciled.append(
                replace(target, state=PurgeTargetState.FAILED, failure_detail=str(error))
            )
        else:
            reconciled.append(
                replace(target, state=PurgeTargetState.COMPLETED, failure_detail=None)
            )
    state = (
        "completed"
        if all(target.state is PurgeTargetState.COMPLETED for target in reconciled)
        else "running"
    )
    return replace(operation, state=state, targets=tuple(reconciled))


def may_mark_document_purged(operation: PurgeOperation) -> bool:
    """The sole completion gate for a document lifecycle purged transition."""

    return operation.state == "completed" and all(
        target.state is PurgeTargetState.COMPLETED for target in operation.targets
    )
