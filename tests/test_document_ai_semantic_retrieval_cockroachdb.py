"""CockroachDB-backed semantic retrieval coverage for Document AI."""

from __future__ import annotations

from uuid import uuid4
from pathlib import Path

from dotenv import load_dotenv
import pytest
import psycopg

from services.document_ai.app.openai_embeddings import vector_literal
from services.document_ai.app.openai_embeddings import DOCUMENT_AI_EMBEDDING_DIMENSIONS
from services.document_ai.app.semantic_retrieval import SemanticRetrievalRequest
from services.document_ai.app.semantic_retrieval import build_semantic_retrieval_query
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


def test_semantic_retrieval_query_uses_the_native_vector_index(
    cockroach_document_ai_database: str,
) -> None:
    query_vector = vector_literal(tuple(0.0 for _ in range(DOCUMENT_AI_EMBEDDING_DIMENSIONS)))
    query, parameters = build_semantic_retrieval_query(
        tenant_id="tenant-a",
        owner_user_id=uuid4(),
        request=SemanticRetrievalRequest(query="employment income", limit=5),
        query_vector=query_vector,
    )

    with psycopg.connect(cockroach_document_ai_database) as connection:
        with connection.cursor() as cursor:
            cursor.execute(f"EXPLAIN {query}", parameters)
            plan = "\n".join(str(row[0]) for row in cursor.fetchall())

    assert "top-k" in plan.lower()
    assert "semantic_distance" in plan
    assert "semantic_distance" in query
    assert "document_ai_chunk_embeddings" in query
