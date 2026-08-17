"""Durable, provider-independent document processing operation controls."""

from __future__ import annotations

from uuid import UUID
from typing import cast
from typing import Literal
from dataclasses import replace
from dataclasses import dataclass

from services.document_ai.app.persistence_support import connect_document_ai_database

ProcessingOperationKind = Literal[
    "source_inspection",
    "general_document_understanding",
    "canonical_assembly",
    "embedding_generation",
    "targeted_evidence_derivation",
    "reprocessing",
    "correction_invalidation",
    "purge",
    "reconciliation",
    "legacy_migration",
]
ProcessingOperationState = Literal["queued", "running", "succeeded", "failed", "cancelled"]

_OPERATION_KINDS: frozenset[str] = frozenset(
    {
        "source_inspection", "general_document_understanding", "canonical_assembly",
        "embedding_generation", "targeted_evidence_derivation", "reprocessing",
        "correction_invalidation", "purge", "reconciliation", "legacy_migration",
    }
)
_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})
_TRANSITIONS: dict[ProcessingOperationState, frozenset[ProcessingOperationState]] = {
    "queued": frozenset({"running", "cancelled"}),
    "running": frozenset({"succeeded", "failed", "cancelled"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


class ProcessingOperationError(ValueError):
    """Represent a standard operation validation or state-transition rejection."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ProcessingOperation:
    """One accepted logical request, distinct from its work and attempts."""

    processing_operation_id: UUID
    tenant_id: str
    document_id: UUID
    document_version_id: UUID
    operation_kind: ProcessingOperationKind
    state: ProcessingOperationState = "queued"
    cancellation_requested: bool = False
    result_reference: str | None = None
    failure_category: str | None = None


def load_processing_operation(
    *,
    database_url: str,
    tenant_id: str,
    document_id: UUID,
    processing_operation_id: UUID,
) -> ProcessingOperation | None:
    """Reload a durable operation only through its same-tenant document graph."""

    with connect_document_ai_database(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT operation.processing_operation_id, operation.tenant_id,
                       version.document_id, operation.document_version_id,
                       operation.operation_kind, operation.state,
                       operation.cancellation_requested_at IS NOT NULL,
                       operation.result_reference, operation.failure_category
                FROM document_ai_processing_operations AS operation
                JOIN document_ai_document_versions AS version
                  ON version.tenant_id = operation.tenant_id
                 AND version.document_version_id = operation.document_version_id
                WHERE operation.tenant_id = %s AND version.document_id = %s
                  AND operation.processing_operation_id = %s
                """,
                (tenant_id, document_id, processing_operation_id),
            )
            row = cursor.fetchone()
    if row is None:
        return None
    return ProcessingOperation(
        processing_operation_id=UUID(str(row[0])),
        tenant_id=str(row[1]),
        document_id=UUID(str(row[2])),
        document_version_id=UUID(str(row[3])),
        operation_kind=validate_operation_kind(str(row[4])),
        state=cast(ProcessingOperationState, str(row[5])),
        cancellation_requested=bool(row[6]),
        result_reference=str(row[7]) if row[7] is not None else None,
        failure_category=str(row[8]) if row[8] is not None else None,
    )


def validate_operation_kind(operation_kind: str) -> ProcessingOperationKind:
    """Reject provider-, workflow-, and document-type-shaped operation names."""

    if operation_kind not in _OPERATION_KINDS:
        raise ProcessingOperationError("invalid_processing_operation_kind")
    return cast(ProcessingOperationKind, operation_kind)


def request_cancellation(operation: ProcessingOperation) -> ProcessingOperation:
    """Persist a request separately; a running operation is not falsely cancelled."""

    if operation.state in _TERMINAL_STATES:
        raise ProcessingOperationError("processing_operation_terminal")
    return replace(operation, cancellation_requested=True)


def transition_operation(
    operation: ProcessingOperation,
    requested_state: ProcessingOperationState,
    *,
    result_reference: str | None = None,
    failure_category: str | None = None,
) -> ProcessingOperation:
    """Apply a guarded transition without permitting stale terminal overwrites."""

    if operation.state in _TERMINAL_STATES:
        raise ProcessingOperationError("processing_operation_terminal")
    if requested_state not in _TRANSITIONS[operation.state]:
        raise ProcessingOperationError("invalid_processing_operation_transition")
    if operation.cancellation_requested and requested_state == "succeeded":
        raise ProcessingOperationError("processing_operation_cancellation_requested")
    if requested_state == "succeeded" and not result_reference:
        raise ProcessingOperationError("processing_operation_result_required")
    if requested_state == "failed" and not failure_category:
        raise ProcessingOperationError("processing_operation_failure_category_required")
    return replace(
        operation,
        state=requested_state,
        result_reference=result_reference if requested_state == "succeeded" else None,
        failure_category=failure_category if requested_state == "failed" else None,
    )
