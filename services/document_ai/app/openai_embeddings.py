"""Governed OpenAI embedding boundary and durable canonical vector generation."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from math import isfinite
from uuid import UUID
from typing import Protocol
from collections.abc import Iterable
from collections.abc import Sequence

from openai import OpenAI
from openai import APIConnectionError
from openai import APIStatusError
from openai import APITimeoutError

from services.document_ai.app.config import get_document_ai_embedding_api_key
from services.document_ai.app.config import get_document_ai_embedding_model
from services.document_ai.app.config import get_document_ai_openai_timeout_seconds
from services.document_ai.app.persistence_support import execute_document_ai_database_transaction

EMBEDDING_VERSION = "v1"
DOCUMENT_AI_EMBEDDING_DIMENSIONS = 1536
CANONICAL_EMBEDDING_BATCH_SIZE = 32
CANONICAL_EMBEDDING_CONTINUATION_EVENT = "canonical_activation_requested"


class EmbeddingProviderError(RuntimeError):
    """A classified OpenAI embedding-boundary failure."""

    def __init__(self, reason: str, *, retryable: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable


class EmbeddingClientProtocol(Protocol):
    def embed(self, *, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]: ...


class GovernedOpenAIEmbeddingClient:
    """The sole provider adapter; callers cannot pass provider-specific options."""

    def __init__(self, *, api_key: str, model: str, timeout_seconds: int) -> None:
        self._client = OpenAI(api_key=api_key, timeout=timeout_seconds)
        self._model = model

    @classmethod
    def from_environment(cls) -> GovernedOpenAIEmbeddingClient:
        api_key = get_document_ai_embedding_api_key()
        if api_key is None:
            raise EmbeddingProviderError("missing_openai_embedding_configuration", retryable=False)
        return cls(
            api_key=api_key,
            model=get_document_ai_embedding_model(),
            timeout_seconds=get_document_ai_openai_timeout_seconds(),
        )

    def embed(self, *, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        if not texts:
            return ()
        try:
            response = self._client.embeddings.create(model=self._model, input=list(texts))
        except APITimeoutError as error:
            raise EmbeddingProviderError("openai_embedding_timeout", retryable=True) from error
        except APIConnectionError as error:
            raise EmbeddingProviderError(
                "openai_embedding_connection_failure", retryable=True
            ) from error
        except APIStatusError as error:
            raise EmbeddingProviderError(
                "openai_embedding_rejected",
                retryable=error.status_code == 429 or error.status_code >= 500,
            ) from error
        return tuple(tuple(float(value) for value in item.embedding) for item in response.data)


@dataclass(frozen=True)
class _EmbeddingChunkPlan:
    """Represent one exact retrieval chunk selected for embedding."""

    retrieval_chunk_id: UUID
    chunk_key: str
    content_hash_sha256: str
    chunking_policy_version: str
    embedding_text: str
    has_current_embedding: bool


@dataclass(frozen=True)
class _EmbeddingGenerationPlan:
    """Represent one fenced embedding-generation decision."""

    tenant_id: str
    document_id: UUID
    document_version_id: UUID
    canonical_representation_id: UUID
    representation_state: str
    readiness_state: str
    is_active: bool
    chunks: tuple[_EmbeddingChunkPlan, ...]

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)

    @property
    def current_embedding_count(self) -> int:
        return sum(1 for chunk in self.chunks if chunk.has_current_embedding)

    @property
    def missing_chunks(self) -> tuple[_EmbeddingChunkPlan, ...]:
        return tuple(chunk for chunk in self.chunks if not chunk.has_current_embedding)


@dataclass(frozen=True)
class CanonicalEmbeddingGenerationResult:
    """A durable embedding-generation decision and completion summary."""

    state: str
    chunk_count: int
    current_embedding_count: int
    embedding_generation_identity: str
    continuation_event: str
    embedding_model: str
    embedding_version: str
    embedding_dimensions: int
    chunk_keys: tuple[str, ...]


class _Cursor(Protocol):
    def execute(self, query: str, params: tuple[object, ...]) -> object: ...

    def fetchone(self) -> Sequence[object] | None: ...

    def fetchall(self) -> list[Sequence[object]]: ...


class CanonicalEmbeddingRepository:
    """Generate embeddings outside transactions and persist them with a fenced replay."""

    def __init__(
        self,
        *,
        database_url: str,
        embedding_client: EmbeddingClientProtocol | None = None,
    ) -> None:
        self._database_url = database_url
        self._embedding_client = embedding_client

    def index_active_representation(
        self,
        *,
        tenant_id: str,
        canonical_representation_id: UUID,
        allow_validated_candidate: bool = False,
    ) -> CanonicalEmbeddingGenerationResult:
        """Embed all current chunks for the fenced canonical representation."""

        plan = execute_document_ai_database_transaction(
            database_url=self._database_url,
            transaction_name="document_ai.openai_embeddings.prepare_embedding_generation",
            transaction_callback=lambda cursor: self._load_embedding_plan(
                cursor=cursor,
                tenant_id=tenant_id,
                canonical_representation_id=canonical_representation_id,
                allow_validated_candidate=allow_validated_candidate,
            ),
            reconcile_ambiguous_result=lambda connection: self._reconcile_embedding_plan(
                connection=connection,
                tenant_id=tenant_id,
                canonical_representation_id=canonical_representation_id,
                allow_validated_candidate=allow_validated_candidate,
            ),
        )
        if plan is None:
            raise EmbeddingProviderError(
                "canonical_representation_not_available_for_embedding",
                retryable=True,
            )
        if not plan.missing_chunks:
            return self._result_from_plan(plan, state="replayed")

        vectors_by_chunk_id = self._embed_missing_chunks(chunks=plan.missing_chunks)
        return execute_document_ai_database_transaction(
            database_url=self._database_url,
            transaction_name="document_ai.openai_embeddings.persist_embedding_generation",
            transaction_callback=lambda cursor: self._persist_embedding_generation(
                cursor=cursor,
                plan=plan,
                vectors_by_chunk_id=vectors_by_chunk_id,
                allow_validated_candidate=allow_validated_candidate,
            ),
            reconcile_ambiguous_result=lambda connection: self._reconcile_embedding_result(
                connection=connection,
                tenant_id=tenant_id,
                canonical_representation_id=canonical_representation_id,
                allow_validated_candidate=allow_validated_candidate,
            ),
        )

    def index_validated_candidate(
        self,
        *,
        tenant_id: str,
        canonical_representation_id: UUID,
    ) -> CanonicalEmbeddingGenerationResult:
        """Prepare non-active candidate vectors before activation."""

        return self.index_active_representation(
            tenant_id=tenant_id,
            canonical_representation_id=canonical_representation_id,
            allow_validated_candidate=True,
        )

    def _load_embedding_plan(
        self,
        *,
        cursor: _Cursor,
        tenant_id: str,
        canonical_representation_id: UUID,
        allow_validated_candidate: bool,
    ) -> _EmbeddingGenerationPlan:
        cursor.execute(
            """SELECT document.document_id, representation.document_version_id,
                      representation.state, representation.readiness_state,
                      representation.is_active
                 FROM document_ai_canonical_representations AS representation
                 JOIN document_ai_document_versions AS version
                   ON version.tenant_id = representation.tenant_id
                  AND version.document_version_id = representation.document_version_id
                 JOIN document_ai_documents AS document
                   ON document.tenant_id = version.tenant_id
                  AND document.document_id = version.document_id
                WHERE representation.tenant_id = %s
                  AND representation.canonical_representation_id = %s
                  AND document.state NOT IN (
                      'trashed', 'purge_pending', 'eligible_for_purge', 'purged'
                  )
                  AND (
                      (representation.is_active AND representation.state = 'active')
                      OR (%s AND NOT representation.is_active
                          AND representation.state = 'validated'
                          AND representation.readiness_state = 'full')
                  )
                FOR UPDATE""",
            (tenant_id, canonical_representation_id, allow_validated_candidate),
        )
        scope_row = cursor.fetchone()
        if scope_row is None:
            raise EmbeddingProviderError(
                "canonical_representation_not_available_for_embedding",
                retryable=True,
            )
        cursor.execute(
            """SELECT chunk.retrieval_chunk_id, chunk.chunk_key, chunk.content_hash_sha256,
                      chunk.chunking_policy_version, chunk.embedding_text,
                      embedding.chunk_embedding_id IS NOT NULL
                 FROM document_ai_retrieval_chunks AS chunk
                 LEFT JOIN document_ai_chunk_embeddings AS embedding
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
                         chunk.created_at ASC, chunk.chunk_key ASC
                FOR UPDATE""",
            (
                get_document_ai_embedding_model(),
                EMBEDDING_VERSION,
                DOCUMENT_AI_EMBEDDING_DIMENSIONS,
                tenant_id,
                canonical_representation_id,
            ),
        )
        rows = cursor.fetchall()
        if not rows:
            raise EmbeddingProviderError(
                "canonical_retrieval_chunks_not_available_for_embedding",
                retryable=True,
            )
        chunks: list[_EmbeddingChunkPlan] = []
        for row in rows:
            embedding_text = str(row[4])
            if not embedding_text.strip():
                raise EmbeddingProviderError("openai_embedding_empty_input", retryable=False)
            chunks.append(
                _EmbeddingChunkPlan(
                    retrieval_chunk_id=UUID(str(row[0])),
                    chunk_key=str(row[1]),
                    content_hash_sha256=str(row[2]),
                    chunking_policy_version=str(row[3]),
                    embedding_text=embedding_text,
                    has_current_embedding=bool(row[5]),
                )
            )
        return _EmbeddingGenerationPlan(
            tenant_id=str(tenant_id),
            document_id=UUID(str(scope_row[0])),
            document_version_id=UUID(str(scope_row[1])),
            canonical_representation_id=UUID(str(canonical_representation_id)),
            representation_state=str(scope_row[2]),
            readiness_state=str(scope_row[3]),
            is_active=bool(scope_row[4]),
            chunks=tuple(chunks),
        )

    def _persist_embedding_generation(
        self,
        *,
        cursor: _Cursor,
        plan: _EmbeddingGenerationPlan,
        vectors_by_chunk_id: dict[UUID, tuple[float, ...]],
        allow_validated_candidate: bool,
    ) -> CanonicalEmbeddingGenerationResult:
        current_plan = self._load_embedding_plan(
            cursor=cursor,
            tenant_id=plan.tenant_id,
            canonical_representation_id=plan.canonical_representation_id,
            allow_validated_candidate=allow_validated_candidate,
        )
        if _embedding_plan_signature(current_plan) != _embedding_plan_signature(plan):
            raise EmbeddingProviderError("embedding_generation_plan_mismatch", retryable=True)
        for chunk in current_plan.chunks:
            if chunk.has_current_embedding:
                continue
            vector = vectors_by_chunk_id.get(chunk.retrieval_chunk_id)
            if vector is None:
                raise EmbeddingProviderError("openai_embedding_vector_missing", retryable=True)
            self._insert_embedding(
                cursor=cursor,
                tenant_id=current_plan.tenant_id,
                document_version_id=current_plan.document_version_id,
                canonical_representation_id=current_plan.canonical_representation_id,
                chunk=chunk,
                vector=vector,
            )
        return self._result_from_plan(
            self._load_embedding_plan(
                cursor=cursor,
                tenant_id=plan.tenant_id,
                canonical_representation_id=plan.canonical_representation_id,
                allow_validated_candidate=allow_validated_candidate,
            ),
            state="generated",
        )

    def _insert_embedding(
        self,
        *,
        cursor: _Cursor,
        tenant_id: str,
        document_version_id: UUID,
        canonical_representation_id: UUID,
        chunk: _EmbeddingChunkPlan,
        vector: tuple[float, ...],
    ) -> None:
        cursor.execute(
            """INSERT INTO document_ai_chunk_embeddings (
                   tenant_id, retrieval_chunk_id, document_version_id, canonical_representation_id,
                   content_hash_sha256, chunking_policy_version, embedding_model, embedding_version,
                   embedding_dimensions, embedding, index_state
               ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, 'active')
               ON CONFLICT (tenant_id, retrieval_chunk_id, embedding_model, embedding_version)
               DO NOTHING""",
            (
                tenant_id,
                chunk.retrieval_chunk_id,
                document_version_id,
                canonical_representation_id,
                chunk.content_hash_sha256,
                chunk.chunking_policy_version,
                get_document_ai_embedding_model(),
                EMBEDDING_VERSION,
                DOCUMENT_AI_EMBEDDING_DIMENSIONS,
                vector_literal(vector),
            ),
        )

    def _embed_missing_chunks(
        self, *, chunks: tuple[_EmbeddingChunkPlan, ...]
    ) -> dict[UUID, tuple[float, ...]]:
        client = self._embedding_client or GovernedOpenAIEmbeddingClient.from_environment()
        vectors_by_chunk_id: dict[UUID, tuple[float, ...]] = {}
        for batch in _batched(chunks, CANONICAL_EMBEDDING_BATCH_SIZE):
            batch_texts = tuple(chunk.embedding_text for chunk in batch)
            vectors = client.embed(texts=batch_texts)
            if len(vectors) != len(batch):
                raise EmbeddingProviderError("openai_embedding_count_mismatch", retryable=True)
            for chunk, vector in zip(batch, vectors, strict=True):
                vectors_by_chunk_id[chunk.retrieval_chunk_id] = _validate_embedding_vector(
                    vector,
                    expected_dimensions=DOCUMENT_AI_EMBEDDING_DIMENSIONS,
                )
        return vectors_by_chunk_id

    def _reconcile_embedding_plan(
        self,
        *,
        connection: object,
        tenant_id: str,
        canonical_representation_id: UUID,
        allow_validated_candidate: bool,
    ) -> _EmbeddingGenerationPlan | None:
        with connection.cursor() as cursor:
            return self._load_embedding_plan(
                cursor=cursor,
                tenant_id=tenant_id,
                canonical_representation_id=canonical_representation_id,
                allow_validated_candidate=allow_validated_candidate,
            )

    def _reconcile_embedding_result(
        self,
        *,
        connection: object,
        tenant_id: str,
        canonical_representation_id: UUID,
        allow_validated_candidate: bool,
    ) -> CanonicalEmbeddingGenerationResult | None:
        with connection.cursor() as cursor:
            plan = self._load_embedding_plan(
                cursor=cursor,
                tenant_id=tenant_id,
                canonical_representation_id=canonical_representation_id,
                allow_validated_candidate=allow_validated_candidate,
            )
        if plan.current_embedding_count != plan.chunk_count:
            return None
        return self._result_from_plan(plan, state="replayed")

    def _result_from_plan(
        self,
        plan: _EmbeddingGenerationPlan,
        *,
        state: str,
    ) -> CanonicalEmbeddingGenerationResult:
        return CanonicalEmbeddingGenerationResult(
            state=state,
            chunk_count=plan.chunk_count,
            current_embedding_count=plan.current_embedding_count,
            embedding_generation_identity=_embedding_generation_identity(plan),
            continuation_event=CANONICAL_EMBEDDING_CONTINUATION_EVENT,
            embedding_model=get_document_ai_embedding_model(),
            embedding_version=EMBEDDING_VERSION,
            embedding_dimensions=DOCUMENT_AI_EMBEDDING_DIMENSIONS,
            chunk_keys=tuple(chunk.chunk_key for chunk in plan.chunks),
        )


def vector_literal(vector: tuple[float, ...]) -> str:
    normalized = _validate_embedding_vector(vector)
    return "[" + ",".join(str(value) for value in normalized) + "]"


def _validate_embedding_vector(
    vector: Sequence[float],
    *,
    expected_dimensions: int | None = None,
) -> tuple[float, ...]:
    if not vector:
        raise EmbeddingProviderError("openai_embedding_empty_vector", retryable=False)
    normalized = tuple(float(value) for value in vector)
    if expected_dimensions is not None and len(normalized) != expected_dimensions:
        raise EmbeddingProviderError("openai_embedding_dimension_mismatch", retryable=False)
    if any(not isfinite(value) for value in normalized):
        raise EmbeddingProviderError("openai_embedding_non_finite_value", retryable=False)
    return normalized


def _batched(
    chunks: Sequence[_EmbeddingChunkPlan],
    batch_size: int,
) -> Iterable[tuple[_EmbeddingChunkPlan, ...]]:
    if batch_size < 1:
        raise ValueError("embedding_batch_size_required")
    for start in range(0, len(chunks), batch_size):
        yield tuple(chunks[start : start + batch_size])


def _embedding_generation_identity(plan: _EmbeddingGenerationPlan) -> str:
    payload = "|".join(
        (
            plan.tenant_id,
            str(plan.canonical_representation_id),
            get_document_ai_embedding_model(),
            EMBEDDING_VERSION,
            str(plan.chunk_count),
            ",".join(chunk.chunk_key for chunk in plan.chunks),
            ",".join(chunk.content_hash_sha256 for chunk in plan.chunks),
        )
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _embedding_plan_signature(plan: _EmbeddingGenerationPlan) -> tuple[object, ...]:
    return tuple(
        (
            str(chunk.retrieval_chunk_id),
            chunk.chunk_key,
            chunk.content_hash_sha256,
            chunk.chunking_policy_version,
            chunk.embedding_text,
        )
        for chunk in plan.chunks
    )
