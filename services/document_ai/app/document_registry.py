"""Deterministic document metadata persistence for upload-completion registration."""

from __future__ import annotations

import json
from uuid import UUID
from uuid import uuid5
from uuid import NAMESPACE_URL
from typing import Any
from typing import cast
from typing import Literal
from typing import Protocol
from typing import TypedDict
from typing import LiteralString
from hashlib import sha256
from datetime import UTC
from datetime import datetime

import psycopg
from psycopg import sql
from pydantic import Field
from pydantic import BaseModel

from shared.determinism.input_hash import compute_canonical_hash
from services.document_ai.app.config import MAX_UPLOAD_SIZE_BYTES
from services.document_ai.app.config import is_valid_checksum_sha256
from services.document_ai.app.config import ALLOWED_UPLOAD_MIME_TYPES
from services.document_ai.app.config import is_allowed_upload_mime_type
from services.document_ai.app.config import is_within_upload_size_limit
from services.document_ai.app.config import get_document_ai_processing_max_attempts
from services.document_ai.app.config import ARCHITECTURE_DEFINED_UNSUPPORTED_MIME_TYPES
from services.document_ai.app.config import get_document_ai_processing_max_retry_elapsed_seconds
from services.document_ai.app.storage_keys import build_tenant_document_object_key
from services.document_ai.app.upload_sessions import UploadSessionRecord
from services.document_ai.app.upload_sessions import UploadSessionTraceability
from services.document_ai.app.document_lifecycle import DocumentLifecycleState
from services.document_ai.app.document_lifecycle import DocumentLifecycleActionError
from services.document_ai.app.document_lifecycle import enforce_execute_purge_action
from services.document_ai.app.document_lifecycle import enforce_document_trash_action
from services.document_ai.app.document_lifecycle import enforce_document_restore_action
from services.document_ai.app.document_lifecycle import enforce_document_state_transition
from services.document_ai.app.document_lifecycle import enforce_mark_eligible_for_purge_action
from services.document_ai.app.document_foundation import SourceArtifactCreate
from services.document_ai.app.document_foundation import InMemorySourceArtifactStore
from services.document_ai.app.document_foundation import SourceArtifactStoreProtocol
from services.document_ai.app.persistence_support import connect_document_ai_database
from services.document_ai.app.persistence_support import execute_document_ai_database_transaction


class UploadCompletionRequest(BaseModel):
    """Represent upload completion registration request payload."""

    session_id: UUID | None = None
    upload_session_id: UUID | None = None
    object_key: str = Field(min_length=1, max_length=512)
    checksum_sha256: str = Field(min_length=1, max_length=128)
    size_bytes: int = Field(ge=1)
    content_type: str = Field(min_length=1, max_length=127)

    def resolved_session_id(self) -> UUID:
        """Resolve canonical session id from supported request aliases."""

        if self.session_id is not None:
            return self.session_id
        if self.upload_session_id is not None:
            return self.upload_session_id
        raise ValueError("session_id or upload_session_id is required")


class PersistedDocumentRecord(BaseModel):
    """Represent canonical persisted document metadata."""

    document_id: UUID
    tenant_id: str
    owner_user_id: UUID
    state: DocumentLifecycleState = "uploaded"
    storage_key: str
    uploaded_at: str
    checksum_sha256: str
    size_bytes: int
    content_type: str
    computation_id: str | None = None
    purge_eligible_at: str | None = None
    purged_at: str | None = None
    compliance_lock_until: str | None = None
    display_name: str | None = None
    category: str | None = None
    tags: list[str] = Field(default_factory=list)
    description: str | None = None
    revision: int = 0


class DocumentRecord(BaseModel):
    """Represent canonical public document metadata payload."""

    document_id: UUID
    state: DocumentLifecycleState = "uploaded"
    uploaded_at: str
    computation_id: str | None = None
    purge_eligible_at: str | None = None
    purged_at: str | None = None
    compliance_lock_until: str | None = None
    display_name: str | None = None
    category: str | None = None
    tags: list[str] = Field(default_factory=list)
    description: str | None = None
    revision: int = 0


class DocumentRecordEnvelope(BaseModel):
    """Represent canonical upload-completion success envelope."""

    status: Literal["ok"] = "ok"
    duplicate_detected: bool = False
    document: DocumentRecord
    processing_operation_id: UUID | None = None
    traceability: UploadSessionTraceability


class DocumentListEnvelope(BaseModel):
    """Represent canonical scoped document-list success envelope."""

    status: Literal["ok"] = "ok"
    documents: list[DocumentRecord]
    traceability: UploadSessionTraceability


class DocumentMetadataUpdateRequest(BaseModel):
    """Allow only user-facing metadata, guarded by the document revision."""

    model_config = {"extra": "forbid"}
    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    category: str | None = Field(default=None, min_length=1, max_length=100)
    tags: list[str] | None = Field(default=None, max_length=32)
    description: str | None = Field(default=None, max_length=2000)
    expected_revision: int = Field(ge=0)

    def fields_to_update(self) -> dict[str, object]:
        return self.model_dump(exclude={"expected_revision"}, exclude_none=True)


DocumentRetentionAction = Literal[
    "trash",
    "restore",
    "mark_eligible_for_purge",
    "execute_purge",
]


class MarkEligibleForPurgeRequest(BaseModel):
    """Represent explicit purge-eligibility marking request payload."""

    purge_eligible_at: str = Field(min_length=1, max_length=64)


class ExecutePurgeRequest(BaseModel):
    """Represent explicit purge execution request payload."""

    purged_at: str | None = Field(default=None, min_length=1, max_length=64)


class CompletionConflictError(ValueError):
    """Represent deterministic idempotency/document conflict for completion calls."""

    def __init__(self, reason: str, details: dict[str, object]) -> None:
        super().__init__(reason)
        self.reason = reason
        self._details = details

    def details(self) -> dict[str, object]:
        """Return deterministic conflict details payload."""

        return self._details


class CompletionValidationError(ValueError):
    """Represent deterministic upload-completion validation failure."""

    def __init__(self, reason: str, message: str, details: dict[str, object]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.message = message
        self.details = details


class DocumentRetentionActionError(ValueError):
    """Represent deterministic document-retention action rejection."""

    def __init__(self, *, reason: str, message: str, details: dict[str, object]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.message = message
        self.details = details


class DocumentMetadataConflictError(ValueError):
    """Reject a stale or incompatible metadata command deterministically."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CompletionIdempotencyRecord(TypedDict):
    """Represent one stored idempotency completion record."""

    idempotency_key: str
    request_fingerprint: str
    response_payload: dict[str, object]


class DocumentRegistryStoreProtocol(Protocol):
    """Define deterministic completion persistence store contract."""

    def get_completion(self, idempotency_key: str) -> CompletionIdempotencyRecord | None:
        """Lookup completion record by idempotency key."""

        ...

    def set_completion(self, record: CompletionIdempotencyRecord) -> None:
        """Persist completion idempotency record."""

        ...

    def get_document(self, document_id: UUID) -> PersistedDocumentRecord | None:
        """Lookup document metadata record."""

        ...

    def get_document_for_scope(
        self,
        document_id: UUID,
        tenant_id: str,
        owner_user_id: UUID,
    ) -> PersistedDocumentRecord | None:
        """Lookup one document record constrained to tenant/owner scope."""

        ...

    def list_documents_for_scope(
        self,
        tenant_id: str,
        owner_user_id: UUID,
        state: DocumentLifecycleState | None = None,
        uploaded_from: datetime | None = None,
        uploaded_to: datetime | None = None,
        computation_id: str | None = None,
    ) -> list[PersistedDocumentRecord]:
        """List document records constrained to tenant/owner scope."""

        ...

    def set_document(self, document_record: PersistedDocumentRecord) -> None:
        """Persist document metadata record."""

        ...

    def get_document_by_scope_hash(
        self,
        tenant_id: str,
        owner_user_id: UUID,
        checksum_sha256: str,
    ) -> PersistedDocumentRecord | None:
        """Lookup document by duplicate detection scope key."""

        ...

    def clear(self) -> None:
        """Clear all persisted registry records."""

        ...


class InMemoryDocumentRegistryStore:
    """Provide deterministic in-memory document registry storage."""

    def __init__(self) -> None:
        self._completion_records: dict[str, CompletionIdempotencyRecord] = {}
        self._documents: dict[str, PersistedDocumentRecord] = {}
        self._scope_hash_index: dict[str, str] = {}

    def get_completion(self, idempotency_key: str) -> CompletionIdempotencyRecord | None:
        return self._completion_records.get(idempotency_key)

    def set_completion(self, record: CompletionIdempotencyRecord) -> None:
        self._completion_records[record["idempotency_key"]] = record

    def get_document(self, document_id: UUID) -> PersistedDocumentRecord | None:
        return self._documents.get(str(document_id))

    def get_document_for_scope(
        self,
        document_id: UUID,
        tenant_id: str,
        owner_user_id: UUID,
    ) -> PersistedDocumentRecord | None:
        document = self.get_document(document_id)
        if document is None:
            return None
        if document.tenant_id != tenant_id:
            return None
        if document.owner_user_id != owner_user_id:
            return None
        return document

    def list_documents_for_scope(
        self,
        tenant_id: str,
        owner_user_id: UUID,
        state: DocumentLifecycleState | None = None,
        uploaded_from: datetime | None = None,
        uploaded_to: datetime | None = None,
        computation_id: str | None = None,
    ) -> list[PersistedDocumentRecord]:
        scoped_records = [
            document
            for document in self._documents.values()
            if document.tenant_id == tenant_id and document.owner_user_id == owner_user_id
        ]
        if state is not None:
            scoped_records = [document for document in scoped_records if document.state == state]
        if computation_id is not None:
            scoped_records = [
                document for document in scoped_records if document.computation_id == computation_id
            ]
        if uploaded_from is not None:
            scoped_records = [
                document
                for document in scoped_records
                if _parse_uploaded_at(document.uploaded_at) >= uploaded_from
            ]
        if uploaded_to is not None:
            scoped_records = [
                document
                for document in scoped_records
                if _parse_uploaded_at(document.uploaded_at) <= uploaded_to
            ]
        return sorted(
            scoped_records,
            key=lambda item: (item.uploaded_at, str(item.document_id)),
        )

    def set_document(self, document_record: PersistedDocumentRecord) -> None:
        self._documents[str(document_record.document_id)] = document_record
        self._scope_hash_index[
            _build_duplicate_scope_key(
                tenant_id=document_record.tenant_id,
                owner_user_id=document_record.owner_user_id,
                checksum_sha256=document_record.checksum_sha256,
            )
        ] = str(document_record.document_id)

    def get_document_by_scope_hash(
        self,
        tenant_id: str,
        owner_user_id: UUID,
        checksum_sha256: str,
    ) -> PersistedDocumentRecord | None:
        indexed_document_id = self._scope_hash_index.get(
            _build_duplicate_scope_key(
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                checksum_sha256=checksum_sha256,
            )
        )
        if indexed_document_id is None:
            return None
        return self._documents.get(indexed_document_id)

    def clear(self) -> None:
        self._completion_records.clear()
        self._documents.clear()
        self._scope_hash_index.clear()


class PersistentDocumentRegistryStore:
    """Persist deterministic document registry state to PostgreSQL."""

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    @property
    def database_url(self) -> str:
        """Expose the configured connection target to the transaction boundary."""

        return self._database_url

    def get_completion(self, idempotency_key: str) -> CompletionIdempotencyRecord | None:
        try:
            with connect_document_ai_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT idempotency_key, request_fingerprint, response_payload
                        FROM document_ai_completion_idempotency
                        WHERE idempotency_key = %s
                        """,
                        (idempotency_key,),
                    )
                    row = cursor.fetchone()
        except psycopg.Error as error:
            raise RuntimeError("document_ai_document_registry_persistence_unavailable") from error
        if row is None:
            return None
        return CompletionIdempotencyRecord(
            idempotency_key=str(row[0]),
            request_fingerprint=str(row[1]),
            response_payload=dict(row[2]),
        )

    def set_completion(self, record: CompletionIdempotencyRecord) -> None:
        try:
            with connect_document_ai_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO document_ai_completion_idempotency (
                            idempotency_key,
                            request_fingerprint,
                            response_payload
                        )
                        VALUES (%s, %s, %s::jsonb)
                        ON CONFLICT (idempotency_key) DO UPDATE SET
                            request_fingerprint = EXCLUDED.request_fingerprint,
                            response_payload = EXCLUDED.response_payload
                        """,
                        (
                            record["idempotency_key"],
                            record["request_fingerprint"],
                            json.dumps(record["response_payload"], sort_keys=True),
                        ),
                    )
                    connection.commit()
        except psycopg.Error as error:
            raise RuntimeError("document_ai_document_registry_persistence_unavailable") from error

    def get_document(self, document_id: UUID) -> PersistedDocumentRecord | None:
        try:
            with connect_document_ai_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            document_id,
                            tenant_id,
                            owner_user_id,
                            state,
                            storage_key,
                            uploaded_at,
                            checksum_sha256,
                            size_bytes,
                            content_type,
                            computation_id,
                            purge_eligible_at,
                            purged_at,
                            compliance_lock_until,
                            display_name,
                            category,
                            tags,
                            description,
                            revision
                        FROM document_ai_documents
                        WHERE document_id = %s
                        """,
                        (document_id,),
                    )
                    row = cursor.fetchone()
        except psycopg.Error as error:
            raise RuntimeError("document_ai_document_registry_persistence_unavailable") from error
        return _row_to_document_record(row)

    def get_document_for_scope(
        self,
        document_id: UUID,
        tenant_id: str,
        owner_user_id: UUID,
    ) -> PersistedDocumentRecord | None:
        document = self.get_document(document_id)
        if document is None:
            return None
        if document.tenant_id != tenant_id or document.owner_user_id != owner_user_id:
            return None
        return document

    def list_documents_for_scope(
        self,
        tenant_id: str,
        owner_user_id: UUID,
        state: DocumentLifecycleState | None = None,
        uploaded_from: datetime | None = None,
        uploaded_to: datetime | None = None,
        computation_id: str | None = None,
    ) -> list[PersistedDocumentRecord]:
        filters: list[str] = ["tenant_id = %s", "owner_user_id = %s"]
        params: list[object] = [tenant_id, owner_user_id]
        if state is not None:
            filters.append("state = %s")
            params.append(state)
        if uploaded_from is not None:
            filters.append("uploaded_at >= %s")
            params.append(uploaded_from)
        if uploaded_to is not None:
            filters.append("uploaded_at <= %s")
            params.append(uploaded_to)
        if computation_id is not None:
            filters.append("computation_id = %s")
            params.append(computation_id)
        query = f"""
            SELECT
                document_id,
                tenant_id,
                owner_user_id,
                state,
                storage_key,
                uploaded_at,
                checksum_sha256,
                size_bytes,
                content_type,
                computation_id,
                purge_eligible_at,
                purged_at,
                compliance_lock_until,
                display_name,
                category,
                tags,
                description,
                revision
            FROM document_ai_documents
            WHERE {" AND ".join(filters)}
            ORDER BY uploaded_at ASC, document_id ASC
        """
        try:
            with connect_document_ai_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(sql.SQL(cast(LiteralString, query)), tuple(params))
                    rows = cursor.fetchall()
        except psycopg.Error as error:
            raise RuntimeError("document_ai_document_registry_persistence_unavailable") from error
        return [record for row in rows if (record := _row_to_document_record(row)) is not None]

    def set_document(self, document_record: PersistedDocumentRecord) -> None:
        try:
            with connect_document_ai_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO document_ai_documents (
                            document_id,
                            tenant_id,
                            owner_user_id,
                            state,
                            storage_key,
                            uploaded_at,
                            checksum_sha256,
                            size_bytes,
                            content_type,
                            computation_id,
                            purge_eligible_at,
                            purged_at,
                            compliance_lock_until,
                            display_name,
                            category,
                            tags,
                            description,
                            revision
                        )
                        VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        ON CONFLICT (document_id) DO UPDATE SET
                            tenant_id = EXCLUDED.tenant_id,
                            owner_user_id = EXCLUDED.owner_user_id,
                            state = EXCLUDED.state,
                            storage_key = EXCLUDED.storage_key,
                            uploaded_at = EXCLUDED.uploaded_at,
                            checksum_sha256 = EXCLUDED.checksum_sha256,
                            size_bytes = EXCLUDED.size_bytes,
                            content_type = EXCLUDED.content_type,
                            computation_id = EXCLUDED.computation_id,
                            purge_eligible_at = EXCLUDED.purge_eligible_at,
                            purged_at = EXCLUDED.purged_at,
                            compliance_lock_until = EXCLUDED.compliance_lock_until,
                            display_name = EXCLUDED.display_name,
                            category = EXCLUDED.category,
                            tags = EXCLUDED.tags,
                            description = EXCLUDED.description,
                            revision = EXCLUDED.revision
                        """,
                        (
                            document_record.document_id,
                            document_record.tenant_id,
                            document_record.owner_user_id,
                            document_record.state,
                            document_record.storage_key,
                            _parse_iso_datetime(document_record.uploaded_at),
                            document_record.checksum_sha256,
                            document_record.size_bytes,
                            document_record.content_type,
                            document_record.computation_id,
                            _parse_iso_datetime_or_none(document_record.purge_eligible_at),
                            _parse_iso_datetime_or_none(document_record.purged_at),
                            _parse_iso_datetime_or_none(document_record.compliance_lock_until),
                            document_record.display_name,
                            document_record.category,
                            document_record.tags,
                            document_record.description,
                            document_record.revision,
                        ),
                    )
                    # The document-ai registry is the service-local projection,
                    # while the core documents table is the durable cross-service
                    # lineage record consumed by other bounded contexts.
                    cursor.execute(
                        """
                        INSERT INTO documents (
                            id, user_id, computation_id, storage_key, state,
                            uploaded_at, purge_eligible_at, purged_at, compliance_lock_until
                        )
                        VALUES (%s, %s, NULL, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET
                            user_id = EXCLUDED.user_id,
                            storage_key = EXCLUDED.storage_key,
                            state = EXCLUDED.state,
                            uploaded_at = EXCLUDED.uploaded_at,
                            purge_eligible_at = EXCLUDED.purge_eligible_at,
                            purged_at = EXCLUDED.purged_at,
                            compliance_lock_until = EXCLUDED.compliance_lock_until
                        """,
                        (
                            document_record.document_id,
                            document_record.owner_user_id,
                            document_record.storage_key,
                            (
                                "eligible_for_purge"
                                if document_record.state in {"trashed", "purge_pending"}
                                else document_record.state
                            ),
                            _parse_iso_datetime(document_record.uploaded_at),
                            _parse_iso_datetime_or_none(document_record.purge_eligible_at),
                            _parse_iso_datetime_or_none(document_record.purged_at),
                            _parse_iso_datetime_or_none(document_record.compliance_lock_until),
                        ),
                    )
                    connection.commit()
        except psycopg.Error as error:
            raise RuntimeError("document_ai_document_registry_persistence_unavailable") from error

    def get_document_by_scope_hash(
        self,
        tenant_id: str,
        owner_user_id: UUID,
        checksum_sha256: str,
    ) -> PersistedDocumentRecord | None:
        try:
            with connect_document_ai_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            document_id,
                            tenant_id,
                            owner_user_id,
                            state,
                            storage_key,
                            uploaded_at,
                            checksum_sha256,
                            size_bytes,
                            content_type,
                            computation_id,
                            purge_eligible_at,
                            purged_at,
                            compliance_lock_until
                        FROM document_ai_documents
                        WHERE tenant_id = %s
                          AND owner_user_id = %s
                          AND checksum_sha256 = %s
                        """,
                        (tenant_id, owner_user_id, checksum_sha256),
                    )
                    row = cursor.fetchone()
        except psycopg.Error as error:
            raise RuntimeError("document_ai_document_registry_persistence_unavailable") from error
        return _row_to_document_record(row)

    def clear(self) -> None:
        try:
            with connect_document_ai_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("DELETE FROM document_ai_completion_idempotency")
                    cursor.execute("DELETE FROM document_ai_documents")
                    connection.commit()
        except psycopg.Error as error:
            raise RuntimeError("document_ai_document_registry_persistence_unavailable") from error


_DEFAULT_DOCUMENT_REGISTRY_STORE = InMemoryDocumentRegistryStore()
_DEFAULT_SOURCE_ARTIFACT_STORE = InMemorySourceArtifactStore()


def get_default_document_registry_store() -> InMemoryDocumentRegistryStore:
    """Return default document registry store."""

    return _DEFAULT_DOCUMENT_REGISTRY_STORE


def get_default_source_artifact_store() -> SourceArtifactStoreProtocol:
    """Return the non-production source-artifact authority."""

    return _DEFAULT_SOURCE_ARTIFACT_STORE


def reset_default_document_registry_store() -> None:
    """Reset default document registry store for test isolation."""

    _DEFAULT_DOCUMENT_REGISTRY_STORE.clear()


def to_document_record(document_record: PersistedDocumentRecord) -> DocumentRecord:
    """Project persisted document metadata into canonical public document view."""

    return DocumentRecord(
        document_id=document_record.document_id,
        state=document_record.state,
        uploaded_at=document_record.uploaded_at,
        computation_id=document_record.computation_id,
        purge_eligible_at=document_record.purge_eligible_at,
        purged_at=document_record.purged_at,
        compliance_lock_until=document_record.compliance_lock_until,
        display_name=document_record.display_name,
        category=document_record.category,
        tags=document_record.tags,
        description=document_record.description,
        revision=document_record.revision,
    )


def _parse_uploaded_at(uploaded_at: str) -> datetime:
    parsed = datetime.fromisoformat(uploaded_at.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def update_document_metadata(
    *,
    document_record: PersistedDocumentRecord,
    request: DocumentMetadataUpdateRequest,
    idempotency_key: str,
    correlation_id: str,
    document_registry_store: DocumentRegistryStoreProtocol,
) -> DocumentRecordEnvelope:
    """Persist a replay-safe, revision-guarded update of user-facing metadata only."""

    command_key = f"document-metadata:{document_record.document_id}:{idempotency_key}"
    fingerprint = compute_canonical_hash(
        {"document_id": str(document_record.document_id), **request.model_dump(mode="json")}
    ).sha256_hex
    existing = document_registry_store.get_completion(command_key)
    if existing is not None:
        if existing["request_fingerprint"] != fingerprint:
            raise DocumentMetadataConflictError("idempotency_key_payload_mismatch")
        return DocumentRecordEnvelope.model_validate(existing["response_payload"])
    if document_record.state in {"trashed", "purge_pending", "purged", "eligible_for_purge"}:
        raise DocumentMetadataConflictError("document_lifecycle_blocks_mutation")
    if document_record.revision != request.expected_revision:
        raise DocumentMetadataConflictError("stale_document_revision")
    updated = document_record.model_copy(
        update={**request.fields_to_update(), "revision": document_record.revision + 1}
    )
    document_registry_store.set_document(updated)
    trace_id = sha256(
        f"{correlation_id}:metadata:{updated.document_id}:{updated.revision}".encode()
    ).hexdigest()
    response = DocumentRecordEnvelope(
        document=to_document_record(updated),
        traceability=UploadSessionTraceability(trace_id=trace_id, correlation_id=correlation_id),
    )
    document_registry_store.set_completion(
        {
            "idempotency_key": command_key,
            "request_fingerprint": fingerprint,
            "response_payload": response.model_dump(mode="json"),
        }
    )
    return response


def apply_document_retention_action(
    *,
    action: DocumentRetentionAction,
    document_record: PersistedDocumentRecord,
    principal_user_id: UUID,
    correlation_id: str,
    document_registry_store: DocumentRegistryStoreProtocol,
    compliance_override_granted: bool = False,
    purge_eligible_at: str | None = None,
    purged_at: str | None = None,
) -> DocumentRecordEnvelope:
    """Apply deterministic trash/restore action under lifecycle and lock constraints."""

    if document_record.owner_user_id != principal_user_id:
        raise DocumentRetentionActionError(
            reason="owner_user_mismatch",
            message="Document ownership context does not match authenticated principal.",
            details={
                "owner_user_id": str(document_record.owner_user_id),
                "principal_user_id": str(principal_user_id),
            },
        )

    update_fields: dict[str, object] = {}
    try:
        if action == "trash":
            target_state = enforce_document_trash_action(
                current_state=document_record.state,
                compliance_lock_until=document_record.compliance_lock_until,
                compliance_override_granted=compliance_override_granted,
            )
            update_fields["state"] = target_state
            update_fields["purge_eligible_at"] = None
            update_fields["purged_at"] = None
        elif action == "restore":
            target_state = enforce_document_restore_action(
                current_state=document_record.state,
                compliance_lock_until=document_record.compliance_lock_until,
                compliance_override_granted=compliance_override_granted,
            )
            update_fields["state"] = target_state
            update_fields["purge_eligible_at"] = None
            update_fields["purged_at"] = None
        elif action == "mark_eligible_for_purge":
            target_state, resolved_purge_eligible_at = enforce_mark_eligible_for_purge_action(
                current_state=document_record.state,
                compliance_lock_until=document_record.compliance_lock_until,
                purge_eligible_at=purge_eligible_at,
                uploaded_at=document_record.uploaded_at,
                compliance_override_granted=compliance_override_granted,
            )
            update_fields["state"] = target_state
            update_fields["purge_eligible_at"] = resolved_purge_eligible_at
            update_fields["purged_at"] = None
        else:
            target_state, resolved_purged_at = enforce_execute_purge_action(
                current_state=document_record.state,
                compliance_lock_until=document_record.compliance_lock_until,
                purge_eligible_at=document_record.purge_eligible_at,
                purged_at=purged_at,
                compliance_override_granted=compliance_override_granted,
            )
            update_fields["state"] = target_state
            update_fields["purged_at"] = resolved_purged_at
    except DocumentLifecycleActionError as error:
        requested_state = (
            error.requested_state if error.requested_state is not None else document_record.state
        )
        raise DocumentRetentionActionError(
            reason=error.reason,
            message=error.message,
            details={
                "action": action,
                "current_state": error.current_state,
                "requested_state": requested_state,
                "document_id": str(document_record.document_id),
            },
        ) from error

    updated_document = document_record.model_copy(update=update_fields)
    document_registry_store.set_document(updated_document)
    trace_id = sha256(
        (
            f"{correlation_id}:{action}:{updated_document.document_id}:{updated_document.state}:"
            f"{updated_document.purge_eligible_at}:{updated_document.purged_at}"
        ).encode()
    ).hexdigest()
    return DocumentRecordEnvelope(
        status="ok",
        duplicate_detected=False,
        document=to_document_record(updated_document),
        traceability=UploadSessionTraceability(trace_id=trace_id, correlation_id=correlation_id),
    )


def register_upload_completion(
    upload_completion_request: UploadCompletionRequest,
    session_record: UploadSessionRecord,
    principal_user_id: UUID,
    idempotency_key: str,
    correlation_id: str,
    document_registry_store: DocumentRegistryStoreProtocol,
    source_artifact_store: SourceArtifactStoreProtocol | None = None,
) -> DocumentRecordEnvelope:
    """Persist deterministic document metadata for upload completion."""

    _validate_completion_metadata(upload_completion_request, session_record)

    request_fingerprint = _build_completion_request_fingerprint(
        upload_completion_request=upload_completion_request,
        session_record=session_record,
        principal_user_id=principal_user_id,
    )

    existing_completion = document_registry_store.get_completion(idempotency_key)
    if existing_completion is not None:
        if existing_completion["request_fingerprint"] != request_fingerprint:
            raise CompletionConflictError(
                reason="idempotency_key_payload_mismatch",
                details={
                    "idempotency_key": idempotency_key,
                    "conflict_field": "request_fingerprint",
                },
            )
        return DocumentRecordEnvelope.model_validate(existing_completion["response_payload"])

    uploaded_at = _utc_now_iso()
    source_artifact_store = source_artifact_store or _DEFAULT_SOURCE_ARTIFACT_STORE
    duplicate_detected = False
    if session_record.storage_key is None:
        raise CompletionValidationError(
            reason="missing_session_storage_key",
            message="Upload session is missing its reserved storage key.",
            details={"session_id": str(session_record.session_id)},
        )
    expected_object_key = session_record.storage_key
    existing_document = document_registry_store.get_document(session_record.document_id)
    if existing_document is None:
        document_record = PersistedDocumentRecord(
            document_id=session_record.document_id,
            tenant_id=session_record.tenant_id,
            owner_user_id=session_record.owner_user_id,
            state="uploaded",
            storage_key=expected_object_key,
            uploaded_at=uploaded_at,
            checksum_sha256=upload_completion_request.checksum_sha256,
            size_bytes=upload_completion_request.size_bytes,
            content_type=upload_completion_request.content_type,
        )
        document_registry_store.set_document(document_record)
    else:
        _assert_document_metadata_match(
            existing_document=existing_document,
            upload_completion_request=upload_completion_request,
            session_record=session_record,
        )
        document_record = existing_document

    if not duplicate_detected:
        transitioned_state = enforce_document_state_transition(
            current_state=document_record.state,
            requested_state="processing",
        )
        if transitioned_state != document_record.state:
            document_record = document_record.model_copy(update={"state": transitioned_state})
            document_registry_store.set_document(document_record)

        source_artifact_store.register_source_artifact(
            document_id=document_record.document_id,
            idempotency_key=idempotency_key,
            record=SourceArtifactCreate(
                tenant_id=document_record.tenant_id,
                document_version_id=uuid5(
                    NAMESPACE_URL,
                    f"document-ai:{document_record.tenant_id}:"
                    f"{document_record.document_id}:{idempotency_key}",
                ),
                storage_key=expected_object_key,
                checksum_sha256=upload_completion_request.checksum_sha256,
                content_type=upload_completion_request.content_type,
                size_bytes=upload_completion_request.size_bytes,
                integrity_state="verified",
                retention_state="active",
            ),
        )

    trace_id = sha256(f"{correlation_id}:{request_fingerprint}".encode()).hexdigest()
    response_payload = DocumentRecordEnvelope(
        status="ok",
        duplicate_detected=duplicate_detected,
        document=DocumentRecord(
            document_id=document_record.document_id,
            state=document_record.state,
            uploaded_at=document_record.uploaded_at,
        ),
        traceability=UploadSessionTraceability(
            trace_id=trace_id,
            correlation_id=correlation_id,
        ),
    ).model_dump(mode="json")
    document_registry_store.set_completion(
        CompletionIdempotencyRecord(
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            response_payload=response_payload,
        )
    )
    return DocumentRecordEnvelope.model_validate(response_payload)


def register_durable_upload_confirmation(
    upload_completion_request: UploadCompletionRequest,
    session_record: UploadSessionRecord,
    principal_user_id: UUID,
    idempotency_key: str,
    correlation_id: str,
    document_registry_store: DocumentRegistryStoreProtocol,
) -> DocumentRecordEnvelope:
    """Atomically register the accepted-document graph and durable processing work.

    This is the authoritative persistent confirmation path for FR-001, FR-002,
    FR-018, and FR-019.  In-memory stores retain the legacy deterministic path
    used by isolated unit tests; they are not an accepted production boundary.
    """

    if not isinstance(document_registry_store, PersistentDocumentRegistryStore):
        return register_upload_completion(
            upload_completion_request=upload_completion_request,
            session_record=session_record,
            principal_user_id=principal_user_id,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            document_registry_store=document_registry_store,
        )

    _validate_completion_metadata(upload_completion_request, session_record)
    request_fingerprint = _build_completion_request_fingerprint(
        upload_completion_request=upload_completion_request,
        session_record=session_record,
        principal_user_id=principal_user_id,
    )
    trace_id = sha256(f"{correlation_id}:{request_fingerprint}".encode()).hexdigest()
    response_payload = execute_document_ai_database_transaction(
        database_url=document_registry_store.database_url,
        transaction_name="document_ai.upload_completion.confirmation",
        transaction_callback=lambda cursor: _register_persistent_confirmation_transaction(
            cursor=cursor,
            upload_completion_request=upload_completion_request,
            session_record=session_record,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            correlation_id=correlation_id,
            trace_id=trace_id,
        ),
        reconcile_ambiguous_result=lambda connection: _reconcile_persistent_confirmation_result(
            connection=connection,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        ),
    )
    return DocumentRecordEnvelope.model_validate(response_payload)


def _register_persistent_confirmation_transaction(
    *,
    cursor: psycopg.Cursor[object],
    upload_completion_request: UploadCompletionRequest,
    session_record: UploadSessionRecord,
    idempotency_key: str,
    request_fingerprint: str,
    correlation_id: str,
    trace_id: str,
) -> dict[str, object]:
    """Persist confirmation state, operation, work, and outbox in one commit."""

    cursor.execute(
        """
        SELECT session_record
        FROM document_ai_upload_sessions
        WHERE session_id = %s
        FOR UPDATE
        """,
        (session_record.session_id,),
    )
    current_session_row = cursor.fetchone()
    if current_session_row is None:
        raise CompletionValidationError(
            reason="session_not_found",
            message="Upload session was not found.",
            details={"session_id": str(session_record.session_id)},
        )
    current_session_record = UploadSessionRecord.model_validate(
        cast(dict[str, object], dict(current_session_row[0]))
    )
    if current_session_record.model_dump(mode="json") != session_record.model_dump(mode="json"):
        raise CompletionConflictError(
            reason="session_state_mismatch",
            details={"session_id": str(session_record.session_id)},
        )

    cursor.execute(
        """
        SELECT request_fingerprint, response_payload
        FROM document_ai_completion_idempotency
        WHERE idempotency_key = %s
        FOR UPDATE
        """,
        (idempotency_key,),
    )
    prior_completion = cursor.fetchone()
    if prior_completion is not None:
        if str(prior_completion[0]) != request_fingerprint:
            raise CompletionConflictError(
                reason="idempotency_key_payload_mismatch",
                details={
                    "idempotency_key": idempotency_key,
                    "conflict_field": "request_fingerprint",
                },
            )
        return cast(dict[str, object], dict(prior_completion[1]))

    cursor.execute(
        """
        SELECT document_id, uploaded_at, state
        FROM document_ai_documents
        WHERE tenant_id = %s AND document_id = %s
        FOR UPDATE
        """,
        (session_record.tenant_id, session_record.document_id),
    )
    existing_document = cursor.fetchone()
    existing_version: tuple[object, ...] | None = None
    if existing_document is None:
        uploaded_at = _utc_now_iso()
        cursor.execute(
            """
            INSERT INTO document_ai_documents (
                document_id, tenant_id, owner_user_id, state, storage_key,
                uploaded_at, checksum_sha256, size_bytes, content_type
            ) VALUES (%s, %s, %s, 'processing', %s, %s, %s, %s, %s)
            """,
            (
                session_record.document_id,
                session_record.tenant_id,
                session_record.owner_user_id,
                upload_completion_request.object_key,
                _parse_iso_datetime(uploaded_at),
                upload_completion_request.checksum_sha256,
                upload_completion_request.size_bytes,
                upload_completion_request.content_type,
            ),
        )
        cursor.execute(
            """
            INSERT INTO documents (
                id, user_id, computation_id, storage_key, state, uploaded_at
            )
            VALUES (%s, %s, NULL, %s, 'processing', %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (
                session_record.document_id,
                session_record.owner_user_id,
                upload_completion_request.object_key,
                _parse_iso_datetime(uploaded_at),
            ),
        )
    else:
        uploaded_at = _to_iso_utc(existing_document[1])
        cursor.execute(
            """
            SELECT version.document_version_id
            FROM document_ai_document_versions AS version
            JOIN document_ai_source_artifacts AS artifact
              ON artifact.tenant_id = version.tenant_id
             AND artifact.document_version_id = version.document_version_id
            WHERE version.tenant_id = %s AND version.document_id = %s
              AND artifact.storage_key = %s AND artifact.checksum_sha256 = %s
            ORDER BY version.created_at ASC
            LIMIT 1
            """,
            (
                session_record.tenant_id,
                session_record.document_id,
                upload_completion_request.object_key,
                upload_completion_request.checksum_sha256,
            ),
        )
        row = cursor.fetchone()
        existing_version = cast(tuple[object, ...] | None, row)
        if existing_version is None:
            raise CompletionConflictError(
                reason="document_metadata_mismatch",
                details={"document_id": str(session_record.document_id)},
            )

    if existing_document is None:
        version_id = uuid5(
            NAMESPACE_URL,
            f"document-ai:{session_record.tenant_id}:{session_record.document_id}:"
            f"{idempotency_key}",
        )
        cursor.execute(
            """
            INSERT INTO document_ai_document_versions (
                document_version_id, tenant_id, document_id, version_number,
                version_state, idempotency_key
            ) VALUES (%s, %s, %s, 1, 'current', %s)
            """,
            (
                version_id,
                session_record.tenant_id,
                session_record.document_id,
                idempotency_key,
            ),
        )
        cursor.execute(
            """
            INSERT INTO document_ai_source_artifacts (
                tenant_id, document_version_id, storage_key, checksum_sha256,
                checksum_algorithm, verified_media_type, content_type, size_bytes,
                retention_state, integrity_state
            ) VALUES (%s, %s, %s, %s, 'sha256', %s, %s, %s, 'active', 'verified')
            """,
            (
                session_record.tenant_id,
                version_id,
                upload_completion_request.object_key,
                upload_completion_request.checksum_sha256,
                upload_completion_request.content_type,
                upload_completion_request.content_type,
                upload_completion_request.size_bytes,
            ),
        )
        cursor.execute(
            """
            UPDATE document_ai_documents
            SET active_document_version_id = %s
            WHERE tenant_id = %s AND document_id = %s
            """,
            (version_id, session_record.tenant_id, session_record.document_id),
        )
    else:
        if existing_version is None:
            raise RuntimeError("document_ai_existing_version_missing")
        version_id = UUID(str(existing_version[0]))

    cursor.execute(
        """
        SELECT processing_operation_id
        FROM document_ai_processing_operations
        WHERE tenant_id = %s AND document_version_id = %s
          AND operation_kind = 'source_inspection'
        FOR UPDATE
        """,
        (session_record.tenant_id, version_id),
    )
    operation = cursor.fetchone()
    if operation is None:
        cursor.execute(
            """
            INSERT INTO document_ai_processing_operations (
                tenant_id, document_version_id, operation_kind,
                processing_policy_version, processor_version, correlation_id,
                idempotency_key, request_payload
            ) VALUES (
                %s, %s, 'source_inspection', 'v1', 'pending', %s, %s,
                %s::jsonb
            )
            RETURNING processing_operation_id
            """,
            (
                session_record.tenant_id,
                version_id,
                correlation_id,
                idempotency_key,
                json.dumps({"upload_session_id": str(session_record.session_id)}),
            ),
        )
        operation = cursor.fetchone()
        if operation is None:
            raise RuntimeError("document_ai_processing_operation_missing_identifier")
    operation_id = UUID(str(operation[0]))
    cursor.execute(
        """
        INSERT INTO document_ai_processing_work_items (
            tenant_id, processing_operation_id, work_kind, state,
            workload_class, priority,
            max_attempts, max_retry_elapsed_seconds
        ) VALUES (
            %s, %s, 'source_inspection', 'queued', 'background', 10, %s, %s
        )
        ON CONFLICT (tenant_id, processing_operation_id, work_kind) DO NOTHING
        RETURNING processing_work_item_id
        """,
        (
            session_record.tenant_id,
            operation_id,
            get_document_ai_processing_max_attempts(),
            get_document_ai_processing_max_retry_elapsed_seconds(),
        ),
    )
    work_item = cursor.fetchone()
    if work_item is None:
        cursor.execute(
            """
            SELECT processing_work_item_id
            FROM document_ai_processing_work_items
            WHERE tenant_id = %s AND processing_operation_id = %s
              AND work_kind = 'source_inspection'
            FOR UPDATE
            """,
            (session_record.tenant_id, operation_id),
        )
        work_item = cursor.fetchone()
    if work_item is None:
        raise RuntimeError("document_ai_processing_work_item_missing_identifier")
    work_item_id = UUID(str(work_item[0]))
    cursor.execute(
        """
        INSERT INTO document_ai_processing_outbox (
            tenant_id, processing_operation_id, processing_work_item_id, event_type,
            routing_key, correlation_id, payload
        ) VALUES (
            %s, %s, %s, 'source_inspection_requested',
            'document_ai.processing', %s, %s::jsonb
        )
        ON CONFLICT (tenant_id, processing_operation_id, event_type) DO NOTHING
        """,
        (
            session_record.tenant_id,
            operation_id,
            work_item_id,
            correlation_id,
            json.dumps(
                {
                    "document_id": str(session_record.document_id),
                    "version_id": str(version_id),
                }
            ),
        ),
    )
    response = DocumentRecordEnvelope(
        document=DocumentRecord(
            document_id=session_record.document_id,
            state="processing",
            uploaded_at=uploaded_at,
        ),
        processing_operation_id=operation_id,
        traceability=UploadSessionTraceability(trace_id=trace_id, correlation_id=correlation_id),
    ).model_dump(mode="json")
    cursor.execute(
        """
        INSERT INTO document_ai_completion_idempotency (
            idempotency_key, request_fingerprint, response_payload
        ) VALUES (%s, %s, %s::jsonb)
        """,
        (idempotency_key, request_fingerprint, json.dumps(response, sort_keys=True)),
    )
    cursor.execute(
        """
        UPDATE document_ai_upload_sessions
        SET session_state = 'completed', completed_at = now(),
            session_record = jsonb_set(
                jsonb_set(session_record, '{session_state}', '"completed"'::jsonb),
                '{completed_at}', to_jsonb(now()::text)
            )
        WHERE session_id = %s
        """,
        (session_record.session_id,),
    )
    return response


def _reconcile_persistent_confirmation_result(
    *,
    connection: psycopg.Connection[Any],
    idempotency_key: str,
    request_fingerprint: str,
) -> dict[str, object] | None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT request_fingerprint, response_payload
            FROM document_ai_completion_idempotency
            WHERE idempotency_key = %s
            """,
            (idempotency_key,),
        )
        prior_completion = cursor.fetchone()
    if prior_completion is None:
        return None
    if str(prior_completion[0]) != request_fingerprint:
        raise CompletionConflictError(
            reason="idempotency_key_payload_mismatch",
            details={
                "idempotency_key": idempotency_key,
                "conflict_field": "request_fingerprint",
            },
        )
    return cast(dict[str, object], dict(prior_completion[1]))


def _build_completion_request_fingerprint(
    upload_completion_request: UploadCompletionRequest,
    session_record: UploadSessionRecord,
    principal_user_id: UUID,
) -> str:
    envelope = {
        "principal_user_id": str(principal_user_id),
        "document_id": str(session_record.document_id),
        "session_id": str(session_record.session_id),
        "upload_completion_request": upload_completion_request.model_dump(mode="json"),
    }
    return compute_canonical_hash(envelope).sha256_hex


def _validate_completion_metadata(
    upload_completion_request: UploadCompletionRequest,
    session_record: UploadSessionRecord,
) -> None:
    if not is_allowed_upload_mime_type(upload_completion_request.content_type):
        if upload_completion_request.content_type in ARCHITECTURE_DEFINED_UNSUPPORTED_MIME_TYPES:
            reason = "format_not_supported_in_production"
        else:
            reason = "unsupported_mime_type"
        raise CompletionValidationError(
            reason=reason,
            message="Upload completion content type is not supported for production ingestion.",
            details={
                "content_type": upload_completion_request.content_type,
                "allowed_mime_types": list(ALLOWED_UPLOAD_MIME_TYPES),
            },
        )
    if not is_within_upload_size_limit(upload_completion_request.size_bytes):
        raise CompletionValidationError(
            reason="upload_size_exceeds_limit",
            message="Upload completion size exceeds allowed limit.",
            details={
                "size_bytes": upload_completion_request.size_bytes,
                "max_size_bytes": MAX_UPLOAD_SIZE_BYTES,
            },
        )
    if not is_valid_checksum_sha256(upload_completion_request.checksum_sha256):
        raise CompletionValidationError(
            reason="invalid_checksum_format",
            message="Upload completion checksum must be lowercase SHA-256 hex.",
            details={"checksum_sha256": upload_completion_request.checksum_sha256},
        )
    expected_object_key = build_tenant_document_object_key(
        session_record.tenant_id, session_record.document_id
    )
    if upload_completion_request.object_key != expected_object_key:
        raise CompletionValidationError(
            reason="object_key_mismatch",
            message="Upload completion object key does not match the governed storage key.",
            details={
                "tenant_id": session_record.tenant_id,
                "document_id": str(session_record.document_id),
                "expected_object_key": expected_object_key,
                "object_key": upload_completion_request.object_key,
            },
        )
    if upload_completion_request.checksum_sha256 != session_record.checksum_sha256:
        raise CompletionValidationError(
            reason="checksum_mismatch",
            message="Upload completion checksum does not match upload session.",
            details={"field": "checksum_sha256"},
        )
    if upload_completion_request.size_bytes != session_record.expected_size_bytes:
        raise CompletionValidationError(
            reason="size_mismatch",
            message="Upload completion size does not match upload session.",
            details={"field": "size_bytes"},
        )
    if upload_completion_request.content_type != session_record.content_type:
        raise CompletionValidationError(
            reason="content_type_mismatch",
            message="Upload completion content type does not match upload session.",
            details={"field": "content_type"},
        )


def _assert_document_metadata_match(
    existing_document: PersistedDocumentRecord,
    upload_completion_request: UploadCompletionRequest,
    session_record: UploadSessionRecord,
) -> None:
    expected_object_key = build_tenant_document_object_key(
        session_record.tenant_id, session_record.document_id
    )
    expected = {
        "storage_key": expected_object_key,
        "checksum_sha256": upload_completion_request.checksum_sha256,
        "size_bytes": upload_completion_request.size_bytes,
        "content_type": upload_completion_request.content_type,
        "tenant_id": session_record.tenant_id,
        "owner_user_id": str(session_record.owner_user_id),
    }
    actual = {
        "storage_key": existing_document.storage_key,
        "checksum_sha256": existing_document.checksum_sha256,
        "size_bytes": existing_document.size_bytes,
        "content_type": existing_document.content_type,
        "tenant_id": existing_document.tenant_id,
        "owner_user_id": str(existing_document.owner_user_id),
    }
    if actual != expected:
        raise CompletionConflictError(
            reason="document_metadata_mismatch",
            details={"document_id": str(existing_document.document_id)},
        )


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso_datetime(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized).astimezone(UTC)


def _parse_iso_datetime_or_none(value: str | None) -> datetime | None:
    if value is None:
        return None
    return _parse_iso_datetime(value)


def _row_to_document_record(row: tuple[object, ...] | None) -> PersistedDocumentRecord | None:
    if row is None:
        return None
    return PersistedDocumentRecord(
        document_id=cast(UUID, row[0]),
        tenant_id=cast(str, row[1]),
        owner_user_id=cast(UUID, row[2]),
        state=cast(DocumentLifecycleState, row[3]),
        storage_key=cast(str, row[4]),
        uploaded_at=_to_iso_utc(cast(datetime, row[5])),
        checksum_sha256=cast(str, row[6]),
        size_bytes=cast(int, row[7]),
        content_type=cast(str, row[8]),
        computation_id=cast(str | None, row[9]),
        purge_eligible_at=_to_iso_utc_or_none(cast(datetime | None, row[10])),
        purged_at=_to_iso_utc_or_none(cast(datetime | None, row[11])),
        compliance_lock_until=_to_iso_utc_or_none(cast(datetime | None, row[12])),
        display_name=cast(str | None, row[13]),
        category=cast(str | None, row[14]),
        tags=cast(list[str], row[15]),
        description=cast(str | None, row[16]),
        revision=cast(int, row[17]),
    )


def _to_iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _to_iso_utc_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _to_iso_utc(value)


def _build_duplicate_scope_key(
    tenant_id: str,
    owner_user_id: UUID,
    checksum_sha256: str,
) -> str:
    return f"{tenant_id}::{owner_user_id}::{checksum_sha256}"
