"""Durable OpenAI understanding execution for accepted document sources."""

from __future__ import annotations

from uuid import UUID
from hashlib import sha256
from dataclasses import dataclass

from shared.determinism.input_hash import compute_canonical_hash

from services.document_ai.app.governed_openai import OpenAIProviderError
from services.document_ai.app.governed_openai import GovernedOpenAIClient
from services.document_ai.app.governed_openai import PreparedOpenAISource
from services.document_ai.app.governed_openai import GovernedOpenAIRequest
from services.document_ai.app.governed_openai import MAX_OPENAI_SOURCE_BYTES
from services.document_ai.app.storage_adapter import StorageAdapterProtocol
from services.document_ai.app.processing_workers import DurableCheckpoint
from services.document_ai.app.processing_workers import ProcessingAttemptLease
from services.document_ai.app.persistence_support import connect_document_ai_database
from services.document_ai.app.provider_result_repository import ProviderResultRepository
from services.document_ai.app.provider_result_repository import ProviderResultReservationDetails


@dataclass(frozen=True)
class EligibleUnderstandingSource:
    """A source selected only through the fenced, tenant-scoped durable graph."""

    document_version_id: UUID
    source_artifact_id: UUID
    storage_key: str
    content_type: str
    size_bytes: int
    checksum_sha256: str
    structural_scopes: tuple[EligibleUnderstandingStructuralScope, ...]


@dataclass(frozen=True)
class EligibleUnderstandingStructuralScope:
    """Represent one exact structural scope claimed for OpenAI processing."""

    structural_scope_id: UUID
    scope_kind: str
    scope_ordinal: int
    scope_identity: str
    structural_coordinates: dict[str, object]
    scope_payload: dict[str, object]


class OpenAIUnderstandingRepository:
    """Reload the exact accepted source and result authority for one worker lease."""

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def existing_result_reference(self, *, lease: ProcessingAttemptLease) -> str | None:
        """Resolve a prior durable result before repeating an uncertain provider call."""

        with connect_document_ai_database(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT provider_result_id
                       FROM document_ai_provider_results
                      WHERE tenant_id = %s AND processing_operation_id = %s
                      ORDER BY created_at ASC LIMIT 1""",
                    (lease.tenant_id, lease.processing_operation_id),
                )
                row = cursor.fetchone()
        return None if row is None else f"provider-result:{row[0]}"

    def load_eligible_source(
        self, *, lease: ProcessingAttemptLease
    ) -> EligibleUnderstandingSource:
        """Authorize one exact inspected source under the currently held worker fence."""

        with connect_document_ai_database(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT operation.document_version_id, artifact.source_artifact_id,
                              artifact.storage_key, artifact.content_type, artifact.size_bytes,
                              artifact.checksum_sha256
                       FROM document_ai_processing_work_items AS work
                       JOIN document_ai_processing_operations AS operation
                         ON operation.tenant_id = work.tenant_id
                        AND operation.processing_operation_id = work.processing_operation_id
                       JOIN document_ai_processing_attempts AS attempt
                         ON attempt.tenant_id = work.tenant_id
                        AND attempt.processing_attempt_id = work.current_processing_attempt_id
                       JOIN document_ai_document_versions AS version
                         ON version.tenant_id = operation.tenant_id
                        AND version.document_version_id = operation.document_version_id
                       JOIN document_ai_documents AS document
                         ON document.tenant_id = version.tenant_id
                        AND document.document_id = version.document_id
                       JOIN document_ai_source_artifacts AS artifact
                         ON artifact.tenant_id = operation.tenant_id
                        AND artifact.document_version_id = operation.document_version_id
                       JOIN document_ai_source_inspections AS inspection
                         ON inspection.tenant_id = operation.tenant_id
                        AND inspection.document_version_id = operation.document_version_id
                      WHERE work.tenant_id = %s
                        AND work.processing_work_item_id = %s
                        AND work.processing_operation_id = %s
                        AND work.current_processing_attempt_id = %s
                        AND work.fencing_token = %s
                        AND work.state = 'leased' AND work.leased_until > now()
                        AND attempt.state = 'running' AND attempt.fencing_token = %s
                        AND operation.operation_kind = 'general_document_understanding'
                        AND operation.cancellation_requested_at IS NULL
                        AND document.state IN ('uploaded', 'processing', 'validated', 'active')
                        AND version.version_state = 'current'
                        AND artifact.retention_state IN ('active', 'held')
                        AND artifact.integrity_state = 'verified'
                        AND inspection.policy_version = 'v1'
                        AND inspection.disposition = 'accepted'""",
                    (
                        lease.tenant_id,
                        lease.processing_work_item_id,
                        lease.processing_operation_id,
                        lease.processing_attempt_id,
                        lease.fencing_token,
                        lease.fencing_token,
                    ),
                )
                row = cursor.fetchone()
        if row is None:
            raise OpenAIProviderError(
                "understanding_source_not_eligible",
                retryable=False,
                message="The source is not eligible for OpenAI understanding.",
            )
        structural_scopes = self._load_structural_scopes(lease=lease)
        return EligibleUnderstandingSource(
            document_version_id=UUID(str(row[0])),
            source_artifact_id=UUID(str(row[1])),
            storage_key=str(row[2]),
            content_type=str(row[3]),
            size_bytes=int(row[4]),
            checksum_sha256=str(row[5]),
            structural_scopes=structural_scopes,
        )

    def _load_structural_scopes(
        self, *, lease: ProcessingAttemptLease
    ) -> tuple[EligibleUnderstandingStructuralScope, ...]:
        with connect_document_ai_database(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT scope.structural_scope_id, scope.scope_kind, scope.scope_ordinal,
                           scope.scope_identity, scope.structural_coordinates, scope.scope_payload
                    FROM document_ai_structural_scopes AS scope
                    JOIN document_ai_processing_operations AS operation
                      ON operation.tenant_id = scope.tenant_id
                     AND operation.processing_operation_id = scope.processing_operation_id
                    JOIN document_ai_processing_work_items AS work
                      ON work.tenant_id = operation.tenant_id
                     AND work.processing_operation_id = operation.processing_operation_id
                    JOIN document_ai_processing_attempts AS attempt
                      ON attempt.tenant_id = work.tenant_id
                     AND attempt.processing_attempt_id = work.current_processing_attempt_id
                    WHERE scope.tenant_id = %s
                      AND scope.processing_operation_id = %s
                      AND scope.policy_version = 'v1'
                      AND work.processing_work_item_id = %s
                      AND work.current_processing_attempt_id = %s
                      AND work.fencing_token = %s
                      AND work.state = 'leased'
                      AND work.leased_until > now()
                      AND attempt.state = 'running'
                      AND attempt.fencing_token = %s
                    ORDER BY scope.scope_ordinal ASC
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
                rows = cursor.fetchall()
        return tuple(
            EligibleUnderstandingStructuralScope(
                structural_scope_id=UUID(str(row[0])),
                scope_kind=str(row[1]),
                scope_ordinal=int(row[2]),
                scope_identity=str(row[3]),
                structural_coordinates=dict(row[4]),
                scope_payload=dict(row[5]),
            )
            for row in rows
        )


class OpenAIUnderstandingWorkExecutor:
    """Execute one general-understanding work item without any legacy fallback."""

    def __init__(
        self,
        *,
        repository: OpenAIUnderstandingRepository,
        result_repository: ProviderResultRepository,
        storage: StorageAdapterProtocol,
        client: GovernedOpenAIClient,
    ) -> None:
        self._repository = repository
        self._result_repository = result_repository
        self._storage = storage
        self._client = client

    def execute(
        self, *, lease: ProcessingAttemptLease, checkpoint: DurableCheckpoint | None
    ) -> str:
        """Persist a validated result before the generic worker can commit completion."""

        del checkpoint
        existing = self._repository.existing_result_reference(lease=lease)
        if existing is not None:
            return existing
        source = self._repository.load_eligible_source(lease=lease)
        details = self._build_reservation_details(lease=lease, source=source)
        reservation = self._result_repository.reserve(lease=lease, details=details)
        if reservation.result_reference is not None:
            return reservation.result_reference
        if not reservation.can_call_provider:
            raise OpenAIProviderError(
                "provider_result_reserved",
                retryable=True,
                message="The provider operation is already reserved by another worker.",
            )
        prepared = self._prepare_source(source=source)
        # Re-read the gate after local source preparation and before the
        # external side effect to avoid using a stale durable graph.
        self._repository.load_eligible_source(lease=lease)
        reservation = self._result_repository.mark_in_progress(
            lease=lease, reservation=reservation
        )
        if reservation.result_reference is not None:
            return reservation.result_reference
        result = self._client.understand(
            GovernedOpenAIRequest(
                processing_operation_id=str(lease.processing_operation_id),
                processing_attempt_id=str(lease.processing_attempt_id),
                tenant_id=lease.tenant_id,
                source=prepared,
            )
        )
        reference = self._result_repository.persist(
            lease=lease,
            details=details,
            reservation=reservation,
            result=result,
        )
        if reference is None:
            raise OpenAIProviderError(
                "stale_understanding_result",
                retryable=False,
                message="The OpenAI result could not be committed by this worker attempt.",
            )
        return reference

    def _build_reservation_details(
        self, *, lease: ProcessingAttemptLease, source: EligibleUnderstandingSource
    ) -> ProviderResultReservationDetails:
        model = getattr(self._client, "model", "gpt-4.1-mini")
        request_fingerprint = compute_canonical_hash(
            {
                "tenant_id": lease.tenant_id,
                "processing_operation_id": str(lease.processing_operation_id),
                "processing_work_item_id": str(lease.processing_work_item_id),
                "processing_attempt_id": str(lease.processing_attempt_id),
                "document_version_id": str(source.document_version_id),
                "source_artifact_id": str(source.source_artifact_id),
                "source_scope_id": str(source.source_artifact_id),
                "structural_scope_ids": [
                    str(structural_scope.structural_scope_id)
                    for structural_scope in source.structural_scopes
                ],
                "source_checksum_sha256": source.checksum_sha256,
                "source_size_bytes": source.size_bytes,
                "model": model,
                "processing_policy_version": "v1",
                "prompt_version": "general-document-understanding-v1",
                "canonical_schema_version": "v1",
            }
        ).sha256_hex
        return ProviderResultReservationDetails(
            request_fingerprint=request_fingerprint,
            model=model,
            processing_policy_version="v1",
            prompt_version="general-document-understanding-v1",
            canonical_schema_version="v1",
            document_version_id=str(source.document_version_id),
            source_artifact_id=str(source.source_artifact_id),
            source_scope_id=str(source.source_artifact_id),
            structural_scope_ids=tuple(
                str(structural_scope.structural_scope_id)
                for structural_scope in source.structural_scopes
            ),
            source_checksum_sha256=source.checksum_sha256,
            source_size_bytes=source.size_bytes,
        )

    def _prepare_source(self, *, source: EligibleUnderstandingSource) -> PreparedOpenAISource:
        if source.size_bytes > MAX_OPENAI_SOURCE_BYTES:
            raise OpenAIProviderError(
                "provider_input_unsupported",
                retryable=False,
                message="The inspected source is not supported for OpenAI understanding.",
            )
        source_path, media_type = self._storage.resolve_download_object(source.storage_key)
        try:
            if media_type != source.content_type or source_path.stat().st_size != source.size_bytes:
                raise OpenAIProviderError(
                    "provider_input_mismatch",
                    retryable=False,
                    message="The retrieved source does not match its durable artifact record.",
                )
            with source_path.open("rb") as handle:
                content = handle.read(source.size_bytes + 1)
            if (
                len(content) != source.size_bytes
                or sha256(content).hexdigest() != source.checksum_sha256
            ):
                raise OpenAIProviderError(
                    "provider_input_mismatch",
                    retryable=False,
                    message="The retrieved source does not match its durable artifact record.",
                )
            return PreparedOpenAISource(
                document_version_id=str(source.document_version_id),
                source_scope_id=str(source.source_artifact_id),
                media_type=source.content_type,
                content=content,
                structural_scope_ids=tuple(
                    str(structural_scope.structural_scope_id)
                    for structural_scope in source.structural_scopes
                ),
                structural_scope_manifest=tuple(
                    {
                        "structural_scope_id": str(structural_scope.structural_scope_id),
                        "scope_kind": structural_scope.scope_kind,
                        "scope_ordinal": structural_scope.scope_ordinal,
                        "scope_identity": structural_scope.scope_identity,
                        "structural_coordinates": structural_scope.structural_coordinates,
                        "scope_payload": structural_scope.scope_payload,
                    }
                    for structural_scope in source.structural_scopes
                ),
            )
        finally:
            if source_path.name.startswith("document-ai-download-"):
                source_path.unlink(missing_ok=True)
