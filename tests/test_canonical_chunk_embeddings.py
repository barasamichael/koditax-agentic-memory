"""Milestone 18 canonical chunk and embedding-index boundaries."""

from __future__ import annotations

from pathlib import Path

import pytest

from services.document_ai.app.openai_embeddings import vector_literal
from services.document_ai.app.openai_embeddings import EmbeddingProviderError
from services.document_ai.app.canonical_assembly import CanonicalElement
from services.document_ai.app.canonical_chunking import build_retrieval_chunks


def _element(*, key: str, kind: str, text: str, order: int = 0) -> CanonicalElement:
    return CanonicalElement(
        stable_key=key,
        element_type=kind,
        page_number=1,
        reading_order=order,
        observed_value={"text": text},
        normalized_value={"text": text},
        uncertainty={"state": "observed"},
        source_region={"page_number": 1},
    )


def test_prose_chunk_retains_heading_and_exact_element_reference() -> None:
    chunks = build_retrieval_chunks(
        elements=(
            _element(key="heading", kind="heading", text="Income summary"),
            _element(key="prose", kind="paragraph", text="Salary paid in July.", order=1),
        )
    )
    assert len(chunks) == 1
    assert chunks[0].embedding_text == "Income summary\nSalary paid in July."
    assert chunks[0].canonical_element_keys == ("prose",)
    assert chunks[0].structural_context["heading"] == "Income summary"


def test_table_and_spreadsheet_cells_preserve_structural_context() -> None:
    chunks = build_retrieval_chunks(
        elements=(
            _element(key="table", kind="table", text="Month | Gross pay\nJuly | 1000"),
            _element(key="cell", kind="form_field", text="Gross pay: 1000", order=1),
        )
    )
    assert chunks[0].structural_context["content_kind"] == "table"
    assert "Month | Gross pay" in chunks[0].embedding_text
    assert chunks[1].structural_context["content_kind"] == "cell"


def test_content_hash_reuses_unchanged_content_and_changes_for_correction() -> None:
    original = build_retrieval_chunks(
        elements=(_element(key="first", kind="paragraph", text="KES 100"),)
    )[0]
    unchanged = build_retrieval_chunks(
        elements=(_element(key="second", kind="paragraph", text="KES 100"),)
    )[0]
    corrected = build_retrieval_chunks(
        elements=(_element(key="third", kind="paragraph", text="KES 120"),)
    )[0]
    assert original.content_hash == unchanged.content_hash
    assert original.content_hash != corrected.content_hash
    assert original.chunk_key != unchanged.chunk_key


def test_embedding_vector_requires_at_least_one_dimension() -> None:
    assert vector_literal((0.1, 0.2)) == "[0.1,0.2]"
    with pytest.raises(EmbeddingProviderError, match="empty_vector"):
        vector_literal(())


def test_embedding_migration_enforces_active_authority_tenant_and_lifecycle_scope() -> None:
    sql = Path("database/migrations/0044_document_ai_canonical_chunk_embeddings.sql").read_text()
    for marker in (
        "CREATE EXTENSION IF NOT EXISTS vector",
        "document_ai_retrieval_chunks",
        "document_ai_chunk_embeddings",
        "canonical_element_keys",
        "source_location",
        "structural_context",
        "content_hash_sha256",
        "chunking_policy_version",
        "trg_document_ai_retrieval_chunk_active_authority",
        "trg_document_ai_embedding_representation_scope",
        "trg_document_ai_retrieval_lifecycle_scope",
    ):
        assert marker in sql
