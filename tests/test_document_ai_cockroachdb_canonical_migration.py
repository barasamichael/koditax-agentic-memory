"""Regression coverage for the CockroachDB canonical/retrieval migration lane."""

from __future__ import annotations

from pathlib import Path


def test_canonical_retrieval_cockroach_migration_exposes_required_state() -> None:
    migration = Path(
        "services/document_ai/migrations/cockroachdb/0004_document_ai_canonical_retrieval_persistence.sql"
    ).read_text(encoding="utf-8").lower()

    for marker in (
        "create table if not exists document_ai_canonical_representations",
        "create table if not exists document_ai_canonical_elements",
        "create table if not exists document_ai_canonical_relationships",
        "create table if not exists document_ai_source_regions",
        "create table if not exists document_ai_retrieval_chunks",
        "create table if not exists document_ai_chunk_embeddings",
        "create table if not exists document_ai_embedding_records",
        "uq_document_ai_active_canonical_representation",
        "uq_document_ai_canonical_representation_provider_result",
        "idx_document_ai_canonical_validation_readiness",
        "uq_document_ai_canonical_elements_stable_key",
        "uq_document_ai_canonical_elements_reading_order",
        "idx_document_ai_canonical_elements_representation",
        "idx_document_ai_source_regions_structural_lookup",
        "idx_document_ai_retrieval_chunks_scope",
        "idx_document_ai_retrieval_chunks_active_canonical_scope",
        "idx_document_ai_chunk_embeddings_scope",
        "chk_document_ai_embedding_records_vector",
    ):
        assert marker in migration


def test_canonical_retrieval_cockroach_migration_avoids_postgresql_vector_indexing() -> None:
    migration = Path(
        "services/document_ai/migrations/cockroachdb/0004_document_ai_canonical_retrieval_persistence.sql"
    ).read_text(encoding="utf-8").lower()

    assert "create trigger" not in migration
    assert "create or replace function" not in migration
    assert "create extension if not exists vector" not in migration
    assert "using gin" not in migration
    assert "using hnsw" not in migration


def test_chunk_embedding_vector_migration_exposes_native_cockroach_vector_index() -> None:
    migration = Path(
        "services/document_ai/migrations/cockroachdb/0007_document_ai_chunk_embeddings_vector_index.sql"
    ).read_text(encoding="utf-8").lower()

    for marker in (
        "alter table document_ai_chunk_embeddings",
        "drop constraint if exists chk_document_ai_chunk_embeddings_dimensions",
        "add constraint chk_document_ai_chunk_embeddings_dimensions",
        "check (embedding_dimensions = 1536)",
        "create vector index if not exists idx_document_ai_chunk_embeddings_vector_search",
        "tenant_id",
        "embedding_model",
        "embedding_version",
        "index_state",
        "embedding",
    ):
        assert marker in migration


def test_chunk_embedding_vector_migration_avoids_postgresql_vector_extensions() -> None:
    migration = Path(
        "services/document_ai/migrations/cockroachdb/0007_document_ai_chunk_embeddings_vector_index.sql"
    ).read_text(encoding="utf-8").lower()

    assert "create extension if not exists vector" not in migration
    assert "using hnsw" not in migration
    assert "using ivfflat" not in migration


def test_exact_retrieval_cockroach_migration_exposes_active_chunk_search_indexes() -> None:
    migration = Path(
        "services/document_ai/migrations/cockroachdb/0014_document_ai_exact_retrieval_indexes.sql"
    ).read_text(encoding="utf-8").lower()

    for marker in (
        "create index if not exists idx_document_ai_retrieval_chunks_exact_lexical",
        (
            "create inverted index if not exists "
            "idx_document_ai_retrieval_chunks_exact_source_location"
        ),
        (
            "create inverted index if not exists "
            "idx_document_ai_retrieval_chunks_exact_structural_context"
        ),
        "where lifecycle_state = 'active'",
    ):
        assert marker in migration


def test_exact_retrieval_cockroach_migration_avoids_postgresql_text_search_indexes() -> None:
    migration = Path(
        "services/document_ai/migrations/cockroachdb/0014_document_ai_exact_retrieval_indexes.sql"
    ).read_text(encoding="utf-8").lower()

    assert "create trigger" not in migration
    assert "tsvector" not in migration
    assert "tsquery" not in migration
