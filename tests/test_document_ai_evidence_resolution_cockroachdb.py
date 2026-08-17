"""Live CockroachDB coverage for Document AI evidence resolution."""

from __future__ import annotations

import json
from uuid import UUID
from uuid import uuid4
from typing import cast
from pathlib import Path

from dotenv import load_dotenv
import pytest
import psycopg

from services.document_ai.app.hybrid_retrieval import HybridRetrievalCandidate
from services.document_ai.migrations.cockroachdb import runner
from services.document_ai.app.persistence_support import load_document_ai_database_url
from services.document_ai.app.document_evidence_resolution import (
    DocumentAIEvidenceResolutionRepository,
)

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


def test_evidence_resolution_keeps_same_text_distinct_when_source_locations_differ(
    cockroach_document_ai_database: str,
) -> None:
    seed_a = _seed_document_authority(
        database_url=cockroach_document_ai_database,
        tenant_id=f"tenant-{uuid4().hex[:8]}",
        display_name="Doc A",
        document_label="doc-a",
        stable_key="salary-a",
        page_number=1,
        reading_order=0,
        text="KSh 120,000",
        effective_value="120000",
    )
    seed_b = _seed_document_authority(
        database_url=cockroach_document_ai_database,
        tenant_id=seed_a["tenant_id"],
        display_name="Doc B",
        document_label="doc-b",
        stable_key="salary-b",
        page_number=2,
        reading_order=0,
        text="KSh 120,000",
        effective_value="120000",
    )

    resolver = DocumentAIEvidenceResolutionRepository(database_url=cockroach_document_ai_database)
    result = resolver.resolve_hybrid_candidates(
        tenant_id=seed_a["tenant_id"],
        candidates=[
            _candidate_from_seed(seed_a, fusion_rank=1, fusion_score=0.96),
            _candidate_from_seed(seed_b, fusion_rank=2, fusion_score=0.94),
        ],
    )

    assert result.document_ids == tuple(
        sorted([str(seed_a["document_id"]), str(seed_b["document_id"])])
    )
    assert len(result.evidence_items) == 2
    assert result.conflicts == ()
    assert {item.evidence_state for item in result.evidence_items} == {"current"}
    assert len(
        {
            item.provenance["source_location"]
            for item in result.evidence_items
        }
    ) == 2
    assert len({item.effective_value for item in result.evidence_items}) == 1


def test_evidence_resolution_collapses_overlapping_chunks_from_the_same_source_region(
    cockroach_document_ai_database: str,
) -> None:
    seed = _seed_document_authority(
        database_url=cockroach_document_ai_database,
        tenant_id=f"tenant-{uuid4().hex[:8]}",
        display_name="Overlap Doc",
        document_label="doc-overlap",
        stable_key="employment-income",
        page_number=1,
        reading_order=0,
        text="KSh 220,000",
        effective_value="220000",
    )
    first = _candidate_from_seed(seed, fusion_rank=1, fusion_score=0.99)
    second = _candidate_from_seed(seed, fusion_rank=2, fusion_score=0.95)
    second = second.model_copy(
        update={
            "retrieval_chunk_id": uuid4(),
            "chunk_key": f"{seed['chunk_key']}-alt",
            "fusion_rank": 2,
            "fusion_score": 0.94,
        }
    )

    resolver = DocumentAIEvidenceResolutionRepository(database_url=cockroach_document_ai_database)
    result = resolver.resolve_hybrid_candidates(
        tenant_id=seed["tenant_id"],
        candidates=[first, second],
    )

    assert len(result.evidence_items) == 1
    assert result.evidence_items[0].retrieval_chunk_ids == (
        str(first.retrieval_chunk_id),
        str(second.retrieval_chunk_id),
    )
    assert result.evidence_items[0].conflict_state == "none"


def test_evidence_resolution_marks_current_corrections_and_excludes_historical_versions(
    cockroach_document_ai_database: str,
) -> None:
    tenant_id = f"tenant-{uuid4().hex[:8]}"
    seed_current = _seed_document_authority(
        database_url=cockroach_document_ai_database,
        tenant_id=tenant_id,
        display_name="Current Version",
        document_label="doc-current",
        document_id=uuid4(),
        document_version_id=uuid4(),
        version_number=2,
        stable_key="gross-pay",
        page_number=1,
        reading_order=0,
        text="KSh 180,000",
        effective_value="190000",
        corrected_value="190000",
        correction_state="active",
    )
    seed_historical = _seed_document_authority(
        database_url=cockroach_document_ai_database,
        tenant_id=tenant_id,
        display_name="Historical Version",
        document_label="doc-current",
        document_id=cast(UUID, seed_current["document_id"]),
        document_version_id=uuid4(),
        version_number=1,
        stable_key="gross-pay",
        page_number=1,
        reading_order=0,
        text="KSh 180,000",
        effective_value="180000",
        activate_document_version=False,
    )
    with psycopg.connect(cockroach_document_ai_database) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE document_ai_documents
                   SET active_document_version_id = %s
                 WHERE tenant_id = %s
                   AND document_id = %s
                """,
                (
                    seed_current["document_version_id"],
                    tenant_id,
                    seed_current["document_id"],
                ),
            )

    resolver = DocumentAIEvidenceResolutionRepository(database_url=cockroach_document_ai_database)
    current_only = resolver.resolve_hybrid_candidates(
        tenant_id=tenant_id,
        candidates=[
            _candidate_from_seed(seed_current, fusion_rank=1, fusion_score=0.99),
            _candidate_from_seed(seed_historical, fusion_rank=2, fusion_score=0.91),
        ],
    )
    assert len(current_only.evidence_items) == 1
    assert current_only.evidence_items[0].evidence_state == "current"
    assert current_only.evidence_items[0].correction_state == "corrected"
    assert current_only.evidence_items[0].effective_value == "190000"
    assert "inactive_document_version_excluded" in current_only.diagnostics

    with_historical = resolver.resolve_hybrid_candidates(
        tenant_id=tenant_id,
        candidates=[
            _candidate_from_seed(seed_current, fusion_rank=1, fusion_score=0.99),
            _candidate_from_seed(seed_historical, fusion_rank=2, fusion_score=0.91),
        ],
        include_historical=True,
    )
    assert len(with_historical.evidence_items) == 2
    assert {item.evidence_state for item in with_historical.evidence_items} == {
        "current",
        "historical",
    }
    assert with_historical.conflicts
    assert with_historical.conflicts[0].evidence_item_ids == tuple(
        sorted(item.evidence_item_id for item in with_historical.evidence_items)
    )


def _seed_document_authority(
    *,
    database_url: str,
    tenant_id: str,
    display_name: str,
    document_label: str,
    stable_key: str,
    page_number: int,
    reading_order: int,
    text: str,
    effective_value: object,
    corrected_value: object | None = None,
    correction_state: str = "original",
    document_id: UUID | None = None,
    version_number: int | None = None,
    document_version_id: UUID | None = None,
    activate_document_version: bool = True,
) -> dict[str, object]:
    document_id = document_id or uuid4()
    document_version_id = document_version_id or uuid4()
    version_number = version_number or 1
    source_artifact_id = uuid4()
    processing_operation_id = uuid4()
    canonical_representation_id = uuid4()
    canonical_element_id = uuid4()
    source_region_id = uuid4()
    checksum = "0" * 64
    artifact_storage_key = f"{document_label}-v{version_number}.txt"
    source_location = {"page_number": page_number}
    source_partition_key = f"page:{page_number}"
    value_payload = json.dumps({"text": text}, sort_keys=True)
    representation_payload = json.dumps({"source_label": document_label}, sort_keys=True)
    validation_report = json.dumps({"reason_codes": []}, sort_keys=True)

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO document_ai_documents (
                    document_id, tenant_id, owner_user_id, state, storage_key,
                    uploaded_at, checksum_sha256, size_bytes, content_type, computation_id,
                    purge_eligible_at, purged_at, compliance_lock_until, display_name,
                    category, tags, description, revision, registry_revision,
                    active_document_version_id
                ) VALUES (
                    %s, %s, %s, 'active', %s, now(), %s, 1, 'text/plain', NULL,
                    NULL, NULL, NULL, %s, NULL, '[]'::jsonb, NULL, 1, 1, NULL
                )
                ON CONFLICT (tenant_id, document_id) DO NOTHING
                """,
                (
                    document_id,
                    tenant_id,
                    uuid4(),
                    f"{document_label}.txt",
                    checksum,
                    display_name,
                ),
            )
            cursor.execute(
                """
                INSERT INTO document_ai_document_versions (
                    document_version_id, tenant_id, document_id, version_number,
                    version_state, created_at, supersedes_document_version_id, idempotency_key
                ) VALUES (%s, %s, %s, %s, 'current', now(), NULL, %s)
                ON CONFLICT (tenant_id, document_id, version_number) DO NOTHING
                """,
                (
                    document_version_id,
                    tenant_id,
                    document_id,
                    version_number,
                    f"idem-{document_label}-{version_number}",
                ),
            )
            if activate_document_version:
                cursor.execute(
                    """
                    UPDATE document_ai_documents
                       SET active_document_version_id = %s
                     WHERE tenant_id = %s
                       AND document_id = %s
                    """,
                    (document_version_id, tenant_id, document_id),
                )
            cursor.execute(
                """
                INSERT INTO document_ai_source_artifacts (
                    source_artifact_id, tenant_id, document_version_id, storage_key,
                    checksum_sha256, content_type, size_bytes, retention_state,
                    integrity_state, created_at
                ) VALUES (%s, %s, %s, %s, %s, 'text/plain', 1, 'active', 'verified', now())
                ON CONFLICT (tenant_id, document_version_id) DO UPDATE SET
                    storage_key = EXCLUDED.storage_key
                """,
                (
                    source_artifact_id,
                    tenant_id,
                    document_version_id,
                    artifact_storage_key,
                    checksum,
                ),
            )
            cursor.execute(
                """
                INSERT INTO document_ai_processing_operations (
                    processing_operation_id, tenant_id, document_version_id, operation_kind,
                    processing_policy_version, processor_version, state, requested_at,
                    completed_at, correlation_id, idempotency_key, request_payload,
                    cancellation_requested_at, cancellation_requested_by_user_id,
                    result_reference, failure_category
                ) VALUES (
                    %s, %s, %s, 'seed', 'v1', 'fixture', 'succeeded', now(), now(),
                    %s, %s, '{}'::jsonb, NULL, NULL, 'seed', NULL
                )
                ON CONFLICT (tenant_id, processing_operation_id) DO NOTHING
                """,
                (
                    processing_operation_id,
                    tenant_id,
                    document_version_id,
                    f"corr-{document_label}-v{version_number}",
                    f"idem-{document_label}-v{version_number}",
                ),
            )
            cursor.execute(
                """
                INSERT INTO document_ai_canonical_representations (
                    canonical_representation_id, tenant_id, document_version_id,
                    processing_operation_id, canonical_schema_version,
                    processing_policy_family, state, is_active, representation_payload,
                    created_at, activated_at, source_artifact_id, provider_result_id,
                    assembly_policy_version, content_hash_sha256, canonical_validation_version,
                    validation_report, readiness_state, validated_at, rejected_at
                ) VALUES (
                    %s, %s, %s, %s, 'v1', 'fixture', 'active', TRUE, %s::jsonb,
                    now(), now(), %s, NULL, 'v1', %s, 'v1', %s::jsonb, 'full', now(), NULL
                )
                ON CONFLICT (tenant_id, canonical_representation_id) DO NOTHING
                """,
                (
                    canonical_representation_id,
                    tenant_id,
                    document_version_id,
                    processing_operation_id,
                    representation_payload,
                    source_artifact_id,
                    "1" * 64,
                    validation_report,
                ),
            )
            cursor.execute(
                """
                INSERT INTO document_ai_canonical_elements (
                    canonical_element_id, tenant_id, canonical_representation_id,
                    parent_element_id, element_type, ordinal, observed_value,
                    normalized_value, uncertainty, created_at, stable_key,
                    page_number, reading_order
                ) VALUES (
                    %s, %s, %s, NULL, 'paragraph', 0, %s::jsonb, %s::jsonb,
                    '{}'::jsonb, now(), %s, %s, %s
                )
                ON CONFLICT (tenant_id, canonical_representation_id, ordinal) DO NOTHING
                """,
                (
                    canonical_element_id,
                    tenant_id,
                    canonical_representation_id,
                    value_payload,
                    value_payload,
                    stable_key,
                    page_number,
                    reading_order,
                ),
            )
            cursor.execute(
                """
                INSERT INTO document_ai_source_regions (
                    source_region_id, tenant_id, source_artifact_id, canonical_element_id,
                    structural_unit_kind, structural_unit_index, region_payload, created_at
                ) VALUES (
                    %s, %s, %s, %s, 'page', %s, %s::jsonb, now()
                )
                ON CONFLICT (tenant_id, source_region_id) DO NOTHING
                """,
                (
                    source_region_id,
                    tenant_id,
                    source_artifact_id,
                    canonical_element_id,
                    page_number,
                    json.dumps(
                        {
                            "source_region": source_location,
                            "source_partition_key": source_partition_key,
                        }
                    ),
                ),
            )
            cursor.execute(
                """
                INSERT INTO document_ai_effective_values (
                    tenant_id, canonical_element_id, source_observed_value,
                    original_interpreted_value, corrected_value, effective_value,
                    active_correction_id, correction_state, updated_at
                ) VALUES (
                    %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, NULL, %s, now()
                )
                ON CONFLICT (tenant_id, canonical_element_id) DO UPDATE SET
                    corrected_value = EXCLUDED.corrected_value,
                    effective_value = EXCLUDED.effective_value,
                    active_correction_id = EXCLUDED.active_correction_id,
                    correction_state = EXCLUDED.correction_state,
                    updated_at = now()
                """,
                (
                    tenant_id,
                    canonical_element_id,
                    value_payload,
                    value_payload,
                    (
                        json.dumps(corrected_value, sort_keys=True)
                        if corrected_value is not None
                        else None
                    ),
                    json.dumps(effective_value, sort_keys=True),
                    "corrected" if correction_state == "active" else "original",
                ),
            )
            if correction_state == "active":
                cursor.execute(
                    """
                    INSERT INTO document_ai_corrections (
                        correction_id, tenant_id, document_version_id, canonical_element_id,
                        evidence_item_id, supersedes_correction_id, reversal_of_correction_id,
                        prior_observed_value, prior_normalized_value, corrected_value, reason,
                        actor_user_id, correction_state, idempotency_key, source_observed_value,
                        original_interpreted_value, effective_value, policy_version, created_at,
                        updated_at
                    ) VALUES (
                        %s, %s, %s, %s, NULL, NULL, NULL, %s::jsonb, %s::jsonb, %s::jsonb,
                        'fixture correction', %s, 'active', %s, %s::jsonb, %s::jsonb,
                        %s::jsonb, 'v1', now(), now()
                    )
                    ON CONFLICT (tenant_id, correction_id) DO NOTHING
                    """,
                    (
                        uuid4(),
                        tenant_id,
                        document_version_id,
                        canonical_element_id,
                        value_payload,
                        value_payload,
                        json.dumps(
                            corrected_value if corrected_value is not None else effective_value,
                            sort_keys=True,
                        ),
                        uuid4(),
                        f"idem-correction-{document_label}",
                        value_payload,
                        value_payload,
                        json.dumps(
                            corrected_value if corrected_value is not None else effective_value,
                            sort_keys=True,
                        ),
                    ),
                )
        connection.commit()

    return {
        "tenant_id": tenant_id,
        "document_id": document_id,
        "document_version_id": document_version_id,
        "canonical_representation_id": canonical_representation_id,
        "canonical_element_id": canonical_element_id,
        "source_artifact_id": source_artifact_id,
        "chunk_key": f"{document_label}-chunk",
        "stable_key": stable_key,
        "source_location": source_location,
        "source_partition_key": source_partition_key,
        "effective_value": effective_value,
    }


def _candidate_from_seed(
    seed: dict[str, object],
    *,
    fusion_rank: int,
    fusion_score: float,
) -> HybridRetrievalCandidate:
    return HybridRetrievalCandidate(
        retrieval_chunk_id=uuid4(),
        document_id=cast(UUID, seed["document_id"]),
        document_version_id=cast(UUID, seed["document_version_id"]),
        canonical_representation_id=cast(UUID, seed["canonical_representation_id"]),
        chunk_key=str(seed["chunk_key"]),
        content_hash_sha256="f" * 64,
        chunking_policy_version="v2",
        canonical_element_keys=(str(seed["stable_key"]),),
        source_location=dict(cast(dict[str, object], seed["source_location"])),
        structural_context={
            "page_number": cast(dict[str, object], seed["source_location"]).get("page_number"),
            "source_partition_key": seed["source_partition_key"],
        },
        source_lineage={
            "document_version_id": str(seed["document_version_id"]),
            "source_artifact_id": str(seed["source_artifact_id"]),
            "canonical_representation_id": str(seed["canonical_representation_id"]),
        },
        source_filename=f"{seed['document_id']}.txt",
        display_name=None,
        semantic_distance=None,
        semantic_score=None,
        exact_match_rank=None,
        retrieval_methods=["exact", "semantic"],
        fusion_rank=fusion_rank,
        fusion_score=fusion_score,
    )
