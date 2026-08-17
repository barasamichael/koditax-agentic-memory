"""Durable purge saga helpers for Document AI lifecycle completion."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from hashlib import sha256
from typing import Any
from typing import cast
from uuid import UUID

import psycopg

from shared.determinism.input_hash import compute_canonical_hash
from services.document_ai.app.document_lifecycle import DocumentLifecycleActionError
from services.document_ai.app.document_lifecycle import DocumentLifecycleState
from services.document_ai.app.document_lifecycle import enforce_execute_purge_action
from services.document_ai.app.document_registry import DocumentRecordEnvelope
from services.document_ai.app.document_registry import DocumentRetentionActionError
from services.document_ai.app.document_registry import PersistedDocumentRecord
from services.document_ai.app.document_registry import PersistentDocumentRegistryStore
from services.document_ai.app.document_registry import to_document_record
from services.document_ai.app.persistence_support import connect_document_ai_database
from services.document_ai.app.persistence_support import execute_document_ai_database_transaction
from services.document_ai.app.storage_adapter import StorageAdapterPermanentError
from services.document_ai.app.storage_adapter import StorageAdapterProtocol
from services.document_ai.app.storage_adapter import StorageAdapterTransientError
from services.document_ai.app.upload_sessions import UploadSessionTraceability


@dataclass(frozen=True)
class PurgeTargetRecord:
    """Represent one durable purge target for a persisted object identity."""

    target_kind: str
    target_reference: str
    state: str
    attempt_count: int


@dataclass(frozen=True)
class PurgeOperationRecord:
    """Represent one durable purge request and its target manifest."""

    purge_operation_id: UUID
    state: str
    targets: tuple[PurgeTargetRecord, ...]


def execute_document_purge(
    *,
    document_record: PersistedDocumentRecord,
    principal_user_id: UUID,
    correlation_id: str,
    document_registry_store: object,
    storage_adapter: StorageAdapterProtocol,
    purged_at: str | None = None,
    compliance_override_granted: bool = False,
    now_utc: datetime | None = None,
) -> DocumentRecordEnvelope:
    """Execute or resume the durable purge saga for one scoped document."""

    if not isinstance(document_registry_store, PersistentDocumentRegistryStore):
        return _execute_in_memory_purge(
            document_record=document_record,
            principal_user_id=principal_user_id,
            correlation_id=correlation_id,
            document_registry_store=document_registry_store,
            purged_at=purged_at,
            compliance_override_granted=compliance_override_granted,
            now_utc=now_utc,
        )

    if document_record.owner_user_id != principal_user_id:
        raise DocumentRetentionActionError(
            reason="owner_user_mismatch",
            message="Document ownership context does not match authenticated principal.",
            details={
                "owner_user_id": str(document_record.owner_user_id),
                "principal_user_id": str(principal_user_id),
            },
        )

    target_purged_at = _resolve_purged_at(
        current_state=document_record.state,
        compliance_lock_until=document_record.compliance_lock_until,
        purge_eligible_at=document_record.purge_eligible_at,
        purged_at=purged_at,
        compliance_override_granted=compliance_override_granted,
        now_utc=now_utc,
    )
    request_fingerprint = _build_request_fingerprint(
        document_record=document_record,
        principal_user_id=principal_user_id,
        correlation_id=correlation_id,
        purged_at=target_purged_at,
    )
    operation = _request_purge_operation(
        database_url=cast(PersistentDocumentRegistryStore, document_registry_store).database_url,
        document_record=document_record,
        principal_user_id=principal_user_id,
        correlation_id=correlation_id,
        request_fingerprint=request_fingerprint,
    )
    if operation.state == "completed":
        return _load_document_envelope(
            document_record=document_record.model_copy(
                update={"state": "purged", "purged_at": target_purged_at}
            ),
            correlation_id=correlation_id,
        )

    pending_targets = [
        target for target in operation.targets if target.state in {"pending", "failed"}
    ]
    target_results: list[tuple[str, bool, str | None]] = []
    for target in pending_targets:
        target_results.append(
            _delete_purge_target(
                storage_adapter=storage_adapter,
                target_reference=target.target_reference,
            )
        )
    return _record_purge_attempt_and_maybe_finalize(
        database_url=cast(PersistentDocumentRegistryStore, document_registry_store).database_url,
        document_record=document_record,
        principal_user_id=principal_user_id,
        correlation_id=correlation_id,
        request_fingerprint=request_fingerprint,
        target_purged_at=target_purged_at,
        target_results=target_results,
    )


def recover_pending_document_purges(
    *,
    database_url: str,
    storage_adapter: StorageAdapterProtocol,
    batch_size: int = 25,
) -> int:
    """Resume incomplete purge operations after a crash or deployment restart."""

    if batch_size < 1:
        return 0

    recovered = 0
    document_registry_store = PersistentDocumentRegistryStore(database_url=database_url)
    with connect_document_ai_database(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT op.correlation_id, op.requested_by_user_id, op.request_fingerprint,
                       doc.document_id, doc.tenant_id, doc.owner_user_id, doc.state,
                       doc.storage_key, doc.uploaded_at, doc.checksum_sha256, doc.size_bytes,
                       doc.content_type, doc.computation_id, doc.purge_eligible_at,
                       doc.purged_at, doc.compliance_lock_until, doc.display_name,
                       doc.category, doc.tags, doc.description, doc.revision
                FROM document_ai_purge_operations AS op
                JOIN document_ai_documents AS doc
                  ON doc.tenant_id = op.tenant_id
                 AND doc.document_id = op.document_id
                WHERE op.state IN ('requested', 'running')
                ORDER BY op.requested_at ASC, op.purge_operation_id ASC
                LIMIT %s
                """,
                (batch_size,),
            )
            rows = cursor.fetchall()

    for row in rows:
        document_record = PersistedDocumentRecord(
            document_id=UUID(str(row[3])),
            tenant_id=str(row[4]),
            owner_user_id=UUID(str(row[5])),
            state=cast(str, row[6]),
            storage_key=str(row[7]),
            uploaded_at=_to_iso_utc(cast(datetime, row[8])),
            checksum_sha256=str(row[9]),
            size_bytes=int(row[10]),
            content_type=str(row[11]),
            computation_id=None if row[12] is None else str(row[12]),
            purge_eligible_at=_to_iso_utc_or_none(cast(datetime | None, row[13])),
            purged_at=_to_iso_utc_or_none(cast(datetime | None, row[14])),
            compliance_lock_until=_to_iso_utc_or_none(cast(datetime | None, row[15])),
            display_name=None if row[16] is None else str(row[16]),
            category=None if row[17] is None else str(row[17]),
            tags=list(row[18]) if row[18] is not None else [],
            description=None if row[19] is None else str(row[19]),
            revision=int(row[20]),
        )
        execute_document_purge(
            document_record=document_record,
            principal_user_id=UUID(str(row[1])),
            correlation_id=str(row[0]),
            document_registry_store=document_registry_store,
            storage_adapter=storage_adapter,
            purged_at=document_record.purged_at,
        )
        recovered += 1
    return recovered


def _execute_in_memory_purge(
    *,
    document_record: PersistedDocumentRecord,
    principal_user_id: UUID,
    correlation_id: str,
    document_registry_store: object,
    purged_at: str | None,
    compliance_override_granted: bool,
    now_utc: datetime | None,
) -> DocumentRecordEnvelope:
    from services.document_ai.app.document_registry import apply_document_retention_action

    return apply_document_retention_action(
        action="execute_purge",
        document_record=document_record,
        principal_user_id=principal_user_id,
        correlation_id=correlation_id,
        document_registry_store=document_registry_store,
        compliance_override_granted=compliance_override_granted,
        purged_at=purged_at,
    )


def _request_purge_operation(
    *,
    database_url: str,
    document_record: PersistedDocumentRecord,
    principal_user_id: UUID,
    correlation_id: str,
    request_fingerprint: str,
) -> PurgeOperationRecord:
    def _request(cursor: psycopg.Cursor[Any]) -> PurgeOperationRecord:
        cursor.execute(
            """
            SELECT purge_operation_id, state
            FROM document_ai_purge_operations
            WHERE tenant_id = %s
              AND document_id = %s
              AND idempotency_key = %s
            FOR UPDATE
            """,
            (document_record.tenant_id, document_record.document_id, request_fingerprint),
        )
        existing = cursor.fetchone()
        if existing is not None:
            return _load_operation(cursor=cursor, purge_operation_id=UUID(str(existing[0])))

        cursor.execute(
            """
            INSERT INTO document_ai_purge_operations (
                tenant_id,
                document_id,
                requested_by_user_id,
                requested_by_role,
                correlation_id,
                idempotency_key,
                request_fingerprint,
                payload_fingerprint,
                state,
                manifest_version,
                replay_count
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'running', 'v1', 1)
            RETURNING purge_operation_id
            """,
            (
                document_record.tenant_id,
                document_record.document_id,
                principal_user_id,
                None,
                correlation_id,
                request_fingerprint,
                request_fingerprint,
                request_fingerprint,
            ),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("document_ai_purge_operation_missing_identifier")
        purge_operation_id = UUID(str(row[0]))
        target_references = _collect_purge_target_references(
            cursor=cursor,
            tenant_id=document_record.tenant_id,
            document_id=document_record.document_id,
            document_storage_key=document_record.storage_key,
        )
        for target_reference in target_references:
            cursor.execute(
                """
                INSERT INTO document_ai_purge_targets (
                    tenant_id,
                    purge_operation_id,
                    target_kind,
                    target_reference,
                    state,
                    required
                )
                VALUES (%s, %s, 'source_artifact', %s, 'pending', TRUE)
                ON CONFLICT (tenant_id, purge_operation_id, target_kind, target_reference)
                DO NOTHING
                """,
                (document_record.tenant_id, purge_operation_id, target_reference),
            )
        return _load_operation(cursor=cursor, purge_operation_id=purge_operation_id)

    return execute_document_ai_database_transaction(
        database_url=database_url,
        transaction_name="document_ai.document_purge.request",
        transaction_callback=_request,
        reconcile_ambiguous_result=lambda connection: _reconcile_purge_request(
            connection=connection,
            document_id=document_record.document_id,
            tenant_id=document_record.tenant_id,
            request_fingerprint=request_fingerprint,
        ),
    )


def _record_purge_attempt_and_maybe_finalize(
    *,
    database_url: str,
    document_record: PersistedDocumentRecord,
    principal_user_id: UUID,
    correlation_id: str,
    request_fingerprint: str,
    target_purged_at: str,
    target_results: list[tuple[str, bool, str | None]],
) -> DocumentRecordEnvelope:
    def _record(cursor: psycopg.Cursor[Any]) -> DocumentRecordEnvelope:
        operation = _load_operation_by_request(
            cursor=cursor,
            tenant_id=document_record.tenant_id,
            document_id=document_record.document_id,
            request_fingerprint=request_fingerprint,
        )
        if operation is None:
            raise RuntimeError("document_ai_purge_operation_missing")
        attempt_number = _next_attempt_number(
            cursor=cursor, operation_id=operation.purge_operation_id
        )
        attempt_state = "succeeded" if all(result[1] for result in target_results) else "failed"
        cursor.execute(
            """
            INSERT INTO document_ai_purge_attempts (
                tenant_id,
                purge_operation_id,
                attempt_number,
                state,
                requested_by_user_id,
                correlation_id,
                request_fingerprint,
                failure_detail
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                document_record.tenant_id,
                operation.purge_operation_id,
                attempt_number,
                attempt_state,
                principal_user_id,
                correlation_id,
                request_fingerprint,
                json.dumps(
                    {
                        "target_results": [
                            {
                                "target_reference": reference,
                                "resolved": resolved,
                                "failure_reason": failure_reason,
                            }
                            for reference, resolved, failure_reason in target_results
                        ]
                    },
                    sort_keys=True,
                ),
            ),
        )
        for reference, resolved, failure_reason in target_results:
            cursor.execute(
                """
                UPDATE document_ai_purge_targets
                SET attempt_count = attempt_count + 1,
                    state = CASE WHEN %s THEN 'completed' ELSE 'failed' END,
                    completed_at = CASE WHEN %s THEN now() ELSE completed_at END,
                    verified_at = CASE WHEN %s THEN now() ELSE verified_at END,
                    failure_detail = CASE
                        WHEN %s THEN NULL
                        ELSE jsonb_build_object('reason', %s)
                    END
                WHERE tenant_id = %s
                  AND purge_operation_id = %s
                  AND target_reference = %s
                """,
                (
                    resolved,
                    resolved,
                    resolved,
                    resolved,
                    failure_reason,
                    document_record.tenant_id,
                    operation.purge_operation_id,
                    reference,
                ),
            )
        return _finalize_purge_if_ready(
            cursor=cursor,
            operation_id=operation.purge_operation_id,
            document_record=document_record,
            target_purged_at=target_purged_at,
            correlation_id=correlation_id,
        )

    return execute_document_ai_database_transaction(
        database_url=database_url,
        transaction_name="document_ai.document_purge.record_attempt",
        transaction_callback=_record,
        reconcile_ambiguous_result=lambda connection: _reconcile_purge_record(
            connection=connection,
            tenant_id=document_record.tenant_id,
            document_id=document_record.document_id,
            request_fingerprint=request_fingerprint,
        ),
    )


def _finalize_purge_if_ready(
    *,
    cursor: psycopg.Cursor[Any],
    operation_id: UUID,
    document_record: PersistedDocumentRecord,
    target_purged_at: str,
    correlation_id: str,
) -> DocumentRecordEnvelope:
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM document_ai_purge_targets
        WHERE tenant_id = %s
          AND purge_operation_id = %s
          AND required = TRUE
          AND state <> 'completed'
        """,
        (document_record.tenant_id, operation_id),
    )
    remaining_row = cursor.fetchone()
    remaining = 0 if remaining_row is None else int(remaining_row[0])
    if remaining > 0:
        cursor.execute(
            """
            UPDATE document_ai_purge_operations
            SET state = 'running',
                updated_at = now(),
                last_reconciled_at = now()
            WHERE tenant_id = %s AND purge_operation_id = %s
            """,
            (document_record.tenant_id, operation_id),
        )
        return _load_document_envelope(
            document_record=document_record.model_copy(
                update={"state": document_record.state, "purged_at": document_record.purged_at}
            ),
            correlation_id=correlation_id,
        )

    cursor.execute(
        """
        UPDATE document_ai_source_artifacts AS artifact
        SET retention_state = 'purged'
        FROM document_ai_document_versions AS version
        WHERE artifact.tenant_id = version.tenant_id
          AND artifact.document_version_id = version.document_version_id
          AND version.document_id = %s
        """,
        (document_record.document_id,),
    )
    cursor.execute(
        """
        UPDATE document_ai_document_versions
        SET version_state = 'purged'
        WHERE tenant_id = %s AND document_id = %s
        """,
        (document_record.tenant_id, document_record.document_id),
    )
    cursor.execute(
        """
        UPDATE document_ai_canonical_representations
        SET state = 'purged', is_active = FALSE
        WHERE tenant_id = %s
          AND document_version_id IN (
              SELECT document_version_id
              FROM document_ai_document_versions
              WHERE tenant_id = %s AND document_id = %s
          )
        """,
        (document_record.tenant_id, document_record.tenant_id, document_record.document_id),
    )
    cursor.execute(
        """
        UPDATE document_ai_retrieval_chunks
        SET lifecycle_state = 'purged'
        WHERE tenant_id = %s AND document_id = %s
        """,
        (document_record.tenant_id, document_record.document_id),
    )
    cursor.execute(
        """
        UPDATE document_ai_chunk_embeddings
        SET index_state = 'purged'
        WHERE tenant_id = %s
          AND document_version_id IN (
              SELECT document_version_id
              FROM document_ai_document_versions
              WHERE tenant_id = %s AND document_id = %s
          )
        """,
        (document_record.tenant_id, document_record.tenant_id, document_record.document_id),
    )
    cursor.execute(
        """
        UPDATE document_ai_documents
        SET state = 'purged',
            purged_at = %s::timestamptz,
            active_document_version_id = NULL
        WHERE tenant_id = %s AND document_id = %s
        """,
        (target_purged_at, document_record.tenant_id, document_record.document_id),
    )
    cursor.execute(
        """
        UPDATE documents
        SET state = 'purged',
            purged_at = %s::timestamptz
        WHERE id = %s
        """,
        (target_purged_at, document_record.document_id),
    )
    cursor.execute(
        """
        UPDATE document_ai_purge_operations
        SET state = 'completed',
            completed_at = %s::timestamptz,
            updated_at = now(),
            last_reconciled_at = now()
        WHERE tenant_id = %s AND purge_operation_id = %s
        """,
        (target_purged_at, document_record.tenant_id, operation_id),
    )
    return _load_document_envelope(
        document_record=document_record.model_copy(
            update={"state": "purged", "purged_at": target_purged_at}
        ),
        correlation_id=correlation_id,
    )


def _collect_purge_target_references(
    *,
    cursor: psycopg.Cursor[Any],
    tenant_id: str,
    document_id: UUID,
    document_storage_key: str,
) -> tuple[str, ...]:
    cursor.execute(
        """
        SELECT DISTINCT artifact.storage_key
        FROM document_ai_source_artifacts AS artifact
        JOIN document_ai_document_versions AS version
          ON version.tenant_id = artifact.tenant_id
         AND version.document_version_id = artifact.document_version_id
        WHERE version.tenant_id = %s
          AND version.document_id = %s
        ORDER BY artifact.storage_key ASC
        """,
        (tenant_id, document_id),
    )
    rows = [str(row[0]) for row in cursor.fetchall()]
    if document_storage_key not in rows:
        rows.append(document_storage_key)
    return tuple(dict.fromkeys(rows))


def _delete_purge_target(
    *,
    storage_adapter: StorageAdapterProtocol,
    target_reference: str,
) -> tuple[str, bool, str | None]:
    try:
        storage_adapter.delete_object(target_reference)
        if storage_adapter.verify_object_absent(target_reference):
            return target_reference, True, None
        return target_reference, False, "storage_object_still_present"
    except StorageAdapterPermanentError as error:
        if error.reason == "storage_object_not_found":
            return target_reference, True, None
        return target_reference, False, error.reason
    except StorageAdapterTransientError as error:
        return target_reference, False, error.reason


def _load_operation(
    *,
    cursor: psycopg.Cursor[Any],
    purge_operation_id: UUID,
) -> PurgeOperationRecord:
    cursor.execute(
        """
        SELECT purge_operation_id, state
        FROM document_ai_purge_operations
        WHERE purge_operation_id = %s
        """,
        (purge_operation_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError("document_ai_purge_operation_missing_identifier")
    return PurgeOperationRecord(
        purge_operation_id=UUID(str(row[0])),
        state=str(row[1]),
        targets=_load_targets(cursor=cursor, purge_operation_id=purge_operation_id),
    )


def _load_operation_by_request(
    *,
    cursor: psycopg.Cursor[Any],
    tenant_id: str,
    document_id: UUID,
    request_fingerprint: str,
) -> PurgeOperationRecord | None:
    cursor.execute(
        """
        SELECT purge_operation_id
        FROM document_ai_purge_operations
        WHERE tenant_id = %s
          AND document_id = %s
          AND idempotency_key = %s
        """,
        (tenant_id, document_id, request_fingerprint),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return _load_operation(cursor=cursor, purge_operation_id=UUID(str(row[0])))


def _load_targets(
    *,
    cursor: psycopg.Cursor[Any],
    purge_operation_id: UUID,
) -> tuple[PurgeTargetRecord, ...]:
    cursor.execute(
        """
        SELECT target_kind, target_reference, state, attempt_count
        FROM document_ai_purge_targets
        WHERE purge_operation_id = %s
        ORDER BY created_at ASC, target_reference ASC
        """,
        (purge_operation_id,),
    )
    rows = cursor.fetchall()
    return tuple(
        PurgeTargetRecord(
            target_kind=str(row[0]),
            target_reference=str(row[1]),
            state=str(row[2]),
            attempt_count=int(row[3]),
        )
        for row in rows
    )


def _next_attempt_number(*, cursor: psycopg.Cursor[Any], operation_id: UUID) -> int:
    cursor.execute(
        """
        SELECT COALESCE(MAX(attempt_number), 0) + 1
        FROM document_ai_purge_attempts
        WHERE purge_operation_id = %s
        """,
        (operation_id,),
    )
    row = cursor.fetchone()
    return 1 if row is None else int(row[0])


def _reconcile_purge_request(
    *,
    connection: psycopg.Connection[Any],
    document_id: UUID,
    tenant_id: str,
    request_fingerprint: str,
) -> PurgeOperationRecord | None:
    with connection.cursor() as cursor:
        return _load_operation_by_request(
            cursor=cursor,
            tenant_id=tenant_id,
            document_id=document_id,
            request_fingerprint=request_fingerprint,
        )


def _reconcile_purge_record(
    *,
    connection: psycopg.Connection[Any],
    tenant_id: str,
    document_id: UUID,
    request_fingerprint: str,
) -> DocumentRecordEnvelope | None:
    with connection.cursor() as cursor:
        operation = _load_operation_by_request(
            cursor=cursor,
            tenant_id=tenant_id,
            document_id=document_id,
            request_fingerprint=request_fingerprint,
        )
        if operation is None:
            return None
        if operation.state != "completed":
            return None
        cursor.execute(
            """
            SELECT document_id, tenant_id, owner_user_id, state, storage_key, uploaded_at,
                   checksum_sha256, size_bytes, content_type, computation_id,
                   purge_eligible_at, purged_at, compliance_lock_until,
                   display_name, category, tags, description, revision
            FROM document_ai_documents
            WHERE tenant_id = %s AND document_id = %s
            """,
            (tenant_id, document_id),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    document_record = PersistedDocumentRecord(
        document_id=UUID(str(row[0])),
        tenant_id=str(row[1]),
        owner_user_id=UUID(str(row[2])),
        state=cast(str, row[3]),
        storage_key=str(row[4]),
        uploaded_at=_to_iso_utc(cast(datetime, row[5])),
        checksum_sha256=str(row[6]),
        size_bytes=int(row[7]),
        content_type=str(row[8]),
        computation_id=None if row[9] is None else str(row[9]),
        purge_eligible_at=_to_iso_utc_or_none(cast(datetime | None, row[10])),
        purged_at=_to_iso_utc_or_none(cast(datetime | None, row[11])),
        compliance_lock_until=_to_iso_utc_or_none(cast(datetime | None, row[12])),
        display_name=None if row[13] is None else str(row[13]),
        category=None if row[14] is None else str(row[14]),
        tags=list(row[15]) if row[15] is not None else [],
        description=None if row[16] is None else str(row[16]),
        revision=int(row[17]),
    )
    return _load_document_envelope(
        document_record=document_record,
        correlation_id=request_fingerprint,
    )


def _resolve_purged_at(
    *,
    current_state: str,
    compliance_lock_until: str | None,
    purge_eligible_at: str | None,
    purged_at: str | None,
    compliance_override_granted: bool,
    now_utc: datetime | None,
) -> str:
    try:
        _, resolved_purged_at = enforce_execute_purge_action(
            current_state=cast("DocumentLifecycleState", current_state),
            compliance_lock_until=compliance_lock_until,
            purge_eligible_at=purge_eligible_at,
            purged_at=purged_at,
            compliance_override_granted=compliance_override_granted,
            now_utc=now_utc,
        )
        return resolved_purged_at
    except DocumentLifecycleActionError as error:
        raise DocumentRetentionActionError(
            reason=error.reason,
            message=error.message,
            details={
                "action": "execute_purge",
                "current_state": error.current_state,
                "requested_state": error.requested_state or "purged",
            },
        ) from error


def _build_request_fingerprint(
    *,
    document_record: PersistedDocumentRecord,
    principal_user_id: UUID,
    correlation_id: str,
    purged_at: str,
) -> str:
    envelope = {
        "scope": "document_ai.execute_purge",
        "document_id": str(document_record.document_id),
        "tenant_id": document_record.tenant_id,
        "principal_user_id": str(principal_user_id),
        "correlation_id": correlation_id,
        "purged_at": purged_at,
        "storage_key": document_record.storage_key,
    }
    return compute_canonical_hash(envelope).sha256_hex


def _load_document_envelope(
    *,
    document_record: PersistedDocumentRecord,
    correlation_id: str,
) -> DocumentRecordEnvelope:
    trace_id = sha256(
        f"document_purge:{document_record.document_id}:{document_record.state}:"
        f"{document_record.purged_at or ''}".encode("utf-8")
    ).hexdigest()
    return DocumentRecordEnvelope(
        document=to_document_record(document_record),
        traceability=UploadSessionTraceability(
            trace_id=trace_id,
            correlation_id=correlation_id,
        ),
    )


def _to_iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _to_iso_utc_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _to_iso_utc(value)
