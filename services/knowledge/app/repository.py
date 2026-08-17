"""Persistent governed repository for knowledge search, retrieval, ingestion, and publication."""

from __future__ import annotations

import os
from copy import deepcopy
import json
from uuid import UUID
from uuid import uuid4
import base64
from typing import cast
from typing import LiteralString
import hashlib
from pathlib import Path
import binascii
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.parse import urlunsplit
from collections.abc import Mapping
from collections.abc import Sequence

import psycopg
from psycopg import sql

from services.knowledge.app.config import get_knowledge_hybrid_vector_weight
from services.knowledge.app.config import get_knowledge_hybrid_lexical_weight
from services.knowledge.app.config import get_knowledge_hybrid_min_vector_similarity
from services.knowledge.app.embeddings import cosine_similarity
from services.knowledge.app.embeddings import KnowledgeEmbeddingProvider
from services.knowledge.app.embeddings import KnowledgeEmbeddingProviderError
from services.knowledge.app.embeddings import build_default_knowledge_embedding_provider

DATABASE_URL_ENV_VAR = "DATABASE_URL"
DB_USER_ENV_VAR = "DB_USER"
DB_PASSWORD_ENV_VAR = "DB_PASSWORD"
DB_NAME_ENV_VAR = "DB_NAME"
DEFAULT_DB_NAME = "kodi_dev"
SEARCHABLE_PUBLICATION_STATES = ("published", "superseded")
KNOWLEDGE_STORAGE_NOT_CONFIGURED = "knowledge_storage_not_configured"
KNOWLEDGE_STORAGE_UNAVAILABLE = "knowledge_storage_unavailable"
UNSUPPORTED_SOURCE_INPUT_ORIGIN = "unsupported_source_input_origin"
UNSUPPORTED_SOURCE_CLASS = "unsupported_source_class"
INVALID_KNOWLEDGE_REQUEST = "invalid_knowledge_request"
INVALID_KNOWLEDGE_LINEAGE = "invalid_knowledge_lineage"
KNOWLEDGE_IDEMPOTENCY_CONFLICT = "knowledge_idempotency_conflict"
INVALID_PUBLICATION_STATE_TRANSITION = "invalid_publication_state_transition"
INVALID_AUTHORITY_SOURCE_CLASS_BINDING = "invalid_authority_source_class_binding"
INVALID_EFFECTIVE_WINDOW_METADATA = "invalid_effective_window_metadata"
KNOWLEDGE_PUBLICATION_SAFETY_REJECTED = "knowledge_publication_safety_rejected"
KNOWLEDGE_SUPERSESSION_CONFLICT = "knowledge_supersession_conflict"
KNOWLEDGE_TEMPORAL_SCOPE_MISMATCH = "knowledge_temporal_scope_mismatch"
KNOWLEDGE_RECORD_NOT_PUBLISHED = "knowledge_record_not_published"
OFFICIAL_SOURCE_UPLOAD = "official_source_upload"
OFFICIAL_SOURCE_URL = "official_source_url"
CUSTOMER_UPLOADED_DOCUMENT = "customer_uploaded_document"
SUPPORTED_FILE_MIME_TYPES = (
    "application/pdf",
    "text/html",
    "text/plain",
    "text/markdown",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/xml",
)
SUPPORTED_SOURCE_CLASSES = ("tax_law", "regulation", "guidance", "commentary")
SUPPORTED_SOURCE_DOCUMENT_SYSTEMS = ("storage_registered",)
INITIAL_INGESTION_STATE = "uploaded"
REVIEW_PENDING_INGESTION_STATE = "review_pending"
APPROVED_INGESTION_STATE = "approved"
LEGACY_APPROVED_FOR_PUBLICATION_INGESTION_STATE = "approved_for_publication"
PUBLISHED_INGESTION_STATE = "published"
REJECTED_INGESTION_STATE = "rejected"
SEARCHABLE_SOURCE_PUBLICATION_STATE = "published"
DEFAULT_MANAGEMENT_LIMIT = 100
DEFAULT_MANAGEMENT_OFFSET = 0
KNOWLEDGE_RETENTION_POLICY_CODE = "knowledge_runtime_default_retention"
KNOWLEDGE_READ_RETENTION_POLICY_CODE = "knowledge_runtime_query_retention"
KNOWLEDGE_RETENTION_DAYS = 3650
KNOWLEDGE_READ_AUDIT_USER_ID = UUID("00000000-0000-0000-0000-000000000321")
KNOWLEDGE_PUBLICATION_STATES = (
    "draft",
    "review_pending",
    "approved",
    "published",
    "superseded",
    "archived",
    "rejected",
)
KNOWLEDGE_INGESTION_STATES = (
    INITIAL_INGESTION_STATE,
    REVIEW_PENDING_INGESTION_STATE,
    APPROVED_INGESTION_STATE,
    PUBLISHED_INGESTION_STATE,
    REJECTED_INGESTION_STATE,
)
LEGACY_KNOWLEDGE_INGESTION_STATES = (
    INITIAL_INGESTION_STATE,
    REVIEW_PENDING_INGESTION_STATE,
    LEGACY_APPROVED_FOR_PUBLICATION_INGESTION_STATE,
    PUBLISHED_INGESTION_STATE,
    REJECTED_INGESTION_STATE,
)
INGESTION_SORT_FIELDS = ("created_at",)
SOURCE_VERSION_SORT_FIELDS = ("source_family_id", "effective_from")
SOURCE_SORT_FIELDS = ("source_family_id", "tax_domain")
SORT_ORDERS = ("asc", "desc")
METADATA_CORRECTION_EDITABLE_STATES = (
    REVIEW_PENDING_INGESTION_STATE,
    APPROVED_INGESTION_STATE,
    LEGACY_APPROVED_FOR_PUBLICATION_INGESTION_STATE,
)
ALLOWED_PUBLICATION_METADATA_CORRECTION_FIELDS = frozenset(
    {
        "title",
        "issuing_authority",
        "point_in_time_url",
        "tax_year",
    }
)

SOURCE_CLASS_AUTHORITY_BINDING: dict[str, str] = {
    "tax_law": "statute",
    "regulation": "regulation",
    "guidance": "guidance",
    "commentary": "commentary",
}

AUTHORITY_RANK: dict[str, int] = {
    "statute": 0,
    "regulation": 1,
    "guidance": 2,
    "commentary": 3,
}


class KnowledgeRepositoryError(RuntimeError):
    """Represent canonical repository-level failures for knowledge persistence."""

    def __init__(self, *, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


@dataclass(frozen=True)
class KnowledgeSearchRecord:
    """Represent one searchable governed knowledge record."""

    source_id: str
    title: str
    url: str
    source_type: str
    tax_domain: str
    authority_level: str
    effective_from: str
    effective_to: str | None
    tax_year: int | None
    anchor_id: str
    content: str
    canonical_claims: tuple[dict[str, object], ...] | None = None

    def to_public_payload(self) -> dict[str, object]:
        """Return the stable response payload for one record."""

        return {
            "source_id": self.source_id,
            "title": self.title,
            "url": self.url,
            "source_type": self.source_type,
            "tax_domain": self.tax_domain,
            "authority_level": self.authority_level,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "tax_year": self.tax_year,
            "anchor_id": self.anchor_id,
        }


@dataclass(frozen=True)
class KnowledgeTimelineRecord:
    """Represent one governed timeline retrieval record."""

    source_id: str
    source_version_id: str
    anchor_id: str
    title: str
    url: str
    source_type: str
    authority_level: str
    tax_domain: str
    effective_from: str
    effective_to: str | None
    publication_state: str
    timeline_position: int
    content: str

    def to_public_payload(self) -> dict[str, object]:
        """Return the stable response payload for one timeline record."""

        return {
            "source_id": self.source_id,
            "source_version_id": self.source_version_id,
            "anchor_id": self.anchor_id,
            "title": self.title,
            "source_type": self.source_type,
            "authority_level": self.authority_level,
            "tax_domain": self.tax_domain,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "publication_state": self.publication_state,
            "timeline_position": self.timeline_position,
        }


@dataclass(frozen=True)
class KnowledgeIngestionRecord:
    """Represent one persisted governed knowledge ingestion job."""

    ingestion_job_id: str
    document_id: str
    requested_by: str
    ingestion_state: str
    source_input_origin: str
    source_input_ref: str
    payload_checksum_sha256: str
    source_class: str | None

    def to_public_payload(self) -> dict[str, object]:
        """Return the stable response payload for one ingestion job."""

        return {
            "ingestion_job_id": self.ingestion_job_id,
            "document_id": self.document_id,
            "requested_by": self.requested_by,
            "ingestion_state": self.ingestion_state,
            "source_input_origin": self.source_input_origin,
            "source_input_ref": self.source_input_ref,
            "payload_checksum_sha256": self.payload_checksum_sha256,
            "source_class": self.source_class,
        }


@dataclass(frozen=True)
class KnowledgeIngestionDetailRecord:
    """Represent one governed knowledge ingestion job with review metadata."""

    ingestion_job_id: str
    document_id: str
    requested_by: str
    ingestion_state: str
    source_input_origin: str
    source_input_ref: str
    payload_checksum_sha256: str
    source_class: str | None
    extracted_metadata: dict[str, object]
    proposed_source_record: dict[str, object]
    review_notes: tuple[dict[str, object], ...]
    completed_at: str | None

    def to_public_payload(self) -> dict[str, object]:
        """Return the stable response payload for one detailed ingestion job."""

        return {
            "ingestion_job_id": self.ingestion_job_id,
            "document_id": self.document_id,
            "requested_by": self.requested_by,
            "ingestion_state": self.ingestion_state,
            "source_input_origin": self.source_input_origin,
            "source_input_ref": self.source_input_ref,
            "payload_checksum_sha256": self.payload_checksum_sha256,
            "source_class": self.source_class,
            "extracted_metadata": deepcopy(self.extracted_metadata),
            "proposed_source_record": deepcopy(self.proposed_source_record),
            "review_notes": [deepcopy(item) for item in self.review_notes],
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True)
class KnowledgeIngestionSummaryRecord:
    """Represent one governed ingestion job for management visibility."""

    ingestion_job_id: str
    document_id: str
    requested_by: str
    ingestion_state: str
    source_input_origin: str
    source_input_ref: str
    payload_checksum_sha256: str
    source_class: str | None
    created_at: str
    completed_at: str | None

    def to_public_payload(self) -> dict[str, object]:
        """Return the stable response payload for one ingestion summary item."""

        return {
            "ingestion_job_id": self.ingestion_job_id,
            "document_id": self.document_id,
            "requested_by": self.requested_by,
            "ingestion_state": self.ingestion_state,
            "source_input_origin": self.source_input_origin,
            "source_input_ref": self.source_input_ref,
            "payload_checksum_sha256": self.payload_checksum_sha256,
            "source_class": self.source_class,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True)
class KnowledgeSourceVersionLifecycleRecord:
    """Represent one governed source-version lifecycle state."""

    source_version_id: str
    source_id: str
    source_family_id: str
    publication_state: str
    source_input_origin: str
    source_version_form: str
    effective_from: str
    effective_to: str | None
    tax_year: int | None
    supersedes_source_version_id: str | None
    superseded_by_source_version_id: str | None

    def to_public_payload(self) -> dict[str, object]:
        """Return the stable response payload for one source-version lifecycle record."""

        return {
            "source_version_id": self.source_version_id,
            "source_id": self.source_id,
            "source_family_id": self.source_family_id,
            "publication_state": self.publication_state,
            "source_input_origin": self.source_input_origin,
            "source_version_form": self.source_version_form,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "tax_year": self.tax_year,
            "supersedes_source_version_id": self.supersedes_source_version_id,
            "superseded_by_source_version_id": self.superseded_by_source_version_id,
        }


@dataclass(frozen=True)
class KnowledgeSourceVersionSummaryRecord:
    """Represent one governed source version for management visibility."""

    source_version_id: str
    source_id: str
    source_family_id: str
    title: str
    source_class: str
    tax_domain: str
    authority_level: str
    publication_state: str
    source_input_origin: str
    source_version_form: str
    effective_from: str
    effective_to: str | None
    tax_year: int | None
    supersedes_source_version_id: str | None
    superseded_by_source_version_id: str | None

    def to_public_payload(self) -> dict[str, object]:
        """Return the stable response payload for one source-version summary item."""

        return {
            "source_version_id": self.source_version_id,
            "source_id": self.source_id,
            "source_family_id": self.source_family_id,
            "title": self.title,
            "source_class": self.source_class,
            "tax_domain": self.tax_domain,
            "authority_level": self.authority_level,
            "publication_state": self.publication_state,
            "source_input_origin": self.source_input_origin,
            "source_version_form": self.source_version_form,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "tax_year": self.tax_year,
            "supersedes_source_version_id": self.supersedes_source_version_id,
            "superseded_by_source_version_id": self.superseded_by_source_version_id,
        }


@dataclass(frozen=True)
class KnowledgeSourceSummaryRecord:
    """Represent one governed source summary for management visibility."""

    source_id: str
    source_family_id: str
    title: str
    canonical_url: str
    source_class: str
    tax_domain: str
    authority_level: str
    issuing_authority: str
    version_count: int
    anchor_count: int
    created_at: str
    retired_at: str | None

    def to_public_payload(self) -> dict[str, object]:
        """Return the stable response payload for one source summary item."""

        return {
            "source_id": self.source_id,
            "source_family_id": self.source_family_id,
            "title": self.title,
            "canonical_url": self.canonical_url,
            "source_class": self.source_class,
            "tax_domain": self.tax_domain,
            "authority_level": self.authority_level,
            "issuing_authority": self.issuing_authority,
            "version_count": self.version_count,
            "anchor_count": self.anchor_count,
            "created_at": self.created_at,
            "retired_at": self.retired_at,
        }


@dataclass(frozen=True)
class KnowledgeChunkSummaryRecord:
    """Represent one governance-safe chunk summary."""

    chunk_id: str
    chunk_index: int
    has_embedding: bool

    def to_public_payload(self) -> dict[str, object]:
        """Return the stable response payload for one chunk summary."""

        return {
            "chunk_id": self.chunk_id,
            "chunk_index": self.chunk_index,
            "has_embedding": self.has_embedding,
        }


@dataclass(frozen=True)
class KnowledgeSourceDetailRecord:
    """Represent one governed source detail record."""

    source_id: str
    source_family_id: str
    title: str
    canonical_url: str
    source_class: str
    tax_domain: str
    authority_level: str
    issuing_authority: str
    version_count: int
    anchor_count: int
    chunk_count: int
    created_at: str
    retired_at: str | None
    versions: tuple[KnowledgeSourceVersionSummaryRecord, ...]
    retention_summary: dict[str, object]

    def to_public_payload(self) -> dict[str, object]:
        """Return the stable response payload for one source detail item."""

        return {
            "source_id": self.source_id,
            "source_family_id": self.source_family_id,
            "title": self.title,
            "canonical_url": self.canonical_url,
            "source_class": self.source_class,
            "tax_domain": self.tax_domain,
            "authority_level": self.authority_level,
            "issuing_authority": self.issuing_authority,
            "version_count": self.version_count,
            "anchor_count": self.anchor_count,
            "chunk_count": self.chunk_count,
            "created_at": self.created_at,
            "retired_at": self.retired_at,
            "versions": [version.to_public_payload() for version in self.versions],
            "retention_summary": deepcopy(self.retention_summary),
        }


@dataclass(frozen=True)
class KnowledgeAnchorDetailRecord:
    """Represent one governed anchor detail record."""

    anchor_id: str
    source_id: str
    source_family_id: str
    source_version_id: str
    source_title: str
    source_type: str
    tax_domain: str
    authority_level: str
    publication_state: str
    anchor_title: str
    anchor_path: str
    temporal_scope_from: str
    temporal_scope_to: str | None
    chunk_count: int
    chunks: tuple[KnowledgeChunkSummaryRecord, ...]

    def to_public_payload(self) -> dict[str, object]:
        """Return the stable response payload for one anchor detail item."""

        return {
            "anchor_id": self.anchor_id,
            "source_id": self.source_id,
            "source_family_id": self.source_family_id,
            "source_version_id": self.source_version_id,
            "source_title": self.source_title,
            "source_type": self.source_type,
            "tax_domain": self.tax_domain,
            "authority_level": self.authority_level,
            "publication_state": self.publication_state,
            "anchor_title": self.anchor_title,
            "anchor_path": self.anchor_path,
            "temporal_scope_from": self.temporal_scope_from,
            "temporal_scope_to": self.temporal_scope_to,
            "chunk_count": self.chunk_count,
            "chunks": [chunk.to_public_payload() for chunk in self.chunks],
        }


@dataclass(frozen=True)
class KnowledgeBulkOperationItemRecord:
    """Represent one deterministic bulk-management item outcome."""

    id: str
    status: str
    outcome: str
    error_code: str | None
    reason: str | None

    def to_public_payload(self) -> dict[str, object]:
        """Return the stable response payload for one bulk outcome item."""

        return {
            "id": self.id,
            "status": self.status,
            "outcome": self.outcome,
            "error_code": self.error_code,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class KnowledgeBulkIngestionItemRecord:
    """Represent one deterministic bulk-ingestion item outcome."""

    index: int
    idempotency_key: str
    status: str
    outcome: str
    ingestion_job_id: str | None
    error_code: str | None
    reason: str | None

    def to_public_payload(self) -> dict[str, object]:
        """Return the stable response payload for one bulk-ingestion item."""

        return {
            "index": self.index,
            "idempotency_key": self.idempotency_key,
            "status": self.status,
            "outcome": self.outcome,
            "ingestion_job_id": self.ingestion_job_id,
            "error_code": self.error_code,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class _PreparedKnowledgeChunk:
    """Represent one prepared publication chunk with optional stored embedding."""

    chunk_id: UUID
    chunk_index: int
    chunk_text: str
    normalized_chunk_text: str
    embedding_vector_ref: str | None
    embedding_vector: tuple[float, ...] | None


@dataclass(frozen=True)
class _PreparedKnowledgeAnchor:
    """Represent one prepared publication anchor and its deterministic chunks."""

    anchor_id: str
    anchor_title: str
    anchor_path: str
    anchor_text: str
    normalized_anchor_text: str
    temporal_scope_from: date
    temporal_scope_to: date | None
    chunks: tuple[_PreparedKnowledgeChunk, ...]


class KnowledgeRepository:
    """Provide deterministic persistent read operations for knowledge runtime."""

    def __init__(
        self,
        *,
        database_url: str | None = None,
        embedding_provider: KnowledgeEmbeddingProvider | None = None,
        hybrid_vector_weight: float | None = None,
        hybrid_lexical_weight: float | None = None,
        hybrid_min_vector_similarity: float | None = None,
    ) -> None:
        self._database_url = _load_database_url() if database_url is None else database_url
        self._embedding_provider = (
            build_default_knowledge_embedding_provider()
            if embedding_provider is None
            else embedding_provider
        )
        self._hybrid_vector_weight = (
            get_knowledge_hybrid_vector_weight()
            if hybrid_vector_weight is None
            else hybrid_vector_weight
        )
        self._hybrid_lexical_weight = (
            get_knowledge_hybrid_lexical_weight()
            if hybrid_lexical_weight is None
            else hybrid_lexical_weight
        )
        self._hybrid_min_vector_similarity = (
            get_knowledge_hybrid_min_vector_similarity()
            if hybrid_min_vector_similarity is None
            else hybrid_min_vector_similarity
        )

    def search_records(
        self,
        *,
        query: str,
        source_type: str | None,
        tax_domain: str | None,
        effective_date: date | None,
    ) -> tuple[KnowledgeSearchRecord, ...]:
        """Return deterministically ranked governed search results."""

        candidates = self._fetch_candidates(
            source_type=source_type,
            tax_domain=tax_domain,
            effective_date=effective_date,
        )
        normalized_query = " ".join(query.strip().split())
        query_tokens = _query_tokens(normalized_query)
        lexical_denominator = len(query_tokens)
        vector_scores = self._vector_scores_by_anchor_id(
            query=normalized_query,
            candidates=candidates,
        )
        ranked: list[tuple[float, float, float, int, str, str, KnowledgeSearchRecord]] = []
        for record in candidates:
            lexical_score = _match_score(
                query=normalized_query,
                title=record.title,
                content=record.content,
                anchor_id=record.anchor_id,
                source_type=record.source_type,
            )
            lexical_fraction = (
                round(lexical_score / lexical_denominator, 12) if lexical_denominator > 0 else 0.0
            )
            vector_score = round(vector_scores.get(record.anchor_id, 0.0), 12)
            combined_score = round(
                (self._hybrid_lexical_weight * lexical_fraction)
                + (self._hybrid_vector_weight * vector_score),
                12,
            )
            if lexical_score == 0 and vector_score < self._hybrid_min_vector_similarity:
                continue
            ranked.append(
                (
                    combined_score,
                    lexical_fraction,
                    vector_score,
                    AUTHORITY_RANK.get(record.authority_level, 99),
                    record.source_id,
                    record.anchor_id,
                    record,
                )
            )

        ranked.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3], item[4], item[5]))
        records = tuple(entry[6] for entry in ranked)
        self._record_best_effort_read_audit_event(
            event_type="knowledge_search",
            correlation_id=(
                "knowledge-search-"
                + hashlib.sha256(normalized_query.encode("utf-8")).hexdigest()[:16]
            ),
            details={
                "query": normalized_query,
                "source_type": source_type,
                "tax_domain": tax_domain,
                "effective_date": (effective_date or date.today()).isoformat(),
                "result_total": len(records),
            },
        )
        return records

    def retrieve_records(
        self,
        *,
        source_ids: Sequence[str],
        anchor_ids: Sequence[str],
    ) -> tuple[KnowledgeSearchRecord, ...]:
        """Return governed records by stable identifiers."""

        normalized_source_ids = tuple(sorted({value for value in source_ids if value}))
        normalized_anchor_ids = tuple(sorted({value for value in anchor_ids if value}))
        if not normalized_source_ids and not normalized_anchor_ids:
            return ()

        self._assert_database_configured()
        try:
            with psycopg.connect(self._database_url_or_raise(), connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            ks.source_id,
                            ks.title,
                            ks.canonical_url,
                            ks.source_class,
                            ks.tax_domain,
                            ks.authority_level,
                            ksv.effective_from,
                            ksv.effective_to,
                            ksv.tax_year,
                            ka.anchor_id,
                            ka.anchor_text
                        FROM knowledge_sources AS ks
                        JOIN knowledge_source_versions AS ksv
                          ON ksv.source_id = ks.source_id
                        JOIN knowledge_anchors AS ka
                          ON ka.source_version_id = ksv.id
                        WHERE ksv.publication_state = ANY(%s::text[])
                          AND ksv.source_input_origin = ANY(%s::text[])
                          AND ksv.publication_event_id IS NOT NULL
                          AND char_length(btrim(ksv.source_input_ref)) > 0
                          AND (
                                ks.source_id = ANY(%s::text[])
                                OR ka.anchor_id = ANY(%s::text[])
                          )
                        ORDER BY ks.source_id ASC, ka.anchor_id ASC
                        """,
                        (
                            list(SEARCHABLE_PUBLICATION_STATES),
                            ["official_source_upload", "official_source_url"],
                            list(normalized_source_ids),
                            list(normalized_anchor_ids),
                        ),
                    )
                    rows = cursor.fetchall()
        except psycopg.Error as error:
            raise KnowledgeRepositoryError(
                reason_code=KNOWLEDGE_STORAGE_UNAVAILABLE,
                message="Knowledge persistence storage is unavailable.",
            ) from error

        records = tuple(_row_to_record(row) for row in rows)
        self._record_best_effort_read_audit_event(
            event_type="knowledge_retrieve",
            correlation_id=(
                "knowledge-retrieve-"
                + hashlib.sha256(
                    json.dumps(
                        {
                            "source_ids": normalized_source_ids,
                            "anchor_ids": normalized_anchor_ids,
                        },
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest()[:16]
            ),
            details={
                "source_ids": list(normalized_source_ids),
                "anchor_ids": list(normalized_anchor_ids),
                "result_total": len(records),
            },
        )
        return records

    def timeline_search_records(
        self,
        *,
        query: str,
        source_type: str | None,
        tax_domain: str,
        start_date: date,
        end_date: date,
    ) -> tuple[KnowledgeTimelineRecord, ...]:
        """Return deterministic timeline retrieval records across governed effective windows."""

        if end_date < start_date:
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_REQUEST,
                message="Knowledge timeline date range is invalid.",
            )

        candidates = self._fetch_timeline_candidates(
            source_type=source_type,
            tax_domain=tax_domain,
            start_date=start_date,
            end_date=end_date,
        )
        normalized_query = " ".join(query.strip().split())
        query_tokens = _query_tokens(normalized_query)
        lexical_denominator = len(query_tokens)
        vector_scores = self._vector_scores_by_anchor_ids(
            query=normalized_query,
            anchor_ids=tuple(record.anchor_id for record in candidates),
        )
        ranked: list[tuple[tuple[object, ...], KnowledgeTimelineRecord]] = []
        for record in candidates:
            lexical_score = _match_score(
                query=normalized_query,
                title=record.title,
                content=record.content,
                anchor_id=record.anchor_id,
                source_type=record.source_type,
            )
            lexical_fraction = (
                round(lexical_score / lexical_denominator, 12) if lexical_denominator > 0 else 0.0
            )
            vector_score = round(vector_scores.get(record.anchor_id, 0.0), 12)
            combined_score = round(
                (self._hybrid_lexical_weight * lexical_fraction)
                + (self._hybrid_vector_weight * vector_score),
                12,
            )
            if lexical_score == 0 and vector_score < self._hybrid_min_vector_similarity:
                continue
            ranked.append(
                (
                    (
                        record.effective_from,
                        record.effective_to or "9999-12-31",
                        AUTHORITY_RANK.get(record.authority_level, 99),
                        -combined_score,
                        -lexical_fraction,
                        -vector_score,
                        record.source_id,
                        record.source_version_id,
                        record.anchor_id,
                    ),
                    record,
                )
            )

        ranked.sort(key=lambda item: item[0])
        timeline_records: list[KnowledgeTimelineRecord] = []
        for index, (_sort_key, record) in enumerate(ranked, start=1):
            timeline_records.append(
                KnowledgeTimelineRecord(
                    source_id=record.source_id,
                    source_version_id=record.source_version_id,
                    anchor_id=record.anchor_id,
                    title=record.title,
                    url=record.url,
                    source_type=record.source_type,
                    authority_level=record.authority_level,
                    tax_domain=record.tax_domain,
                    effective_from=record.effective_from,
                    effective_to=record.effective_to,
                    publication_state=record.publication_state,
                    timeline_position=index,
                    content=record.content,
                )
            )
        records = tuple(timeline_records)
        self._record_best_effort_read_audit_event(
            event_type="knowledge_timeline_search",
            correlation_id=(
                "knowledge-timeline-"
                + hashlib.sha256(
                    json.dumps(
                        {
                            "query": normalized_query,
                            "source_type": source_type,
                            "tax_domain": tax_domain,
                            "start_date": start_date.isoformat(),
                            "end_date": end_date.isoformat(),
                        },
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest()[:16]
            ),
            details={
                "query": normalized_query,
                "source_type": source_type,
                "tax_domain": tax_domain,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "result_total": len(records),
            },
        )
        return records

    def ingest_file_source(
        self,
        *,
        requested_by: str,
        idempotency_key: str,
        filename: str,
        mime_type: str,
        file_content_base64: str,
        source_input_origin: str | None,
        source_class: str | None,
        legacy_import_acknowledged: bool,
    ) -> KnowledgeIngestionRecord:
        """Persist one governed legacy direct-import file ingestion job."""

        if not legacy_import_acknowledged:
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_REQUEST,
                message=(
                    "Knowledge direct file ingestion requires explicit legacy import "
                    "acknowledgement."
                ),
            )

        normalized_origin = _normalize_origin(
            provided_origin=source_input_origin,
            expected_origin=OFFICIAL_SOURCE_UPLOAD,
        )
        normalized_source_class = _normalize_source_class(source_class)
        normalized_filename = _normalize_required_string(
            value=filename,
            field_name="filename",
        )
        normalized_mime_type = _normalize_mime_type(mime_type)
        payload_bytes = _decode_base64_payload(file_content_base64)
        payload_checksum_sha256 = hashlib.sha256(payload_bytes).hexdigest()
        source_input_ref = _build_legacy_import_source_input_ref(payload_checksum_sha256)
        payload_fingerprint = _payload_fingerprint(
            {
                "requested_by": requested_by,
                "idempotency_key": idempotency_key,
                "filename": normalized_filename,
                "mime_type": normalized_mime_type,
                "payload_checksum_sha256": payload_checksum_sha256,
                "source_input_origin": normalized_origin,
                "source_class": normalized_source_class,
            }
        )
        extracted_metadata = {
            "filename": normalized_filename,
            "mime_type": normalized_mime_type,
            "payload_checksum_sha256": payload_checksum_sha256,
        }
        proposed_source_record = {
            "ingestion_kind": "file",
            "ingestion_mode": "legacy_direct_import",
            "source_input_origin": normalized_origin,
            "source_input_ref": source_input_ref,
            "payload_checksum_sha256": payload_checksum_sha256,
            "idempotency_key": idempotency_key,
            "payload_fingerprint": payload_fingerprint,
            "source_class": normalized_source_class,
            "filename": normalized_filename,
            "mime_type": normalized_mime_type,
            "legacy_import_acknowledged": True,
        }
        return self._persist_ingestion_job(
            requested_by=requested_by,
            idempotency_key=idempotency_key,
            source_input_origin=normalized_origin,
            source_input_ref=source_input_ref,
            payload_checksum_sha256=payload_checksum_sha256,
            source_class=normalized_source_class,
            payload_fingerprint=payload_fingerprint,
            storage_key=(
                f"knowledge-official-upload/{payload_checksum_sha256}/{normalized_filename}"
            ),
            extracted_metadata=extracted_metadata,
            proposed_source_record=proposed_source_record,
        )

    def ingest_registered_document_source(
        self,
        *,
        requested_by: str,
        idempotency_key: str,
        document_id: str,
        storage_key: str,
        mime_type: str,
        payload_checksum_sha256: str,
        source_document_system: str,
        source_input_origin: str | None,
        source_class: str | None,
    ) -> KnowledgeIngestionRecord:
        """Persist one governed document-backed official-source ingestion job."""

        normalized_origin = _normalize_origin(
            provided_origin=source_input_origin,
            expected_origin=OFFICIAL_SOURCE_UPLOAD,
        )
        normalized_source_class = _normalize_source_class(source_class)
        normalized_document_id = _normalize_uuid_string(
            value=document_id,
            field_name="document_id",
        )
        normalized_storage_key = _normalize_required_string(
            value=storage_key,
            field_name="storage_key",
        )
        _assert_local_storage_key(normalized_storage_key)
        normalized_mime_type = _normalize_mime_type(mime_type)
        normalized_checksum = _normalize_sha256_checksum(
            value=payload_checksum_sha256,
            field_name="payload_checksum_sha256",
        )
        normalized_source_document_system = _normalize_source_document_system(
            source_document_system
        )
        stored_document = self._load_registered_document(document_id=normalized_document_id)
        if stored_document.storage_key != normalized_storage_key:
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_LINEAGE,
                message="Knowledge ingestion document storage reference is invalid.",
            )
        if normalized_storage_key.startswith("knowledge-official-upload/"):
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_LINEAGE,
                message=(
                    "Knowledge ingestion document handoff requires upstream document "
                    "registration provenance."
                ),
            )
        source_input_ref = _build_document_source_input_ref(
            source_document_system=normalized_source_document_system,
            document_id=normalized_document_id,
        )
        payload_fingerprint = _payload_fingerprint(
            {
                "requested_by": requested_by,
                "idempotency_key": idempotency_key,
                "document_id": normalized_document_id,
                "storage_key": normalized_storage_key,
                "mime_type": normalized_mime_type,
                "payload_checksum_sha256": normalized_checksum,
                "source_document_system": normalized_source_document_system,
                "source_input_origin": normalized_origin,
                "source_class": normalized_source_class,
            }
        )
        extracted_metadata = {
            "document_id": normalized_document_id,
            "storage_key": normalized_storage_key,
            "mime_type": normalized_mime_type,
            "payload_checksum_sha256": normalized_checksum,
            "source_document_system": normalized_source_document_system,
            "registered_document_state": stored_document.state,
        }
        proposed_source_record = {
            "ingestion_kind": "document",
            "ingestion_mode": "document_backed_handoff",
            "source_document_system": normalized_source_document_system,
            "document_id": normalized_document_id,
            "storage_key": normalized_storage_key,
            "source_input_origin": normalized_origin,
            "source_input_ref": source_input_ref,
            "payload_checksum_sha256": normalized_checksum,
            "idempotency_key": idempotency_key,
            "payload_fingerprint": payload_fingerprint,
            "source_class": normalized_source_class,
            "mime_type": normalized_mime_type,
        }
        return self._persist_ingestion_job(
            requested_by=requested_by,
            idempotency_key=idempotency_key,
            source_input_origin=normalized_origin,
            source_input_ref=source_input_ref,
            payload_checksum_sha256=normalized_checksum,
            source_class=normalized_source_class,
            payload_fingerprint=payload_fingerprint,
            storage_key=normalized_storage_key,
            extracted_metadata=extracted_metadata,
            proposed_source_record=proposed_source_record,
            registered_document_id=normalized_document_id,
        )

    def ingest_url_source(
        self,
        *,
        requested_by: str,
        idempotency_key: str,
        url: str,
        source_input_origin: str | None,
        source_class: str | None,
    ) -> KnowledgeIngestionRecord:
        """Persist one governed official-source URL ingestion job."""

        normalized_origin = _normalize_origin(
            provided_origin=source_input_origin,
            expected_origin=OFFICIAL_SOURCE_URL,
        )
        normalized_source_class = _normalize_source_class(source_class)
        normalized_url = _normalize_url(url)
        payload_checksum_sha256 = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()
        source_input_ref = _build_url_source_input_ref(normalized_url)
        payload_fingerprint = _payload_fingerprint(
            {
                "requested_by": requested_by,
                "idempotency_key": idempotency_key,
                "url": normalized_url,
                "payload_checksum_sha256": payload_checksum_sha256,
                "source_input_origin": normalized_origin,
                "source_class": normalized_source_class,
            }
        )
        extracted_metadata = {
            "normalized_url": normalized_url,
            "payload_checksum_sha256": payload_checksum_sha256,
        }
        proposed_source_record = {
            "ingestion_kind": "url",
            "ingestion_mode": "official_source_url_registration",
            "source_input_origin": normalized_origin,
            "source_input_ref": source_input_ref,
            "payload_checksum_sha256": payload_checksum_sha256,
            "idempotency_key": idempotency_key,
            "payload_fingerprint": payload_fingerprint,
            "source_class": normalized_source_class,
            "normalized_url": normalized_url,
        }
        return self._persist_ingestion_job(
            requested_by=requested_by,
            idempotency_key=idempotency_key,
            source_input_origin=normalized_origin,
            source_input_ref=source_input_ref,
            payload_checksum_sha256=payload_checksum_sha256,
            source_class=normalized_source_class,
            payload_fingerprint=payload_fingerprint,
            storage_key=f"knowledge-official-url/{payload_checksum_sha256}",
            extracted_metadata=extracted_metadata,
            proposed_source_record=proposed_source_record,
        )

    def bulk_ingest_file_sources(
        self,
        *,
        requested_by: str,
        items: Sequence[Mapping[str, object]],
    ) -> tuple[KnowledgeBulkIngestionItemRecord, ...]:
        """Persist multiple governed official-source file ingestion jobs deterministically."""

        return self._bulk_ingest_sources(
            requested_by=requested_by,
            items=items,
            ingestion_kind="file",
        )

    def bulk_ingest_registered_document_sources(
        self,
        *,
        requested_by: str,
        items: Sequence[Mapping[str, object]],
    ) -> tuple[KnowledgeBulkIngestionItemRecord, ...]:
        """Persist multiple governed document-backed ingestion jobs deterministically."""

        return self._bulk_ingest_sources(
            requested_by=requested_by,
            items=items,
            ingestion_kind="document",
        )

    def bulk_ingest_url_sources(
        self,
        *,
        requested_by: str,
        items: Sequence[Mapping[str, object]],
    ) -> tuple[KnowledgeBulkIngestionItemRecord, ...]:
        """Persist multiple governed official-source URL ingestion jobs deterministically."""

        return self._bulk_ingest_sources(
            requested_by=requested_by,
            items=items,
            ingestion_kind="url",
        )

    def get_ingestion_job(
        self,
        *,
        ingestion_job_id: str,
    ) -> KnowledgeIngestionDetailRecord:
        """Return one governed ingestion job for deterministic review fetch."""

        record = self._get_ingestion_record_by_id(ingestion_job_id=ingestion_job_id)
        if record is None:
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_REQUEST,
                message="Knowledge ingestion job identifier is invalid.",
            )
        return record.to_detail_record()

    def list_ingestion_jobs(
        self,
        *,
        ingestion_state: str | None,
        source_input_origin: str | None,
        source_class: str | None,
        requested_by: str | None,
        limit: int,
        offset: int,
        sort_by: str | None,
        sort_order: str | None,
    ) -> tuple[KnowledgeIngestionSummaryRecord, ...]:
        """List governed ingestion jobs with deterministic management ordering."""

        normalized_ingestion_state = _normalize_ingestion_state_filter(ingestion_state)
        normalized_source_input_origin = _normalize_source_input_origin_filter(source_input_origin)
        normalized_source_class = _normalize_source_class(source_class)
        requested_by_uuid = (
            _parse_uuid_string(requested_by, field_name="requested_by")
            if requested_by is not None
            else None
        )
        normalized_limit = _normalize_management_limit(limit)
        normalized_offset = _normalize_management_offset(offset)
        normalized_sort_by = _normalize_ingestion_sort_by(sort_by)
        normalized_sort_order = _normalize_sort_order(
            sort_order,
            default="desc",
        )
        self._assert_database_configured()
        query = sql.SQL(
            """
            SELECT
                id::text,
                document_id::text,
                requested_by::text,
                ingestion_state,
                proposed_source_record ->> 'source_input_origin' AS source_input_origin,
                proposed_source_record ->> 'source_input_ref' AS source_input_ref,
                proposed_source_record ->> 'payload_checksum_sha256' AS payload_checksum_sha256,
                proposed_source_record ->> 'source_class' AS source_class,
                created_at::text,
                completed_at::text
            FROM knowledge_ingestion_jobs
            """
        )
        clauses: list[sql.SQL] = []
        parameters: list[object] = []
        if normalized_ingestion_state is not None:
            if normalized_ingestion_state == APPROVED_INGESTION_STATE:
                clauses.append(sql.SQL("ingestion_state = ANY(%s::text[])"))
                parameters.append(
                    [
                        APPROVED_INGESTION_STATE,
                        LEGACY_APPROVED_FOR_PUBLICATION_INGESTION_STATE,
                    ]
                )
            else:
                clauses.append(sql.SQL("ingestion_state = %s"))
                parameters.append(normalized_ingestion_state)
        if normalized_source_input_origin is not None:
            clauses.append(sql.SQL("proposed_source_record ->> 'source_input_origin' = %s"))
            parameters.append(normalized_source_input_origin)
        if normalized_source_class is not None:
            clauses.append(sql.SQL("proposed_source_record ->> 'source_class' = %s"))
            parameters.append(normalized_source_class)
        if requested_by_uuid is not None:
            clauses.append(sql.SQL("requested_by = %s"))
            parameters.append(requested_by_uuid)
        if clauses:
            query += sql.SQL(" WHERE ") + sql.SQL(" AND ").join(clauses)
        query += _ingestion_order_by_clause(
            sort_by=normalized_sort_by,
            sort_order=normalized_sort_order,
        )
        query += sql.SQL(" LIMIT %s OFFSET %s")
        parameters.append(normalized_limit)
        parameters.append(normalized_offset)
        try:
            with psycopg.connect(self._database_url_or_raise(), connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(query, tuple(parameters))
                    rows = cursor.fetchall()
        except psycopg.Error as error:
            raise KnowledgeRepositoryError(
                reason_code=KNOWLEDGE_STORAGE_UNAVAILABLE,
                message="Knowledge persistence storage is unavailable.",
            ) from error
        return tuple(_row_to_ingestion_summary_record(row) for row in rows)

    def review_ingestion_job(
        self,
        *,
        ingestion_job_id: str,
        reviewed_by: str,
        review_notes: Sequence[Mapping[str, object]],
        proposed_source_updates: Mapping[str, object] | None,
    ) -> KnowledgeIngestionDetailRecord:
        """Persist deterministic review notes and keep the job non-searchable."""

        reviewed_by_uuid = _parse_uuid_string(reviewed_by, field_name="reviewed_by")
        _validate_review_notes(review_notes)
        self._assert_database_configured()
        try:
            with psycopg.connect(self._database_url_or_raise(), connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    self._ensure_user_row(cursor=cursor, user_id=reviewed_by_uuid)
                    stored = self._load_ingestion_record_for_update(
                        cursor=cursor,
                        ingestion_job_id=ingestion_job_id,
                    )
                    self._assert_reviewable_state(stored.ingestion_state)
                    merged_proposed = deepcopy(stored.proposed_source_record)
                    if proposed_source_updates is not None:
                        merged_proposed.update(proposed_source_updates)
                    merged_proposed["last_reviewed_by"] = str(reviewed_by_uuid)
                    merged_proposed["last_reviewed_at"] = datetime.now(UTC).isoformat()
                    cursor.execute(
                        """
                        UPDATE knowledge_ingestion_jobs
                        SET ingestion_state = %s,
                            proposed_source_record = %s::jsonb,
                            review_notes = %s::jsonb
                        WHERE id = %s
                        """,
                        (
                            REVIEW_PENDING_INGESTION_STATE,
                            json.dumps(merged_proposed, sort_keys=True),
                            json.dumps(list(review_notes), sort_keys=True),
                            UUID(ingestion_job_id),
                        ),
                    )
                    self._record_audit_event(
                        cursor=cursor,
                        user_id=reviewed_by_uuid,
                        event_type="knowledge_review",
                        resource_type="knowledge_ingestion_job",
                        resource_id=_parse_uuid_string(
                            ingestion_job_id,
                            field_name="ingestion_job_id",
                        ),
                        correlation_id=f"knowledge-review-{ingestion_job_id}",
                        details={
                            "ingestion_job_id": ingestion_job_id,
                            "review_notes_count": len(review_notes),
                        },
                    )
                connection.commit()
        except psycopg.Error as error:
            raise KnowledgeRepositoryError(
                reason_code=KNOWLEDGE_STORAGE_UNAVAILABLE,
                message="Knowledge persistence storage is unavailable.",
            ) from error

        return self.get_ingestion_job(ingestion_job_id=ingestion_job_id)

    def approve_ingestion_job(
        self,
        *,
        ingestion_job_id: str,
        reviewed_by: str,
        publication_payload: Mapping[str, object],
        review_notes: Sequence[Mapping[str, object]],
    ) -> KnowledgeIngestionDetailRecord:
        """Approve one ingestion job for governed publication."""

        reviewed_by_uuid = _parse_uuid_string(reviewed_by, field_name="reviewed_by")
        _validate_review_notes(review_notes)
        normalized_publication_payload = _normalize_publication_payload(publication_payload)
        self._assert_database_configured()
        try:
            with psycopg.connect(self._database_url_or_raise(), connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    self._ensure_user_row(cursor=cursor, user_id=reviewed_by_uuid)
                    stored = self._load_ingestion_record_for_update(
                        cursor=cursor,
                        ingestion_job_id=ingestion_job_id,
                    )
                    if stored.source_input_origin not in {
                        OFFICIAL_SOURCE_UPLOAD,
                        OFFICIAL_SOURCE_URL,
                    }:
                        raise KnowledgeRepositoryError(
                            reason_code=INVALID_KNOWLEDGE_LINEAGE,
                            message=(
                                "Knowledge publication approval requires "
                                "governed official-source lineage."
                            ),
                        )
                    if stored.ingestion_state not in {
                        INITIAL_INGESTION_STATE,
                        REVIEW_PENDING_INGESTION_STATE,
                        REJECTED_INGESTION_STATE,
                    }:
                        raise KnowledgeRepositoryError(
                            reason_code=INVALID_PUBLICATION_STATE_TRANSITION,
                            message=(
                                "Knowledge ingestion job cannot transition "
                                "to approval from its current state."
                            ),
                        )
                    if stored.source_class is not None and stored.source_class != str(
                        normalized_publication_payload["source_class"]
                    ):
                        raise KnowledgeRepositoryError(
                            reason_code=INVALID_KNOWLEDGE_LINEAGE,
                            message=(
                                "Knowledge publication source class conflicts "
                                "with ingested lineage metadata."
                            ),
                        )
                    merged_proposed = deepcopy(stored.proposed_source_record)
                    merged_proposed["publication_payload"] = normalized_publication_payload
                    merged_proposed["approved_by"] = str(reviewed_by_uuid)
                    merged_proposed["approved_at"] = datetime.now(UTC).isoformat()
                    cursor.execute(
                        """
                        UPDATE knowledge_ingestion_jobs
                        SET ingestion_state = %s,
                            proposed_source_record = %s::jsonb,
                            review_notes = %s::jsonb
                        WHERE id = %s
                        """,
                        (
                            APPROVED_INGESTION_STATE,
                            json.dumps(merged_proposed, sort_keys=True),
                            json.dumps(list(review_notes), sort_keys=True),
                            UUID(ingestion_job_id),
                        ),
                    )
                    self._record_audit_event(
                        cursor=cursor,
                        user_id=reviewed_by_uuid,
                        event_type="knowledge_approval",
                        resource_type="knowledge_ingestion_job",
                        resource_id=_parse_uuid_string(
                            ingestion_job_id,
                            field_name="ingestion_job_id",
                        ),
                        correlation_id=f"knowledge-approval-{ingestion_job_id}",
                        details={
                            "ingestion_job_id": ingestion_job_id,
                            "source_id": normalized_publication_payload["source_id"],
                        },
                    )
                connection.commit()
        except psycopg.Error as error:
            raise KnowledgeRepositoryError(
                reason_code=KNOWLEDGE_STORAGE_UNAVAILABLE,
                message="Knowledge persistence storage is unavailable.",
            ) from error

        return self.get_ingestion_job(ingestion_job_id=ingestion_job_id)

    def reject_ingestion_job(
        self,
        *,
        ingestion_job_id: str,
        reviewed_by: str,
        review_notes: Sequence[Mapping[str, object]],
    ) -> KnowledgeIngestionDetailRecord:
        """Reject one ingestion job and preserve fail-closed publication behavior."""

        reviewed_by_uuid = _parse_uuid_string(reviewed_by, field_name="reviewed_by")
        _validate_review_notes(review_notes)
        self._assert_database_configured()
        try:
            with psycopg.connect(self._database_url_or_raise(), connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    self._ensure_user_row(cursor=cursor, user_id=reviewed_by_uuid)
                    stored = self._load_ingestion_record_for_update(
                        cursor=cursor,
                        ingestion_job_id=ingestion_job_id,
                    )
                    if stored.ingestion_state == PUBLISHED_INGESTION_STATE:
                        raise KnowledgeRepositoryError(
                            reason_code=INVALID_PUBLICATION_STATE_TRANSITION,
                            message="Published knowledge ingestion jobs cannot be rejected.",
                        )
                    merged_proposed = deepcopy(stored.proposed_source_record)
                    merged_proposed["rejected_by"] = str(reviewed_by_uuid)
                    merged_proposed["rejected_at"] = datetime.now(UTC).isoformat()
                    cursor.execute(
                        """
                        UPDATE knowledge_ingestion_jobs
                        SET ingestion_state = %s,
                            proposed_source_record = %s::jsonb,
                            review_notes = %s::jsonb,
                            completed_at = CURRENT_TIMESTAMP
                        WHERE id = %s
                        """,
                        (
                            REJECTED_INGESTION_STATE,
                            json.dumps(merged_proposed, sort_keys=True),
                            json.dumps(list(review_notes), sort_keys=True),
                            UUID(ingestion_job_id),
                        ),
                    )
                    self._record_audit_event(
                        cursor=cursor,
                        user_id=reviewed_by_uuid,
                        event_type="knowledge_rejection",
                        resource_type="knowledge_ingestion_job",
                        resource_id=_parse_uuid_string(
                            ingestion_job_id,
                            field_name="ingestion_job_id",
                        ),
                        correlation_id=f"knowledge-rejection-{ingestion_job_id}",
                        details={
                            "ingestion_job_id": ingestion_job_id,
                            "review_notes_count": len(review_notes),
                        },
                    )
                connection.commit()
        except psycopg.Error as error:
            raise KnowledgeRepositoryError(
                reason_code=KNOWLEDGE_STORAGE_UNAVAILABLE,
                message="Knowledge persistence storage is unavailable.",
            ) from error

        return self.get_ingestion_job(ingestion_job_id=ingestion_job_id)

    def publish_ingestion_job(
        self,
        *,
        ingestion_job_id: str,
        published_by: str,
    ) -> KnowledgeIngestionDetailRecord:
        """Publish one approved ingestion job into searchable governed records."""

        published_by_uuid = _parse_uuid_string(published_by, field_name="published_by")
        self._assert_database_configured()
        try:
            with psycopg.connect(self._database_url_or_raise(), connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    self._ensure_user_row(cursor=cursor, user_id=published_by_uuid)
                    stored = self._load_ingestion_record_for_update(
                        cursor=cursor,
                        ingestion_job_id=ingestion_job_id,
                    )
                    if stored.ingestion_state == PUBLISHED_INGESTION_STATE:
                        return stored.to_detail_record()
                    if not _is_approved_ingestion_state(stored.ingestion_state):
                        raise KnowledgeRepositoryError(
                            reason_code=INVALID_PUBLICATION_STATE_TRANSITION,
                            message="Knowledge ingestion job must be approved before publication.",
                        )
                    if stored.source_input_origin not in {
                        OFFICIAL_SOURCE_UPLOAD,
                        OFFICIAL_SOURCE_URL,
                    }:
                        raise KnowledgeRepositoryError(
                            reason_code=INVALID_KNOWLEDGE_LINEAGE,
                            message=(
                                "Knowledge publication requires governed official-source lineage."
                            ),
                        )
                    lineage_document_id = self._verify_publishable_ingestion_lineage(stored)

                    publication_payload = _extract_publication_payload(
                        stored.proposed_source_record
                    )
                    approved_by = str(stored.proposed_source_record.get("approved_by", "")).strip()
                    if not approved_by:
                        raise KnowledgeRepositoryError(
                            reason_code=KNOWLEDGE_PUBLICATION_SAFETY_REJECTED,
                            message=(
                                "Knowledge publication requires prior reviewer approval metadata."
                            ),
                        )
                    if approved_by == str(published_by_uuid):
                        raise KnowledgeRepositoryError(
                            reason_code=KNOWLEDGE_PUBLICATION_SAFETY_REJECTED,
                            message=(
                                "Knowledge publication requires a publisher "
                                "distinct from the approving reviewer."
                            ),
                        )

                    source_id = str(publication_payload["source_id"])
                    source_family_id = str(publication_payload["source_family_id"])
                    point_in_time_url = str(publication_payload["point_in_time_url"])
                    source_class = str(publication_payload["source_class"])
                    authority_level = str(publication_payload["authority_level"])
                    tax_domain = str(publication_payload["tax_domain"])
                    issuing_authority = str(publication_payload["issuing_authority"])
                    title = str(publication_payload["title"])
                    source_version_form = str(publication_payload["source_version_form"])
                    effective_from = date.fromisoformat(str(publication_payload["effective_from"]))
                    effective_to_value = publication_payload.get("effective_to")
                    effective_to = (
                        date.fromisoformat(str(effective_to_value))
                        if effective_to_value is not None
                        else None
                    )
                    tax_year_value = publication_payload.get("tax_year")
                    tax_year = _coerce_optional_int(
                        tax_year_value,
                        field_name="knowledge_source_versions.tax_year",
                    )
                    prepared_anchors = self._prepare_publication_anchors(
                        anchors=cast(tuple[dict[str, object], ...], publication_payload["anchors"])
                    )

                    self._upsert_governed_source(
                        cursor=cursor,
                        source_id=source_id,
                        source_family_id=source_family_id,
                        title=title,
                        canonical_url=point_in_time_url,
                        source_class=source_class,
                        authority_level=authority_level,
                        tax_domain=tax_domain,
                        issuing_authority=issuing_authority,
                        created_by=published_by_uuid,
                    )
                    publication_event_id = self._create_publication_event(
                        cursor=cursor,
                        user_id=published_by_uuid,
                        ingestion_job_id=ingestion_job_id,
                        source_id=source_id,
                    )
                    source_version_id = uuid4()
                    cursor.execute(
                        """
                        INSERT INTO knowledge_source_versions (
                            id,
                            source_id,
                            document_id,
                            point_in_time_url,
                            source_checksum_sha256,
                            source_version_form,
                            source_input_origin,
                            source_input_ref,
                            publication_state,
                            effective_from,
                            effective_to,
                            tax_year,
                            publication_event_id,
                            approved_at,
                            approved_by
                        )
                        VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, CURRENT_TIMESTAMP, %s
                        )
                        """,
                        (
                            source_version_id,
                            source_id,
                            UUID(lineage_document_id) if lineage_document_id is not None else None,
                            point_in_time_url,
                            stored.payload_checksum_sha256,
                            source_version_form,
                            stored.source_input_origin,
                            stored.source_input_ref,
                            SEARCHABLE_SOURCE_PUBLICATION_STATE,
                            effective_from,
                            effective_to,
                            tax_year,
                            publication_event_id,
                            UUID(approved_by),
                        ),
                    )
                    self._insert_publication_anchors_and_chunks(
                        cursor=cursor,
                        source_version_id=source_version_id,
                        anchors=prepared_anchors,
                    )
                    merged_proposed = deepcopy(stored.proposed_source_record)
                    merged_proposed["published_source_id"] = source_id
                    merged_proposed["published_source_version_id"] = str(source_version_id)
                    merged_proposed["publication_event_id"] = str(publication_event_id)
                    merged_proposed["published_by"] = str(published_by_uuid)
                    merged_proposed["published_at"] = datetime.now(UTC).isoformat()
                    cursor.execute(
                        """
                        UPDATE knowledge_ingestion_jobs
                        SET ingestion_state = %s,
                            proposed_source_record = %s::jsonb,
                            completed_at = CURRENT_TIMESTAMP
                        WHERE id = %s
                        """,
                        (
                            PUBLISHED_INGESTION_STATE,
                            json.dumps(merged_proposed, sort_keys=True),
                            UUID(ingestion_job_id),
                        ),
                    )
                    self._record_audit_event(
                        cursor=cursor,
                        user_id=published_by_uuid,
                        event_type="knowledge_ingestion_publish",
                        resource_type="knowledge_ingestion_job",
                        resource_id=_parse_uuid_string(
                            ingestion_job_id,
                            field_name="ingestion_job_id",
                        ),
                        correlation_id=f"knowledge-ingestion-publish-{ingestion_job_id}",
                        details={
                            "ingestion_job_id": ingestion_job_id,
                            "source_id": source_id,
                            "source_version_id": str(source_version_id),
                        },
                    )
                connection.commit()
        except psycopg.Error as error:
            raise KnowledgeRepositoryError(
                reason_code=KNOWLEDGE_STORAGE_UNAVAILABLE,
                message="Knowledge persistence storage is unavailable.",
            ) from error

        return self.get_ingestion_job(ingestion_job_id=ingestion_job_id)

    def bulk_reject_ingestion_jobs(
        self,
        *,
        reviewed_by: str,
        ingestion_job_ids: Sequence[str],
        review_notes: Sequence[Mapping[str, object]],
    ) -> tuple[KnowledgeBulkOperationItemRecord, ...]:
        """Reject multiple governed ingestion jobs deterministically."""

        _parse_uuid_string(reviewed_by, field_name="acting_user")
        _validate_review_notes(review_notes)
        normalized_job_ids = _normalize_bulk_identifier_list(
            ingestion_job_ids,
            field_name="ids",
        )
        outcomes: list[KnowledgeBulkOperationItemRecord] = []
        for ingestion_job_id in normalized_job_ids:
            try:
                current = self.get_ingestion_job(ingestion_job_id=ingestion_job_id)
                if current.ingestion_state == REJECTED_INGESTION_STATE:
                    outcomes.append(
                        KnowledgeBulkOperationItemRecord(
                            id=ingestion_job_id,
                            status="ok",
                            outcome="rejected",
                            error_code=None,
                            reason=None,
                        )
                    )
                    continue
                self.reject_ingestion_job(
                    ingestion_job_id=ingestion_job_id,
                    reviewed_by=reviewed_by,
                    review_notes=review_notes,
                )
            except KnowledgeRepositoryError as error:
                outcomes.append(
                    KnowledgeBulkOperationItemRecord(
                        id=ingestion_job_id,
                        status="error",
                        outcome="failed",
                        error_code=error.reason_code,
                        reason=error.reason_code,
                    )
                )
                continue
            outcomes.append(
                KnowledgeBulkOperationItemRecord(
                    id=ingestion_job_id,
                    status="ok",
                    outcome="rejected",
                    error_code=None,
                    reason=None,
                )
            )
        self._record_best_effort_actor_audit_event(
            user_id=_parse_uuid_string(reviewed_by, field_name="acting_user"),
            event_type="knowledge_bulk_reject",
            correlation_id=(
                "knowledge-bulk-reject-"
                + hashlib.sha256(
                    json.dumps({"ids": list(normalized_job_ids)}, sort_keys=True).encode("utf-8")
                ).hexdigest()[:16]
            ),
            details={
                "acting_user": reviewed_by,
                "ids": list(normalized_job_ids),
                "total": len(outcomes),
            },
        )
        return tuple(outcomes)

    def bulk_publish_ingestion_jobs(
        self,
        *,
        published_by: str,
        ingestion_job_ids: Sequence[str],
    ) -> tuple[KnowledgeBulkOperationItemRecord, ...]:
        """Publish multiple governed ingestion jobs deterministically."""

        _parse_uuid_string(published_by, field_name="acting_user")
        normalized_job_ids = _normalize_bulk_identifier_list(
            ingestion_job_ids,
            field_name="ids",
        )
        outcomes: list[KnowledgeBulkOperationItemRecord] = []
        for ingestion_job_id in normalized_job_ids:
            try:
                current = self.get_ingestion_job(ingestion_job_id=ingestion_job_id)
                if current.ingestion_state == PUBLISHED_INGESTION_STATE:
                    outcomes.append(
                        KnowledgeBulkOperationItemRecord(
                            id=ingestion_job_id,
                            status="ok",
                            outcome="published",
                            error_code=None,
                            reason=None,
                        )
                    )
                    continue
                self.publish_ingestion_job(
                    ingestion_job_id=ingestion_job_id,
                    published_by=published_by,
                )
            except KnowledgeRepositoryError as error:
                outcomes.append(
                    KnowledgeBulkOperationItemRecord(
                        id=ingestion_job_id,
                        status="error",
                        outcome="failed",
                        error_code=error.reason_code,
                        reason=error.reason_code,
                    )
                )
                continue
            outcomes.append(
                KnowledgeBulkOperationItemRecord(
                    id=ingestion_job_id,
                    status="ok",
                    outcome="published",
                    error_code=None,
                    reason=None,
                )
            )
        self._record_best_effort_actor_audit_event(
            user_id=_parse_uuid_string(published_by, field_name="acting_user"),
            event_type="knowledge_bulk_publish",
            correlation_id=(
                "knowledge-bulk-publish-"
                + hashlib.sha256(
                    json.dumps({"ids": list(normalized_job_ids)}, sort_keys=True).encode("utf-8")
                ).hexdigest()[:16]
            ),
            details={
                "acting_user": published_by,
                "ids": list(normalized_job_ids),
                "total": len(outcomes),
            },
        )
        return tuple(outcomes)

    def supersede_source_version(
        self,
        *,
        source_version_id: str,
        successor_source_version_id: str,
        superseded_by: str,
    ) -> KnowledgeSourceVersionLifecycleRecord:
        """Supersede one published source version with a same-source successor."""

        predecessor_uuid = _parse_uuid_string(
            source_version_id,
            field_name="source_version_id",
        )
        successor_uuid = _parse_uuid_string(
            successor_source_version_id,
            field_name="successor_source_version_id",
        )
        if predecessor_uuid == successor_uuid:
            raise KnowledgeRepositoryError(
                reason_code=KNOWLEDGE_SUPERSESSION_CONFLICT,
                message="Knowledge supersession requires distinct predecessor and successor.",
            )
        acted_by_uuid = _parse_uuid_string(superseded_by, field_name="superseded_by")
        self._assert_database_configured()
        try:
            with psycopg.connect(self._database_url_or_raise(), connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    self._ensure_user_row(cursor=cursor, user_id=acted_by_uuid)
                    locked_versions = self._load_source_versions_for_update(
                        cursor=cursor,
                        source_version_ids=(str(predecessor_uuid), str(successor_uuid)),
                    )
                    predecessor = locked_versions[str(predecessor_uuid)]
                    successor = locked_versions[str(successor_uuid)]
                    self._assert_official_source_version(record=predecessor)
                    self._assert_official_source_version(record=successor)
                    if predecessor.source_family_id != successor.source_family_id:
                        raise KnowledgeRepositoryError(
                            reason_code=KNOWLEDGE_SUPERSESSION_CONFLICT,
                            message=(
                                "Knowledge supersession requires predecessor and "
                                "successor from the same governed source family."
                            ),
                        )
                    if successor.publication_state != SEARCHABLE_SOURCE_PUBLICATION_STATE:
                        raise KnowledgeRepositoryError(
                            reason_code=KNOWLEDGE_RECORD_NOT_PUBLISHED,
                            message="Knowledge supersession successor must already be published.",
                        )
                    if predecessor.publication_state == "superseded":
                        if (
                            predecessor.superseded_by_source_version_id
                            == successor.source_version_id
                        ):
                            return predecessor.to_lifecycle_record()
                        raise KnowledgeRepositoryError(
                            reason_code=KNOWLEDGE_SUPERSESSION_CONFLICT,
                            message=(
                                "Knowledge source version is already superseded by a "
                                "different successor."
                            ),
                        )
                    if predecessor.publication_state != SEARCHABLE_SOURCE_PUBLICATION_STATE:
                        raise KnowledgeRepositoryError(
                            reason_code=KNOWLEDGE_RECORD_NOT_PUBLISHED,
                            message=(
                                "Knowledge source version must be published before supersession."
                            ),
                        )
                    if successor.effective_from <= predecessor.effective_from:
                        raise KnowledgeRepositoryError(
                            reason_code=KNOWLEDGE_TEMPORAL_SCOPE_MISMATCH,
                            message=(
                                "Knowledge supersession successor must begin after "
                                "the predecessor effective window starts."
                            ),
                        )
                    if (
                        predecessor.effective_to is not None
                        and successor.effective_from <= predecessor.effective_to
                    ):
                        raise KnowledgeRepositoryError(
                            reason_code=KNOWLEDGE_TEMPORAL_SCOPE_MISMATCH,
                            message=(
                                "Knowledge supersession successor must begin after "
                                "the predecessor effective window ends."
                            ),
                        )
                    self._create_source_version_transition_event(
                        cursor=cursor,
                        user_id=acted_by_uuid,
                        source_version_id=predecessor.source_version_id,
                        event_type="knowledge_supersession",
                        details={
                            "predecessor_source_version_id": predecessor.source_version_id,
                            "successor_source_version_id": successor.source_version_id,
                        },
                    )
                    cursor.execute(
                        """
                        UPDATE knowledge_source_versions
                        SET publication_state = 'superseded'
                        WHERE id = %s
                        """,
                        (predecessor_uuid,),
                    )
                    self._record_audit_event(
                        cursor=cursor,
                        user_id=acted_by_uuid,
                        event_type="knowledge_supersession_request",
                        resource_type="knowledge_source_version",
                        resource_id=predecessor_uuid,
                        correlation_id=f"knowledge-supersession-request-{source_version_id}",
                        details={
                            "predecessor_source_version_id": predecessor.source_version_id,
                            "successor_source_version_id": successor.source_version_id,
                        },
                    )
                connection.commit()
        except psycopg.Error as error:
            raise KnowledgeRepositoryError(
                reason_code=KNOWLEDGE_STORAGE_UNAVAILABLE,
                message="Knowledge persistence storage is unavailable.",
            ) from error

        return self.get_source_version_lifecycle(source_version_id=source_version_id)

    def archive_source_version(
        self,
        *,
        source_version_id: str,
        archived_by: str,
    ) -> KnowledgeSourceVersionLifecycleRecord:
        """Archive one governed published or superseded source version."""

        source_version_uuid = _parse_uuid_string(
            source_version_id,
            field_name="source_version_id",
        )
        acted_by_uuid = _parse_uuid_string(archived_by, field_name="archived_by")
        self._assert_database_configured()
        try:
            with psycopg.connect(self._database_url_or_raise(), connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    self._ensure_user_row(cursor=cursor, user_id=acted_by_uuid)
                    record = self._load_source_version_for_update(
                        cursor=cursor,
                        source_version_id=source_version_id,
                    )
                    self._assert_official_source_version(record=record)
                    if record.publication_state == "archived":
                        return record.to_lifecycle_record()
                    if record.publication_state not in {"published", "superseded"}:
                        raise KnowledgeRepositoryError(
                            reason_code=KNOWLEDGE_RECORD_NOT_PUBLISHED,
                            message=(
                                "Knowledge source version must be published or "
                                "superseded before archiving."
                            ),
                        )
                    if (
                        record.publication_state == "published"
                        and record.superseded_by_source_version_id is None
                    ):
                        raise KnowledgeRepositoryError(
                            reason_code=INVALID_PUBLICATION_STATE_TRANSITION,
                            message=(
                                "Knowledge source version cannot be archived while it "
                                "remains the active published version."
                            ),
                        )
                    self._create_source_version_transition_event(
                        cursor=cursor,
                        user_id=acted_by_uuid,
                        source_version_id=record.source_version_id,
                        event_type="knowledge_archive",
                        details={"archived_source_version_id": record.source_version_id},
                    )
                    cursor.execute(
                        """
                        UPDATE knowledge_source_versions
                        SET publication_state = 'archived'
                        WHERE id = %s
                        """,
                        (source_version_uuid,),
                    )
                    self._record_audit_event(
                        cursor=cursor,
                        user_id=acted_by_uuid,
                        event_type="knowledge_archive_request",
                        resource_type="knowledge_source_version",
                        resource_id=source_version_uuid,
                        correlation_id=f"knowledge-archive-request-{source_version_id}",
                        details={"source_version_id": source_version_id},
                    )
                connection.commit()
        except psycopg.Error as error:
            raise KnowledgeRepositoryError(
                reason_code=KNOWLEDGE_STORAGE_UNAVAILABLE,
                message="Knowledge persistence storage is unavailable.",
            ) from error

        return self.get_source_version_lifecycle(source_version_id=source_version_id)

    def bulk_archive_source_versions(
        self,
        *,
        archived_by: str,
        source_version_ids: Sequence[str],
    ) -> tuple[KnowledgeBulkOperationItemRecord, ...]:
        """Archive multiple governed source versions deterministically."""

        _parse_uuid_string(archived_by, field_name="acting_user")
        normalized_source_version_ids = _normalize_bulk_identifier_list(
            source_version_ids,
            field_name="ids",
        )
        outcomes: list[KnowledgeBulkOperationItemRecord] = []
        for source_version_id in normalized_source_version_ids:
            try:
                current = self.get_source_version_lifecycle(source_version_id=source_version_id)
                if current.publication_state == "archived":
                    outcomes.append(
                        KnowledgeBulkOperationItemRecord(
                            id=source_version_id,
                            status="ok",
                            outcome="archived",
                            error_code=None,
                            reason=None,
                        )
                    )
                    continue
                self.archive_source_version(
                    source_version_id=source_version_id,
                    archived_by=archived_by,
                )
            except KnowledgeRepositoryError as error:
                outcomes.append(
                    KnowledgeBulkOperationItemRecord(
                        id=source_version_id,
                        status="error",
                        outcome="failed",
                        error_code=error.reason_code,
                        reason=error.reason_code,
                    )
                )
                continue
            outcomes.append(
                KnowledgeBulkOperationItemRecord(
                    id=source_version_id,
                    status="ok",
                    outcome="archived",
                    error_code=None,
                    reason=None,
                )
            )
        self._record_best_effort_actor_audit_event(
            user_id=_parse_uuid_string(archived_by, field_name="acting_user"),
            event_type="knowledge_bulk_archive",
            correlation_id=(
                "knowledge-bulk-archive-"
                + hashlib.sha256(
                    json.dumps(
                        {"ids": list(normalized_source_version_ids)},
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest()[:16]
            ),
            details={
                "acting_user": archived_by,
                "ids": list(normalized_source_version_ids),
                "total": len(outcomes),
            },
        )
        return tuple(outcomes)

    def get_source_version_lifecycle(
        self,
        *,
        source_version_id: str,
    ) -> KnowledgeSourceVersionLifecycleRecord:
        """Return one governed source-version lifecycle record."""

        record = self._get_source_version_record_by_id(source_version_id=source_version_id)
        if record is None:
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_REQUEST,
                message="Knowledge source version identifier is invalid.",
            )
        return record.to_lifecycle_record()

    def list_source_versions(
        self,
        *,
        publication_state: str | None,
        source_id: str | None,
        source_family_id: str | None,
        tax_domain: str | None,
        source_class: str | None,
        limit: int,
        offset: int,
        sort_by: str | None,
        sort_order: str | None,
    ) -> tuple[KnowledgeSourceVersionSummaryRecord, ...]:
        """List governed source versions with deterministic management ordering."""

        normalized_publication_state = _normalize_publication_state_filter(publication_state)
        normalized_source_id = _normalize_optional_identifier(source_id)
        normalized_source_family_id = _normalize_optional_identifier(source_family_id)
        normalized_tax_domain = _normalize_optional_identifier(tax_domain)
        normalized_source_class = _normalize_source_class(source_class)
        normalized_limit = _normalize_management_limit(limit)
        normalized_offset = _normalize_management_offset(offset)
        normalized_sort_by = _normalize_source_version_sort_by(sort_by)
        normalized_sort_order = _normalize_sort_order(
            sort_order,
            default="asc",
        )
        self._assert_database_configured()
        query = sql.SQL(
            """
            SELECT
                ksv.id::text,
                ksv.source_id,
                ks.source_family_id,
                ks.title,
                ks.source_class,
                ks.tax_domain,
                ks.authority_level,
                ksv.publication_state,
                ksv.source_input_origin,
                ksv.source_version_form,
                ksv.effective_from,
                ksv.effective_to,
                ksv.tax_year,
                ksv.supersedes_source_version_id::text,
                (
                    SELECT CAST(ae.details ->> 'successor_source_version_id' AS text)
                    FROM audit_events AS ae
                    WHERE ae.event_type = 'knowledge_supersession'
                      AND ae.resource_type = 'knowledge_source_version'
                      AND ae.resource_id = ksv.id
                    ORDER BY ae.created_at DESC, ae.id DESC
                    LIMIT 1
                ) AS superseded_by_source_version_id
            FROM knowledge_source_versions AS ksv
            JOIN knowledge_sources AS ks
              ON ks.source_id = ksv.source_id
            """
        )
        clauses: list[sql.SQL] = []
        parameters: list[object] = []
        if normalized_publication_state is not None:
            clauses.append(sql.SQL("ksv.publication_state = %s"))
            parameters.append(normalized_publication_state)
        if normalized_source_id is not None:
            clauses.append(sql.SQL("ksv.source_id = %s"))
            parameters.append(normalized_source_id)
        if normalized_source_family_id is not None:
            clauses.append(sql.SQL("ks.source_family_id = %s"))
            parameters.append(normalized_source_family_id)
        if normalized_tax_domain is not None:
            clauses.append(sql.SQL("ks.tax_domain = %s"))
            parameters.append(normalized_tax_domain)
        if normalized_source_class is not None:
            clauses.append(sql.SQL("ks.source_class = %s"))
            parameters.append(normalized_source_class)
        if clauses:
            query += sql.SQL(" WHERE ") + sql.SQL(" AND ").join(clauses)
        query += _source_version_order_by_clause(
            sort_by=normalized_sort_by,
            sort_order=normalized_sort_order,
        )
        query += sql.SQL(" LIMIT %s OFFSET %s")
        parameters.append(normalized_limit)
        parameters.append(normalized_offset)
        try:
            with psycopg.connect(self._database_url_or_raise(), connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(query, tuple(parameters))
                    rows = cursor.fetchall()
        except psycopg.Error as error:
            raise KnowledgeRepositoryError(
                reason_code=KNOWLEDGE_STORAGE_UNAVAILABLE,
                message="Knowledge persistence storage is unavailable.",
            ) from error
        return tuple(_row_to_source_version_summary_record(row) for row in rows)

    def list_sources(
        self,
        *,
        source_class: str | None,
        tax_domain: str | None,
        limit: int,
        offset: int,
        sort_by: str | None,
        sort_order: str | None,
    ) -> tuple[KnowledgeSourceSummaryRecord, ...]:
        """List governed source records with deterministic management ordering."""

        normalized_source_class = _normalize_source_class(source_class)
        normalized_tax_domain = _normalize_optional_identifier(tax_domain)
        normalized_limit = _normalize_management_limit(limit)
        normalized_offset = _normalize_management_offset(offset)
        normalized_sort_by = _normalize_source_sort_by(sort_by)
        normalized_sort_order = _normalize_sort_order(sort_order, default="asc")
        self._assert_database_configured()
        query = sql.SQL(
            """
            SELECT
                ks.source_id,
                ks.source_family_id,
                ks.title,
                ks.canonical_url,
                ks.source_class,
                ks.tax_domain,
                ks.authority_level,
                ks.issuing_authority,
                COUNT(DISTINCT ksv.id) AS version_count,
                COUNT(DISTINCT ka.anchor_id) AS anchor_count,
                ks.created_at::text,
                ks.retired_at::text
            FROM knowledge_sources AS ks
            LEFT JOIN knowledge_source_versions AS ksv
              ON ksv.source_id = ks.source_id
            LEFT JOIN knowledge_anchors AS ka
              ON ka.source_version_id = ksv.id
            """
        )
        clauses: list[sql.SQL] = []
        parameters: list[object] = []
        if normalized_source_class is not None:
            clauses.append(sql.SQL("ks.source_class = %s"))
            parameters.append(normalized_source_class)
        if normalized_tax_domain is not None:
            clauses.append(sql.SQL("ks.tax_domain = %s"))
            parameters.append(normalized_tax_domain)
        if clauses:
            query += sql.SQL(" WHERE ") + sql.SQL(" AND ").join(clauses)
        query += sql.SQL(
            """
            GROUP BY
                ks.source_id,
                ks.source_family_id,
                ks.title,
                ks.canonical_url,
                ks.source_class,
                ks.tax_domain,
                ks.authority_level,
                ks.issuing_authority,
                ks.created_at,
                ks.retired_at
            """
        )
        query += _source_order_by_clause(
            sort_by=normalized_sort_by,
            sort_order=normalized_sort_order,
        )
        query += sql.SQL(" LIMIT %s OFFSET %s")
        parameters.append(normalized_limit)
        parameters.append(normalized_offset)
        try:
            with psycopg.connect(self._database_url_or_raise(), connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(query, tuple(parameters))
                    rows = cursor.fetchall()
        except psycopg.Error as error:
            raise KnowledgeRepositoryError(
                reason_code=KNOWLEDGE_STORAGE_UNAVAILABLE,
                message="Knowledge persistence storage is unavailable.",
            ) from error
        return tuple(_row_to_source_summary_record(row) for row in rows)

    def get_source(
        self,
        *,
        source_id: str,
    ) -> KnowledgeSourceDetailRecord:
        """Return one governed source detail record."""

        normalized_source_id = _normalize_required_string(value=source_id, field_name="source_id")
        self._assert_database_configured()
        try:
            with psycopg.connect(self._database_url_or_raise(), connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            ks.source_id,
                            ks.source_family_id,
                            ks.title,
                            ks.canonical_url,
                            ks.source_class,
                            ks.tax_domain,
                            ks.authority_level,
                            ks.issuing_authority,
                            COUNT(DISTINCT ksv.id) AS version_count,
                            COUNT(DISTINCT ka.anchor_id) AS anchor_count,
                            COUNT(DISTINCT kc.id) AS chunk_count,
                            ks.created_at::text,
                            ks.retired_at::text,
                            BOOL_OR(d.id IS NOT NULL) AS has_document_lineage,
                            BOOL_OR(d.state = 'purged') AS has_purged_document_lineage,
                            BOOL_OR(
                                ksv.source_input_ref LIKE 'document_ai://documents/%'
                            ) AS has_historical_compatibility_lineage,
                            BOOL_OR(
                                ksv.source_input_ref LIKE 'official-source-upload://legacy-import/%'
                            ) AS has_legacy_import_lineage,
                            BOOL_OR(
                                ksv.source_input_origin = 'official_source_url'
                            ) AS has_url_lineage
                        FROM knowledge_sources AS ks
                        LEFT JOIN knowledge_source_versions AS ksv
                          ON ksv.source_id = ks.source_id
                        LEFT JOIN knowledge_anchors AS ka
                          ON ka.source_version_id = ksv.id
                        LEFT JOIN knowledge_chunks AS kc
                          ON kc.anchor_id = ka.anchor_id
                        LEFT JOIN documents AS d
                          ON d.id = ksv.document_id
                        WHERE ks.source_id = %s
                        GROUP BY
                            ks.source_id,
                            ks.source_family_id,
                            ks.title,
                            ks.canonical_url,
                            ks.source_class,
                            ks.tax_domain,
                            ks.authority_level,
                            ks.issuing_authority,
                            ks.created_at,
                            ks.retired_at
                        """,
                        (normalized_source_id,),
                    )
                    source_row = cursor.fetchone()
                    if source_row is None:
                        raise KnowledgeRepositoryError(
                            reason_code=INVALID_KNOWLEDGE_REQUEST,
                            message="Knowledge source identifier is invalid.",
                        )
                    cursor.execute(
                        """
                        SELECT
                            ksv.id::text,
                            ksv.source_id,
                            ks.source_family_id,
                            ks.title,
                            ks.source_class,
                            ks.tax_domain,
                            ks.authority_level,
                            ksv.publication_state,
                            ksv.source_input_origin,
                            ksv.source_version_form,
                            ksv.effective_from,
                            ksv.effective_to,
                            ksv.tax_year,
                            ksv.supersedes_source_version_id::text,
                            (
                                SELECT CAST(ae.details ->> 'successor_source_version_id' AS text)
                                FROM audit_events AS ae
                                WHERE ae.event_type = 'knowledge_supersession'
                                  AND ae.resource_type = 'knowledge_source_version'
                                  AND ae.resource_id = ksv.id
                                ORDER BY ae.created_at DESC, ae.id DESC
                                LIMIT 1
                            ) AS superseded_by_source_version_id
                        FROM knowledge_source_versions AS ksv
                        JOIN knowledge_sources AS ks
                          ON ks.source_id = ksv.source_id
                        WHERE ksv.source_id = %s
                        ORDER BY
                            ks.source_family_id ASC,
                            ksv.effective_from ASC,
                            COALESCE(ksv.effective_to, DATE '9999-12-31') ASC,
                            ksv.id ASC
                        """,
                        (normalized_source_id,),
                    )
                    version_rows = cursor.fetchall()
        except psycopg.Error as error:
            raise KnowledgeRepositoryError(
                reason_code=KNOWLEDGE_STORAGE_UNAVAILABLE,
                message="Knowledge persistence storage is unavailable.",
            ) from error

        versions = tuple(_row_to_source_version_summary_record(row) for row in version_rows)
        return _row_to_source_detail_record(source_row, versions=versions)

    def get_anchor(
        self,
        *,
        anchor_id: str,
    ) -> KnowledgeAnchorDetailRecord:
        """Return one governed anchor detail record."""

        normalized_anchor_id = _normalize_required_string(value=anchor_id, field_name="anchor_id")
        self._assert_database_configured()
        try:
            with psycopg.connect(self._database_url_or_raise(), connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            ka.anchor_id,
                            ks.source_id,
                            ks.source_family_id,
                            ksv.id::text,
                            ks.title,
                            ks.source_class,
                            ks.tax_domain,
                            ks.authority_level,
                            ksv.publication_state,
                            ka.anchor_title,
                            ka.anchor_path,
                            ka.temporal_scope_from,
                            ka.temporal_scope_to
                        FROM knowledge_anchors AS ka
                        JOIN knowledge_source_versions AS ksv
                          ON ksv.id = ka.source_version_id
                        JOIN knowledge_sources AS ks
                          ON ks.source_id = ksv.source_id
                        WHERE ka.anchor_id = %s
                        """,
                        (normalized_anchor_id,),
                    )
                    anchor_row = cursor.fetchone()
                    if anchor_row is None:
                        raise KnowledgeRepositoryError(
                            reason_code=INVALID_KNOWLEDGE_REQUEST,
                            message="Knowledge anchor identifier is invalid.",
                        )
                    cursor.execute(
                        """
                        SELECT
                            kc.id::text,
                            kc.chunk_index,
                            kc.embedding_vector_ref
                        FROM knowledge_chunks AS kc
                        WHERE kc.anchor_id = %s
                        ORDER BY kc.chunk_index ASC, kc.id ASC
                        """,
                        (normalized_anchor_id,),
                    )
                    chunk_rows = cursor.fetchall()
        except psycopg.Error as error:
            raise KnowledgeRepositoryError(
                reason_code=KNOWLEDGE_STORAGE_UNAVAILABLE,
                message="Knowledge persistence storage is unavailable.",
            ) from error

        chunk_summaries = tuple(_row_to_chunk_summary_record(row) for row in chunk_rows)
        return _row_to_anchor_detail_record(anchor_row, chunks=chunk_summaries)

    def correct_ingestion_metadata(
        self,
        *,
        ingestion_job_id: str,
        corrected_by: str,
        review_notes: Sequence[Mapping[str, object]],
        publication_payload_updates: Mapping[str, object],
    ) -> KnowledgeIngestionDetailRecord:
        """Apply narrow pre-publication metadata corrections in governed states."""

        corrected_by_uuid = _parse_uuid_string(corrected_by, field_name="corrected_by")
        _validate_review_notes(review_notes)
        updates = _coerce_object_mapping(
            publication_payload_updates,
            reason_code=INVALID_KNOWLEDGE_REQUEST,
            message="Knowledge metadata correction payload is invalid.",
        )
        invalid_fields = sorted(
            key for key in updates if key not in ALLOWED_PUBLICATION_METADATA_CORRECTION_FIELDS
        )
        if invalid_fields:
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_LINEAGE,
                message="Knowledge metadata correction contains immutable lineage fields.",
            )
        self._assert_database_configured()
        try:
            with psycopg.connect(self._database_url_or_raise(), connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    self._ensure_user_row(cursor=cursor, user_id=corrected_by_uuid)
                    stored = self._load_ingestion_record_for_update(
                        cursor=cursor,
                        ingestion_job_id=ingestion_job_id,
                    )
                    if stored.ingestion_state not in METADATA_CORRECTION_EDITABLE_STATES:
                        raise KnowledgeRepositoryError(
                            reason_code=INVALID_PUBLICATION_STATE_TRANSITION,
                            message=(
                                "Knowledge metadata correction is allowed only for "
                                "editable unpublished review-stage material."
                            ),
                        )
                    merged_proposed = deepcopy(stored.proposed_source_record)
                    publication_payload = _extract_publication_payload(merged_proposed)
                    publication_payload.update(updates)
                    normalized_publication_payload = _normalize_publication_payload(
                        publication_payload
                    )
                    merged_proposed["publication_payload"] = normalized_publication_payload
                    merged_proposed["last_corrected_by"] = str(corrected_by_uuid)
                    merged_proposed["last_corrected_at"] = datetime.now(UTC).isoformat()
                    cursor.execute(
                        """
                        UPDATE knowledge_ingestion_jobs
                        SET proposed_source_record = %s::jsonb,
                            review_notes = %s::jsonb
                        WHERE id = %s
                        """,
                        (
                            json.dumps(merged_proposed, sort_keys=True),
                            json.dumps(list(review_notes), sort_keys=True),
                            UUID(ingestion_job_id),
                        ),
                    )
                    self._record_audit_event(
                        cursor=cursor,
                        user_id=corrected_by_uuid,
                        event_type="knowledge_metadata_correction",
                        resource_type="knowledge_ingestion_job",
                        resource_id=_parse_uuid_string(
                            ingestion_job_id,
                            field_name="ingestion_job_id",
                        ),
                        correlation_id=f"knowledge-metadata-correction-{ingestion_job_id}",
                        details={
                            "ingestion_job_id": ingestion_job_id,
                            "updated_fields": sorted(updates),
                        },
                    )
                connection.commit()
        except psycopg.Error as error:
            raise KnowledgeRepositoryError(
                reason_code=KNOWLEDGE_STORAGE_UNAVAILABLE,
                message="Knowledge persistence storage is unavailable.",
            ) from error
        return self.get_ingestion_job(ingestion_job_id=ingestion_job_id)

    def _fetch_candidates(
        self,
        *,
        source_type: str | None,
        tax_domain: str | None,
        effective_date: date | None,
    ) -> tuple[KnowledgeSearchRecord, ...]:
        applicable_date = date.today() if effective_date is None else effective_date
        self._assert_database_configured()
        try:
            with psycopg.connect(self._database_url_or_raise(), connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            ks.source_id,
                            ks.title,
                            ks.canonical_url,
                            ks.source_class,
                            ks.tax_domain,
                            ks.authority_level,
                            ksv.effective_from,
                            ksv.effective_to,
                            ksv.tax_year,
                            ka.anchor_id,
                            ka.anchor_text
                        FROM knowledge_sources AS ks
                        JOIN knowledge_source_versions AS ksv
                          ON ksv.source_id = ks.source_id
                        JOIN knowledge_anchors AS ka
                          ON ka.source_version_id = ksv.id
                        WHERE ksv.publication_state = ANY(%s::text[])
                          AND ksv.source_input_origin = ANY(%s::text[])
                          AND ksv.publication_event_id IS NOT NULL
                          AND char_length(btrim(ksv.source_input_ref)) > 0
                          AND (%s::text IS NULL OR ks.source_class = %s::text)
                          AND (%s::text IS NULL OR ks.tax_domain = %s::text)
                          AND (
                                ksv.effective_from <= %s::date
                                AND (
                                    ksv.effective_to IS NULL
                                    OR %s::date <= ksv.effective_to
                                )
                          )
                          AND (
                                ksv.publication_state <> 'superseded'
                                OR NOT EXISTS (
                                    SELECT 1
                                    FROM knowledge_source_versions AS successor
                                    WHERE successor.source_id = ksv.source_id
                                      AND successor.id <> ksv.id
                                      AND successor.publication_state = ANY(%s::text[])
                                      AND successor.source_input_origin = ANY(%s::text[])
                                      AND successor.publication_event_id IS NOT NULL
                                      AND char_length(btrim(successor.source_input_ref)) > 0
                                      AND successor.effective_from <= %s::date
                                      AND (
                                            successor.effective_to IS NULL
                                            OR %s::date <= successor.effective_to
                                      )
                                      AND successor.effective_from > ksv.effective_from
                                )
                          )
                        ORDER BY ks.source_id ASC, ka.anchor_id ASC
                        """,
                        (
                            list(SEARCHABLE_PUBLICATION_STATES),
                            ["official_source_upload", "official_source_url"],
                            source_type,
                            source_type,
                            tax_domain,
                            tax_domain,
                            applicable_date,
                            applicable_date,
                            list(SEARCHABLE_PUBLICATION_STATES),
                            ["official_source_upload", "official_source_url"],
                            applicable_date,
                            applicable_date,
                        ),
                    )
                    rows = cursor.fetchall()
        except psycopg.Error as error:
            raise KnowledgeRepositoryError(
                reason_code=KNOWLEDGE_STORAGE_UNAVAILABLE,
                message="Knowledge persistence storage is unavailable.",
            ) from error

        return tuple(_row_to_record(row) for row in rows)

    def _fetch_timeline_candidates(
        self,
        *,
        source_type: str | None,
        tax_domain: str,
        start_date: date,
        end_date: date,
    ) -> tuple[KnowledgeTimelineRecord, ...]:
        self._assert_database_configured()
        try:
            with psycopg.connect(self._database_url_or_raise(), connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            ks.source_id,
                            ksv.id::text,
                            ka.anchor_id,
                            ks.title,
                            ks.canonical_url,
                            ks.source_class,
                            ks.authority_level,
                            ks.tax_domain,
                            ksv.effective_from,
                            ksv.effective_to,
                            ksv.publication_state,
                            ka.anchor_text
                        FROM knowledge_sources AS ks
                        JOIN knowledge_source_versions AS ksv
                          ON ksv.source_id = ks.source_id
                        JOIN knowledge_anchors AS ka
                          ON ka.source_version_id = ksv.id
                        WHERE ksv.publication_state = ANY(%s::text[])
                          AND ksv.source_input_origin = ANY(%s::text[])
                          AND ksv.publication_event_id IS NOT NULL
                          AND char_length(btrim(ksv.source_input_ref)) > 0
                          AND (%s::text IS NULL OR ks.source_class = %s::text)
                          AND ks.tax_domain = %s::text
                          AND ksv.effective_from <= %s::date
                          AND (
                                ksv.effective_to IS NULL
                                OR ksv.effective_to >= %s::date
                          )
                        ORDER BY
                            ksv.effective_from ASC,
                            COALESCE(ksv.effective_to, DATE '9999-12-31') ASC,
                            ks.source_id ASC,
                            ksv.id ASC,
                            ka.anchor_id ASC
                        """,
                        (
                            list(SEARCHABLE_PUBLICATION_STATES),
                            [OFFICIAL_SOURCE_UPLOAD, OFFICIAL_SOURCE_URL],
                            source_type,
                            source_type,
                            tax_domain,
                            end_date,
                            start_date,
                        ),
                    )
                    rows = cursor.fetchall()
        except psycopg.Error as error:
            raise KnowledgeRepositoryError(
                reason_code=KNOWLEDGE_STORAGE_UNAVAILABLE,
                message="Knowledge persistence storage is unavailable.",
            ) from error

        return tuple(_row_to_timeline_record(row) for row in rows)

    def _vector_scores_by_anchor_id(
        self,
        *,
        query: str,
        candidates: tuple[KnowledgeSearchRecord, ...],
    ) -> dict[str, float]:
        return self._vector_scores_by_anchor_ids(
            query=query,
            anchor_ids=tuple(record.anchor_id for record in candidates),
        )

    def _vector_scores_by_anchor_ids(
        self,
        *,
        query: str,
        anchor_ids: Sequence[str],
    ) -> dict[str, float]:
        provider = self._embedding_provider
        normalized_anchor_ids = tuple(anchor_id for anchor_id in anchor_ids if anchor_id)
        if provider is None or not normalized_anchor_ids or not query.strip():
            return {}
        stored_embeddings = self._fetch_chunk_embeddings(
            anchor_ids=normalized_anchor_ids,
            embedding_model=provider.model_name,
        )
        if not stored_embeddings:
            return {}
        try:
            query_embeddings = provider.embed_texts((query,))
        except KnowledgeEmbeddingProviderError:
            return {}
        if not query_embeddings:
            return {}
        query_vector = query_embeddings[0]
        scores: dict[str, float] = {}
        for anchor_id, vectors in stored_embeddings.items():
            best_similarity = 0.0
            for vector in vectors:
                similarity = max(0.0, cosine_similarity(query_vector, vector))
                if similarity > best_similarity:
                    best_similarity = similarity
            if best_similarity > 0:
                scores[anchor_id] = round(best_similarity, 12)
        return scores

    def _fetch_chunk_embeddings(
        self,
        *,
        anchor_ids: tuple[str, ...],
        embedding_model: str,
    ) -> dict[str, tuple[tuple[float, ...], ...]]:
        normalized_anchor_ids = tuple(sorted({anchor_id for anchor_id in anchor_ids if anchor_id}))
        if not normalized_anchor_ids:
            return {}
        self._assert_database_configured()
        try:
            with psycopg.connect(self._database_url_or_raise(), connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            kc.anchor_id,
                            kce.embedding_vector_json
                        FROM knowledge_chunk_embeddings AS kce
                        JOIN knowledge_chunks AS kc
                          ON kc.id = kce.chunk_id
                        WHERE kc.anchor_id = ANY(%s::text[])
                          AND kce.embedding_model = %s
                        ORDER BY kc.anchor_id ASC, kc.chunk_index ASC, kce.id ASC
                        """,
                        (list(normalized_anchor_ids), embedding_model),
                    )
                    rows = cursor.fetchall()
        except psycopg.Error:
            return {}
        embeddings_by_anchor: dict[str, list[tuple[float, ...]]] = {}
        for row in rows:
            anchor_id = str(row[0])
            vector = _parse_embedding_vector_json(row[1])
            if not vector:
                continue
            embeddings_by_anchor.setdefault(anchor_id, []).append(vector)
        return {anchor_id: tuple(vectors) for anchor_id, vectors in embeddings_by_anchor.items()}

    def _assert_database_configured(self) -> None:
        if self._database_url is None or not self._database_url.strip():
            raise KnowledgeRepositoryError(
                reason_code=KNOWLEDGE_STORAGE_NOT_CONFIGURED,
                message="Knowledge persistence storage is not configured.",
            )

    def _database_url_or_raise(self) -> str:
        self._assert_database_configured()
        database_url = self._database_url
        if database_url is None or not database_url.strip():
            raise KnowledgeRepositoryError(
                reason_code=KNOWLEDGE_STORAGE_NOT_CONFIGURED,
                message="Knowledge persistence storage is not configured.",
            )
        return database_url

    def _embedding_provider_model_name(self) -> str:
        provider = self._embedding_provider
        if provider is None:
            return "unconfigured"
        return provider.model_name

    def _get_ingestion_record_by_id(
        self,
        *,
        ingestion_job_id: str,
    ) -> _StoredKnowledgeIngestionRecord | None:
        parsed_job_id = _parse_uuid_string(ingestion_job_id, field_name="ingestion_job_id")
        return self._get_first_ingestion_record(
            query="""
                SELECT
                    id::text,
                    document_id::text,
                    requested_by::text,
                    ingestion_state,
                    extracted_metadata,
                    proposed_source_record,
                    review_notes,
                    completed_at
                FROM knowledge_ingestion_jobs
                WHERE id = %s
                ORDER BY created_at ASC, id ASC
                LIMIT 1
            """,
            parameters=(parsed_job_id,),
        )

    def _load_ingestion_record_for_update(
        self,
        *,
        cursor: psycopg.Cursor[tuple[object, ...]],
        ingestion_job_id: str,
    ) -> _StoredKnowledgeIngestionRecord:
        parsed_job_id = _parse_uuid_string(ingestion_job_id, field_name="ingestion_job_id")
        cursor.execute(
            """
            SELECT
                id::text,
                document_id::text,
                requested_by::text,
                ingestion_state,
                extracted_metadata,
                proposed_source_record,
                review_notes,
                completed_at
            FROM knowledge_ingestion_jobs
            WHERE id = %s
            FOR UPDATE
            """,
            (parsed_job_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_REQUEST,
                message="Knowledge ingestion job identifier is invalid.",
            )
        return _StoredKnowledgeIngestionRecord.from_row(row)

    def _assert_reviewable_state(self, ingestion_state: str) -> None:
        if ingestion_state in {
            PUBLISHED_INGESTION_STATE,
            APPROVED_INGESTION_STATE,
            LEGACY_APPROVED_FOR_PUBLICATION_INGESTION_STATE,
        }:
            raise KnowledgeRepositoryError(
                reason_code=INVALID_PUBLICATION_STATE_TRANSITION,
                message="Knowledge ingestion job cannot return to review from its current state.",
            )

    def _get_source_version_record_by_id(
        self,
        *,
        source_version_id: str,
    ) -> _StoredKnowledgeSourceVersionRecord | None:
        parsed_source_version_id = _parse_uuid_string(
            source_version_id,
            field_name="source_version_id",
        )
        self._assert_database_configured()
        try:
            with psycopg.connect(self._database_url_or_raise(), connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT
                            ksv.id::text,
                            ksv.source_id,
                            ks.source_family_id,
                            ksv.publication_state,
                            ksv.source_input_origin,
                            ksv.source_version_form,
                            ksv.effective_from,
                            ksv.effective_to,
                            ksv.tax_year,
                            ksv.supersedes_source_version_id::text,
                            (
                                SELECT CAST(ae.details ->> 'successor_source_version_id' AS text)
                                FROM audit_events AS ae
                                WHERE ae.event_type = 'knowledge_supersession'
                                  AND ae.resource_type = 'knowledge_source_version'
                                  AND ae.resource_id = ksv.id
                                ORDER BY ae.created_at DESC, ae.id DESC
                                LIMIT 1
                            ) AS superseded_by_source_version_id
                        FROM knowledge_source_versions AS ksv
                        JOIN knowledge_sources AS ks
                          ON ks.source_id = ksv.source_id
                        WHERE ksv.id = %s
                        """,
                        (parsed_source_version_id,),
                    )
                    row = cursor.fetchone()
        except psycopg.Error as error:
            raise KnowledgeRepositoryError(
                reason_code=KNOWLEDGE_STORAGE_UNAVAILABLE,
                message="Knowledge persistence storage is unavailable.",
            ) from error
        if row is None:
            return None
        return _StoredKnowledgeSourceVersionRecord.from_row(row)

    def _load_source_version_for_update(
        self,
        *,
        cursor: psycopg.Cursor[tuple[object, ...]],
        source_version_id: str,
    ) -> _StoredKnowledgeSourceVersionRecord:
        loaded_versions = self._load_source_versions_for_update(
            cursor=cursor,
            source_version_ids=(source_version_id,),
        )
        return loaded_versions[source_version_id]

    def _load_source_versions_for_update(
        self,
        *,
        cursor: psycopg.Cursor[tuple[object, ...]],
        source_version_ids: tuple[str, ...],
    ) -> dict[str, _StoredKnowledgeSourceVersionRecord]:
        parsed_ids = tuple(
            _parse_uuid_string(value, field_name="source_version_id")
            for value in sorted(set(source_version_ids))
        )
        cursor.execute(
            """
            SELECT
                ksv.id::text,
                ksv.source_id,
                ks.source_family_id,
                ksv.publication_state,
                ksv.source_input_origin,
                ksv.source_version_form,
                ksv.effective_from,
                ksv.effective_to,
                ksv.tax_year,
                ksv.supersedes_source_version_id::text,
                (
                    SELECT CAST(ae.details ->> 'successor_source_version_id' AS text)
                    FROM audit_events AS ae
                    WHERE ae.event_type = 'knowledge_supersession'
                      AND ae.resource_type = 'knowledge_source_version'
                      AND ae.resource_id = ksv.id
                    ORDER BY ae.created_at DESC, ae.id DESC
                    LIMIT 1
                ) AS superseded_by_source_version_id
            FROM knowledge_source_versions AS ksv
            JOIN knowledge_sources AS ks
              ON ks.source_id = ksv.source_id
            WHERE ksv.id = ANY(%s::uuid[])
            ORDER BY ksv.id ASC
            FOR UPDATE
            """,
            (list(parsed_ids),),
        )
        rows = cursor.fetchall()
        loaded = {
            record.source_version_id: record
            for record in (_StoredKnowledgeSourceVersionRecord.from_row(row) for row in rows)
        }
        for source_version_id in source_version_ids:
            if source_version_id not in loaded:
                raise KnowledgeRepositoryError(
                    reason_code=INVALID_KNOWLEDGE_REQUEST,
                    message="Knowledge source version identifier is invalid.",
                )
        return loaded

    def _assert_official_source_version(
        self,
        *,
        record: _StoredKnowledgeSourceVersionRecord,
    ) -> None:
        if record.source_input_origin not in {OFFICIAL_SOURCE_UPLOAD, OFFICIAL_SOURCE_URL}:
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_LINEAGE,
                message="Knowledge source version requires governed official-source lineage.",
            )

    def _upsert_governed_source(
        self,
        *,
        cursor: psycopg.Cursor[tuple[object, ...]],
        source_id: str,
        source_family_id: str,
        title: str,
        canonical_url: str,
        source_class: str,
        authority_level: str,
        tax_domain: str,
        issuing_authority: str,
        created_by: UUID,
    ) -> None:
        cursor.execute(
            """
            SELECT
                source_family_id,
                title,
                canonical_url,
                source_class,
                authority_level,
                tax_domain,
                issuing_authority
            FROM knowledge_sources
            WHERE source_id = %s
            """,
            (source_id,),
        )
        row = cursor.fetchone()
        if row is None:
            cursor.execute(
                """
                INSERT INTO knowledge_sources (
                    source_id,
                    source_family_id,
                    title,
                    canonical_url,
                    source_class,
                    authority_level,
                    tax_domain,
                    issuing_authority,
                    created_by
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    source_id,
                    source_family_id,
                    title,
                    canonical_url,
                    source_class,
                    authority_level,
                    tax_domain,
                    issuing_authority,
                    created_by,
                ),
            )
            return
        existing = tuple(str(value) for value in row)
        expected = (
            source_family_id,
            title,
            canonical_url,
            source_class,
            authority_level,
            tax_domain,
            issuing_authority,
        )
        if existing != expected:
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_LINEAGE,
                message=(
                    "Knowledge publication source family metadata conflicts "
                    "with an existing governed source."
                ),
            )

    def _create_publication_event(
        self,
        *,
        cursor: psycopg.Cursor[tuple[object, ...]],
        user_id: UUID,
        ingestion_job_id: str,
        source_id: str,
    ) -> UUID:
        return self._record_audit_event(
            cursor=cursor,
            user_id=user_id,
            event_type="knowledge_publication",
            resource_type="knowledge_source",
            resource_id=None,
            correlation_id=f"knowledge-publication-{ingestion_job_id}",
            details={
                "source_id": source_id,
                "ingestion_job_id": ingestion_job_id,
            },
        )

    def _create_source_version_transition_event(
        self,
        *,
        cursor: psycopg.Cursor[tuple[object, ...]],
        user_id: UUID,
        source_version_id: str,
        event_type: str,
        details: dict[str, object],
    ) -> UUID:
        return self._record_audit_event(
            cursor=cursor,
            user_id=user_id,
            event_type=event_type,
            resource_type="knowledge_source_version",
            resource_id=_parse_uuid_string(
                source_version_id,
                field_name="source_version_id",
            ),
            correlation_id=f"{event_type}-{source_version_id}",
            details=details,
        )

    def _record_audit_event(
        self,
        *,
        cursor: psycopg.Cursor[tuple[object, ...]],
        user_id: UUID,
        event_type: str,
        resource_type: str,
        resource_id: UUID | None,
        correlation_id: str,
        details: Mapping[str, object],
        role_at_time: str = "Administrator",
        retention_policy_code: str = KNOWLEDGE_RETENTION_POLICY_CODE,
        retention_days: int = KNOWLEDGE_RETENTION_DAYS,
    ) -> UUID:
        event_id = uuid4()
        self._ensure_user_row(cursor=cursor, user_id=user_id)
        previous_event_hash = self._load_latest_audit_event_hash(
            cursor=cursor,
            user_id=user_id,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        cursor.execute(
            """
            INSERT INTO audit_events (
                id,
                user_id,
                role_at_time,
                event_type,
                resource_type,
                resource_id,
                correlation_id,
                previous_event_hash,
                details,
                retention_expires_at,
                retention_policy_code,
                retention_days
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s
            )
            """,
            (
                event_id,
                user_id,
                role_at_time,
                event_type,
                resource_type,
                resource_id,
                correlation_id,
                previous_event_hash,
                json.dumps(dict(details), sort_keys=True),
                datetime.now(UTC) + timedelta(days=retention_days),
                retention_policy_code,
                retention_days,
            ),
        )
        return event_id

    def _record_best_effort_read_audit_event(
        self,
        *,
        event_type: str,
        correlation_id: str,
        details: Mapping[str, object],
    ) -> None:
        try:
            with psycopg.connect(self._database_url_or_raise(), connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    self._record_audit_event(
                        cursor=cursor,
                        user_id=KNOWLEDGE_READ_AUDIT_USER_ID,
                        event_type=event_type,
                        resource_type="knowledge_query",
                        resource_id=None,
                        correlation_id=correlation_id,
                        details=details,
                        retention_policy_code=KNOWLEDGE_READ_RETENTION_POLICY_CODE,
                    )
                connection.commit()
        except (KnowledgeRepositoryError, psycopg.Error):
            return

    def _record_best_effort_actor_audit_event(
        self,
        *,
        user_id: UUID,
        event_type: str,
        correlation_id: str,
        details: Mapping[str, object],
    ) -> None:
        try:
            with psycopg.connect(self._database_url_or_raise(), connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    self._record_audit_event(
                        cursor=cursor,
                        user_id=user_id,
                        event_type=event_type,
                        resource_type="knowledge_bulk_operation",
                        resource_id=None,
                        correlation_id=correlation_id,
                        details=details,
                    )
                connection.commit()
        except (KnowledgeRepositoryError, psycopg.Error):
            return

    def _load_latest_audit_event_hash(
        self,
        *,
        cursor: psycopg.Cursor[tuple[object, ...]],
        user_id: UUID,
        resource_type: str,
        resource_id: UUID | None,
    ) -> str | None:
        cursor.execute(
            """
            SELECT event_hash
            FROM audit_events
            WHERE user_id = %s
              AND resource_type = %s
              AND resource_id IS NOT DISTINCT FROM %s
            ORDER BY event_timestamp DESC, created_at DESC, id DESC
            LIMIT 1
            """,
            (user_id, resource_type, resource_id),
        )
        row = cursor.fetchone()
        if row is None or row[0] is None:
            return None
        return str(row[0])

    def _prepare_publication_anchors(
        self,
        *,
        anchors: tuple[dict[str, object], ...],
    ) -> tuple[_PreparedKnowledgeAnchor, ...]:
        prepared_anchors: list[_PreparedKnowledgeAnchor] = []
        flat_chunks: list[_PreparedKnowledgeChunk] = []
        for anchor in anchors:
            temporal_scope_to_value = anchor.get("temporal_scope_to")
            prepared_chunks: list[_PreparedKnowledgeChunk] = []
            anchor_chunks = cast(tuple[dict[str, object], ...], anchor["chunks"])
            for chunk_index, chunk in enumerate(anchor_chunks):
                chunk_text = str(chunk["chunk_text"])
                prepared_chunk = _PreparedKnowledgeChunk(
                    chunk_id=uuid4(),
                    chunk_index=chunk_index,
                    chunk_text=chunk_text,
                    normalized_chunk_text=chunk_text.lower(),
                    embedding_vector_ref=None,
                    embedding_vector=None,
                )
                prepared_chunks.append(prepared_chunk)
                flat_chunks.append(prepared_chunk)
            prepared_anchors.append(
                _PreparedKnowledgeAnchor(
                    anchor_id=str(anchor["anchor_id"]),
                    anchor_title=str(anchor["anchor_title"]),
                    anchor_path=str(anchor["anchor_path"]),
                    anchor_text=str(anchor["anchor_text"]),
                    normalized_anchor_text=str(anchor["anchor_text"]).lower(),
                    temporal_scope_from=date.fromisoformat(str(anchor["temporal_scope_from"])),
                    temporal_scope_to=(
                        date.fromisoformat(str(temporal_scope_to_value))
                        if temporal_scope_to_value is not None
                        else None
                    ),
                    chunks=tuple(prepared_chunks),
                )
            )
        prepared_by_id = {
            chunk.chunk_id: chunk
            for chunk in self._prepare_chunk_embeddings(chunks=tuple(flat_chunks))
        }
        return tuple(
            _PreparedKnowledgeAnchor(
                anchor_id=anchor.anchor_id,
                anchor_title=anchor.anchor_title,
                anchor_path=anchor.anchor_path,
                anchor_text=anchor.anchor_text,
                normalized_anchor_text=anchor.normalized_anchor_text,
                temporal_scope_from=anchor.temporal_scope_from,
                temporal_scope_to=anchor.temporal_scope_to,
                chunks=tuple(prepared_by_id.get(chunk.chunk_id, chunk) for chunk in anchor.chunks),
            )
            for anchor in prepared_anchors
        )

    def _prepare_chunk_embeddings(
        self,
        *,
        chunks: tuple[_PreparedKnowledgeChunk, ...],
    ) -> tuple[_PreparedKnowledgeChunk, ...]:
        provider = self._embedding_provider
        if provider is None or not chunks:
            return chunks
        try:
            embeddings = provider.embed_texts(tuple(chunk.chunk_text for chunk in chunks))
        except KnowledgeEmbeddingProviderError:
            return chunks
        if len(embeddings) != len(chunks):
            return chunks
        return tuple(
            _PreparedKnowledgeChunk(
                chunk_id=chunk.chunk_id,
                chunk_index=chunk.chunk_index,
                chunk_text=chunk.chunk_text,
                normalized_chunk_text=chunk.normalized_chunk_text,
                embedding_vector_ref=(
                    f"openai-embedding://{provider.model_name}/{chunk.chunk_id}"
                    if embedding
                    else None
                ),
                embedding_vector=embedding if embedding else None,
            )
            for chunk, embedding in zip(chunks, embeddings, strict=True)
        )

    def _insert_publication_anchors_and_chunks(
        self,
        *,
        cursor: psycopg.Cursor[tuple[object, ...]],
        source_version_id: UUID,
        anchors: tuple[_PreparedKnowledgeAnchor, ...],
    ) -> None:
        for anchor in anchors:
            cursor.execute(
                """
                INSERT INTO knowledge_anchors (
                    anchor_id,
                    source_version_id,
                    anchor_title,
                    anchor_path,
                    anchor_text,
                    normalized_anchor_text,
                    temporal_scope_from,
                    temporal_scope_to
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    anchor.anchor_id,
                    source_version_id,
                    anchor.anchor_title,
                    anchor.anchor_path,
                    anchor.anchor_text,
                    anchor.normalized_anchor_text,
                    anchor.temporal_scope_from,
                    anchor.temporal_scope_to,
                ),
            )
            for chunk in anchor.chunks:
                cursor.execute(
                    """
                    INSERT INTO knowledge_chunks (
                        id,
                        anchor_id,
                        chunk_index,
                        chunk_text,
                        normalized_chunk_text,
                        embedding_vector_ref
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        chunk.chunk_id,
                        anchor.anchor_id,
                        chunk.chunk_index,
                        chunk.chunk_text,
                        chunk.normalized_chunk_text,
                        chunk.embedding_vector_ref,
                    ),
                )
                if chunk.embedding_vector is not None:
                    cursor.execute(
                        """
                        INSERT INTO knowledge_chunk_embeddings (
                            id,
                            chunk_id,
                            embedding_model,
                            embedding_dimensions,
                            embedding_vector_json,
                            content_checksum_sha256
                        )
                        VALUES (%s, %s, %s, %s, %s::jsonb, %s)
                        """,
                        (
                            uuid4(),
                            chunk.chunk_id,
                            self._embedding_provider_model_name(),
                            len(chunk.embedding_vector),
                            json.dumps(list(chunk.embedding_vector), separators=(",", ":")),
                            hashlib.sha256(chunk.chunk_text.encode("utf-8")).hexdigest(),
                        ),
                    )

    def _persist_ingestion_job(
        self,
        *,
        requested_by: str,
        idempotency_key: str,
        source_input_origin: str,
        source_input_ref: str,
        payload_checksum_sha256: str,
        source_class: str | None,
        payload_fingerprint: str,
        storage_key: str,
        extracted_metadata: Mapping[str, object],
        proposed_source_record: Mapping[str, object],
        registered_document_id: str | None = None,
    ) -> KnowledgeIngestionRecord:
        self._assert_database_configured()
        requested_by_uuid = _parse_uuid_string(requested_by, field_name="requested_by")

        existing_by_idempotency = self._get_ingestion_by_idempotency_key(
            idempotency_key=idempotency_key
        )
        if existing_by_idempotency is not None:
            existing_fingerprint = str(
                existing_by_idempotency.proposed_source_record.get("payload_fingerprint", "")
            )
            if existing_fingerprint != payload_fingerprint:
                raise KnowledgeRepositoryError(
                    reason_code=KNOWLEDGE_IDEMPOTENCY_CONFLICT,
                    message="Knowledge ingestion idempotency key conflicts with existing payload.",
                )
            return existing_by_idempotency.to_public_record()

        existing_duplicate = self._get_ingestion_by_lineage(
            source_input_origin=source_input_origin,
            source_input_ref=source_input_ref,
            payload_checksum_sha256=payload_checksum_sha256,
        )
        if existing_duplicate is not None:
            existing_source_class = existing_duplicate.proposed_source_record.get("source_class")
            if (
                existing_source_class is not None
                and source_class is not None
                and str(existing_source_class) != source_class
            ):
                raise KnowledgeRepositoryError(
                    reason_code=INVALID_KNOWLEDGE_LINEAGE,
                    message=(
                        "Knowledge ingestion duplicate payload has conflicting "
                        "source classification."
                    ),
                )
            return existing_duplicate.to_public_record()

        document_id = (
            registered_document_id
            if registered_document_id is not None
            else str(
                self._create_document_row(
                    requested_by=requested_by_uuid,
                    storage_key=storage_key,
                )
            )
        )

        try:
            with psycopg.connect(self._database_url_or_raise(), connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    self._ensure_user_row(cursor=cursor, user_id=requested_by_uuid)
                    cursor.execute(
                        """
                        INSERT INTO knowledge_ingestion_jobs (
                            document_id,
                            requested_by,
                            ingestion_state,
                            extracted_metadata,
                            proposed_source_record,
                            review_notes
                        )
                        VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb)
                        RETURNING id::text
                        """,
                        (
                            document_id,
                            requested_by_uuid,
                            INITIAL_INGESTION_STATE,
                            json.dumps(extracted_metadata, sort_keys=True),
                            json.dumps(proposed_source_record, sort_keys=True),
                            "[]",
                        ),
                    )
                    row = cursor.fetchone()
                    if row is not None:
                        self._record_audit_event(
                            cursor=cursor,
                            user_id=requested_by_uuid,
                            event_type=_ingestion_event_type(proposed_source_record),
                            resource_type="knowledge_ingestion_job",
                            resource_id=_parse_uuid_string(
                                str(row[0]),
                                field_name="knowledge_ingestion_jobs.id",
                            ),
                            correlation_id=f"knowledge-ingestion-{idempotency_key}",
                            details={
                                "source_input_origin": source_input_origin,
                                "source_input_ref": source_input_ref,
                                "payload_checksum_sha256": payload_checksum_sha256,
                            },
                        )
                connection.commit()
        except psycopg.Error as error:
            raise KnowledgeRepositoryError(
                reason_code=KNOWLEDGE_STORAGE_UNAVAILABLE,
                message="Knowledge persistence storage is unavailable.",
            ) from error

        if row is None:
            raise KnowledgeRepositoryError(
                reason_code=KNOWLEDGE_STORAGE_UNAVAILABLE,
                message="Knowledge ingestion persistence did not return a job identifier.",
            )
        return KnowledgeIngestionRecord(
            ingestion_job_id=str(row[0]),
            document_id=document_id,
            requested_by=str(requested_by_uuid),
            ingestion_state=INITIAL_INGESTION_STATE,
            source_input_origin=source_input_origin,
            source_input_ref=source_input_ref,
            payload_checksum_sha256=payload_checksum_sha256,
            source_class=source_class,
        )

    def _bulk_ingest_sources(
        self,
        *,
        requested_by: str,
        items: Sequence[Mapping[str, object]],
        ingestion_kind: str,
    ) -> tuple[KnowledgeBulkIngestionItemRecord, ...]:
        _parse_uuid_string(requested_by, field_name="acting_user")
        if not items:
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_REQUEST,
                message="Knowledge request field `items` is invalid.",
            )
        seen_idempotency_keys: dict[str, str] = {}
        cached_results: dict[tuple[str, str], KnowledgeBulkIngestionItemRecord] = {}
        outcomes: list[KnowledgeBulkIngestionItemRecord] = []
        for index, raw_item in enumerate(items):
            try:
                normalized_item = _coerce_object_mapping(
                    raw_item,
                    reason_code=INVALID_KNOWLEDGE_REQUEST,
                    message="Knowledge bulk ingestion item is invalid.",
                )
                idempotency_key = _bulk_required_string(
                    normalized_item,
                    field_name="idempotency_key",
                )
                payload_signature = self._bulk_ingestion_payload_signature(
                    requested_by=requested_by,
                    item=normalized_item,
                    ingestion_kind=ingestion_kind,
                )
                previous_signature = seen_idempotency_keys.get(idempotency_key)
                if previous_signature is not None and previous_signature != payload_signature:
                    outcomes.append(
                        _bulk_ingestion_error(
                            index=index,
                            idempotency_key=idempotency_key,
                            error=KnowledgeRepositoryError(
                                reason_code=KNOWLEDGE_IDEMPOTENCY_CONFLICT,
                                message=(
                                    "Knowledge ingestion idempotency key conflicts "
                                    "with existing payload."
                                ),
                            ),
                        )
                    )
                    continue
                seen_idempotency_keys[idempotency_key] = payload_signature
                cached_result = cached_results.get((idempotency_key, payload_signature))
                if cached_result is not None:
                    outcomes.append(
                        KnowledgeBulkIngestionItemRecord(
                            index=index,
                            idempotency_key=idempotency_key,
                            status=cached_result.status,
                            outcome=cached_result.outcome,
                            ingestion_job_id=cached_result.ingestion_job_id,
                            error_code=cached_result.error_code,
                            reason=cached_result.reason,
                        )
                    )
                    continue
                record = self._bulk_ingest_one_source(
                    requested_by=requested_by,
                    item=normalized_item,
                    ingestion_kind=ingestion_kind,
                )
                outcome = KnowledgeBulkIngestionItemRecord(
                    index=index,
                    idempotency_key=idempotency_key,
                    status="ok",
                    outcome="accepted",
                    ingestion_job_id=record.ingestion_job_id,
                    error_code=None,
                    reason=None,
                )
                cached_results[(idempotency_key, payload_signature)] = outcome
                outcomes.append(outcome)
            except KnowledgeRepositoryError as error:
                outcomes.append(
                    _bulk_ingestion_error(
                        index=index,
                        idempotency_key=_bulk_item_idempotency_key(raw_item),
                        error=error,
                    )
                )
        self._record_best_effort_actor_audit_event(
            user_id=_parse_uuid_string(requested_by, field_name="acting_user"),
            event_type=(
                "knowledge_bulk_ingestion_file"
                if ingestion_kind == "file"
                else "knowledge_bulk_ingestion_url"
            ),
            correlation_id=(
                f"knowledge-bulk-{ingestion_kind}-"
                + hashlib.sha256(
                    json.dumps(
                        {
                            "requested_by": requested_by,
                            "items": [dict(item) for item in items],
                        },
                        sort_keys=True,
                        default=str,
                    ).encode("utf-8")
                ).hexdigest()[:16]
            ),
            details={
                "requested_by": requested_by,
                "ingestion_kind": ingestion_kind,
                "total": len(outcomes),
                "accepted": sum(1 for outcome in outcomes if outcome.status == "ok"),
            },
        )
        return tuple(outcomes)

    def _bulk_ingestion_payload_signature(
        self,
        *,
        requested_by: str,
        item: Mapping[str, object],
        ingestion_kind: str,
    ) -> str:
        if ingestion_kind == "file":
            payload = {
                "requested_by": requested_by,
                "idempotency_key": _bulk_required_string(item, field_name="idempotency_key"),
                "filename": _bulk_required_string(item, field_name="filename"),
                "mime_type": _bulk_required_string(item, field_name="mime_type"),
                "file_content_base64": _bulk_required_string(
                    item,
                    field_name="file_content_base64",
                ),
                "source_input_origin": _bulk_optional_string_field(
                    item,
                    field_name="source_input_origin",
                ),
                "source_class": _bulk_optional_string_field(item, field_name="source_class"),
                "legacy_import_acknowledged": _bulk_required_true_boolean(
                    item,
                    field_name="legacy_import_acknowledged",
                ),
            }
            return _payload_fingerprint(payload)
        if ingestion_kind == "document":
            payload = {
                "requested_by": requested_by,
                "idempotency_key": _bulk_required_string(item, field_name="idempotency_key"),
                "document_id": _bulk_required_uuid_string(item, field_name="document_id"),
                "storage_key": _bulk_required_string(item, field_name="storage_key"),
                "mime_type": _bulk_required_string(item, field_name="mime_type"),
                "payload_checksum_sha256": _bulk_required_string(
                    item,
                    field_name="payload_checksum_sha256",
                ),
                "source_document_system": _bulk_required_string(
                    item,
                    field_name="source_document_system",
                ),
                "source_input_origin": _bulk_optional_string_field(
                    item,
                    field_name="source_input_origin",
                ),
                "source_class": _bulk_optional_string_field(item, field_name="source_class"),
            }
            return _payload_fingerprint(payload)
        payload = {
            "requested_by": requested_by,
            "idempotency_key": _bulk_required_string(item, field_name="idempotency_key"),
            "url": _bulk_required_string(item, field_name="url"),
            "source_input_origin": _bulk_optional_string_field(
                item,
                field_name="source_input_origin",
            ),
            "source_class": _bulk_optional_string_field(item, field_name="source_class"),
        }
        return _payload_fingerprint(payload)

    def _bulk_ingest_one_source(
        self,
        *,
        requested_by: str,
        item: Mapping[str, object],
        ingestion_kind: str,
    ) -> KnowledgeIngestionRecord:
        if ingestion_kind == "file":
            return self.ingest_file_source(
                requested_by=requested_by,
                idempotency_key=_bulk_required_string(item, field_name="idempotency_key"),
                filename=_bulk_required_string(item, field_name="filename"),
                mime_type=_bulk_required_string(item, field_name="mime_type"),
                file_content_base64=_bulk_required_string(item, field_name="file_content_base64"),
                source_input_origin=_bulk_optional_string_field(
                    item,
                    field_name="source_input_origin",
                ),
                source_class=_bulk_optional_string_field(item, field_name="source_class"),
                legacy_import_acknowledged=_bulk_required_true_boolean(
                    item,
                    field_name="legacy_import_acknowledged",
                ),
            )
        if ingestion_kind == "document":
            return self.ingest_registered_document_source(
                requested_by=requested_by,
                idempotency_key=_bulk_required_string(item, field_name="idempotency_key"),
                document_id=_bulk_required_uuid_string(item, field_name="document_id"),
                storage_key=_bulk_required_string(item, field_name="storage_key"),
                mime_type=_bulk_required_string(item, field_name="mime_type"),
                payload_checksum_sha256=_bulk_required_string(
                    item,
                    field_name="payload_checksum_sha256",
                ),
                source_document_system=_bulk_required_string(
                    item,
                    field_name="source_document_system",
                ),
                source_input_origin=_bulk_optional_string_field(
                    item,
                    field_name="source_input_origin",
                ),
                source_class=_bulk_optional_string_field(item, field_name="source_class"),
            )
        return self.ingest_url_source(
            requested_by=requested_by,
            idempotency_key=_bulk_required_string(item, field_name="idempotency_key"),
            url=_bulk_required_string(item, field_name="url"),
            source_input_origin=_bulk_optional_string_field(
                item,
                field_name="source_input_origin",
            ),
            source_class=_bulk_optional_string_field(item, field_name="source_class"),
        )

    def _create_document_row(
        self,
        *,
        requested_by: UUID,
        storage_key: str,
    ) -> UUID:
        try:
            with psycopg.connect(self._database_url_or_raise(), connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    self._ensure_user_row(cursor=cursor, user_id=requested_by)
                    cursor.execute(
                        """
                        INSERT INTO documents (
                            user_id,
                            storage_key,
                            state
                        )
                        VALUES (%s, %s, %s)
                        RETURNING id
                        """,
                        (requested_by, storage_key, "uploaded"),
                    )
                    row = cursor.fetchone()
                connection.commit()
        except psycopg.Error as error:
            raise KnowledgeRepositoryError(
                reason_code=KNOWLEDGE_STORAGE_UNAVAILABLE,
                message="Knowledge persistence storage is unavailable.",
            ) from error
        if row is None:
            raise KnowledgeRepositoryError(
                reason_code=KNOWLEDGE_STORAGE_UNAVAILABLE,
                message=(
                    "Knowledge ingestion document persistence did not return a document identifier."
                ),
            )
        return cast(UUID, row[0])

    def _load_registered_document(
        self,
        *,
        document_id: str,
    ) -> _RegisteredDocumentRow:
        try:
            with psycopg.connect(self._database_url_or_raise(), connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT id::text, storage_key, state
                        FROM documents
                        WHERE id = %s
                        """,
                        (UUID(document_id),),
                    )
                    row = cursor.fetchone()
        except psycopg.Error as error:
            raise KnowledgeRepositoryError(
                reason_code=KNOWLEDGE_STORAGE_UNAVAILABLE,
                message="Knowledge persistence storage is unavailable.",
            ) from error
        if row is None:
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_LINEAGE,
                message="Knowledge ingestion document provenance is invalid.",
            )
        record = _RegisteredDocumentRow.from_row(row)
        if record.state == "purged":
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_LINEAGE,
                message="Knowledge ingestion document provenance is unavailable.",
            )
        _assert_local_storage_key(record.storage_key)
        return record

    def _verify_publishable_ingestion_lineage(
        self,
        stored: _StoredKnowledgeIngestionRecord,
    ) -> str | None:
        if stored.source_input_origin == OFFICIAL_SOURCE_UPLOAD:
            return self._verify_publishable_upload_lineage(stored)
        self._verify_publishable_url_lineage(stored)
        return None

    def _verify_publishable_upload_lineage(
        self,
        stored: _StoredKnowledgeIngestionRecord,
    ) -> str:
        parsed = _parse_upload_source_input_ref(stored.source_input_ref)
        if parsed.kind == "document":
            if parsed.document_id != stored.document_id:
                raise KnowledgeRepositoryError(
                    reason_code=INVALID_KNOWLEDGE_LINEAGE,
                    message="Knowledge publication document lineage is inconsistent.",
                )
            document = self._load_registered_document(document_id=stored.document_id)
            expected_storage_key = _extract_storage_key_for_publish_verification(
                stored.extracted_metadata
            )
            if document.storage_key != expected_storage_key:
                raise KnowledgeRepositoryError(
                    reason_code=INVALID_KNOWLEDGE_LINEAGE,
                    message="Knowledge publication document storage lineage is invalid.",
                )
            return stored.document_id
        if parsed.kind == "legacy_import":
            if parsed.payload_checksum_sha256 != stored.payload_checksum_sha256:
                raise KnowledgeRepositoryError(
                    reason_code=INVALID_KNOWLEDGE_LINEAGE,
                    message="Knowledge publication legacy import lineage is invalid.",
                )
            document = self._load_registered_document(document_id=stored.document_id)
            expected_storage_key = _legacy_import_storage_key(stored.extracted_metadata)
            if document.storage_key != expected_storage_key:
                raise KnowledgeRepositoryError(
                    reason_code=INVALID_KNOWLEDGE_LINEAGE,
                    message="Knowledge publication legacy import storage lineage is invalid.",
                )
            return stored.document_id
        raise KnowledgeRepositoryError(
            reason_code=INVALID_KNOWLEDGE_LINEAGE,
            message="Knowledge publication source input reference is invalid.",
        )

    def _verify_publishable_url_lineage(
        self,
        stored: _StoredKnowledgeIngestionRecord,
    ) -> None:
        normalized_url = _extract_normalized_url_for_publish_verification(stored.extracted_metadata)
        expected_source_input_ref = _build_url_source_input_ref(normalized_url)
        expected_payload_checksum = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()
        if stored.source_input_ref != expected_source_input_ref:
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_LINEAGE,
                message="Knowledge publication URL lineage is invalid.",
            )
        if stored.payload_checksum_sha256 != expected_payload_checksum:
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_LINEAGE,
                message="Knowledge publication URL checksum lineage is invalid.",
            )

    def _get_ingestion_by_idempotency_key(
        self,
        *,
        idempotency_key: str,
    ) -> _StoredKnowledgeIngestionRecord | None:
        return self._get_first_ingestion_record(
            query="""
                SELECT
                    id::text,
                    document_id::text,
                    requested_by::text,
                    ingestion_state,
                    extracted_metadata,
                    proposed_source_record,
                    review_notes,
                    completed_at
                FROM knowledge_ingestion_jobs
                WHERE proposed_source_record ->> 'idempotency_key' = %s
                ORDER BY created_at ASC, id ASC
                LIMIT 1
            """,
            parameters=(idempotency_key,),
        )

    def _get_ingestion_by_lineage(
        self,
        *,
        source_input_origin: str,
        source_input_ref: str,
        payload_checksum_sha256: str,
    ) -> _StoredKnowledgeIngestionRecord | None:
        return self._get_first_ingestion_record(
            query="""
                SELECT
                    id::text,
                    document_id::text,
                    requested_by::text,
                    ingestion_state,
                    extracted_metadata,
                    proposed_source_record,
                    review_notes,
                    completed_at
                FROM knowledge_ingestion_jobs
                WHERE proposed_source_record ->> 'source_input_origin' = %s
                  AND proposed_source_record ->> 'source_input_ref' = %s
                  AND proposed_source_record ->> 'payload_checksum_sha256' = %s
                ORDER BY created_at ASC, id ASC
                LIMIT 1
            """,
            parameters=(
                source_input_origin,
                source_input_ref,
                payload_checksum_sha256,
            ),
        )

    def _get_first_ingestion_record(
        self,
        *,
        query: LiteralString,
        parameters: tuple[object, ...],
    ) -> _StoredKnowledgeIngestionRecord | None:
        try:
            with psycopg.connect(self._database_url_or_raise(), connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(query, parameters)
                    row = cursor.fetchone()
        except psycopg.Error as error:
            raise KnowledgeRepositoryError(
                reason_code=KNOWLEDGE_STORAGE_UNAVAILABLE,
                message="Knowledge persistence storage is unavailable.",
            ) from error
        if row is None:
            return None
        return _StoredKnowledgeIngestionRecord.from_row(row)

    def _ensure_user_row(
        self,
        *,
        cursor: psycopg.Cursor[tuple[object, ...]],
        user_id: UUID,
    ) -> None:
        suffix = str(user_id)
        cursor.execute(
            """
            INSERT INTO users (
                id,
                phone_number_encrypted,
                email_encrypted,
                role,
                subscription_tier
            )
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (
                user_id,
                f"knowledge-ingestion-phone-{suffix}",
                f"knowledge-ingestion-{suffix}@kodi.local",
                "Administrator",
                "standard",
            ),
        )


_default_knowledge_repository: KnowledgeRepository | None = None


@dataclass(frozen=True)
class _RegisteredDocumentRow:
    document_id: str
    storage_key: str
    state: str

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> _RegisteredDocumentRow:
        return cls(
            document_id=str(row[0]),
            storage_key=str(row[1]),
            state=str(row[2]),
        )


@dataclass(frozen=True)
class _ParsedUploadSourceInputRef:
    kind: str
    document_system: str | None
    document_id: str | None
    payload_checksum_sha256: str | None


@dataclass(frozen=True)
class _StoredKnowledgeIngestionRecord:
    ingestion_job_id: str
    document_id: str
    requested_by: str
    ingestion_state: str
    extracted_metadata: dict[str, object]
    proposed_source_record: dict[str, object]
    review_notes: tuple[dict[str, object], ...]
    completed_at: str | None

    @property
    def source_input_origin(self) -> str:
        return str(self.proposed_source_record["source_input_origin"])

    @property
    def source_input_ref(self) -> str:
        return str(self.proposed_source_record["source_input_ref"])

    @property
    def payload_checksum_sha256(self) -> str:
        return str(self.proposed_source_record["payload_checksum_sha256"])

    @property
    def source_class(self) -> str | None:
        return cast(str | None, self.proposed_source_record.get("source_class"))

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> _StoredKnowledgeIngestionRecord:
        return cls(
            ingestion_job_id=str(row[0]),
            document_id=str(row[1]),
            requested_by=str(row[2]),
            ingestion_state=_canonicalize_ingestion_state(str(row[3])),
            extracted_metadata=_parse_json_object(row[4]),
            proposed_source_record=_parse_json_object(row[5]),
            review_notes=_parse_json_array_of_objects(row[6]),
            completed_at=str(row[7]) if row[7] is not None else None,
        )

    def to_public_record(self) -> KnowledgeIngestionRecord:
        return KnowledgeIngestionRecord(
            ingestion_job_id=self.ingestion_job_id,
            document_id=self.document_id,
            requested_by=self.requested_by,
            ingestion_state=self.ingestion_state,
            source_input_origin=self.source_input_origin,
            source_input_ref=self.source_input_ref,
            payload_checksum_sha256=self.payload_checksum_sha256,
            source_class=self.source_class,
        )

    def to_detail_record(self) -> KnowledgeIngestionDetailRecord:
        return KnowledgeIngestionDetailRecord(
            ingestion_job_id=self.ingestion_job_id,
            document_id=self.document_id,
            requested_by=self.requested_by,
            ingestion_state=self.ingestion_state,
            source_input_origin=self.source_input_origin,
            source_input_ref=self.source_input_ref,
            payload_checksum_sha256=self.payload_checksum_sha256,
            source_class=self.source_class,
            extracted_metadata=deepcopy(self.extracted_metadata),
            proposed_source_record=deepcopy(self.proposed_source_record),
            review_notes=tuple(deepcopy(item) for item in self.review_notes),
            completed_at=self.completed_at,
        )


@dataclass(frozen=True)
class _StoredKnowledgeSourceVersionRecord:
    source_version_id: str
    source_id: str
    source_family_id: str
    publication_state: str
    source_input_origin: str
    source_version_form: str
    effective_from: date
    effective_to: date | None
    tax_year: int | None
    supersedes_source_version_id: str | None
    superseded_by_source_version_id: str | None

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> _StoredKnowledgeSourceVersionRecord:
        effective_to_value = row[7]
        tax_year_value = row[8]
        return cls(
            source_version_id=str(row[0]),
            source_id=str(row[1]),
            source_family_id=str(row[2]),
            publication_state=str(row[3]),
            source_input_origin=str(row[4]),
            source_version_form=str(row[5]),
            effective_from=cast(date, row[6]),
            effective_to=_coerce_optional_date(
                effective_to_value,
                field_name="knowledge_source_versions.effective_to",
            ),
            tax_year=_coerce_optional_int(
                tax_year_value,
                field_name="knowledge_source_versions.tax_year",
            ),
            supersedes_source_version_id=str(row[9]) if row[9] is not None else None,
            superseded_by_source_version_id=str(row[10]) if row[10] is not None else None,
        )

    def to_lifecycle_record(self) -> KnowledgeSourceVersionLifecycleRecord:
        return KnowledgeSourceVersionLifecycleRecord(
            source_version_id=self.source_version_id,
            source_id=self.source_id,
            source_family_id=self.source_family_id,
            publication_state=self.publication_state,
            source_input_origin=self.source_input_origin,
            source_version_form=self.source_version_form,
            effective_from=self.effective_from.isoformat(),
            effective_to=self.effective_to.isoformat() if self.effective_to is not None else None,
            tax_year=self.tax_year,
            supersedes_source_version_id=self.supersedes_source_version_id,
            superseded_by_source_version_id=self.superseded_by_source_version_id,
        )


def get_default_knowledge_repository() -> KnowledgeRepository:
    """Return singleton default knowledge repository."""

    global _default_knowledge_repository
    if _default_knowledge_repository is None:
        _default_knowledge_repository = KnowledgeRepository()
    return _default_knowledge_repository


def _load_database_url() -> str | None:
    env_value = os.getenv(DATABASE_URL_ENV_VAR)
    if env_value is not None and env_value.strip():
        return env_value.strip()

    env_values = _read_env_file_values()
    raw_database_url = env_values.get(DATABASE_URL_ENV_VAR)
    if raw_database_url:
        return raw_database_url

    db_user = env_values.get(DB_USER_ENV_VAR)
    db_password = env_values.get(DB_PASSWORD_ENV_VAR)
    db_name = env_values.get(DB_NAME_ENV_VAR, DEFAULT_DB_NAME)
    if not db_user or not db_password:
        return None
    return f"postgresql://{db_user}:{db_password}@localhost:54329/{db_name}"


def _read_env_file_values() -> dict[str, str]:
    env_path = Path(".env")
    if not env_path.exists():
        return {}
    try:
        raw_lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    values: dict[str, str] = {}
    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", maxsplit=1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def _row_to_ingestion_summary_record(
    row: tuple[object, ...],
) -> KnowledgeIngestionSummaryRecord:
    return KnowledgeIngestionSummaryRecord(
        ingestion_job_id=str(row[0]),
        document_id=str(row[1]),
        requested_by=str(row[2]),
        ingestion_state=_canonicalize_ingestion_state(str(row[3])),
        source_input_origin=str(row[4]),
        source_input_ref=str(row[5]),
        payload_checksum_sha256=str(row[6]),
        source_class=str(row[7]) if row[7] is not None else None,
        created_at=str(row[8]),
        completed_at=str(row[9]) if row[9] is not None else None,
    )


def _row_to_source_version_summary_record(
    row: tuple[object, ...],
) -> KnowledgeSourceVersionSummaryRecord:
    effective_to_value = row[11]
    tax_year_value = row[12]
    effective_to = _coerce_optional_date(
        effective_to_value,
        field_name="knowledge_source_versions.effective_to",
    )
    return KnowledgeSourceVersionSummaryRecord(
        source_version_id=str(row[0]),
        source_id=str(row[1]),
        source_family_id=str(row[2]),
        title=str(row[3]),
        source_class=str(row[4]),
        tax_domain=str(row[5]),
        authority_level=str(row[6]),
        publication_state=str(row[7]),
        source_input_origin=str(row[8]),
        source_version_form=str(row[9]),
        effective_from=cast(date, row[10]).isoformat(),
        effective_to=effective_to.isoformat() if effective_to is not None else None,
        tax_year=_coerce_optional_int(
            tax_year_value,
            field_name="knowledge_source_versions.tax_year",
        ),
        supersedes_source_version_id=str(row[13]) if row[13] is not None else None,
        superseded_by_source_version_id=str(row[14]) if row[14] is not None else None,
    )


def _row_to_source_summary_record(
    row: tuple[object, ...],
) -> KnowledgeSourceSummaryRecord:
    return KnowledgeSourceSummaryRecord(
        source_id=str(row[0]),
        source_family_id=str(row[1]),
        title=str(row[2]),
        canonical_url=str(row[3]),
        source_class=str(row[4]),
        tax_domain=str(row[5]),
        authority_level=str(row[6]),
        issuing_authority=str(row[7]),
        version_count=_coerce_required_int(row[8], field_name="knowledge_sources.version_count"),
        anchor_count=_coerce_required_int(row[9], field_name="knowledge_sources.anchor_count"),
        created_at=str(row[10]),
        retired_at=str(row[11]) if row[11] is not None else None,
    )


def _row_to_source_detail_record(
    row: tuple[object, ...],
    *,
    versions: tuple[KnowledgeSourceVersionSummaryRecord, ...],
) -> KnowledgeSourceDetailRecord:
    has_document_lineage = _coerce_required_bool(
        row[13],
        field_name="knowledge_sources.has_document_lineage",
    )
    has_purged_document_lineage = _coerce_required_bool(
        row[14],
        field_name="knowledge_sources.has_purged_document_lineage",
    )
    has_historical_compatibility_lineage = _coerce_required_bool(
        row[15],
        field_name="knowledge_sources.has_historical_compatibility_lineage",
    )
    has_legacy_import_lineage = _coerce_required_bool(
        row[16],
        field_name="knowledge_sources.has_legacy_import_lineage",
    )
    has_url_lineage = _coerce_required_bool(
        row[17],
        field_name="knowledge_sources.has_url_lineage",
    )
    return KnowledgeSourceDetailRecord(
        source_id=str(row[0]),
        source_family_id=str(row[1]),
        title=str(row[2]),
        canonical_url=str(row[3]),
        source_class=str(row[4]),
        tax_domain=str(row[5]),
        authority_level=str(row[6]),
        issuing_authority=str(row[7]),
        version_count=_coerce_required_int(row[8], field_name="knowledge_sources.version_count"),
        anchor_count=_coerce_required_int(row[9], field_name="knowledge_sources.anchor_count"),
        chunk_count=_coerce_required_int(row[10], field_name="knowledge_sources.chunk_count"),
        created_at=str(row[11]),
        retired_at=str(row[12]) if row[12] is not None else None,
        versions=versions,
        retention_summary={
            "lineage_preserved": not has_purged_document_lineage,
            "has_document_lineage": has_document_lineage,
            "has_purged_document_lineage": has_purged_document_lineage,
            "has_historical_compatibility_lineage": has_historical_compatibility_lineage,
            "has_legacy_import_lineage": has_legacy_import_lineage,
            "has_url_lineage": has_url_lineage,
            "retention_policy_code": KNOWLEDGE_RETENTION_POLICY_CODE,
            "purge_supported": False,
        },
    )


def _row_to_chunk_summary_record(
    row: tuple[object, ...],
) -> KnowledgeChunkSummaryRecord:
    return KnowledgeChunkSummaryRecord(
        chunk_id=str(row[0]),
        chunk_index=_coerce_required_int(row[1], field_name="knowledge_chunks.chunk_index"),
        has_embedding=str(row[2]).strip() != "" if row[2] is not None else False,
    )


def _row_to_anchor_detail_record(
    row: tuple[object, ...],
    *,
    chunks: tuple[KnowledgeChunkSummaryRecord, ...],
) -> KnowledgeAnchorDetailRecord:
    temporal_scope_to = _coerce_optional_date(
        row[12],
        field_name="knowledge_anchors.temporal_scope_to",
    )
    return KnowledgeAnchorDetailRecord(
        anchor_id=str(row[0]),
        source_id=str(row[1]),
        source_family_id=str(row[2]),
        source_version_id=str(row[3]),
        source_title=str(row[4]),
        source_type=str(row[5]),
        tax_domain=str(row[6]),
        authority_level=str(row[7]),
        publication_state=str(row[8]),
        anchor_title=str(row[9]),
        anchor_path=str(row[10]),
        temporal_scope_from=cast(date, row[11]).isoformat(),
        temporal_scope_to=temporal_scope_to.isoformat() if temporal_scope_to is not None else None,
        chunk_count=len(chunks),
        chunks=chunks,
    )


def _storage_invalid(field_name: str) -> KnowledgeRepositoryError:
    return KnowledgeRepositoryError(
        reason_code=KNOWLEDGE_STORAGE_UNAVAILABLE,
        message=f"Knowledge persistence storage returned invalid {field_name}.",
    )


def _coerce_optional_date(value: object, *, field_name: str) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    raise _storage_invalid(field_name)


def _coerce_optional_int(value: object, *, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise _storage_invalid(field_name)


def _coerce_required_int(value: object, *, field_name: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise _storage_invalid(field_name)


def _coerce_required_bool(value: object, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise _storage_invalid(field_name)


def _coerce_object_mapping(
    value: object,
    *,
    reason_code: str,
    message: str,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise KnowledgeRepositoryError(
            reason_code=reason_code,
            message=message,
        )
    normalized: dict[str, object] = {}
    raw_mapping = cast(dict[object, object], value)
    for key, item in raw_mapping.items():
        if not isinstance(key, str):
            raise KnowledgeRepositoryError(
                reason_code=reason_code,
                message=message,
            )
        normalized[key] = item
    return normalized


def _row_to_record(row: tuple[object, ...]) -> KnowledgeSearchRecord:
    effective_to_value = row[7]
    tax_year_value = row[8]
    effective_to = _coerce_optional_date(
        effective_to_value,
        field_name="knowledge_source_versions.effective_to",
    )
    return KnowledgeSearchRecord(
        source_id=str(row[0]),
        title=str(row[1]),
        url=str(row[2]),
        source_type=str(row[3]),
        tax_domain=str(row[4]),
        authority_level=str(row[5]),
        effective_from=cast(date, row[6]).isoformat(),
        effective_to=effective_to.isoformat() if effective_to is not None else None,
        tax_year=_coerce_optional_int(
            tax_year_value,
            field_name="knowledge_source_versions.tax_year",
        ),
        anchor_id=str(row[9]),
        content=str(row[10]),
    )


def _row_to_timeline_record(row: tuple[object, ...]) -> KnowledgeTimelineRecord:
    effective_to_value = row[9]
    effective_to = _coerce_optional_date(
        effective_to_value,
        field_name="knowledge_source_versions.effective_to",
    )
    return KnowledgeTimelineRecord(
        source_id=str(row[0]),
        source_version_id=str(row[1]),
        anchor_id=str(row[2]),
        title=str(row[3]),
        url=str(row[4]),
        source_type=str(row[5]),
        authority_level=str(row[6]),
        tax_domain=str(row[7]),
        effective_from=cast(date, row[8]).isoformat(),
        effective_to=effective_to.isoformat() if effective_to is not None else None,
        publication_state=str(row[10]),
        timeline_position=0,
        content=str(row[11]),
    )


def _parse_embedding_vector_json(value: object) -> tuple[float, ...]:
    if isinstance(value, list):
        vector: list[float] = []
        for item in cast(list[object], value):
            if isinstance(item, (int, float)) and not isinstance(item, bool):
                vector.append(float(item))
            else:
                return ()
        return tuple(vector)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return ()
        return _parse_embedding_vector_json(parsed)
    return ()


def _query_tokens(query: str) -> tuple[str, ...]:
    normalized = " ".join(query.strip().lower().split())
    if not normalized:
        return ()
    return tuple(token for token in normalized.split(" ") if token)


def _match_score(
    *,
    query: str,
    title: str,
    content: str,
    anchor_id: str,
    source_type: str,
) -> int:
    tokens = _query_tokens(query)
    if not tokens:
        return 0
    haystack = (f"{title} {content} {anchor_id} {source_type}").lower()
    return sum(1 for token in tokens if token in haystack)


def _normalize_origin(*, provided_origin: str | None, expected_origin: str) -> str:
    if provided_origin is None:
        return expected_origin
    normalized = _normalize_required_string(
        value=provided_origin,
        field_name="source_input_origin",
    ).lower()
    if normalized == CUSTOMER_UPLOADED_DOCUMENT:
        raise KnowledgeRepositoryError(
            reason_code=UNSUPPORTED_SOURCE_INPUT_ORIGIN,
            message=(
                "Customer-uploaded documents are forbidden from shared-corpus knowledge ingestion."
            ),
        )
    if normalized != expected_origin:
        raise KnowledgeRepositoryError(
            reason_code=UNSUPPORTED_SOURCE_INPUT_ORIGIN,
            message="Knowledge ingestion source input origin is unsupported for this endpoint.",
        )
    return normalized


def _normalize_source_class(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = _normalize_required_string(value=value, field_name="source_class").lower()
    if normalized not in SUPPORTED_SOURCE_CLASSES:
        raise KnowledgeRepositoryError(
            reason_code=UNSUPPORTED_SOURCE_CLASS,
            message="Knowledge ingestion source class is unsupported.",
        )
    return normalized


def _normalize_source_document_system(value: str) -> str:
    normalized = _normalize_required_string(
        value=value,
        field_name="source_document_system",
    ).lower()
    if normalized not in SUPPORTED_SOURCE_DOCUMENT_SYSTEMS:
        raise KnowledgeRepositoryError(
            reason_code=INVALID_KNOWLEDGE_REQUEST,
            message="Knowledge ingestion source document system is invalid.",
        )
    return normalized


def _build_document_source_input_ref(*, source_document_system: str, document_id: str) -> str:
    return f"official-source-upload://{source_document_system}/documents/{document_id}"


def _build_legacy_import_source_input_ref(payload_checksum_sha256: str) -> str:
    return f"official-source-upload://legacy-import/sha256/{payload_checksum_sha256}"


def _build_url_source_input_ref(normalized_url: str) -> str:
    return f"official-source-url://{normalized_url}"


def _parse_upload_source_input_ref(value: str) -> _ParsedUploadSourceInputRef:
    normalized = _normalize_required_string(value=value, field_name="source_input_ref")
    if normalized.startswith("official-source-upload://"):
        suffix = normalized.removeprefix("official-source-upload://")
        if suffix.startswith("legacy-import/sha256/"):
            checksum = suffix.removeprefix("legacy-import/sha256/")
            return _ParsedUploadSourceInputRef(
                kind="legacy_import",
                document_system=None,
                document_id=None,
                payload_checksum_sha256=checksum,
            )
        for document_system in SUPPORTED_SOURCE_DOCUMENT_SYSTEMS:
            prefix = f"{document_system}/documents/"
            if suffix.startswith(prefix):
                document_id = suffix.removeprefix(prefix)
                return _ParsedUploadSourceInputRef(
                    kind="document",
                    document_system=document_system,
                    document_id=_normalize_uuid_string(
                        value=document_id,
                        field_name="source_input_ref",
                    ),
                    payload_checksum_sha256=None,
                )
    if normalized.startswith("document_ai://documents/"):
        document_id = normalized.removeprefix("document_ai://documents/")
        return _ParsedUploadSourceInputRef(
            kind="document",
            document_system="document_ai",
            document_id=_normalize_uuid_string(value=document_id, field_name="source_input_ref"),
            payload_checksum_sha256=None,
        )
    raise KnowledgeRepositoryError(
        reason_code=INVALID_KNOWLEDGE_LINEAGE,
        message="Knowledge source input reference is invalid.",
    )


def _normalize_source_input_origin_filter(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = _normalize_required_string(value=value, field_name="source_input_origin").lower()
    if normalized == CUSTOMER_UPLOADED_DOCUMENT or normalized not in {
        OFFICIAL_SOURCE_UPLOAD,
        OFFICIAL_SOURCE_URL,
    }:
        raise KnowledgeRepositoryError(
            reason_code=UNSUPPORTED_SOURCE_INPUT_ORIGIN,
            message="Knowledge management source input origin filter is unsupported.",
        )
    return normalized


def _normalize_ingestion_state_filter(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = _canonicalize_ingestion_state(
        _normalize_required_string(value=value, field_name="ingestion_state").lower()
    )
    if normalized not in KNOWLEDGE_INGESTION_STATES:
        raise KnowledgeRepositoryError(
            reason_code=INVALID_KNOWLEDGE_REQUEST,
            message="Knowledge management ingestion_state filter is invalid.",
        )
    return normalized


def _canonicalize_ingestion_state(value: str) -> str:
    if value == LEGACY_APPROVED_FOR_PUBLICATION_INGESTION_STATE:
        return APPROVED_INGESTION_STATE
    return value


def _is_approved_ingestion_state(value: str) -> bool:
    return _canonicalize_ingestion_state(value) == APPROVED_INGESTION_STATE


def _normalize_publication_state_filter(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = _normalize_required_string(value=value, field_name="publication_state").lower()
    if normalized not in KNOWLEDGE_PUBLICATION_STATES:
        raise KnowledgeRepositoryError(
            reason_code=INVALID_KNOWLEDGE_REQUEST,
            message="Knowledge management publication_state filter is invalid.",
        )
    return normalized


def _normalize_optional_identifier(value: str | None) -> str | None:
    if value is None:
        return None
    return _normalize_required_string(value=value, field_name="identifier")


def _normalize_management_limit(value: int | None) -> int:
    if value is None:
        return DEFAULT_MANAGEMENT_LIMIT
    if value > 0:
        return value
    raise KnowledgeRepositoryError(
        reason_code=INVALID_KNOWLEDGE_REQUEST,
        message="Knowledge management limit filter is invalid.",
    )


def _normalize_management_offset(value: int | None) -> int:
    if value is None:
        return DEFAULT_MANAGEMENT_OFFSET
    if value >= 0:
        return value
    raise KnowledgeRepositoryError(
        reason_code=INVALID_KNOWLEDGE_REQUEST,
        message="Knowledge management offset filter is invalid.",
    )


def _normalize_sort_order(value: str | None, *, default: str) -> str:
    if value is None:
        return default
    normalized = _normalize_required_string(value=value, field_name="sort_order").lower()
    if normalized not in SORT_ORDERS:
        raise KnowledgeRepositoryError(
            reason_code=INVALID_KNOWLEDGE_REQUEST,
            message="Knowledge management sort_order filter is invalid.",
        )
    return normalized


def _normalize_ingestion_sort_by(value: str | None) -> str:
    if value is None:
        return "created_at"
    normalized = _normalize_required_string(value=value, field_name="sort_by").lower()
    if normalized not in INGESTION_SORT_FIELDS:
        raise KnowledgeRepositoryError(
            reason_code=INVALID_KNOWLEDGE_REQUEST,
            message="Knowledge management sort_by filter is invalid.",
        )
    return normalized


def _normalize_source_version_sort_by(value: str | None) -> str:
    if value is None:
        return "source_family_id"
    normalized = _normalize_required_string(value=value, field_name="sort_by").lower()
    if normalized not in SOURCE_VERSION_SORT_FIELDS:
        raise KnowledgeRepositoryError(
            reason_code=INVALID_KNOWLEDGE_REQUEST,
            message="Knowledge management sort_by filter is invalid.",
        )
    return normalized


def _normalize_source_sort_by(value: str | None) -> str:
    if value is None:
        return "source_family_id"
    normalized = _normalize_required_string(value=value, field_name="sort_by").lower()
    if normalized not in SOURCE_SORT_FIELDS:
        raise KnowledgeRepositoryError(
            reason_code=INVALID_KNOWLEDGE_REQUEST,
            message="Knowledge management sort_by filter is invalid.",
        )
    return normalized


def _ingestion_order_by_clause(*, sort_by: str, sort_order: str) -> sql.Composable:
    _ = sort_by
    order = sql.SQL("DESC") if sort_order == "desc" else sql.SQL("ASC")
    return sql.SQL(" ORDER BY created_at {order}, id {order}").format(order=order)


def _source_version_order_by_clause(
    *,
    sort_by: str,
    sort_order: str,
) -> sql.Composable:
    order = sql.SQL("DESC") if sort_order == "desc" else sql.SQL("ASC")
    if sort_by == "effective_from":
        return sql.SQL(
            " ORDER BY ksv.effective_from {order}, ks.source_family_id {order}, ksv.id {order}"
        ).format(order=order)
    return sql.SQL(
        " ORDER BY ks.source_family_id {order}, ksv.effective_from {order}, ksv.id {order}"
    ).format(order=order)


def _source_order_by_clause(*, sort_by: str, sort_order: str) -> sql.Composable:
    order = sql.SQL("DESC") if sort_order == "desc" else sql.SQL("ASC")
    if sort_by == "tax_domain":
        return sql.SQL(
            " ORDER BY ks.tax_domain {order}, ks.source_family_id {order}, ks.source_id {order}"
        ).format(order=order)
    return sql.SQL(
        " ORDER BY ks.source_family_id {order}, ks.tax_domain {order}, ks.source_id {order}"
    ).format(order=order)


def _normalize_bulk_identifier_list(
    values: Sequence[str],
    *,
    field_name: str,
) -> tuple[str, ...]:
    if not values:
        raise KnowledgeRepositoryError(
            reason_code=INVALID_KNOWLEDGE_REQUEST,
            message=f"Knowledge request field `{field_name}` is invalid.",
        )
    deduplicated: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _normalize_required_string(value=value, field_name=field_name)
        if normalized in seen:
            continue
        seen.add(normalized)
        deduplicated.append(normalized)
    return tuple(deduplicated)


def _normalize_mime_type(value: str) -> str:
    normalized = _normalize_required_string(value=value, field_name="mime_type").lower()
    if normalized not in SUPPORTED_FILE_MIME_TYPES:
        raise KnowledgeRepositoryError(
            reason_code=INVALID_KNOWLEDGE_REQUEST,
            message="Knowledge ingestion file type is unsupported.",
        )
    return normalized


def _normalize_sha256_checksum(*, value: str, field_name: str) -> str:
    normalized = _normalize_required_string(value=value, field_name=field_name).lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise KnowledgeRepositoryError(
            reason_code=INVALID_KNOWLEDGE_REQUEST,
            message=f"Knowledge request field `{field_name}` is invalid.",
        )
    return normalized


def _assert_local_storage_key(value: str) -> None:
    normalized = _normalize_required_string(value=value, field_name="storage_key")
    lowered = normalized.lower()
    if lowered.startswith("http://") or lowered.startswith("https://") or "://" in normalized:
        raise KnowledgeRepositoryError(
            reason_code=INVALID_KNOWLEDGE_LINEAGE,
            message="Knowledge document storage must use a local storage key, not a URL.",
        )


def _extract_storage_key_for_publish_verification(
    extracted_metadata: Mapping[str, object],
) -> str:
    raw_value = extracted_metadata.get("storage_key")
    if not isinstance(raw_value, str):
        raise KnowledgeRepositoryError(
            reason_code=INVALID_KNOWLEDGE_LINEAGE,
            message="Knowledge publication document storage lineage is incomplete.",
        )
    normalized = _normalize_required_string(value=raw_value, field_name="storage_key")
    _assert_local_storage_key(normalized)
    return normalized


def _extract_normalized_url_for_publish_verification(
    extracted_metadata: Mapping[str, object],
) -> str:
    raw_value = extracted_metadata.get("normalized_url")
    if not isinstance(raw_value, str):
        raise KnowledgeRepositoryError(
            reason_code=INVALID_KNOWLEDGE_LINEAGE,
            message="Knowledge publication URL lineage is incomplete.",
        )
    return _normalize_url(raw_value)


def _legacy_import_storage_key(extracted_metadata: Mapping[str, object]) -> str:
    payload_checksum_sha256 = _normalize_sha256_checksum(
        value=_required_mapping_string(extracted_metadata, "payload_checksum_sha256"),
        field_name="payload_checksum_sha256",
    )
    filename = _required_mapping_string(extracted_metadata, "filename")
    return f"knowledge-official-upload/{payload_checksum_sha256}/{filename}"


def _decode_base64_payload(value: str) -> bytes:
    normalized = _normalize_required_string(
        value=value,
        field_name="file_content_base64",
    )
    try:
        return base64.b64decode(normalized, validate=True)
    except (ValueError, binascii.Error):
        raise KnowledgeRepositoryError(
            reason_code=INVALID_KNOWLEDGE_REQUEST,
            message="Knowledge ingestion file payload is invalid.",
        ) from None


def _normalize_url(value: str) -> str:
    normalized = _normalize_required_string(value=value, field_name="url")
    parsed = urlsplit(normalized)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise KnowledgeRepositoryError(
            reason_code=INVALID_KNOWLEDGE_REQUEST,
            message="Knowledge ingestion URL must use http or https.",
        )
    if not parsed.netloc:
        raise KnowledgeRepositoryError(
            reason_code=INVALID_KNOWLEDGE_REQUEST,
            message="Knowledge ingestion URL is invalid.",
        )
    netloc = parsed.netloc.lower()
    return urlunsplit(
        (
            parsed.scheme.lower(),
            netloc,
            parsed.path or "/",
            parsed.query,
            "",
        )
    )


def _normalize_required_string(*, value: str, field_name: str) -> str:
    normalized = value.strip()
    if normalized:
        return normalized
    raise KnowledgeRepositoryError(
        reason_code=INVALID_KNOWLEDGE_REQUEST,
        message=f"Knowledge request field `{field_name}` is invalid.",
    )


def _bulk_required_string(item: Mapping[str, object], *, field_name: str) -> str:
    value = item.get(field_name)
    if isinstance(value, str):
        return _normalize_required_string(value=value, field_name=field_name)
    raise KnowledgeRepositoryError(
        reason_code=INVALID_KNOWLEDGE_REQUEST,
        message=f"Knowledge request field `{field_name}` is invalid.",
    )


def _required_mapping_string(
    source: Mapping[str, object],
    field_name: str,
) -> str:
    value = source.get(field_name)
    if isinstance(value, str):
        return _normalize_required_string(value=value, field_name=field_name)
    raise KnowledgeRepositoryError(
        reason_code=INVALID_KNOWLEDGE_LINEAGE,
        message=f"Knowledge lineage field `{field_name}` is invalid.",
    )


def _bulk_optional_string_field(
    item: Mapping[str, object],
    *,
    field_name: str,
) -> str | None:
    value = item.get(field_name)
    if value is None:
        return None
    if isinstance(value, str):
        return _normalize_required_string(value=value, field_name=field_name).lower()
    raise KnowledgeRepositoryError(
        reason_code=INVALID_KNOWLEDGE_REQUEST,
        message=f"Knowledge request field `{field_name}` is invalid.",
    )


def _bulk_required_true_boolean(
    item: Mapping[str, object],
    *,
    field_name: str,
) -> bool:
    if item.get(field_name) is True:
        return True
    raise KnowledgeRepositoryError(
        reason_code=INVALID_KNOWLEDGE_REQUEST,
        message=f"Knowledge request field `{field_name}` is invalid.",
    )


def _bulk_required_uuid_string(item: Mapping[str, object], *, field_name: str) -> str:
    value = item.get(field_name)
    if not isinstance(value, str):
        raise KnowledgeRepositoryError(
            reason_code=INVALID_KNOWLEDGE_REQUEST,
            message=f"Knowledge request field `{field_name}` is invalid.",
        )
    return _normalize_uuid_string(value=value, field_name=field_name)


def _parse_uuid_string(value: str, *, field_name: str) -> UUID:
    try:
        return UUID(_normalize_required_string(value=value, field_name=field_name))
    except ValueError:
        raise KnowledgeRepositoryError(
            reason_code=INVALID_KNOWLEDGE_REQUEST,
            message=f"Knowledge request field `{field_name}` is invalid.",
        ) from None


def _normalize_uuid_string(*, value: str, field_name: str) -> str:
    return str(_parse_uuid_string(value, field_name=field_name))


def _ingestion_event_type(proposed_source_record: Mapping[str, object]) -> str:
    ingestion_kind = str(proposed_source_record.get("ingestion_kind", ""))
    if ingestion_kind == "document":
        return "knowledge_ingestion_document"
    if ingestion_kind == "url":
        return "knowledge_ingestion_url"
    return "knowledge_ingestion_file"


def _payload_fingerprint(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bulk_item_idempotency_key(raw_item: object) -> str:
    if not isinstance(raw_item, Mapping):
        return ""
    nested = cast(Mapping[object, object], raw_item)
    value = nested.get("idempotency_key")
    if isinstance(value, str):
        return value.strip()
    return ""


def _bulk_ingestion_error(
    *,
    index: int,
    idempotency_key: str,
    error: KnowledgeRepositoryError,
) -> KnowledgeBulkIngestionItemRecord:
    return KnowledgeBulkIngestionItemRecord(
        index=index,
        idempotency_key=idempotency_key,
        status="error",
        outcome="rejected",
        ingestion_job_id=None,
        error_code=error.reason_code,
        reason=error.reason_code,
    )


def _validate_review_notes(review_notes: Sequence[Mapping[str, object]]) -> None:
    if not review_notes:
        raise KnowledgeRepositoryError(
            reason_code=INVALID_KNOWLEDGE_REQUEST,
            message="Knowledge review notes are required for this action.",
        )


def _normalize_publication_payload(payload: Mapping[str, object]) -> dict[str, object]:
    required_string_fields = (
        "source_id",
        "source_family_id",
        "title",
        "source_class",
        "authority_level",
        "tax_domain",
        "issuing_authority",
        "point_in_time_url",
        "source_version_form",
        "effective_from",
    )
    normalized: dict[str, object] = {}
    for field_name in required_string_fields:
        value = payload.get(field_name)
        if not isinstance(value, str):
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_REQUEST,
                message=f"Knowledge publication field `{field_name}` is invalid.",
            )
        normalized[field_name] = value.strip()
    if normalized["source_class"] not in SOURCE_CLASS_AUTHORITY_BINDING:
        raise KnowledgeRepositoryError(
            reason_code=UNSUPPORTED_SOURCE_CLASS,
            message="Knowledge publication source class is unsupported.",
        )
    if SOURCE_CLASS_AUTHORITY_BINDING[str(normalized["source_class"])] != str(
        normalized["authority_level"]
    ):
        raise KnowledgeRepositoryError(
            reason_code=INVALID_AUTHORITY_SOURCE_CLASS_BINDING,
            message=(
                "Knowledge publication authority level does not match the governed source class."
            ),
        )
    if str(normalized["source_version_form"]) not in {"as_issued", "point_in_time_consolidation"}:
        raise KnowledgeRepositoryError(
            reason_code=INVALID_KNOWLEDGE_REQUEST,
            message="Knowledge publication source version form is invalid.",
        )
    try:
        effective_from = date.fromisoformat(str(normalized["effective_from"]))
    except ValueError:
        raise KnowledgeRepositoryError(
            reason_code=INVALID_EFFECTIVE_WINDOW_METADATA,
            message="Knowledge publication effective_from must be an ISO date.",
        ) from None
    effective_to_value = payload.get("effective_to")
    effective_to: str | None = None
    if effective_to_value is not None:
        if not isinstance(effective_to_value, str):
            raise KnowledgeRepositoryError(
                reason_code=INVALID_EFFECTIVE_WINDOW_METADATA,
                message="Knowledge publication effective_to must be an ISO date or null.",
            )
        try:
            parsed_effective_to = date.fromisoformat(effective_to_value.strip())
        except ValueError:
            raise KnowledgeRepositoryError(
                reason_code=INVALID_EFFECTIVE_WINDOW_METADATA,
                message="Knowledge publication effective_to must be an ISO date or null.",
            ) from None
        if parsed_effective_to < effective_from:
            raise KnowledgeRepositoryError(
                reason_code=INVALID_EFFECTIVE_WINDOW_METADATA,
                message="Knowledge publication effective window is invalid.",
            )
        effective_to = parsed_effective_to.isoformat()
    normalized["effective_from"] = effective_from.isoformat()
    normalized["effective_to"] = effective_to
    tax_year = payload.get("tax_year")
    if tax_year is None:
        normalized["tax_year"] = None
    elif isinstance(tax_year, int):
        normalized["tax_year"] = tax_year
    else:
        raise KnowledgeRepositoryError(
            reason_code=INVALID_KNOWLEDGE_REQUEST,
            message="Knowledge publication tax_year is invalid.",
        )

    anchors_value = payload.get("anchors")
    if not isinstance(anchors_value, list) or not anchors_value:
        raise KnowledgeRepositoryError(
            reason_code=INVALID_KNOWLEDGE_REQUEST,
            message="Knowledge publication anchors are required.",
        )
    anchors = cast(list[object], anchors_value)
    normalized["anchors"] = tuple(_normalize_anchor_payload(item) for item in anchors)
    return normalized


def _normalize_anchor_payload(anchor: object) -> dict[str, object]:
    anchor_payload = _coerce_object_mapping(
        anchor,
        reason_code=INVALID_KNOWLEDGE_REQUEST,
        message="Knowledge publication anchor payload is invalid.",
    )
    required_string_fields = (
        "anchor_id",
        "anchor_title",
        "anchor_path",
        "anchor_text",
        "temporal_scope_from",
    )
    normalized: dict[str, object] = {}
    for field_name in required_string_fields:
        value = anchor_payload.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise KnowledgeRepositoryError(
                reason_code=INVALID_KNOWLEDGE_REQUEST,
                message=f"Knowledge publication anchor field `{field_name}` is invalid.",
            )
        normalized[field_name] = value.strip()
    try:
        temporal_scope_from = date.fromisoformat(str(normalized["temporal_scope_from"]))
    except ValueError:
        raise KnowledgeRepositoryError(
            reason_code=INVALID_EFFECTIVE_WINDOW_METADATA,
            message="Knowledge publication anchor temporal scope is invalid.",
        ) from None
    temporal_scope_to_value = anchor_payload.get("temporal_scope_to")
    temporal_scope_to: str | None = None
    if temporal_scope_to_value is not None:
        if not isinstance(temporal_scope_to_value, str):
            raise KnowledgeRepositoryError(
                reason_code=INVALID_EFFECTIVE_WINDOW_METADATA,
                message="Knowledge publication anchor temporal scope is invalid.",
            )
        try:
            parsed_temporal_scope_to = date.fromisoformat(temporal_scope_to_value.strip())
        except ValueError:
            raise KnowledgeRepositoryError(
                reason_code=INVALID_EFFECTIVE_WINDOW_METADATA,
                message="Knowledge publication anchor temporal scope is invalid.",
            ) from None
        if parsed_temporal_scope_to < temporal_scope_from:
            raise KnowledgeRepositoryError(
                reason_code=INVALID_EFFECTIVE_WINDOW_METADATA,
                message="Knowledge publication anchor temporal scope is invalid.",
            )
        temporal_scope_to = parsed_temporal_scope_to.isoformat()
    normalized["temporal_scope_from"] = temporal_scope_from.isoformat()
    normalized["temporal_scope_to"] = temporal_scope_to
    chunks = anchor_payload.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        raise KnowledgeRepositoryError(
            reason_code=INVALID_KNOWLEDGE_REQUEST,
            message="Knowledge publication anchor chunks are required.",
        )
    chunk_items = cast(list[object], chunks)
    normalized["chunks"] = tuple(_normalize_chunk_payload(item) for item in chunk_items)
    return normalized


def _normalize_chunk_payload(chunk: object) -> dict[str, object]:
    chunk_payload = _coerce_object_mapping(
        chunk,
        reason_code=INVALID_KNOWLEDGE_REQUEST,
        message="Knowledge publication chunk payload is invalid.",
    )
    chunk_text = chunk_payload.get("chunk_text")
    if not isinstance(chunk_text, str) or not chunk_text.strip():
        raise KnowledgeRepositoryError(
            reason_code=INVALID_KNOWLEDGE_REQUEST,
            message="Knowledge publication chunk_text is invalid.",
        )
    return {"chunk_text": chunk_text.strip()}


def _extract_publication_payload(proposed_source_record: dict[str, object]) -> dict[str, object]:
    payload = proposed_source_record.get("publication_payload")
    if payload is None:
        raise KnowledgeRepositoryError(
            reason_code=KNOWLEDGE_PUBLICATION_SAFETY_REJECTED,
            message="Knowledge publication requires governed approval metadata before publishing.",
        )
    return _coerce_object_mapping(
        payload,
        reason_code=KNOWLEDGE_PUBLICATION_SAFETY_REJECTED,
        message="Knowledge publication requires governed approval metadata before publishing.",
    )


def _parse_json_array_of_objects(value: object) -> tuple[dict[str, object], ...]:
    if isinstance(value, list):
        output: list[dict[str, object]] = []
        items = cast(list[object], value)
        for item in items:
            output.append(
                _coerce_object_mapping(
                    item,
                    reason_code=KNOWLEDGE_STORAGE_UNAVAILABLE,
                    message=(
                        "Knowledge persistence storage returned invalid review note metadata."
                    ),
                )
            )
        return tuple(output)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as error:
            raise KnowledgeRepositoryError(
                reason_code=KNOWLEDGE_STORAGE_UNAVAILABLE,
                message="Knowledge persistence storage returned invalid JSON metadata.",
            ) from error
        return _parse_json_array_of_objects(parsed)
    raise KnowledgeRepositoryError(
        reason_code=KNOWLEDGE_STORAGE_UNAVAILABLE,
        message="Knowledge persistence storage returned invalid review note shape.",
    )


def _parse_json_object(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return _coerce_object_mapping(
            cast(object, value),
            reason_code=KNOWLEDGE_STORAGE_UNAVAILABLE,
            message="Knowledge persistence storage returned invalid metadata shape.",
        )
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as error:
            raise KnowledgeRepositoryError(
                reason_code=KNOWLEDGE_STORAGE_UNAVAILABLE,
                message="Knowledge persistence storage returned invalid JSON metadata.",
            ) from error
        if isinstance(parsed, dict):
            return _coerce_object_mapping(
                cast(object, parsed),
                reason_code=KNOWLEDGE_STORAGE_UNAVAILABLE,
                message="Knowledge persistence storage returned invalid metadata shape.",
            )
    raise KnowledgeRepositoryError(
        reason_code=KNOWLEDGE_STORAGE_UNAVAILABLE,
        message="Knowledge persistence storage returned invalid metadata shape.",
    )
