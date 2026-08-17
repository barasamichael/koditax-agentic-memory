"""Durable canonical retrieval-chunk generation and continuation gating."""

from __future__ import annotations

import json
from uuid import UUID
from typing import Protocol
from typing import cast
from typing import Literal
from dataclasses import dataclass
from collections.abc import Mapping
from collections.abc import Sequence

from services.document_ai.app.canonical_assembly import CanonicalElement
from services.document_ai.app.canonical_chunking import CANONICAL_CHUNKING_POLICY_VERSION
from services.document_ai.app.canonical_chunking import RetrievalChunk
from services.document_ai.app.canonical_chunking import build_retrieval_chunks
from services.document_ai.app.canonical_validation import CanonicalValidationError
from services.document_ai.app.persistence_support import connect_document_ai_database
from services.document_ai.app.persistence_support import execute_document_ai_database_transaction

CANONICAL_CHUNK_GENERATION_CONTINUATION_EVENT = "embedding_generation_requested"


class _Cursor(Protocol):
    def execute(self, query: str, params: tuple[object, ...]) -> object: ...

    def fetchone(self) -> Sequence[object] | None: ...

    def fetchall(self) -> list[Sequence[object]]: ...


@dataclass(frozen=True)
class CanonicalChunkGenerationResult:
    """Represent one durable chunk-generation decision."""

    state: Literal["generated", "replayed"]
    chunk_count: int
    chunk_generation_identity: str
    chunk_keys: tuple[str, ...]
    continuation_event: str
    validation_version: str
    validation_report: dict[str, object]


class CanonicalChunkGenerationRepository:
    """Persist validated canonical chunks and their replay-safe continuation."""

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def generate_for_representation(
        self,
        *,
        tenant_id: str,
        canonical_representation_id: UUID,
    ) -> CanonicalChunkGenerationResult:
        return execute_document_ai_database_transaction(
            database_url=self._database_url,
            transaction_name="document_ai.canonical_chunk_generation.generate",
            transaction_callback=lambda cursor: self._generate_transaction(
                cursor=cursor,
                tenant_id=tenant_id,
                canonical_representation_id=canonical_representation_id,
            ),
            reconcile_ambiguous_result=lambda connection: self._reconcile_generation_result(
                connection=connection,
                tenant_id=tenant_id,
                canonical_representation_id=canonical_representation_id,
            ),
        )

    def generate_for_lease(
        self,
        *,
        lease: object,
    ) -> CanonicalChunkGenerationResult:
        tenant_id = getattr(lease, "tenant_id", None)
        processing_operation_id = getattr(lease, "processing_operation_id", None)
        processing_work_item_id = getattr(lease, "processing_work_item_id", None)
        processing_attempt_id = getattr(lease, "processing_attempt_id", None)
        fencing_token = getattr(lease, "fencing_token", None)
        if not isinstance(tenant_id, str):
            raise CanonicalValidationError(
                "canonical_chunk_generation_invalid_lease",
                retryable=False,
                message="The supplied lease is missing tenant identity.",
            )
        if not isinstance(processing_operation_id, UUID):
            raise CanonicalValidationError(
                "canonical_chunk_generation_invalid_lease",
                retryable=False,
                message="The supplied lease is missing processing operation identity.",
            )
        if not isinstance(processing_work_item_id, UUID) or not isinstance(processing_attempt_id, UUID):
            raise CanonicalValidationError(
                "canonical_chunk_generation_invalid_lease",
                retryable=False,
                message="The supplied lease is missing worker identity.",
            )
        if not isinstance(fencing_token, int):
            raise CanonicalValidationError(
                "canonical_chunk_generation_invalid_lease",
                retryable=False,
                message="The supplied lease is missing fencing token.",
            )

        with connect_document_ai_database(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT representation.canonical_representation_id
                      FROM document_ai_canonical_representations AS representation
                      JOIN document_ai_processing_operations AS operation
                        ON operation.tenant_id = representation.tenant_id
                       AND operation.processing_operation_id =
                           representation.processing_operation_id
                      JOIN document_ai_processing_work_items AS work
                        ON work.tenant_id = operation.tenant_id
                       AND work.processing_operation_id = operation.processing_operation_id
                     WHERE representation.tenant_id = %s
                       AND operation.processing_operation_id = %s
                       AND work.processing_work_item_id = %s
                       AND work.current_processing_attempt_id = %s
                       AND work.fencing_token = %s
                       AND work.state = 'leased'
                       AND work.leased_until > now()
                       AND representation.state = 'validated'
                       AND representation.readiness_state = 'full'
                     FOR UPDATE""",
                    (
                        tenant_id,
                        processing_operation_id,
                        processing_work_item_id,
                        processing_attempt_id,
                        fencing_token,
                    ),
                )
                row = cursor.fetchone()
        if row is None:
            raise CanonicalValidationError(
                "canonical_chunk_generation_not_available",
                retryable=True,
                message="The validated canonical candidate is not available for chunking.",
            )
        return self.generate_for_representation(
            tenant_id=tenant_id, canonical_representation_id=UUID(str(row[0]))
        )

    def _generate_transaction(
        self,
        *,
        cursor: _Cursor,
        tenant_id: str,
        canonical_representation_id: UUID,
    ) -> CanonicalChunkGenerationResult:
        candidate = self._load_candidate(
            cursor=cursor,
            tenant_id=tenant_id,
            canonical_representation_id=canonical_representation_id,
        )
        if candidate is None:
            raise CanonicalValidationError(
                "canonical_chunk_generation_candidate_missing",
                retryable=True,
                message="The canonical candidate is not yet durably visible.",
            )

        expected_chunks = build_retrieval_chunks(
            elements=cast(tuple[CanonicalElement, ...], candidate["elements"]),
            source_lineage=cast(Mapping[str, object], candidate["source_lineage"]),
            chunking_policy_version=CANONICAL_CHUNKING_POLICY_VERSION,
        )
        existing_chunks = self._load_existing_chunks(
            cursor=cursor,
            tenant_id=tenant_id,
            canonical_representation_id=canonical_representation_id,
        )
        if existing_chunks:
            self._ensure_chunk_replay_matches(
                expected_chunks=expected_chunks, existing_chunks=existing_chunks
            )
            self._ensure_continuation_outbox(cursor=cursor, candidate=candidate, chunks=existing_chunks)
            return self._result_from_chunks(
                candidate=candidate, chunks=existing_chunks, state="replayed"
            )

        for chunk in expected_chunks:
            self._insert_chunk(cursor=cursor, candidate=candidate, chunk=chunk)
        self._ensure_continuation_outbox(cursor=cursor, candidate=candidate, chunks=expected_chunks)
        return self._result_from_chunks(candidate=candidate, chunks=expected_chunks, state="generated")

    def _load_candidate(
        self,
        *,
        cursor: _Cursor,
        tenant_id: str,
        canonical_representation_id: UUID,
    ) -> dict[str, object] | None:
        cursor.execute(
            """
            SELECT representation.tenant_id, version.document_id,
                   representation.canonical_representation_id,
                   representation.document_version_id,
                   representation.processing_operation_id,
                   representation.source_artifact_id,
                   representation.provider_result_id,
                   representation.canonical_validation_version,
                   representation.validation_report,
                   representation.representation_payload
              FROM document_ai_canonical_representations AS representation
              JOIN document_ai_document_versions AS version
                ON version.tenant_id = representation.tenant_id
               AND version.document_version_id = representation.document_version_id
             WHERE representation.tenant_id = %s
               AND representation.canonical_representation_id = %s
               AND representation.state = 'validated'
               AND representation.readiness_state = 'full'
             FOR UPDATE
            """,
            (tenant_id, canonical_representation_id),
        )
        row = cursor.fetchone()
        if row is None:
            return None

        payload = cast(Mapping[str, object], row[9])
        source_lineage = payload.get("source_lineage")
        source_lineage_map = dict(source_lineage) if isinstance(source_lineage, Mapping) else {}
        elements = self._load_elements(
            cursor=cursor,
            tenant_id=tenant_id,
            canonical_representation_id=canonical_representation_id,
        )
        return {
            "tenant_id": str(row[0]),
            "document_id": UUID(str(row[1])),
            "canonical_representation_id": UUID(str(row[2])),
            "document_version_id": UUID(str(row[3])),
            "processing_operation_id": UUID(str(row[4])),
            "source_artifact_id": UUID(str(row[5])),
            "provider_result_id": None if row[6] is None else UUID(str(row[6])),
            "canonical_validation_version": str(row[7] or ""),
            "validation_report": dict(row[8]) if isinstance(row[8], Mapping) else {},
            "representation_payload": dict(payload),
            "source_lineage": source_lineage_map,
            "elements": elements,
        }

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
             ORDER BY element.page_number ASC, element.reading_order ASC, element.stable_key ASC
            """,
            (tenant_id, canonical_representation_id),
        )
        rows = cursor.fetchall()
        elements: list[CanonicalElement] = []
        for row in rows:
            region_payload = cast(Mapping[str, object], row[7] or {}) if isinstance(row[7], Mapping) else {}
            source_region_raw = region_payload.get("source_region", {})
            source_region = dict(source_region_raw) if isinstance(source_region_raw, Mapping) else {}
            if not source_region:
                source_region = {"page_number": int(row[2])}
            element_lineage = region_payload.get("element_lineage", {})
            lineage_map = dict(element_lineage) if isinstance(element_lineage, Mapping) else {}
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

    def _load_existing_chunks(
        self,
        *,
        cursor: _Cursor,
        tenant_id: str,
        canonical_representation_id: UUID,
    ) -> tuple[RetrievalChunk, ...]:
        cursor.execute(
            """
            SELECT chunk_key, content_hash_sha256, chunking_policy_version, embedding_text,
                   canonical_element_keys, source_location, structural_context
              FROM document_ai_retrieval_chunks
             WHERE tenant_id = %s
               AND canonical_representation_id = %s
             ORDER BY COALESCE((structural_context->>'chunk_ordinal')::INT, 0) ASC,
                      created_at ASC, chunk_key ASC
            """,
            (tenant_id, canonical_representation_id),
        )
        rows = cursor.fetchall()
        if not rows:
            return ()
        chunks: list[RetrievalChunk] = []
        for ordinal, row in enumerate(rows):
            chunks.append(
                RetrievalChunk(
                    chunk_key=str(row[0]),
                    chunk_ordinal=ordinal,
                    generation_identity=str(row[0]),
                    content_hash=str(row[1]),
                    embedding_text=str(row[3]),
                    canonical_element_keys=tuple(
                        str(value)
                        for value in (
                            row[4] if isinstance(row[4], Sequence) else []
                        )
                    ),
                    source_location=dict(row[5]) if isinstance(row[5], Mapping) else {},
                    source_lineage=dict(
                        cast(Mapping[str, object], (row[6] or {}).get("source_lineage", {}))
                    )
                    if isinstance(row[6], Mapping)
                    else {},
                    structural_context=dict(row[6]) if isinstance(row[6], Mapping) else {},
                    chunking_policy_version=str(row[2]),
                )
            )
        return tuple(chunks)

    def _ensure_chunk_replay_matches(
        self,
        *,
        expected_chunks: Sequence[RetrievalChunk],
        existing_chunks: Sequence[RetrievalChunk],
    ) -> None:
        if len(expected_chunks) != len(existing_chunks):
            raise CanonicalValidationError(
                "canonical_chunk_generation_mismatch",
                retryable=False,
                message="Persisted canonical chunks do not match the deterministic replay.",
            )
        for expected, existing in zip(expected_chunks, existing_chunks, strict=True):
            if (
                expected.chunk_key != existing.chunk_key
                or expected.content_hash != existing.content_hash
                or expected.embedding_text != existing.embedding_text
                or expected.canonical_element_keys != existing.canonical_element_keys
                or expected.source_location != existing.source_location
                or expected.structural_context != existing.structural_context
                or expected.chunking_policy_version != existing.chunking_policy_version
            ):
                raise CanonicalValidationError(
                    "canonical_chunk_generation_mismatch",
                    retryable=False,
                    message="Persisted canonical chunks do not match the deterministic replay.",
                )

    def _insert_chunk(
        self,
        *,
        cursor: _Cursor,
        candidate: Mapping[str, object],
        chunk: RetrievalChunk,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO document_ai_retrieval_chunks (
                tenant_id, document_id, document_version_id, canonical_representation_id,
                chunk_key, content_hash_sha256, chunking_policy_version, embedding_text,
                canonical_element_keys, source_location, structural_context
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb)
            ON CONFLICT (tenant_id, canonical_representation_id, chunk_key, chunking_policy_version)
            DO NOTHING
            """,
            (
                candidate["tenant_id"],
                candidate["document_id"],
                candidate["document_version_id"],
                candidate["canonical_representation_id"],
                chunk.chunk_key,
                chunk.content_hash,
                chunk.chunking_policy_version,
                chunk.embedding_text,
                json.dumps(chunk.canonical_element_keys, sort_keys=True),
                json.dumps(chunk.source_location, sort_keys=True),
                json.dumps(chunk.structural_context, sort_keys=True),
            ),
        )

    def _ensure_continuation_outbox(
        self,
        *,
        cursor: _Cursor,
        candidate: Mapping[str, object],
        chunks: Sequence[RetrievalChunk],
    ) -> None:
        payload = {
            "canonical_representation_id": str(candidate["canonical_representation_id"]),
            "document_version_id": str(candidate["document_version_id"]),
            "processing_operation_id": str(candidate["processing_operation_id"]),
            "source_artifact_id": str(candidate["source_artifact_id"]),
            "provider_result_id": (
                str(candidate["provider_result_id"]) if candidate["provider_result_id"] is not None else None
            ),
            "chunk_count": len(chunks),
            "chunking_policy_version": CANONICAL_CHUNKING_POLICY_VERSION,
            "chunk_keys": [chunk.chunk_key for chunk in chunks],
            "chunk_generation_identity": chunks[0].generation_identity if chunks else None,
        }
        cursor.execute(
            """
            INSERT INTO document_ai_processing_outbox (
                tenant_id, processing_operation_id, processing_work_item_id,
                event_type, payload, routing_key, correlation_id
            )
            SELECT representation.tenant_id, representation.processing_operation_id,
                   work.processing_work_item_id, %s, %s::jsonb, 'document_ai.processing',
                   operation.correlation_id
              FROM document_ai_canonical_representations AS representation
              JOIN document_ai_processing_operations AS operation
                ON operation.tenant_id = representation.tenant_id
               AND operation.processing_operation_id = representation.processing_operation_id
              JOIN document_ai_processing_work_items AS work
                ON work.tenant_id = operation.tenant_id
               AND work.processing_operation_id = operation.processing_operation_id
             WHERE representation.tenant_id = %s
               AND representation.canonical_representation_id = %s
            ON CONFLICT (tenant_id, processing_operation_id, event_type) DO NOTHING
            """,
            (
                CANONICAL_CHUNK_GENERATION_CONTINUATION_EVENT,
                json.dumps(payload, sort_keys=True),
                candidate["tenant_id"],
                candidate["canonical_representation_id"],
            ),
        )

    def _result_from_chunks(
        self,
        *,
        candidate: Mapping[str, object],
        chunks: Sequence[RetrievalChunk],
        state: Literal["generated", "replayed"],
    ) -> CanonicalChunkGenerationResult:
        generation_identity = chunks[0].generation_identity if chunks else ""
        return CanonicalChunkGenerationResult(
            state=state,
            chunk_count=len(chunks),
            chunk_generation_identity=generation_identity,
            chunk_keys=tuple(chunk.chunk_key for chunk in chunks),
            continuation_event=CANONICAL_CHUNK_GENERATION_CONTINUATION_EVENT,
            validation_version=str(candidate["canonical_validation_version"] or ""),
            validation_report=dict(candidate["validation_report"]),
        )

    def _reconcile_generation_result(
        self,
        *,
        connection: object,
        tenant_id: str,
        canonical_representation_id: UUID,
    ) -> CanonicalChunkGenerationResult | None:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT representation.canonical_validation_version,
                       representation.validation_report
                  FROM document_ai_canonical_representations AS representation
                 WHERE representation.tenant_id = %s
                   AND representation.canonical_representation_id = %s
                """,
                (tenant_id, canonical_representation_id),
            )
            candidate_row = cursor.fetchone()
            if candidate_row is None:
                return None
            cursor.execute(
                """
                SELECT chunk_key, content_hash_sha256, chunking_policy_version, embedding_text,
                       canonical_element_keys, source_location, structural_context
                  FROM document_ai_retrieval_chunks
                 WHERE tenant_id = %s
                   AND canonical_representation_id = %s
                 ORDER BY COALESCE((structural_context->>'chunk_ordinal')::INT, 0) ASC,
                          created_at ASC, chunk_key ASC
                """,
                (tenant_id, canonical_representation_id),
            )
            rows = cursor.fetchall()
        chunks = tuple(
            RetrievalChunk(
                chunk_key=str(row[0]),
                chunk_ordinal=index,
                generation_identity=str(row[0]),
                content_hash=str(row[1]),
                embedding_text=str(row[3]),
                canonical_element_keys=tuple(
                    str(value) for value in (row[4] if isinstance(row[4], Sequence) else [])
                ),
                source_location=dict(row[5]) if isinstance(row[5], Mapping) else {},
                source_lineage=dict(
                    cast(Mapping[str, object], (row[6] or {}).get("source_lineage", {}))
                )
                if isinstance(row[6], Mapping)
                else {},
                structural_context=dict(row[6]) if isinstance(row[6], Mapping) else {},
                chunking_policy_version=str(row[2]),
            )
            for index, row in enumerate(rows)
        )
        if not chunks:
            return None
        return self._result_from_chunks(
            candidate={
                "canonical_validation_version": candidate_row[0],
                "validation_report": candidate_row[1],
            },
            chunks=chunks,
            state="replayed",
        )
