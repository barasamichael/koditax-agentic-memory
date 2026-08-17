"""Authorized exact and structural retrieval over active canonical chunks."""

from __future__ import annotations

import re
import json
from uuid import UUID
from typing import Any
from typing import cast
from typing import Literal
from typing import LiteralString
from collections.abc import Sequence

import psycopg
from psycopg import sql
from pydantic import Field
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import model_validator

from services.document_ai.app.persistence_support import connect_document_ai_database

_NORMALIZE_WHITESPACE_PATTERN = re.compile(r"\s+")
_MAX_RETRIEVAL_LIMIT = 100


def _normalize_exact_query_text(value: str) -> str:
    """Return one deterministic lexical form for exact retrieval inputs."""

    return _NORMALIZE_WHITESPACE_PATTERN.sub(" ", value).strip()


class ExactRetrievalRequest(BaseModel):
    """Describe exact candidate constraints without semantic-search options."""

    model_config = ConfigDict(extra="forbid")

    document_ids: list[UUID] = Field(default_factory=lambda: list[UUID](), max_length=100)
    document_version_id: UUID | None = None
    conversation_id: str | None = Field(default=None, min_length=1, max_length=255)
    turn_id: str | None = Field(default=None, min_length=1, max_length=255)
    filename: str | None = Field(default=None, min_length=1, max_length=512)
    display_name: str | None = Field(default=None, min_length=1, max_length=512)
    identifier: str | None = Field(default=None, min_length=1, max_length=512)
    amount: str | None = Field(default=None, min_length=1, max_length=128)
    date: str | None = Field(default=None, min_length=1, max_length=128)
    full_text: str | None = Field(default=None, min_length=1, max_length=2_000)
    canonical_element_types: list[str] = Field(default_factory=list, max_length=20)
    page_number: int | None = Field(default=None, ge=1)
    sheet_name: str | None = Field(default=None, min_length=1, max_length=255)
    table_name: str | None = Field(default=None, min_length=1, max_length=255)
    cell_reference: str | None = Field(default=None, min_length=1, max_length=255)
    limit: int = Field(default=25, ge=1, le=_MAX_RETRIEVAL_LIMIT)

    @model_validator(mode="after")
    def validate_scope_and_criteria(self) -> ExactRetrievalRequest:
        if (self.conversation_id is None) != (self.turn_id is None):
            raise ValueError("conversation_id and turn_id must be supplied together")

        normalized_fields = {}
        for field_name in (
            "conversation_id",
            "turn_id",
            "filename",
            "display_name",
            "identifier",
            "amount",
            "date",
            "full_text",
            "sheet_name",
            "table_name",
            "cell_reference",
        ):
            value = getattr(self, field_name)
            if value is not None:
                normalized_value = _normalize_exact_query_text(value)
                if not normalized_value:
                    raise ValueError(f"{field_name} must not be empty")
                normalized_fields[field_name] = normalized_value

        if normalized_fields:
            for field_name, value in normalized_fields.items():
                object.__setattr__(self, field_name, value)

        if self.canonical_element_types:
            canonical_element_types = tuple(
                dict.fromkeys(
                    _normalize_exact_query_text(value)
                    for value in self.canonical_element_types
                    if _normalize_exact_query_text(value)
                )
            )
            object.__setattr__(self, "canonical_element_types", list(canonical_element_types))

        if not any(
            (
                self.document_ids,
                self.document_version_id,
                self.conversation_id,
                self.filename,
                self.display_name,
                self.identifier,
                self.amount,
                self.date,
                self.full_text,
                self.canonical_element_types,
                self.page_number,
                self.sheet_name,
                self.table_name,
                self.cell_reference,
            )
        ):
            raise ValueError("at least one exact retrieval constraint is required")
        return self


class ExactRetrievalCandidate(BaseModel):
    """Represent one exact lexical candidate chunk with deterministic lineage."""

    retrieval_chunk_id: UUID
    document_id: UUID
    document_version_id: UUID
    canonical_representation_id: UUID
    chunk_key: str
    content_hash_sha256: str
    chunking_policy_version: str
    exact_match_rank: int
    canonical_element_keys: tuple[str, ...]
    source_lineage: dict[str, object]
    source_location: dict[str, object]
    structural_context: dict[str, object]
    display_name: str | None = None
    source_filename: str


class ExactRetrievalEnvelope(BaseModel):
    """Expose resolved chunk evidence, never embeddings or semantic candidates."""

    status: Literal["ok"] = "ok"
    evidence: list[ExactRetrievalCandidate]


class ExactRetrievalRepository:
    """Apply authorization and lifecycle scope before lexical or structural matching."""

    def __init__(self, *, database_url: str) -> None:
        self._database_url = database_url

    def retrieve(
        self,
        *,
        tenant_id: str,
        owner_user_id: UUID,
        request: ExactRetrievalRequest,
    ) -> list[ExactRetrievalCandidate]:
        query, parameters = build_exact_retrieval_query(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            request=request,
        )
        try:
            with connect_document_ai_database(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(sql.SQL(cast(LiteralString, query)), parameters)
                    rows = cursor.fetchall()
        except psycopg.Error as error:
            raise RuntimeError("document_ai_exact_retrieval_unavailable") from error
        return [_row_to_candidate(row) for row in rows]


def build_exact_retrieval_query(
    *, tenant_id: str, owner_user_id: UUID, request: ExactRetrievalRequest
) -> tuple[str, list[object]]:
    """Build the authorized lexical query using active chunk and metadata scope."""

    scope_conditions = [
        "document.tenant_id = %s",
        "document.owner_user_id = %s",
        "document.state IN ('uploaded', 'processing', 'validated', 'active')",
    ]
    parameters: list[object] = [tenant_id, owner_user_id]
    if request.document_ids:
        scope_conditions.append("document.document_id = ANY(%s::uuid[])")
        parameters.append([str(item) for item in request.document_ids])
    if request.document_version_id is not None:
        scope_conditions.append("document.active_document_version_id = %s")
        parameters.append(request.document_version_id)
    if request.conversation_id is not None:
        scope_conditions.append(
            """EXISTS (
                   SELECT 1
                     FROM document_ai_document_bindings AS binding
                    WHERE binding.tenant_id = document.tenant_id
                      AND binding.document_id = document.document_id
                      AND binding.bound_by_user_id = %s
                      AND binding.conversation_id = %s
                      AND binding.turn_id = %s
                      AND binding.binding_role = 'current_turn_attachment'
                      AND binding.revoked_at IS NULL
               )"""
        )
        parameters.extend([owner_user_id, request.conversation_id, request.turn_id])
    if request.filename is not None:
        scope_conditions.append("document.storage_key ILIKE %s")
        parameters.append(f"%{request.filename}%")
    if request.display_name is not None:
        scope_conditions.append("document.display_name ILIKE %s")
        parameters.append(f"%{request.display_name}%")

    chunk_conditions: list[str] = ["chunk.lifecycle_state = 'active'"]
    chunk_condition_params: list[object] = []
    rank_expressions: list[str] = []
    rank_params: list[object] = []
    text_term_count = 0

    def add_text_term(
        *,
        normalized_term: str,
        search_target_sql: str,
        exact_target_sql: str,
    ) -> None:
        normalized_term_lower = normalized_term.lower()
        text_pattern = f"%{normalized_term}%"
        chunk_conditions.append(
            f"""(
                   {search_target_sql} ILIKE %s
                   OR EXISTS (
                       SELECT 1
                         FROM jsonb_array_elements_text(chunk.canonical_element_keys) AS key(
                             stable_key
                         )
                         JOIN document_ai_canonical_elements AS element
                           ON element.tenant_id = chunk.tenant_id
                          AND element.canonical_representation_id =
                              chunk.canonical_representation_id
                          AND element.stable_key = key.stable_key
                        WHERE COALESCE(element.normalized_value->>'text', '') = %s
                   )
               )"""
        )
        chunk_condition_params.extend([text_pattern, normalized_term])
        rank_expressions.append(
            f"""CASE
                    WHEN LOWER({exact_target_sql}) = %s THEN 0
                    WHEN {search_target_sql} ILIKE %s THEN 1
                    WHEN EXISTS (
                        SELECT 1
                          FROM jsonb_array_elements_text(chunk.canonical_element_keys) AS key(
                              stable_key
                          )
                          JOIN document_ai_canonical_elements AS element
                            ON element.tenant_id = chunk.tenant_id
                           AND element.canonical_representation_id =
                               chunk.canonical_representation_id
                           AND element.stable_key = key.stable_key
                         WHERE COALESCE(element.normalized_value->>'text', '') = %s
                    ) THEN 1
                    ELSE 2
                END"""
        )
        rank_params.extend([normalized_term_lower, text_pattern, normalized_term])

    if request.full_text is not None:
        normalized_full_text = _normalize_exact_query_text(request.full_text)
        text_term_count += 1
        add_text_term(
            normalized_term=normalized_full_text,
            search_target_sql="chunk.embedding_text",
            exact_target_sql="chunk.embedding_text",
        )
    for field_name in ("identifier", "amount", "date"):
        value = getattr(request, field_name)
        if value is not None:
            normalized_value = _normalize_exact_query_text(value)
            text_term_count += 1
            add_text_term(
                normalized_term=normalized_value,
                search_target_sql="chunk.embedding_text",
                exact_target_sql="chunk.embedding_text",
            )

    if request.canonical_element_types:
        chunk_conditions.append(
            """EXISTS (
                   SELECT 1
                     FROM jsonb_array_elements_text(chunk.canonical_element_keys) AS key(
                         stable_key
                     )
                     JOIN document_ai_canonical_elements AS element
                       ON element.tenant_id = chunk.tenant_id
                      AND element.canonical_representation_id =
                          chunk.canonical_representation_id
                      AND element.stable_key = key.stable_key
                    WHERE element.element_type = ANY(%s::text[])
               )"""
        )
        chunk_condition_params.append(request.canonical_element_types)
    if request.page_number is not None:
        chunk_conditions.append("chunk.source_location @> %s::jsonb")
        chunk_condition_params.append(json.dumps({"page_number": request.page_number}))
    for field_name, json_key in (
        ("sheet_name", "sheet_name"),
        ("table_name", "table_name"),
        ("cell_reference", "cell_reference"),
    ):
        value = getattr(request, field_name)
        if value is not None:
            chunk_conditions.append("chunk.source_location @> %s::jsonb")
            chunk_condition_params.append(json.dumps({json_key: value}))

    if rank_expressions:
        exact_rank_expression = f"LEAST({', '.join(rank_expressions)})"
        parameters.extend(rank_params)
    else:
        exact_rank_expression = "0"

    parameters.extend(chunk_condition_params)
    parameters.append(request.limit)
    chunk_index_hint = "document_ai_retrieval_chunks"
    query = f"""
        WITH authorized_documents AS (
            SELECT document.tenant_id, document.document_id, document.active_document_version_id,
                   document.display_name, document.storage_key
              FROM document_ai_documents AS document
             WHERE {" AND ".join(scope_conditions)}
        )
        SELECT document.document_id, document.active_document_version_id,
               chunk.retrieval_chunk_id, chunk.canonical_representation_id, chunk.chunk_key,
               chunk.content_hash_sha256, chunk.chunking_policy_version,
               chunk.canonical_element_keys, chunk.source_location, chunk.structural_context,
               document.display_name, document.storage_key,
               {exact_rank_expression} AS exact_match_rank
          FROM authorized_documents AS document
          JOIN document_ai_canonical_representations AS representation
            ON representation.tenant_id = document.tenant_id
           AND representation.document_version_id = document.active_document_version_id
           AND representation.is_active
           AND representation.state = 'active'
          JOIN {chunk_index_hint} AS chunk
            ON chunk.tenant_id = document.tenant_id
           AND chunk.document_id = document.document_id
           AND chunk.document_version_id = document.active_document_version_id
           AND chunk.lifecycle_state = 'active'
         WHERE {" AND ".join(chunk_conditions)}
         ORDER BY exact_match_rank ASC, document.document_id, chunk.chunk_key,
                  chunk.retrieval_chunk_id
         LIMIT %s
    """
    return query, parameters


def _row_to_candidate(row: Sequence[Any]) -> ExactRetrievalCandidate:
    structural_context = cast(dict[str, object], row[9]) if isinstance(row[9], dict) else {}
    source_lineage_raw = structural_context.get("source_lineage", {})
    source_lineage = (
        dict(source_lineage_raw) if isinstance(source_lineage_raw, dict) else {}
    )
    return ExactRetrievalCandidate(
        document_id=UUID(str(row[0])),
        document_version_id=UUID(str(row[1])),
        retrieval_chunk_id=UUID(str(row[2])),
        canonical_representation_id=UUID(str(row[3])),
        chunk_key=str(row[4]),
        content_hash_sha256=str(row[5]),
        chunking_policy_version=str(row[6]),
        canonical_element_keys=tuple(str(value) for value in cast(Sequence[object], row[7])),
        source_location=cast(dict[str, object], row[8]),
        structural_context=structural_context,
        source_lineage=source_lineage,
        display_name=str(row[10]) if row[10] is not None else None,
        source_filename=str(row[11]),
        exact_match_rank=int(row[12]),
    )
