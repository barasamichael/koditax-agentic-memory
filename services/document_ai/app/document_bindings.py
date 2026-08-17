"""Durable, tenant-scoped document bindings for conversations and workflows."""

from __future__ import annotations

from uuid import UUID
from uuid import uuid5
from uuid import NAMESPACE_URL
from typing import cast
from typing import Literal
from typing import Protocol
from datetime import UTC
from datetime import datetime

import psycopg
from pydantic import Field
from pydantic import BaseModel
from pydantic import model_validator

from services.document_ai.app.persistence_support import connect_document_ai_database
from services.document_ai.app.persistence_support import execute_document_ai_database_transaction

DocumentBindingRole = Literal[
    "conversation_attachment",
    "current_turn_attachment",
    "existing_library_document",
    "workflow_reference",
]


class DocumentBindingRequest(BaseModel):
    """Describe one approved logical document binding."""

    document_id: UUID
    document_version_id: UUID | None = None
    binding_role: DocumentBindingRole
    conversation_id: str | None = Field(default=None, min_length=1, max_length=255)
    turn_id: str | None = Field(default=None, min_length=1, max_length=255)
    workflow_id: str | None = Field(default=None, min_length=1, max_length=255)
    attachment_order: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_target(self) -> DocumentBindingRequest:
        conversation_binding = self.conversation_id is not None
        workflow_binding = self.workflow_id is not None
        if conversation_binding == workflow_binding:
            raise ValueError("exactly one conversation or workflow target is required")
        if self.turn_id is not None and self.conversation_id is None:
            raise ValueError("turn_id requires conversation_id")
        if self.binding_role == "conversation_attachment":
            if (
                self.conversation_id is None
                or self.turn_id is not None
                or self.attachment_order is not None
            ):
                raise ValueError("conversation_attachment requires only conversation_id")
        elif self.binding_role in {"current_turn_attachment", "existing_library_document"}:
            if (
                self.conversation_id is None
                or self.turn_id is None
                or self.attachment_order is None
            ):
                raise ValueError(
                    "turn attachment requires conversation_id, turn_id, and attachment_order"
                )
        elif self.binding_role == "workflow_reference":
            if self.workflow_id is None or self.attachment_order is not None:
                raise ValueError("workflow_reference requires only workflow_id")
        return self


class DocumentBindingRecord(BaseModel):
    """Represent a persisted binding without any storage-provider locator."""

    document_binding_id: UUID
    tenant_id: str
    document_id: UUID
    document_version_id: UUID | None = None
    resolved_document_version_id: UUID | None = None
    binding_role: DocumentBindingRole
    conversation_id: str | None = None
    turn_id: str | None = None
    workflow_id: str | None = None
    attachment_order: int | None = None
    bound_by_user_id: UUID
    bound_at: str
    correlation_id: str


class DocumentBindingEnvelope(BaseModel):
    """Return one binding creation result without exposing storage internals."""

    status: Literal["ok"] = "ok"
    binding: DocumentBindingRecord


class DocumentBindingListEnvelope(BaseModel):
    """Return deterministic bindings for one authorized target."""

    status: Literal["ok"] = "ok"
    bindings: list[DocumentBindingRecord]


class DocumentBindingConflictError(ValueError):
    """Represent deterministic binding idempotency or version failures."""

    def __init__(self, reason: str, details: dict[str, object]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details


class DocumentBindingStoreProtocol(Protocol):
    """Define persistence required by the binding service boundary."""

    def create(
        self,
        *,
        tenant_id: str,
        actor_user_id: UUID,
        request: DocumentBindingRequest,
        correlation_id: str,
    ) -> DocumentBindingRecord:
        """Create or replay one logical binding."""
        ...

    def list_for_target(
        self,
        *,
        tenant_id: str,
        actor_user_id: UUID,
        conversation_id: str | None,
        turn_id: str | None,
        workflow_id: str | None,
    ) -> list[DocumentBindingRecord]:
        """Return active bindings in deterministic attachment order."""
        ...


class InMemoryDocumentBindingStore:
    """Provide deterministic test-only binding storage."""

    def __init__(self) -> None:
        self._records: dict[tuple[object, ...], DocumentBindingRecord] = {}

    def create(
        self,
        *,
        tenant_id: str,
        actor_user_id: UUID,
        request: DocumentBindingRequest,
        correlation_id: str,
    ) -> DocumentBindingRecord:
        key = _binding_key(tenant_id=tenant_id, request=request)
        record = self._records.get(key)
        if record is not None:
            return record
        binding_id = uuid5(NAMESPACE_URL, ":".join(str(item) for item in key))
        record = DocumentBindingRecord(
            document_binding_id=binding_id,
            tenant_id=tenant_id,
            document_id=request.document_id,
            document_version_id=request.document_version_id,
            binding_role=request.binding_role,
            conversation_id=request.conversation_id,
            turn_id=request.turn_id,
            workflow_id=request.workflow_id,
            attachment_order=request.attachment_order,
            bound_by_user_id=actor_user_id,
            bound_at=datetime.now(UTC).isoformat(),
            correlation_id=correlation_id,
        )
        self._records[key] = record
        return record

    def list_for_target(
        self,
        *,
        tenant_id: str,
        actor_user_id: UUID,
        conversation_id: str | None,
        turn_id: str | None,
        workflow_id: str | None,
    ) -> list[DocumentBindingRecord]:
        records = [
            record
            for record in self._records.values()
            if record.tenant_id == tenant_id
            and record.bound_by_user_id == actor_user_id
            and record.conversation_id == conversation_id
            and record.turn_id == turn_id
            and record.workflow_id == workflow_id
        ]
        return sorted(records, key=_binding_sort_key)


class PersistentDocumentBindingStore:
    """Persist bindings in PostgreSQL and resolve implicit active versions on read."""

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def create(
        self,
        *,
        tenant_id: str,
        actor_user_id: UUID,
        request: DocumentBindingRequest,
        correlation_id: str,
    ) -> DocumentBindingRecord:
        try:
            row = execute_document_ai_database_transaction(
                database_url=self._database_url,
                transaction_name="document_ai.document_bindings.create",
                transaction_callback=lambda cursor: self._create_binding_transaction(
                    cursor=cursor,
                    tenant_id=tenant_id,
                    actor_user_id=actor_user_id,
                    request=request,
                    correlation_id=correlation_id,
                ),
                reconcile_ambiguous_result=lambda connection: self._reconcile_create_result(
                    connection=connection,
                    tenant_id=tenant_id,
                    request=request,
                    actor_user_id=actor_user_id,
                ),
            )
        except psycopg.Error as error:
            raise RuntimeError("document_ai_binding_persistence_unavailable") from error
        if row is None:
            raise RuntimeError("document_ai_binding_missing_after_create")
        return row

    def _create_binding_transaction(
        self,
        *,
        cursor: object,
        tenant_id: str,
        actor_user_id: UUID,
        request: DocumentBindingRequest,
        correlation_id: str,
    ) -> DocumentBindingRecord:
        cursor.execute(
            """
            SELECT 1 FROM document_ai_document_versions
            WHERE tenant_id = %s AND document_id = %s AND document_version_id = %s
            """,
            (tenant_id, request.document_id, request.document_version_id),
        )
        if request.document_version_id is not None and cursor.fetchone() is None:
            raise DocumentBindingConflictError(
                "document_version_not_owned_by_document",
                {"document_id": str(request.document_id)},
            )
        cursor.execute(
            """
            INSERT INTO document_ai_document_bindings (
                tenant_id, document_id, document_version_id, binding_kind,
                binding_scope,
                binding_role, conversation_id, turn_id, workflow_id, attachment_order,
                bound_by_user_id, correlation_id
            ) VALUES (
                %s, %s, %s, 'durable_document_binding', %s, %s, %s,
                %s, %s, %s, %s, %s
            )
            ON CONFLICT DO NOTHING
            RETURNING document_binding_id, document_id, document_version_id,
                      binding_role, conversation_id, turn_id, workflow_id, attachment_order,
                      bound_by_user_id, bound_at, correlation_id
            """,
            (
                tenant_id,
                request.document_id,
                request.document_version_id,
                _binding_scope(request),
                request.binding_role,
                request.conversation_id,
                request.turn_id,
                request.workflow_id,
                request.attachment_order,
                actor_user_id,
                correlation_id,
            ),
        )
        row = cursor.fetchone()
        if row is None:
            cursor.execute(
                """
                SELECT document_binding_id, document_id, document_version_id,
                       binding_role, conversation_id, turn_id, workflow_id, attachment_order,
                       bound_by_user_id, bound_at, correlation_id
                FROM document_ai_document_bindings
                WHERE tenant_id = %s AND document_id = %s
                  AND document_version_id IS NOT DISTINCT FROM %s
                  AND binding_role = %s
                  AND conversation_id IS NOT DISTINCT FROM %s
                  AND turn_id IS NOT DISTINCT FROM %s
                  AND workflow_id IS NOT DISTINCT FROM %s
                  AND revoked_at IS NULL
                """,
                (
                    tenant_id,
                    request.document_id,
                    request.document_version_id,
                    request.binding_role,
                    request.conversation_id,
                    request.turn_id,
                    request.workflow_id,
                ),
            )
            row = cursor.fetchone()
        if row is None:
            raise RuntimeError("document_ai_binding_missing_after_create")
        return _row_to_record(tenant_id, row)

    def _reconcile_create_result(
        self,
        *,
        connection: object,
        tenant_id: str,
        request: DocumentBindingRequest,
        actor_user_id: UUID,
    ) -> DocumentBindingRecord | None:
        del actor_user_id
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT document_binding_id, document_id, document_version_id,
                       binding_role, conversation_id, turn_id, workflow_id, attachment_order,
                       bound_by_user_id, bound_at, correlation_id
                FROM document_ai_document_bindings
                WHERE tenant_id = %s AND document_id = %s
                  AND document_version_id IS NOT DISTINCT FROM %s
                  AND binding_role = %s
                  AND conversation_id IS NOT DISTINCT FROM %s
                  AND turn_id IS NOT DISTINCT FROM %s
                  AND workflow_id IS NOT DISTINCT FROM %s
                  AND revoked_at IS NULL
                """,
                (
                    tenant_id,
                    request.document_id,
                    request.document_version_id,
                    request.binding_role,
                    request.conversation_id,
                    request.turn_id,
                    request.workflow_id,
                ),
            )
            row = cursor.fetchone()
        return None if row is None else _row_to_record(tenant_id, row)

    def list_for_target(
        self,
        *,
        tenant_id: str,
        actor_user_id: UUID,
        conversation_id: str | None,
        turn_id: str | None,
        workflow_id: str | None,
    ) -> list[DocumentBindingRecord]:
        try:
            with connect_document_ai_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT binding.document_binding_id, binding.document_id,
                               binding.document_version_id,
                               COALESCE(binding.document_version_id,
                                        document.active_document_version_id),
                               binding.binding_role, binding.conversation_id, binding.turn_id,
                               binding.workflow_id, binding.attachment_order,
                               binding.bound_by_user_id, binding.bound_at, binding.correlation_id
                        FROM document_ai_document_bindings AS binding
                        JOIN document_ai_documents AS document
                          ON document.tenant_id = binding.tenant_id
                         AND document.document_id = binding.document_id
                        WHERE binding.tenant_id = %s
                          AND binding.bound_by_user_id = %s
                          AND binding.conversation_id IS NOT DISTINCT FROM %s
                          AND binding.turn_id IS NOT DISTINCT FROM %s
                          AND binding.workflow_id IS NOT DISTINCT FROM %s
                          AND binding.revoked_at IS NULL
                          AND document.state IN ('uploaded', 'processing', 'validated')
                        ORDER BY binding.attachment_order NULLS LAST, binding.bound_at,
                                 binding.document_binding_id
                        """,
                        (tenant_id, actor_user_id, conversation_id, turn_id, workflow_id),
                    )
                    rows = cursor.fetchall()
        except psycopg.Error as error:
            raise RuntimeError("document_ai_binding_persistence_unavailable") from error
        return [_row_to_record(tenant_id, row) for row in rows]


def _binding_key(*, tenant_id: str, request: DocumentBindingRequest) -> tuple[object, ...]:
    return (
        tenant_id,
        request.document_id,
        request.document_version_id,
        request.binding_role,
        request.conversation_id,
        request.turn_id,
        request.workflow_id,
    )


def _binding_scope(request: DocumentBindingRequest) -> str:
    if request.workflow_id is not None:
        return f"workflow:{request.workflow_id}"
    if request.turn_id is not None:
        return f"conversation:{request.conversation_id}:turn:{request.turn_id}"
    return f"conversation:{request.conversation_id}"


def _binding_sort_key(record: DocumentBindingRecord) -> tuple[int, str, str]:
    return (
        record.attachment_order if record.attachment_order is not None else 2**31 - 1,
        record.bound_at,
        str(record.document_binding_id),
    )


def _row_to_record(tenant_id: str, row: tuple[object, ...]) -> DocumentBindingRecord:
    resolved_version = row[3] if len(row) == 12 else None
    offset = 1 if len(row) == 12 else 0
    return DocumentBindingRecord(
        document_binding_id=UUID(str(row[0])),
        tenant_id=tenant_id,
        document_id=UUID(str(row[1])),
        document_version_id=UUID(str(row[2])) if row[2] is not None else None,
        resolved_document_version_id=UUID(str(resolved_version))
        if resolved_version is not None
        else None,
        binding_role=cast(DocumentBindingRole, str(row[3 + offset])),
        conversation_id=str(row[4 + offset]) if row[4 + offset] is not None else None,
        turn_id=str(row[5 + offset]) if row[5 + offset] is not None else None,
        workflow_id=str(row[6 + offset]) if row[6 + offset] is not None else None,
        attachment_order=_optional_integer(row[7 + offset]),
        bound_by_user_id=UUID(str(row[8 + offset])),
        bound_at=_iso_timestamp(row[9 + offset]),
        correlation_id=str(row[10 + offset]),
    )


def _iso_timestamp(value: object) -> str:
    if not isinstance(value, datetime):
        raise RuntimeError("document_ai_binding_invalid_timestamp")
    return value.astimezone(UTC).isoformat()


def _optional_integer(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int):
        raise RuntimeError("document_ai_binding_invalid_attachment_order")
    return value
