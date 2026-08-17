"""Durable source-inspection execution and processing-gate publication."""

from __future__ import annotations

import json
from uuid import UUID
from typing import cast
from typing import Literal
from typing import Protocol

from services.document_ai.app.config import MAX_UPLOAD_SIZE_BYTES
from services.document_ai.app.config import SOURCE_INSPECTION_POLICY_VERSION
from services.document_ai.app.storage_adapter import StorageAdapterProtocol
from services.document_ai.app.document_formats import normalize_media_type
from services.document_ai.app.source_inspection import InspectionReason
from services.document_ai.app.source_inspection import inspect_source_bytes
from services.document_ai.app.source_inspection import SourceInspectionError
from services.document_ai.app.source_inspection import SourceInspectionResult
from services.document_ai.app.structural_scopes import load_structural_scope_records
from services.document_ai.app.structural_scopes import persist_structural_scope_records
from services.document_ai.app.processing_workers import DurableCheckpoint
from services.document_ai.app.processing_workers import ProcessingAttemptLease
from services.document_ai.app.persistence_support import execute_document_ai_database_transaction
from services.document_ai.app.provider_partitions import persist_provider_partition_records


class _Cursor(Protocol):
    """The small cursor surface used to publish gated semantic work."""

    def execute(self, query: str, params: tuple[object, ...]) -> object:
        """Execute one parameterized command."""


class SourceInspectionRepository:
    """Use the existing operation/work/outbox authority for inspection results."""

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def inspect_operation(
        self,
        *,
        lease: ProcessingAttemptLease,
        storage: StorageAdapterProtocol,
    ) -> SourceInspectionResult:
        """Inspect exactly one authorized source version and persist its disposition.

        The object locator is fetched only inside the tenant-scoped database
        graph.  Source bytes are bounded and the temporary R2 materialization is
        removed immediately after inspection.
        """

        return execute_document_ai_database_transaction(
            database_url=self._database_url,
            transaction_name="document_ai.source_inspection.inspect_operation",
            transaction_callback=lambda cursor: self._inspect_operation_transaction(
                cursor=cursor,
                lease=lease,
                storage=storage,
            ),
            reconcile_ambiguous_result=lambda connection: self._reconcile_inspect_operation_result(
                connection=connection,
                lease=lease,
            ),
        )

    def _inspect_operation_transaction(
        self,
        *,
        cursor: object,
        lease: ProcessingAttemptLease,
        storage: StorageAdapterProtocol,
    ) -> SourceInspectionResult:
        cursor.execute(
            """
            SELECT artifact.source_artifact_id, artifact.storage_key, artifact.content_type,
                   artifact.checksum_sha256, artifact.size_bytes, operation.document_version_id,
                   version.document_id
            FROM document_ai_processing_operations AS operation
            JOIN document_ai_document_versions AS version
              ON version.tenant_id = operation.tenant_id
             AND version.document_version_id = operation.document_version_id
            JOIN document_ai_documents AS document
              ON document.tenant_id = version.tenant_id
             AND document.document_id = version.document_id
            JOIN document_ai_source_artifacts AS artifact
              ON artifact.tenant_id = version.tenant_id
             AND artifact.document_version_id = version.document_version_id
            JOIN document_ai_processing_work_items AS work
              ON work.tenant_id = operation.tenant_id
             AND work.processing_operation_id = operation.processing_operation_id
            JOIN document_ai_processing_attempts AS attempt
              ON attempt.tenant_id = work.tenant_id
             AND attempt.processing_attempt_id = work.current_processing_attempt_id
            WHERE operation.tenant_id = %s
              AND operation.processing_operation_id = %s
              AND operation.operation_kind = 'source_inspection'
              AND work.processing_work_item_id = %s
              AND work.current_processing_attempt_id = %s
              AND work.fencing_token = %s
              AND work.state = 'leased'
              AND work.leased_until > now()
              AND attempt.state = 'running'
              AND attempt.fencing_token = %s
              AND version.version_state = 'current'
              AND document.state IN ('uploaded', 'processing', 'validated', 'active')
            FOR UPDATE
            """,
            (
                lease.tenant_id,
                lease.processing_operation_id,
                lease.processing_work_item_id,
                lease.processing_attempt_id,
                lease.fencing_token,
                lease.fencing_token,
            ),
        )
        row = cursor.fetchone()
        if row is None:
            raise ValueError("source_inspection_operation_not_found")
        (
            artifact_id,
            storage_key,
            declared_type,
            checksum_sha256,
            size_bytes,
            version_id,
            document_id,
        ) = row
        cursor.execute(
            """SELECT source_inspection_id, disposition, reason_code, observed_media_type,
                      observed_source_family, observed_source_format, source_size_bytes,
                      page_count, structural_scopes, diagnostic_payload
               FROM document_ai_source_inspections
               WHERE tenant_id = %s AND document_version_id = %s
                 AND policy_version = %s""",
            (lease.tenant_id, version_id, SOURCE_INSPECTION_POLICY_VERSION),
        )
        existing = cursor.fetchone()
        if existing is not None:
            source_inspection_id = UUID(str(existing[0]))
            result = SourceInspectionResult(
                policy_version=SOURCE_INSPECTION_POLICY_VERSION,
                disposition=cast(Literal["accepted", "quarantined"], str(existing[1])),
                reason=cast(InspectionReason, str(existing[2])),
                observed_media_type=str(existing[3]) if existing[3] is not None else None,
                observed_source_family=str(existing[4]) if existing[4] is not None else None,
                observed_source_format=str(existing[5]) if existing[5] is not None else None,
                declared_media_type=str(declared_type),
                source_size_bytes=int(existing[6]) if existing[6] is not None else int(size_bytes),
                page_count=int(existing[7]) if existing[7] is not None else None,
                structural_scopes=tuple(
                    (int(scope[0]), int(scope[1]))
                    for scope in cast(list[list[int | str]], existing[8])
                ),
                diagnostic_payload=dict(cast(dict[str, object], existing[9])),
            )
            if result.accepted_for_processing:
                existing_scopes = load_structural_scope_records(
                    cursor=cast(_Cursor, cursor),
                    tenant_id=lease.tenant_id,
                    source_inspection_id=source_inspection_id,
                )
                if not existing_scopes:
                    payload = _read_bounded_source(storage, str(storage_key))
                    existing_scopes = persist_structural_scope_records(
                        cursor=cast(_Cursor, cursor),
                        tenant_id=lease.tenant_id,
                        document_id=UUID(str(document_id)),
                        document_version_id=UUID(str(version_id)),
                        source_artifact_id=UUID(str(artifact_id)),
                        source_inspection_id=source_inspection_id,
                        processing_operation_id=lease.processing_operation_id,
                        inspection=result,
                        source_payload=payload,
                    )
                persist_provider_partition_records(
                    cursor=cast(_Cursor, cursor),
                    tenant_id=lease.tenant_id,
                    document_id=UUID(str(document_id)),
                    document_version_id=UUID(str(version_id)),
                    source_artifact_id=UUID(str(artifact_id)),
                    source_inspection_id=source_inspection_id,
                    processing_operation_id=lease.processing_operation_id,
                    structural_scopes=existing_scopes,
                    source_size_bytes=result.source_size_bytes,
                )
            _queue_general_processing(
                cursor=cast(_Cursor, cursor),
                tenant_id=lease.tenant_id,
                version_id=UUID(str(version_id)),
                correlation_id=lease.processing_operation_id,
            )
            return SourceInspectionResult(
                policy_version=result.policy_version,
                disposition=result.disposition,
                reason=result.reason,
                observed_media_type=result.observed_media_type,
                observed_source_family=result.observed_source_family,
                observed_source_format=result.observed_source_format,
                declared_media_type=str(declared_type),
                source_size_bytes=result.source_size_bytes,
                page_count=result.page_count,
                structural_scopes=result.structural_scopes,
                diagnostic_payload=result.diagnostic_payload,
            )
        _verify_source_storage_identity(
            storage=storage,
            object_key=str(storage_key),
            expected_checksum_sha256=str(checksum_sha256),
            expected_size_bytes=int(size_bytes),
            expected_content_type=str(declared_type),
        )
        if int(size_bytes) > MAX_UPLOAD_SIZE_BYTES:
            result = SourceInspectionResult(
                policy_version=SOURCE_INSPECTION_POLICY_VERSION,
                disposition="quarantined",
                reason="source_too_large",
                observed_media_type=None,
                observed_source_family=None,
                observed_source_format=None,
                declared_media_type=str(declared_type),
                source_size_bytes=int(size_bytes),
                page_count=None,
                structural_scopes=(),
                diagnostic_payload={
                    "declared_media_type": normalize_media_type(str(declared_type)),
                    "reason_code": "source_too_large",
                    "source_size_bytes": int(size_bytes),
                },
            )
            cursor.execute(
                """
                INSERT INTO document_ai_source_inspections (
                    tenant_id, document_version_id, source_artifact_id, processing_operation_id,
                    policy_version, disposition, reason_code, observed_media_type,
                    observed_source_family, observed_source_format, source_size_bytes, page_count,
                    structural_scopes, diagnostic_payload
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
                """,
                (
                    lease.tenant_id,
                    version_id,
                    artifact_id,
                    lease.processing_operation_id,
                    result.policy_version,
                    result.disposition,
                    result.reason,
                    result.observed_media_type,
                    result.observed_source_family,
                    result.observed_source_format,
                    result.source_size_bytes,
                    result.page_count,
                    json.dumps(result.structural_scopes),
                    json.dumps(result.diagnostic_payload, sort_keys=True),
                ),
            )
            return result
        payload = _read_bounded_source(storage, str(storage_key))
        result = inspect_source_bytes(payload, declared_media_type=str(declared_type))
        cursor.execute(
            """
            INSERT INTO document_ai_source_inspections (
                tenant_id, document_version_id, source_artifact_id, processing_operation_id,
                policy_version, disposition, reason_code, observed_media_type,
                observed_source_family, observed_source_format, source_size_bytes, page_count,
                structural_scopes, diagnostic_payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
            RETURNING source_inspection_id
            """,
            (
                lease.tenant_id,
                version_id,
                artifact_id,
                lease.processing_operation_id,
                result.policy_version,
                result.disposition,
                result.reason,
                result.observed_media_type,
                result.observed_source_family,
                result.observed_source_format,
                result.source_size_bytes,
                result.page_count,
                json.dumps(result.structural_scopes),
                json.dumps(result.diagnostic_payload, sort_keys=True),
            ),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("source_inspection_insert_failed")
        source_inspection_id = UUID(str(row[0]))
        if result.accepted_for_processing:
            structural_scopes = persist_structural_scope_records(
                cursor=cast(_Cursor, cursor),
                tenant_id=lease.tenant_id,
                document_id=UUID(str(document_id)),
                document_version_id=UUID(str(version_id)),
                source_artifact_id=UUID(str(artifact_id)),
                source_inspection_id=source_inspection_id,
                processing_operation_id=lease.processing_operation_id,
                inspection=result,
                source_payload=payload,
            )
            persist_provider_partition_records(
                cursor=cast(_Cursor, cursor),
                tenant_id=lease.tenant_id,
                document_id=UUID(str(document_id)),
                document_version_id=UUID(str(version_id)),
                source_artifact_id=UUID(str(artifact_id)),
                source_inspection_id=source_inspection_id,
                processing_operation_id=lease.processing_operation_id,
                structural_scopes=structural_scopes,
                source_size_bytes=result.source_size_bytes,
            )
            _queue_general_processing(
                cursor=cast(_Cursor, cursor),
                tenant_id=lease.tenant_id,
                version_id=UUID(str(version_id)),
                correlation_id=lease.processing_operation_id,
            )
        return result

    def _reconcile_inspect_operation_result(
        self,
        *,
        connection: object,
        lease: ProcessingAttemptLease,
    ) -> SourceInspectionResult | None:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT inspection.disposition, inspection.reason_code,
                       inspection.observed_media_type, inspection.observed_source_family,
                       inspection.observed_source_format, inspection.source_size_bytes,
                       inspection.page_count, inspection.structural_scopes,
                       inspection.diagnostic_payload, artifact.content_type
                FROM document_ai_source_inspections AS inspection
                JOIN document_ai_processing_operations AS operation
                  ON operation.tenant_id = inspection.tenant_id
                 AND operation.document_version_id = inspection.document_version_id
                JOIN document_ai_source_artifacts AS artifact
                  ON artifact.tenant_id = inspection.tenant_id
                 AND artifact.document_version_id = inspection.document_version_id
                WHERE inspection.tenant_id = %s
                  AND inspection.processing_operation_id = %s
                  AND inspection.policy_version = %s
                """,
                (
                    lease.tenant_id,
                    lease.processing_operation_id,
                    SOURCE_INSPECTION_POLICY_VERSION,
                ),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return SourceInspectionResult(
            policy_version=SOURCE_INSPECTION_POLICY_VERSION,
            disposition=cast(Literal["accepted", "quarantined"], str(row[0])),
            reason=cast(InspectionReason, str(row[1])),
            observed_media_type=str(row[2]) if row[2] is not None else None,
            observed_source_family=str(row[3]) if row[3] is not None else None,
            observed_source_format=str(row[4]) if row[4] is not None else None,
            declared_media_type=str(row[9]),
            source_size_bytes=int(row[5]) if row[5] is not None else 0,
            page_count=int(row[6]) if row[6] is not None else None,
            structural_scopes=tuple(
                (int(scope[0]), int(scope[1])) for scope in cast(list[list[int | str]], row[7])
            ),
            diagnostic_payload=dict(cast(dict[str, object], row[8])),
        )


class SourceInspectionWorkExecutor:
    """Execute source-inspection work through the existing worker boundary."""

    def __init__(
        self, *, repository: SourceInspectionRepository, storage: StorageAdapterProtocol
    ) -> None:
        self._repository = repository
        self._storage = storage

    def execute(
        self, *, lease: ProcessingAttemptLease, checkpoint: DurableCheckpoint | None
    ) -> str:
        """Persist the inspection before reporting durable worker success."""

        del checkpoint
        self._repository.inspect_operation(
            tenant_id=lease.tenant_id,
            processing_operation_id=lease.processing_operation_id,
            storage=self._storage,
        )
        return f"source-inspection:{lease.processing_operation_id}"


def _read_bounded_source(storage: StorageAdapterProtocol, storage_key: str) -> bytes:
    """Read at most the configured source maximum and always clean temporary R2 files."""

    source_path, _ = storage.resolve_download_object(storage_key)
    try:
        with source_path.open("rb") as handle:
            return handle.read(MAX_UPLOAD_SIZE_BYTES + 1)
    finally:
        # R2 uses an isolated NamedTemporaryFile; in-memory adapters keep their
        # governed backing object and therefore must not be unlinked.
        if source_path.name.startswith("document-ai-download-"):
            source_path.unlink(missing_ok=True)


def _verify_source_storage_identity(
    *,
    storage: StorageAdapterProtocol,
    object_key: str,
    expected_checksum_sha256: str,
    expected_size_bytes: int,
    expected_content_type: str,
) -> None:
    metadata = storage.get_object_metadata(object_key)
    if metadata.size_bytes != expected_size_bytes:
        raise SourceInspectionError("source_storage_size_mismatch")
    if normalize_media_type(metadata.content_type) != normalize_media_type(expected_content_type):
        raise SourceInspectionError("source_storage_content_type_mismatch")
    if (
        metadata.checksum_sha256 is not None
        and metadata.checksum_sha256 != expected_checksum_sha256
    ):
        raise SourceInspectionError("source_storage_checksum_mismatch")


def _queue_general_processing(
    *, cursor: _Cursor, tenant_id: str, version_id: UUID, correlation_id: UUID
) -> None:
    """Create semantic work only after the accepted row exists in this transaction."""

    execute = cursor.execute
    execute(
        """INSERT INTO document_ai_processing_operations (
               tenant_id, document_version_id, operation_kind, processing_policy_version,
               processor_version, correlation_id, request_payload
           ) VALUES (%s, %s, 'general_document_understanding', 'v1', 'pending', %s, '{}'::jsonb)
           ON CONFLICT (tenant_id, document_version_id, operation_kind) DO NOTHING""",
        (tenant_id, version_id, str(correlation_id)),
    )
    execute(
        """INSERT INTO document_ai_processing_work_items (
               tenant_id, processing_operation_id, work_kind, state, workload_class, priority,
               max_attempts, max_retry_elapsed_seconds
           ) SELECT tenant_id, processing_operation_id, 'general_document_understanding',
                    'queued', 'background', 10, 3, 900
             FROM document_ai_processing_operations
            WHERE tenant_id = %s AND document_version_id = %s
              AND operation_kind = 'general_document_understanding'
           ON CONFLICT (tenant_id, processing_operation_id, work_kind) DO NOTHING""",
        (tenant_id, version_id),
    )
    execute(
        """INSERT INTO document_ai_processing_outbox (
               tenant_id, processing_operation_id, processing_work_item_id, event_type,
               routing_key, correlation_id, payload
           ) SELECT operation.tenant_id, operation.processing_operation_id,
                    work.processing_work_item_id, 'general_document_understanding_requested',
                    'document_ai.processing', %s,
                    jsonb_build_object('version_id', operation.document_version_id)
             FROM document_ai_processing_operations AS operation
             JOIN document_ai_processing_work_items AS work
               ON work.tenant_id = operation.tenant_id
              AND work.processing_operation_id = operation.processing_operation_id
            WHERE operation.tenant_id = %s AND operation.document_version_id = %s
              AND operation.operation_kind = 'general_document_understanding'
           ON CONFLICT (tenant_id, processing_operation_id, event_type) DO NOTHING""",
        (str(correlation_id), tenant_id, version_id),
    )
