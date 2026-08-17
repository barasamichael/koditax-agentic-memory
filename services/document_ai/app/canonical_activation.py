"""Durably validate canonical candidates and activate only governed results."""

from __future__ import annotations

import json
from uuid import UUID
from typing import Literal
from typing import Protocol
from dataclasses import dataclass
from collections.abc import Mapping

from services.document_ai.app.config import get_document_ai_embedding_model
from services.document_ai.app.openai_embeddings import EMBEDDING_VERSION
from services.document_ai.app.openai_embeddings import DOCUMENT_AI_EMBEDDING_DIMENSIONS
from services.document_ai.app.canonical_assembly import CanonicalElement
from services.document_ai.app.canonical_assembly import assemble_canonical_graph
from services.document_ai.app.canonical_chunking import RetrievalChunk
from services.document_ai.app.canonical_chunking import build_retrieval_chunks
from services.document_ai.app.canonical_chunking import CANONICAL_CHUNKING_POLICY_VERSION
from services.document_ai.app.processing_workers import DurableCheckpoint
from services.document_ai.app.processing_workers import ProcessingAttemptLease
from services.document_ai.app.persistence_support import connect_document_ai_database
from services.document_ai.app.persistence_support import execute_document_ai_database_transaction
from services.document_ai.app.canonical_validation import CANONICAL_VALIDATION_VERSION


class _Cursor(Protocol):
    def execute(self, query: str, params: tuple[object, ...]) -> object: ...

    def fetchone(self) -> tuple[object, ...] | None: ...

    def fetchall(self) -> list[tuple[object, ...]]: ...


@dataclass(frozen=True)
class CanonicalActivationResult:
    """Represent one replay-safe canonical activation decision."""

    state: Literal["activated", "replayed"]
    tenant_id: str
    document_id: UUID
    document_version_id: UUID
    canonical_representation_id: UUID
    previous_active_canonical_representation_id: UUID | None


@dataclass(frozen=True)
class _CanonicalActivationPlan:
    tenant_id: str
    document_id: UUID
    document_version_id: UUID
    canonical_representation_id: UUID
    processing_policy_family: str
    candidate_state: str
    candidate_is_active: bool
    current_active_canonical_representation_id: UUID | None
    canonical_validation_version: str
    validation_report: dict[str, object]
    expected_chunks: tuple[RetrievalChunk, ...]
    matching_embedding_chunk_ids: tuple[UUID, ...]

    @property
    def chunk_count(self) -> int:
        return len(self.expected_chunks)

    @property
    def matching_embedding_count(self) -> int:
        return len(self.matching_embedding_chunk_ids)


class CanonicalActivationRepository:
    """The single transactional authority for candidate validation and activation."""

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def activate_for_lease(self, *, lease: ProcessingAttemptLease) -> str:
        """Persist a deterministic canonical candidate for later validation."""

        with connect_document_ai_database(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT representation.canonical_representation_id
                       FROM document_ai_canonical_representations AS representation
                       JOIN document_ai_processing_operations AS operation
                         ON operation.tenant_id = representation.tenant_id
                        AND operation.processing_operation_id =
                            representation.processing_operation_id
                      WHERE representation.tenant_id = %s
                        AND operation.processing_operation_id = %s
                      ORDER BY representation.created_at ASC LIMIT 1 FOR UPDATE""",
                    (lease.tenant_id, lease.processing_operation_id),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    connection.commit()
                    return f"canonical-representation:{existing[0]}"
                cursor.execute(
                    """SELECT operation.document_version_id, result.provider_result_id,
                              result.source_artifact_id, result.validated_result,
                              inspection.page_count, inspection.source_inspection_id
                       FROM document_ai_processing_work_items AS work
                       JOIN document_ai_processing_operations AS operation
                         ON operation.tenant_id = work.tenant_id
                        AND operation.processing_operation_id = work.processing_operation_id
                       JOIN document_ai_processing_attempts AS attempt
                         ON attempt.tenant_id = work.tenant_id
                        AND attempt.processing_attempt_id = work.current_processing_attempt_id
                       JOIN document_ai_provider_results AS result
                         ON result.tenant_id = operation.tenant_id
                        AND result.provider_result_id::text =
                            operation.request_payload->>'provider_result_id'
                        AND result.source_artifact_id::text =
                            operation.request_payload->>'source_artifact_id'
                       JOIN document_ai_source_inspections AS inspection
                         ON inspection.tenant_id = operation.tenant_id
                        AND inspection.document_version_id = operation.document_version_id
                        AND inspection.source_artifact_id = result.source_artifact_id
                      WHERE work.tenant_id = %s AND work.processing_work_item_id = %s
                        AND work.processing_operation_id = %s
                        AND work.current_processing_attempt_id = %s
                        AND work.fencing_token = %s AND work.state = 'leased'
                        AND work.leased_until > now() AND attempt.state = 'running'
                        AND attempt.fencing_token = %s
                        AND operation.operation_kind = 'canonical_assembly'
                        AND operation.cancellation_requested_at IS NULL
                        AND inspection.disposition = 'accepted' AND inspection.policy_version = 'v1'
                      FOR UPDATE""",
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
                    raise ValueError("canonical_candidate_not_eligible")
                (
                    version_id,
                    provider_result_id,
                    artifact_id,
                    validated_result,
                    page_count,
                    source_inspection_id,
                ) = row
                source_lineage = {
                    "document_version_id": str(version_id),
                    "source_artifact_id": str(artifact_id),
                    "source_inspection_id": str(source_inspection_id),
                    "provider_result_id": str(provider_result_id),
                    "processing_operation_id": str(lease.processing_operation_id),
                    "page_count": int(page_count) if page_count is not None else None,
                }
                graph = assemble_canonical_graph(
                    provider_result_id=provider_result_id,
                    source_artifact_id=artifact_id,
                    validated_result=dict(validated_result),
                    source_lineage=source_lineage,
                )
                cursor.execute(
                    """INSERT INTO document_ai_canonical_representations (
                           tenant_id, document_version_id, processing_operation_id,
                           canonical_schema_version, processing_policy_family, state,
                           source_artifact_id, provider_result_id, assembly_policy_version,
                           content_hash_sha256, representation_payload,
                           canonical_validation_version, validation_report, readiness_state
                       ) VALUES (%s, %s, %s, 'v1', 'general-document-understanding', 'candidate',
                                 %s, %s, 'v1', %s, %s::jsonb, NULL, '{}'::jsonb, 'none')
                       RETURNING canonical_representation_id""",
                    (
                        lease.tenant_id,
                        version_id,
                        lease.processing_operation_id,
                        artifact_id,
                        provider_result_id,
                        graph.content_hash,
                        json.dumps(graph.payload),
                    ),
                )
                representation_row = cursor.fetchone()
                if representation_row is None:
                    raise RuntimeError("canonical_representation_insert_failed")
                representation_id = UUID(str(representation_row[0]))
                for ordinal, element in enumerate(graph.elements):
                    cursor.execute(
                        """INSERT INTO document_ai_canonical_elements (
                               tenant_id, canonical_representation_id, element_type, ordinal,
                               observed_value, normalized_value, uncertainty, stable_key,
                               page_number, reading_order
                           ) VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s)
                           RETURNING canonical_element_id""",
                        (
                            lease.tenant_id,
                            representation_id,
                            element.element_type,
                            ordinal,
                            json.dumps(element.observed_value),
                            json.dumps(element.normalized_value),
                            json.dumps(element.uncertainty),
                            element.stable_key,
                            element.page_number,
                            element.reading_order,
                        ),
                    )
                    element_row = cursor.fetchone()
                    if element_row is None:
                        raise RuntimeError("canonical_element_insert_failed")
                    element_id = element_row[0]
                    cursor.execute(
                        """INSERT INTO document_ai_source_regions (
                               tenant_id, source_artifact_id, canonical_element_id,
                               structural_unit_kind, structural_unit_index, region_payload
                           ) VALUES (%s, %s, %s, 'page', %s, %s::jsonb)""",
                        (
                            lease.tenant_id,
                            artifact_id,
                            element_id,
                            element.page_number,
                            json.dumps(
                                {
                                    "source_region": element.source_region,
                                    "source_lineage": source_lineage,
                                    "element_lineage": element.lineage,
                                },
                                sort_keys=True,
                            ),
                        ),
                    )
            connection.commit()
        return f"canonical-representation:{representation_id}"

    def activate_validated_candidate(
        self, *, tenant_id: str, canonical_representation_id: UUID
    ) -> CanonicalActivationResult:
        """Atomically switch authority only after a complete candidate vector set exists."""

        return execute_document_ai_database_transaction(
            database_url=self._database_url,
            transaction_name="document_ai.canonical_activation.activate_candidate",
            transaction_callback=lambda cursor: self._activate_transaction(
                cursor=cursor,
                tenant_id=tenant_id,
                canonical_representation_id=canonical_representation_id,
            ),
            reconcile_ambiguous_result=lambda connection: self._reconcile_activation_result(
                connection=connection,
                tenant_id=tenant_id,
                canonical_representation_id=canonical_representation_id,
            ),
        )

    def _activate_transaction(
        self,
        *,
        cursor: _Cursor,
        tenant_id: str,
        canonical_representation_id: UUID,
    ) -> CanonicalActivationResult:
        plan = self._load_activation_plan(
            cursor=cursor,
            tenant_id=tenant_id,
            canonical_representation_id=canonical_representation_id,
        )
        if plan.chunk_count < 1:
            raise ValueError("canonical_candidate_chunk_count_missing")
        if plan.matching_embedding_count != plan.chunk_count:
            raise ValueError("canonical_candidate_vectors_incomplete")
        if plan.candidate_is_active or plan.current_active_canonical_representation_id == (
            plan.canonical_representation_id
        ):
            return self._result_from_plan(plan=plan, state="replayed")

        if plan.current_active_canonical_representation_id is not None:
            cursor.execute(
                """
                UPDATE document_ai_canonical_representations
                   SET is_active = FALSE, state = 'superseded'
                 WHERE tenant_id = %s
                   AND canonical_representation_id = %s
                   AND is_active
                """,
                (
                    tenant_id,
                    plan.current_active_canonical_representation_id,
                ),
            )
        cursor.execute(
            """
            UPDATE document_ai_canonical_representations
               SET state = 'active',
                   is_active = TRUE,
                   activated_at = COALESCE(activated_at, now())
             WHERE tenant_id = %s
               AND canonical_representation_id = %s
            """,
            (tenant_id, canonical_representation_id),
        )
        return self._result_from_plan(plan=plan, state="activated")

    def _load_activation_plan(
        self,
        *,
        cursor: _Cursor,
        tenant_id: str,
        canonical_representation_id: UUID,
    ) -> _CanonicalActivationPlan:
        cursor.execute(
            """
            SELECT representation.tenant_id, document.document_id,
                   representation.document_version_id, representation.processing_policy_family,
                   representation.state, representation.is_active,
                   representation.canonical_validation_version,
                   representation.validation_report,
                   representation.representation_payload
              FROM document_ai_canonical_representations AS representation
              JOIN document_ai_document_versions AS version
                ON version.tenant_id = representation.tenant_id
               AND version.document_version_id = representation.document_version_id
              JOIN document_ai_documents AS document
                ON document.tenant_id = version.tenant_id
               AND document.document_id = version.document_id
             WHERE representation.tenant_id = %s
               AND representation.canonical_representation_id = %s
               AND representation.readiness_state = 'full'
               AND representation.state IN ('validated', 'active')
               AND representation.canonical_validation_version = %s
               AND version.version_state = 'current'
               AND document.active_document_version_id = representation.document_version_id
               AND document.state NOT IN (
                   'trashed', 'purge_pending', 'eligible_for_purge', 'purged'
               )
             FOR UPDATE OF representation, version, document
            """,
            (tenant_id, canonical_representation_id, CANONICAL_VALIDATION_VERSION),
        )
        row = cursor.fetchone()
        if row is None:
            raise ValueError("canonical_candidate_not_ready_for_activation")

        validation_report = dict(row[7]) if isinstance(row[7], Mapping) else {}
        if validation_report.get("canonical_validation_version") != CANONICAL_VALIDATION_VERSION:
            raise ValueError("canonical_candidate_validation_mismatch")
        if validation_report.get("reason_codes"):
            raise ValueError("canonical_candidate_validation_mismatch")

        representation_payload = dict(row[8]) if isinstance(row[8], Mapping) else {}
        source_lineage = representation_payload.get("source_lineage")
        if not isinstance(source_lineage, Mapping):
            raise ValueError("canonical_candidate_generation_mismatch")
        elements = self._load_elements(
            cursor=cursor,
            tenant_id=tenant_id,
            canonical_representation_id=canonical_representation_id,
        )
        expected_chunks = build_retrieval_chunks(
            elements=elements,
            source_lineage=source_lineage,
            chunking_policy_version=CANONICAL_CHUNKING_POLICY_VERSION,
        )
        persisted_chunks = self._load_persisted_chunks(
            cursor=cursor,
            tenant_id=tenant_id,
            canonical_representation_id=canonical_representation_id,
        )
        if len(expected_chunks) != len(persisted_chunks):
            raise ValueError("canonical_candidate_generation_mismatch")
        for expected_chunk, persisted_chunk in zip(expected_chunks, persisted_chunks, strict=True):
            if expected_chunk.chunk_key != persisted_chunk.chunk_key:
                raise ValueError("canonical_candidate_generation_mismatch")
            if expected_chunk.content_hash != persisted_chunk.content_hash:
                raise ValueError("canonical_candidate_generation_mismatch")
            if expected_chunk.embedding_text != persisted_chunk.embedding_text:
                raise ValueError("canonical_candidate_generation_mismatch")
            if expected_chunk.canonical_element_keys != persisted_chunk.canonical_element_keys:
                raise ValueError("canonical_candidate_generation_mismatch")
            if expected_chunk.source_location != persisted_chunk.source_location:
                raise ValueError("canonical_candidate_generation_mismatch")
            if expected_chunk.structural_context != persisted_chunk.structural_context:
                raise ValueError("canonical_candidate_generation_mismatch")
            if persisted_chunk.chunking_policy_version != CANONICAL_CHUNKING_POLICY_VERSION:
                raise ValueError("canonical_candidate_generation_mismatch")

        cursor.execute(
            """
            SELECT current_representation.canonical_representation_id
              FROM document_ai_canonical_representations AS current_representation
             WHERE current_representation.tenant_id = %s
               AND current_representation.document_version_id = %s
               AND current_representation.processing_policy_family = %s
               AND current_representation.is_active
             FOR UPDATE
            """,
            (tenant_id, row[2], row[3]),
        )
        current_row = cursor.fetchone()
        current_active_canonical_representation_id = (
            UUID(str(current_row[0])) if current_row is not None else None
        )

        cursor.execute(
            """SELECT embedding.retrieval_chunk_id, embedding.content_hash_sha256,
                      embedding.chunking_policy_version, embedding.embedding_model,
                      embedding.embedding_version, embedding.embedding_dimensions,
                      embedding.index_state
                 FROM document_ai_retrieval_chunks AS chunk
                 JOIN document_ai_chunk_embeddings AS embedding
                   ON embedding.tenant_id = chunk.tenant_id
                  AND embedding.retrieval_chunk_id = chunk.retrieval_chunk_id
                  AND embedding.document_version_id = chunk.document_version_id
                  AND embedding.canonical_representation_id = chunk.canonical_representation_id
                  AND embedding.content_hash_sha256 = chunk.content_hash_sha256
                  AND embedding.chunking_policy_version = chunk.chunking_policy_version
                  AND embedding.embedding_model = %s
                  AND embedding.embedding_version = %s
                  AND embedding.embedding_dimensions = %s
                  AND embedding.index_state = 'active'
                WHERE chunk.tenant_id = %s
                  AND chunk.canonical_representation_id = %s
                  AND chunk.lifecycle_state = 'active'
                ORDER BY COALESCE((chunk.structural_context->>'chunk_ordinal')::INT, 0) ASC,
                         chunk.created_at ASC, chunk.chunk_key ASC""",
            (
                get_document_ai_embedding_model(),
                EMBEDDING_VERSION,
                DOCUMENT_AI_EMBEDDING_DIMENSIONS,
                tenant_id,
                canonical_representation_id,
            ),
        )
        embedding_rows = cursor.fetchall()
        if len(embedding_rows) != len(expected_chunks):
            raise ValueError("canonical_candidate_vectors_incomplete")
        matching_embedding_chunk_ids: list[UUID] = []
        for expected_chunk, persisted_chunk, row_item in zip(
            expected_chunks, persisted_chunks, embedding_rows, strict=True
        ):
            chunk_id = UUID(str(row_item[0]))
            if persisted_chunk.chunk_key != expected_chunk.chunk_key:
                raise ValueError("canonical_candidate_vectors_incomplete")
            if str(row_item[1]) != expected_chunk.content_hash:
                raise ValueError("canonical_candidate_vectors_incomplete")
            if str(row_item[2]) != expected_chunk.chunking_policy_version:
                raise ValueError("canonical_candidate_vectors_incomplete")
            if str(row_item[3]) != get_document_ai_embedding_model():
                raise ValueError("canonical_candidate_vectors_incomplete")
            if str(row_item[4]) != EMBEDDING_VERSION:
                raise ValueError("canonical_candidate_vectors_incomplete")
            if int(row_item[5]) != DOCUMENT_AI_EMBEDDING_DIMENSIONS:
                raise ValueError("canonical_candidate_vectors_incomplete")
            if str(row_item[6]) != "active":
                raise ValueError("canonical_candidate_vectors_incomplete")
            matching_embedding_chunk_ids.append(chunk_id)
        return _CanonicalActivationPlan(
            tenant_id=str(row[0]),
            document_id=UUID(str(row[1])),
            document_version_id=UUID(str(row[2])),
            canonical_representation_id=canonical_representation_id,
            processing_policy_family=str(row[3]),
            candidate_state=str(row[4]),
            candidate_is_active=bool(row[5]),
            current_active_canonical_representation_id=current_active_canonical_representation_id,
            canonical_validation_version=str(row[6]),
            validation_report=validation_report,
            expected_chunks=expected_chunks,
            matching_embedding_chunk_ids=tuple(matching_embedding_chunk_ids),
        )

    def _load_elements(
        self,
        *,
        cursor: _Cursor,
        tenant_id: str,
        canonical_representation_id: UUID,
    ) -> tuple[CanonicalElement, ...]:
        cursor.execute(
            """
            SELECT element.stable_key, element.element_type, element.page_number,
                   element.reading_order, element.observed_value, element.normalized_value,
                   element.uncertainty, region.region_payload, element.canonical_element_id
              FROM document_ai_canonical_elements AS element
              LEFT JOIN document_ai_source_regions AS region
                ON region.tenant_id = element.tenant_id
               AND region.canonical_element_id = element.canonical_element_id
             WHERE element.tenant_id = %s
               AND element.canonical_representation_id = %s
             ORDER BY element.ordinal ASC
            """,
            (tenant_id, canonical_representation_id),
        )
        rows = cursor.fetchall()
        elements: list[CanonicalElement] = []
        for row in rows:
            region_payload = row[7] if isinstance(row[7], Mapping) else {}
            source_region_raw = (
                region_payload.get("source_region", {})
                if isinstance(region_payload, Mapping)
                else {}
            )
            source_region = (
                dict(source_region_raw) if isinstance(source_region_raw, Mapping) else {}
            )
            if not source_region:
                source_region = {"page_number": int(row[2])}
            element_lineage_raw = (
                region_payload.get("element_lineage", {})
                if isinstance(region_payload, Mapping)
                else {}
            )
            lineage_map = (
                dict(element_lineage_raw) if isinstance(element_lineage_raw, Mapping) else {}
            )
            lineage_map["canonical_element_id"] = str(row[8])
            elements.append(
                CanonicalElement(
                    stable_key=str(row[0]),
                    element_type=str(row[1]),
                    page_number=int(row[2]),
                    reading_order=int(row[3]),
                    observed_value=dict(row[4]) if isinstance(row[4], Mapping) else None,
                    normalized_value=dict(row[5]) if isinstance(row[5], Mapping) else None,
                    uncertainty=dict(row[6]) if isinstance(row[6], Mapping) else {},
                    source_region=source_region,
                    lineage=lineage_map,
                )
            )
        return tuple(elements)

    def _load_persisted_chunks(
        self,
        *,
        cursor: _Cursor,
        tenant_id: str,
        canonical_representation_id: UUID,
    ) -> tuple[RetrievalChunk, ...]:
        cursor.execute(
            """
            SELECT chunk.retrieval_chunk_id, chunk.chunk_key, chunk.content_hash_sha256,
                   chunk.chunking_policy_version, chunk.embedding_text,
                   chunk.canonical_element_keys, chunk.source_location,
                   chunk.structural_context, chunk.lifecycle_state
              FROM document_ai_retrieval_chunks AS chunk
             WHERE chunk.tenant_id = %s
               AND chunk.canonical_representation_id = %s
             ORDER BY COALESCE((chunk.structural_context->>'chunk_ordinal')::INT, 0) ASC,
                      chunk.created_at ASC, chunk.chunk_key ASC
             FOR UPDATE
            """,
            (tenant_id, canonical_representation_id),
        )
        rows = cursor.fetchall()
        chunks: list[RetrievalChunk] = []
        for ordinal, row in enumerate(rows):
            if str(row[8]) != "active":
                raise ValueError("canonical_candidate_generation_mismatch")
            canonical_element_keys_raw = row[5] if isinstance(row[5], list) else []
            source_location = dict(row[6]) if isinstance(row[6], Mapping) else {}
            structural_context = dict(row[7]) if isinstance(row[7], Mapping) else {}
            source_lineage_raw = structural_context.get("source_lineage", {})
            source_lineage = (
                dict(source_lineage_raw) if isinstance(source_lineage_raw, Mapping) else {}
            )
            chunks.append(
                RetrievalChunk(
                    chunk_key=str(row[1]),
                    chunk_ordinal=ordinal,
                    generation_identity=str(row[1]),
                    content_hash=str(row[2]),
                    embedding_text=str(row[4]),
                    canonical_element_keys=tuple(
                        str(value) for value in canonical_element_keys_raw
                    ),
                    source_location=source_location,
                    source_lineage=source_lineage,
                    structural_context=structural_context,
                    chunking_policy_version=str(row[3]),
                )
            )
        return tuple(chunks)

    def _reconcile_activation_result(
        self,
        *,
        connection: object,
        tenant_id: str,
        canonical_representation_id: UUID,
    ) -> CanonicalActivationResult | None:
        with connection.cursor() as cursor:
            try:
                plan = self._load_activation_plan(
                    cursor=cursor,
                    tenant_id=tenant_id,
                    canonical_representation_id=canonical_representation_id,
                )
            except ValueError:
                return None
        if not plan.candidate_is_active:
            return None
        return self._result_from_plan(plan=plan, state="replayed")

    @staticmethod
    def _result_from_plan(
        *,
        plan: _CanonicalActivationPlan,
        state: Literal["activated", "replayed"],
    ) -> CanonicalActivationResult:
        return CanonicalActivationResult(
            state=state,
            tenant_id=plan.tenant_id,
            document_id=plan.document_id,
            document_version_id=plan.document_version_id,
            canonical_representation_id=plan.canonical_representation_id,
            previous_active_canonical_representation_id=plan.current_active_canonical_representation_id,
        )


class CanonicalActivationWorkExecutor:
    """Use the shared fenced worker without performing any provider or index work."""

    def __init__(self, *, repository: CanonicalActivationRepository) -> None:
        self._repository = repository

    def execute(
        self, *, lease: ProcessingAttemptLease, checkpoint: DurableCheckpoint | None
    ) -> str:
        del checkpoint
        return self._repository.activate_for_lease(lease=lease)
