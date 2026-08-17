"""Embedding-generation boundary coverage for Document AI."""

from __future__ import annotations

from uuid import uuid4

import pytest

from services.document_ai.app import openai_embeddings
from services.document_ai.app.openai_embeddings import CanonicalEmbeddingGenerationResult
from services.document_ai.app.openai_embeddings import CanonicalEmbeddingRepository
from services.document_ai.app.openai_embeddings import EmbeddingProviderError
from services.document_ai.app.openai_embeddings import _EmbeddingChunkPlan
from services.document_ai.app.openai_embeddings import _EmbeddingGenerationPlan


def _vector(value: float) -> tuple[float, ...]:
    return tuple(value for _ in range(openai_embeddings.DOCUMENT_AI_EMBEDDING_DIMENSIONS))


def test_embedding_generation_calls_provider_outside_the_transaction_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    plan = _EmbeddingGenerationPlan(
        tenant_id="tenant-a",
        document_id=uuid4(),
        document_version_id=uuid4(),
        canonical_representation_id=uuid4(),
        representation_state="validated",
        readiness_state="full",
        is_active=False,
        chunks=(
            _EmbeddingChunkPlan(
                retrieval_chunk_id=uuid4(),
                chunk_key="chunk-1",
                content_hash_sha256="a" * 64,
                chunking_policy_version="v2",
                embedding_text="missing chunk text",
                has_current_embedding=False,
            ),
            _EmbeddingChunkPlan(
                retrieval_chunk_id=uuid4(),
                chunk_key="chunk-2",
                content_hash_sha256="b" * 64,
                chunking_policy_version="v2",
                embedding_text="already embedded",
                has_current_embedding=True,
            ),
        ),
    )

    result = CanonicalEmbeddingGenerationResult(
        state="generated",
        chunk_count=2,
        current_embedding_count=2,
        embedding_generation_identity="identity",
        continuation_event=openai_embeddings.CANONICAL_EMBEDDING_CONTINUATION_EVENT,
        embedding_model="text-embedding-3-small",
        embedding_version=openai_embeddings.EMBEDDING_VERSION,
        embedding_dimensions=openai_embeddings.DOCUMENT_AI_EMBEDDING_DIMENSIONS,
        chunk_keys=("chunk-1", "chunk-2"),
    )

    class _FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        def embed(self, *, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
            events.append("provider")
            self.calls.append(texts)
            return tuple(_vector(0.25) for _ in texts)

    fake_client = _FakeClient()
    repository = CanonicalEmbeddingRepository(
        database_url="postgresql://not-used", embedding_client=fake_client
    )

    monkeypatch.setattr(
        repository,
        "_load_embedding_plan",
        lambda **kwargs: plan,
    )
    monkeypatch.setattr(
        repository,
        "_persist_embedding_generation",
        lambda **kwargs: result,
    )

    def fake_transaction(*, transaction_name: str, transaction_callback, **kwargs):
        del kwargs
        events.append(f"{transaction_name}:begin")
        value = transaction_callback(object())
        events.append(f"{transaction_name}:end")
        return value

    monkeypatch.setattr(openai_embeddings, "execute_document_ai_database_transaction", fake_transaction)

    observed = repository.index_active_representation(
        tenant_id="tenant-a", canonical_representation_id=plan.canonical_representation_id
    )

    assert observed == result
    assert fake_client.calls == [("missing chunk text",)]
    assert events == [
        "document_ai.openai_embeddings.prepare_embedding_generation:begin",
        "document_ai.openai_embeddings.prepare_embedding_generation:end",
        "provider",
        "document_ai.openai_embeddings.persist_embedding_generation:begin",
        "document_ai.openai_embeddings.persist_embedding_generation:end",
    ]


def test_embedding_generation_skips_provider_when_every_chunk_is_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _EmbeddingGenerationPlan(
        tenant_id="tenant-a",
        document_id=uuid4(),
        document_version_id=uuid4(),
        canonical_representation_id=uuid4(),
        representation_state="active",
        readiness_state="full",
        is_active=True,
        chunks=(
            _EmbeddingChunkPlan(
                retrieval_chunk_id=uuid4(),
                chunk_key="chunk-1",
                content_hash_sha256="a" * 64,
                chunking_policy_version="v2",
                embedding_text="already embedded",
                has_current_embedding=True,
            ),
        ),
    )
    repository = CanonicalEmbeddingRepository(database_url="postgresql://not-used")

    monkeypatch.setattr(
        repository,
        "_load_embedding_plan",
        lambda **kwargs: plan,
    )

    def fail_if_called(*args: object, **kwargs: object) -> tuple[tuple[float, ...], ...]:
        del args, kwargs
        raise AssertionError("provider should not be called when all chunks are current")

    monkeypatch.setattr(repository, "_embed_missing_chunks", fail_if_called)
    monkeypatch.setattr(
        openai_embeddings,
        "execute_document_ai_database_transaction",
        lambda **kwargs: kwargs["transaction_callback"](object()),
    )

    observed = repository.index_active_representation(
        tenant_id="tenant-a", canonical_representation_id=plan.canonical_representation_id
    )

    assert observed.state == "replayed"
    assert observed.chunk_count == 1
    assert observed.current_embedding_count == 1


def test_embedding_vector_literal_rejects_non_finite_values() -> None:
    with pytest.raises(EmbeddingProviderError, match="non_finite"):
        openai_embeddings.vector_literal((1.0, float("nan")))
