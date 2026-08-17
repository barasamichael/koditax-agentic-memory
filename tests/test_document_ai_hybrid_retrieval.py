"""Milestone 46 hybrid retrieval over exact and semantic candidates."""

from __future__ import annotations

from uuid import UUID
from uuid import uuid4
from pathlib import Path

from dotenv import load_dotenv
import pytest
import psycopg
from pydantic import ValidationError

from services.document_ai.app.exact_retrieval import ExactRetrievalCandidate
from services.document_ai.app.hybrid_retrieval import HybridRetrievalRequest
from services.document_ai.app.hybrid_retrieval import HybridRetrievalRepository
from services.document_ai.app.openai_embeddings import EmbeddingProviderError
from services.document_ai.app.semantic_retrieval import SemanticRetrievalEnvelope
from services.document_ai.app.semantic_retrieval import SemanticRetrievalCandidate
from services.document_ai.migrations.cockroachdb import runner
from services.document_ai.app.persistence_support import load_document_ai_database_url

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


@pytest.fixture(scope="session")
def cockroach_document_ai_database() -> str:
    database_url = load_document_ai_database_url()
    if not database_url:
        pytest.skip("DATABASE_URL is not configured for Document AI CockroachDB tests.")

    with psycopg.connect(database_url) as connection:
        try:
            runner._validate_target_database(connection)  # type: ignore[arg-type]
        except runner.DocumentAITargetError:
            pytest.skip("DATABASE_URL does not target the expected CockroachDB kodi_dev database.")

    assert runner.main() == 0
    return database_url


class _ExactRepositoryStub:
    def __init__(
        self,
        *,
        candidates: list[ExactRetrievalCandidate] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.candidates = candidates or []
        self.error = error
        self.calls: list[dict[str, object]] = []

    def retrieve(
        self,
        *,
        tenant_id: str,
        owner_user_id: UUID,
        request: object,
    ) -> list[ExactRetrievalCandidate]:
        self.calls.append(
            {"tenant_id": tenant_id, "owner_user_id": owner_user_id, "request": request}
        )
        if self.error is not None:
            raise self.error
        return list(self.candidates)


class _SemanticRepositoryStub:
    def __init__(
        self,
        *,
        candidates: list[SemanticRetrievalCandidate] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.candidates = candidates or []
        self.error = error
        self.calls: list[dict[str, object]] = []

    def retrieve(
        self,
        *,
        tenant_id: str,
        owner_user_id: UUID,
        request: object,
    ) -> SemanticRetrievalEnvelope:
        self.calls.append(
            {"tenant_id": tenant_id, "owner_user_id": owner_user_id, "request": request}
        )
        if self.error is not None:
            raise self.error
        return SemanticRetrievalEnvelope(candidates=list(self.candidates))


def _exact_candidate(
    *,
    retrieval_chunk_id: UUID,
    document_id: UUID,
    document_version_id: UUID,
    canonical_representation_id: UUID,
    chunk_key: str,
    exact_match_rank: int,
    source_filename: str,
    source_lineage: dict[str, object] | None = None,
) -> ExactRetrievalCandidate:
    return ExactRetrievalCandidate(
        retrieval_chunk_id=retrieval_chunk_id,
        document_id=document_id,
        document_version_id=document_version_id,
        canonical_representation_id=canonical_representation_id,
        chunk_key=chunk_key,
        content_hash_sha256="f" * 64,
        chunking_policy_version="v2",
        exact_match_rank=exact_match_rank,
        canonical_element_keys=("amount",),
        source_lineage=source_lineage or {},
        source_location={"page_number": 1},
        structural_context={
            "page_number": 1,
            "source_lineage": source_lineage or {},
        },
        display_name="Hybrid Retrieval Fixture",
        source_filename=source_filename,
    )


def _semantic_candidate(
    *,
    retrieval_chunk_id: UUID,
    document_id: UUID,
    document_version_id: UUID,
    canonical_representation_id: UUID,
    chunk_key: str,
    semantic_distance: float,
    semantic_score: float,
    source_filename: str,
    source_lineage: dict[str, object] | None = None,
) -> SemanticRetrievalCandidate:
    return SemanticRetrievalCandidate(
        retrieval_chunk_id=retrieval_chunk_id,
        document_id=document_id,
        document_version_id=document_version_id,
        canonical_representation_id=canonical_representation_id,
        chunk_key=chunk_key,
        content_hash_sha256="f" * 64,
        chunking_policy_version="v2",
        semantic_distance=semantic_distance,
        semantic_score=semantic_score,
        canonical_element_keys=("amount",),
        source_location={"page_number": 1},
        structural_context={
            "page_number": 1,
            "source_lineage": source_lineage or {},
        },
        source_filename=source_filename,
        display_name="Hybrid Retrieval Fixture",
    )


def test_hybrid_retrieval_fuses_branch_signals_and_keeps_distinct_ids() -> None:
    dual_chunk_id = uuid4()
    exact_only_chunk_id = uuid4()
    semantic_only_chunk_id = uuid4()
    document_id = uuid4()
    document_version_id = uuid4()
    canonical_representation_id = uuid4()
    source_lineage = {
        "document_version_id": str(document_version_id),
        "canonical_representation_id": str(canonical_representation_id),
    }

    exact_stub = _ExactRepositoryStub(
        candidates=[
            _exact_candidate(
                retrieval_chunk_id=dual_chunk_id,
                document_id=document_id,
                document_version_id=document_version_id,
                canonical_representation_id=canonical_representation_id,
                chunk_key="chunk-0",
                exact_match_rank=0,
                source_filename="fixture.txt",
                source_lineage=source_lineage,
            ),
            _exact_candidate(
                retrieval_chunk_id=exact_only_chunk_id,
                document_id=document_id,
                document_version_id=document_version_id,
                canonical_representation_id=canonical_representation_id,
                chunk_key="chunk-1",
                exact_match_rank=1,
                source_filename="fixture.txt",
                source_lineage=source_lineage,
            ),
        ]
    )
    semantic_stub = _SemanticRepositoryStub(
        candidates=[
            _semantic_candidate(
                retrieval_chunk_id=dual_chunk_id,
                document_id=document_id,
                document_version_id=document_version_id,
                canonical_representation_id=canonical_representation_id,
                chunk_key="chunk-0",
                semantic_distance=0.05,
                semantic_score=0.95,
                source_filename="fixture.txt",
                source_lineage=source_lineage,
            ),
            _semantic_candidate(
                retrieval_chunk_id=semantic_only_chunk_id,
                document_id=document_id,
                document_version_id=document_version_id,
                canonical_representation_id=canonical_representation_id,
                chunk_key="chunk-2",
                semantic_distance=0.20,
                semantic_score=0.80,
                source_filename="fixture.txt",
                source_lineage=source_lineage,
            ),
        ]
    )

    repository = HybridRetrievalRepository(
        database_url="postgresql://not-used",
        exact_retrieval_repository=exact_stub,  # type: ignore[arg-type]
        semantic_retrieval_repository=semantic_stub,  # type: ignore[arg-type]
    )
    result = repository.retrieve(
        tenant_id="tenant-a",
        owner_user_id=uuid4(),
        request=HybridRetrievalRequest(query="employment income", limit=2),
    )

    assert exact_stub.calls[0]["request"].full_text == "employment income"
    assert exact_stub.calls[0]["request"].limit == 4
    assert semantic_stub.calls[0]["request"].limit == 4
    assert [candidate.retrieval_chunk_id for candidate in result.candidates] == [
        dual_chunk_id,
        exact_only_chunk_id,
    ]
    assert result.candidates[0].retrieval_methods == ["exact", "semantic"]
    assert result.candidates[0].exact_match_rank == 0
    assert result.candidates[0].semantic_distance == 0.05
    assert result.candidates[1].retrieval_methods == ["exact"]
    assert result.candidates[1].retrieval_chunk_id == exact_only_chunk_id
    assert result.candidates[1].source_lineage == source_lineage


def test_hybrid_retrieval_does_not_deduplicate_by_text_value() -> None:
    first_chunk_id = uuid4()
    second_chunk_id = uuid4()
    document_id = uuid4()
    document_version_id = uuid4()
    canonical_representation_id = uuid4()

    exact_stub = _ExactRepositoryStub(
        candidates=[
            _exact_candidate(
                retrieval_chunk_id=first_chunk_id,
                document_id=document_id,
                document_version_id=document_version_id,
                canonical_representation_id=canonical_representation_id,
                chunk_key="chunk-0",
                exact_match_rank=0,
                source_filename="fixture.txt",
            )
        ]
    )
    semantic_stub = _SemanticRepositoryStub(
        candidates=[
            _semantic_candidate(
                retrieval_chunk_id=second_chunk_id,
                document_id=document_id,
                document_version_id=document_version_id,
                canonical_representation_id=canonical_representation_id,
                chunk_key="chunk-1",
                semantic_distance=0.12,
                semantic_score=0.88,
                source_filename="fixture.txt",
            )
        ]
    )
    repository = HybridRetrievalRepository(
        database_url="postgresql://not-used",
        exact_retrieval_repository=exact_stub,  # type: ignore[arg-type]
        semantic_retrieval_repository=semantic_stub,  # type: ignore[arg-type]
    )

    result = repository.retrieve(
        tenant_id="tenant-a",
        owner_user_id=uuid4(),
        request=HybridRetrievalRequest(query="shared text", limit=5),
    )

    assert [candidate.retrieval_chunk_id for candidate in result.candidates] == [
        first_chunk_id,
        second_chunk_id,
    ]
    assert result.candidates[0].fusion_rank == 1
    assert result.candidates[1].fusion_rank == 2


def test_hybrid_retrieval_propagates_exact_branch_failures_without_fallback() -> None:
    exact_stub = _ExactRepositoryStub(error=RuntimeError("document_ai_exact_retrieval_unavailable"))
    semantic_stub = _SemanticRepositoryStub()
    repository = HybridRetrievalRepository(
        database_url="postgresql://not-used",
        exact_retrieval_repository=exact_stub,  # type: ignore[arg-type]
        semantic_retrieval_repository=semantic_stub,  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="document_ai_exact_retrieval_unavailable"):
        repository.retrieve(
            tenant_id="tenant-a",
            owner_user_id=uuid4(),
            request=HybridRetrievalRequest(query="employment income"),
        )
    assert semantic_stub.calls == []


def test_hybrid_retrieval_propagates_semantic_branch_failures_without_fallback() -> None:
    exact_stub = _ExactRepositoryStub()
    semantic_stub = _SemanticRepositoryStub(
        error=EmbeddingProviderError("openai_embedding_timeout", retryable=True)
    )
    repository = HybridRetrievalRepository(
        database_url="postgresql://not-used",
        exact_retrieval_repository=exact_stub,  # type: ignore[arg-type]
        semantic_retrieval_repository=semantic_stub,  # type: ignore[arg-type]
    )

    with pytest.raises(EmbeddingProviderError, match="openai_embedding_timeout"):
        repository.retrieve(
            tenant_id="tenant-a",
            owner_user_id=uuid4(),
            request=HybridRetrievalRequest(query="employment income"),
        )
    assert exact_stub.calls


def test_hybrid_contract_exposes_fused_candidates_in_openapi() -> None:
    contract = Path("contracts/openapi/document_ai.yaml").read_text(encoding="utf-8")
    assert "/v1/document-evidence/hybrid-retrievals" in contract
    assert "HybridRetrievalCandidate" in contract
    assert "retrieval_methods" in contract
    assert "fusion_score" in contract
    assert "exact_match_rank" in contract


def test_real_hybrid_retrieval_returns_fused_dual_hit_candidates(
    cockroach_document_ai_database: str,
) -> None:
    from tests.test_document_ai_exact_retrieval_cockroachdb import _seed_exact_retrieval_fixture

    fixture = _seed_exact_retrieval_fixture(database_url=cockroach_document_ai_database)
    repository = HybridRetrievalRepository(database_url=cockroach_document_ai_database)
    request = HybridRetrievalRequest(query=fixture["chunk_text"], limit=5)

    try:
        result = repository.retrieve(
            tenant_id=str(fixture["tenant_id"]),
            owner_user_id=fixture["owner_user_id"],
            request=request,
        )
    except EmbeddingProviderError as error:
        if error.reason == "missing_openai_embedding_configuration":
            pytest.skip("OpenAI embedding configuration is unavailable for hybrid retrieval.")
        raise

    assert result.candidates
    candidate = result.candidates[0]
    assert candidate.document_id == fixture["document_id"]
    assert candidate.document_version_id == fixture["document_version_id"]
    assert candidate.exact_match_rank == 0
    assert candidate.semantic_distance is not None
    assert candidate.semantic_score is not None
    assert candidate.retrieval_methods == ["exact", "semantic"]
    assert candidate.fusion_rank == 1
    assert candidate.source_lineage["document_version_id"] == str(fixture["document_version_id"])

    cross_tenant = repository.retrieve(
        tenant_id=f"{fixture['tenant_id']}-other",
        owner_user_id=fixture["owner_user_id"],
        request=request,
    )
    assert cross_tenant.candidates == []


def test_hybrid_request_rejects_empty_query_and_partial_scope() -> None:
    with pytest.raises(ValidationError, match="query must not be empty"):
        HybridRetrievalRequest(query="   ")
    with pytest.raises(ValidationError, match="supplied together"):
        HybridRetrievalRequest(query="employment income", conversation_id="conversation-1")
