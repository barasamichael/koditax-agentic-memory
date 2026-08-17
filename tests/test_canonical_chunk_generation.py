"""Deterministic canonical chunk generation coverage."""

from __future__ import annotations

from services.document_ai.app.canonical_assembly import CanonicalElement
from services.document_ai.app.canonical_chunking import build_retrieval_chunks


def _element(
    *,
    key: str,
    kind: str,
    text: str,
    order: int = 0,
    page_number: int = 1,
    source_region: dict[str, object] | None = None,
) -> CanonicalElement:
    return CanonicalElement(
        stable_key=key,
        element_type=kind,
        page_number=page_number,
        reading_order=order,
        observed_value={"text": text},
        normalized_value={"text": text},
        uncertainty={"state": "observed"},
        source_region=source_region or {"page_number": page_number},
    )


def test_chunk_generation_is_deterministic_and_carries_source_lineage() -> None:
    lineage = {
        "document_version_id": "doc-version-1",
        "source_artifact_id": "source-artifact-1",
        "provider_result_id": "provider-result-1",
        "processing_operation_id": "processing-operation-1",
    }
    chunks_one = build_retrieval_chunks(
        elements=(
            _element(key="heading", kind="heading", text="Income summary"),
            _element(
                key="paragraph",
                kind="paragraph",
                text="Salary paid in July.",
                order=1,
                source_region={
                    "page_number": 1,
                    "line_start": 4,
                    "line_end": 4,
                },
            ),
        ),
        source_lineage=lineage,
    )
    chunks_two = build_retrieval_chunks(
        elements=(
            _element(key="heading", kind="heading", text="Income summary"),
            _element(
                key="paragraph",
                kind="paragraph",
                text="Salary paid in July.",
                order=1,
                source_region={
                    "page_number": 1,
                    "line_start": 4,
                    "line_end": 4,
                },
            ),
        ),
        source_lineage=lineage,
    )

    assert chunks_one == chunks_two
    assert len(chunks_one) == 1
    chunk = chunks_one[0]
    assert chunk.chunk_ordinal == 0
    assert chunk.source_lineage == lineage
    assert chunk.structural_context["chunk_ordinal"] == 0
    assert chunk.structural_context["source_lineage"] == lineage
    assert chunk.source_location["page_number"] == 1
    assert chunk.source_location["line_start"] == 4
    assert chunk.source_location["line_end"] == 4


def test_large_chunk_generation_splits_deterministically() -> None:
    chunks = build_retrieval_chunks(
        elements=tuple(
            _element(
                key=f"paragraph-{index}",
                kind="paragraph",
                text=f"Line {index} " + ("x" * 80),
                order=index,
                source_region={"page_number": 1, "line_start": index + 1, "line_end": index + 1},
            )
            for index in range(12)
        ),
        max_chunk_characters=220,
        max_chunk_elements=3,
    )

    assert len(chunks) >= 4
    assert [chunk.chunk_ordinal for chunk in chunks] == list(range(len(chunks)))
    assert all(chunk.content_hash for chunk in chunks)
    assert all(chunk.chunk_key for chunk in chunks)
    assert all(chunk.source_location["page_number"] == 1 for chunk in chunks)
    assert all("source_regions" in chunk.source_location or "line_start" in chunk.source_location for chunk in chunks)
