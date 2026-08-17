"""Milestone 16 deterministic canonical assembly coverage."""

from __future__ import annotations

from uuid import UUID
from pathlib import Path

import pytest

from services.document_ai.app.canonical_assembly import normalize_text
from services.document_ai.app.canonical_assembly import CanonicalAssemblyError
from services.document_ai.app.canonical_assembly import assemble_canonical_graph

_PROVIDER_ID = UUID("00000000-0000-0000-0000-000000000001")
_ARTIFACT_ID = UUID("00000000-0000-0000-0000-000000000002")


def _result() -> dict[str, object]:
    return {
        "result": {
            "schema_version": "v1",
            "warnings": ["  check\r\n source  "],
            "pages": [
                {
                    "page_number": 2,
                    "observations": [
                        {
                            "observation_id": "second",
                            "kind": "amount",
                            "order": 9,
                            "text": " KES\t1,000 ",
                            "state": "observed",
                            "source_location": None,
                        },
                        {
                            "observation_id": "first",
                            "kind": "heading",
                            "order": 1,
                            "text": "Cafe\u0301\r\nTitle",
                            "state": "observed",
                            "source_location": {
                                "page_number": 2,
                                "bounding_box": [1, 2, 3, 4],
                                "start_offset": 0,
                                "end_offset": 11,
                            },
                        },
                    ],
                },
                {
                    "page_number": 1,
                    "observations": [
                        {
                            "observation_id": "unknown",
                            "kind": "unknown",
                            "order": 0,
                            "text": None,
                            "state": "unreadable",
                            "source_location": None,
                        },
                    ],
                },
            ],
        }
    }


def test_assembly_is_deterministic_and_preserves_values_provenance_and_unknowns() -> None:
    first = assemble_canonical_graph(
        provider_result_id=_PROVIDER_ID, source_artifact_id=_ARTIFACT_ID, validated_result=_result()
    )
    second = assemble_canonical_graph(
        provider_result_id=_PROVIDER_ID, source_artifact_id=_ARTIFACT_ID, validated_result=_result()
    )
    assert first == second
    assert first.payload["structural_units"] == [
        {"kind": "page", "page_number": 1},
        {"kind": "page", "page_number": 2},
    ]
    assert [element.element_type for element in first.elements] == ["unknown", "heading", "money"]
    heading = first.elements[1]
    assert heading.observed_value == {"text": "Cafe\u0301\r\nTitle"}
    assert heading.normalized_value == {"text": "Café\nTitle"}
    assert heading.source_region == {
        "page_number": 2,
        "bounding_box": [1, 2, 3, 4],
        "start_offset": 0,
        "end_offset": 11,
    }
    assert first.elements[0].uncertainty == {"state": "unreadable"}
    assert first.elements[0].source_region == {"page_number": 1}


def test_assembly_preserves_source_and_element_lineage_deterministically() -> None:
    lineage = {
        "provider_result_id": str(_PROVIDER_ID),
        "source_artifact_id": str(_ARTIFACT_ID),
        "processing_operation_id": "operation-1",
        "source_inspection_id": "inspection-1",
    }
    element_lineage = {
        "unknown": {
            "provider_partition_id": "partition-1",
            "partition_identity": "partition-identity-1",
            "structural_scope_id": "scope-1",
            "scope_identity": "scope-identity-1",
        }
    }
    graph = assemble_canonical_graph(
        provider_result_id=_PROVIDER_ID,
        source_artifact_id=_ARTIFACT_ID,
        validated_result=_result(),
        source_lineage=lineage,
        element_lineage_by_observation_id=element_lineage,
    )

    assert graph.source_lineage == lineage
    assert graph.payload["source_lineage"] == lineage
    assert graph.elements[0].lineage == element_lineage["unknown"]
    assert graph == assemble_canonical_graph(
        provider_result_id=_PROVIDER_ID,
        source_artifact_id=_ARTIFACT_ID,
        validated_result=_result(),
        source_lineage=lineage,
        element_lineage_by_observation_id=element_lineage,
    )


def test_normalization_is_unicode_linebreak_and_control_character_deterministic() -> None:
    assert normalize_text("\x00 A\t B\r\nCafe\u0301 \rC ") == "A B\nCafé\nC"


def test_assembly_rejects_duplicate_source_observations() -> None:
    result = _result()
    pages = result["result"]["pages"]  # type: ignore[index]
    pages[0]["observations"].append(dict(pages[0]["observations"][0]))  # type: ignore[index]
    with pytest.raises(CanonicalAssemblyError, match="duplicate_observation"):
        assemble_canonical_graph(
            provider_result_id=_PROVIDER_ID,
            source_artifact_id=_ARTIFACT_ID,
            validated_result=result,
        )


def test_canonical_persistence_migration_keeps_source_and_provider_lineage_immutable() -> None:
    sql = (
        Path("database/migrations/0042_document_ai_canonical_representation.sql")
        .read_text()
        .lower()
    )
    for marker in (
        "provider_result_id",
        "source_artifact_id",
        "assembly_policy_version",
        "content_hash_sha256",
        "canonical_relationships",
        "uq_document_ai_canonical_representation_provider_result",
        "trg_document_ai_canonical_generation_prevent_mutation",
    ):
        assert marker in sql
