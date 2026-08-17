from __future__ import annotations

from fastapi.testclient import TestClient

from tests.knowledge.support import response_json
from tests.knowledge.support import require_object
from tests.knowledge.support import admin_auth_headers
from tests.knowledge.support import require_object_list


def test_source_detail_views_preserve_retention_and_lifecycle_visibility(
    client: TestClient,
) -> None:
    published = _publish_document_backed_source(
        client=client,
        seed="detail-doc",
        source_id="KNW-DETAIL-001",
        source_family_id="KNW-DETAIL-FAMILY-001",
    )

    source_response = client.get(
        f"/knowledge/sources/{published['source_id']}",
        headers=admin_auth_headers("source-detail"),
    )
    version_response = client.get(
        f"/knowledge/source-versions/{published['source_version_id']}",
        headers=admin_auth_headers("version-detail"),
    )

    source_payload = response_json(source_response)
    version_payload = response_json(version_response)
    source_result = require_object(source_payload["result"])
    version_result = require_object(version_payload["result"])
    versions = require_object_list(source_result["versions"])
    retention_summary = require_object(source_result["retention_summary"])

    assert source_response.status_code == 200
    assert source_result["source_id"] == published["source_id"]
    assert source_result["source_family_id"] == published["source_family_id"]
    assert source_result["version_count"] == 1
    assert source_result["anchor_count"] == 1
    assert source_result["chunk_count"] == 1
    assert versions[0]["source_version_id"] == published["source_version_id"]
    assert retention_summary["lineage_preserved"] is True
    assert retention_summary["has_document_lineage"] is True
    assert retention_summary["has_legacy_import_lineage"] is False
    assert retention_summary["has_url_lineage"] is False
    assert retention_summary["purge_supported"] is False

    assert version_response.status_code == 200
    assert version_result["source_version_id"] == published["source_version_id"]
    assert version_result["publication_state"] == "published"
    assert version_result["source_input_origin"] == "official_source_upload"


def test_anchor_detail_exposes_chunk_summaries_only(client: TestClient) -> None:
    published = _publish_document_backed_source(
        client=client,
        seed="detail-anchor",
        source_id="KNW-DETAIL-ANCHOR-001",
        source_family_id="KNW-DETAIL-ANCHOR-FAMILY-001",
    )

    response = client.get(
        f"/knowledge/anchors/{published['anchor_id']}",
        headers=admin_auth_headers("anchor-detail"),
    )

    payload = response_json(response)
    result = require_object(payload["result"])
    chunks = require_object_list(result["chunks"])

    assert response.status_code == 200
    assert result["anchor_id"] == published["anchor_id"]
    assert result["source_id"] == published["source_id"]
    assert result["source_version_id"] == published["source_version_id"]
    assert result["publication_state"] == "published"
    assert result["chunk_count"] == 1
    assert chunks == [
        {
            "chunk_id": f"{published['anchor_id']}-chunk-0",
            "chunk_index": 0,
            "has_embedding": True,
        }
    ]
    assert "chunk_text" not in str(result)


def test_missing_management_detail_identifiers_fail_canonically(client: TestClient) -> None:
    missing_source = client.get(
        "/knowledge/sources/unknown-source",
        headers=admin_auth_headers("missing-source"),
    )
    missing_anchor = client.get(
        "/knowledge/anchors/unknown-anchor",
        headers=admin_auth_headers("missing-anchor"),
    )
    missing_version = client.get(
        "/knowledge/source-versions/unknown-version",
        headers=admin_auth_headers("missing-version"),
    )

    for response in (missing_source, missing_anchor, missing_version):
        payload = response_json(response)
        detail = require_object(payload["detail"])
        assert response.status_code == 400
        assert detail["error_code"] == "invalid_knowledge_request"
        assert detail["message"]
        assert detail["reason"] == "invalid_knowledge_request"


def _publish_document_backed_source(
    *,
    client: TestClient,
    seed: str,
    source_id: str,
    source_family_id: str,
) -> dict[str, str]:
    ingest = client.post(
        "/knowledge/ingestion/documents",
        json={
            "requested_by": "123e4567-e89b-12d3-a456-426614174310",
            "idempotency_key": f"{seed}-document-key",
            "document_id": "123e4567-e89b-12d3-a456-426614174313",
            "storage_key": f"knowledge/{seed}.pdf",
            "mime_type": "application/pdf",
            "payload_checksum_sha256": f"{seed}-sha256",
            "source_document_system": "storage_registered",
            "source_input_origin": "official_source_upload",
            "source_class": "tax_law",
        },
        headers=admin_auth_headers(f"{seed}-ingest"),
    )
    ingest_payload = response_json(ingest)
    ingestion_job_id = str(require_object(ingest_payload["result"])["ingestion_job_id"])

    review = client.post(
        f"/knowledge/ingestion/{ingestion_job_id}/review",
        json={
            "reviewed_by": "123e4567-e89b-12d3-a456-426614174311",
            "review_notes": [{"note": f"review-{seed}"}],
            "proposed_source_updates": {"workflow_seed": seed},
        },
        headers=admin_auth_headers(f"{seed}-review"),
    )
    approve = client.post(
        f"/knowledge/ingestion/{ingestion_job_id}/approve",
        json={
            "reviewed_by": "123e4567-e89b-12d3-a456-426614174311",
            "review_notes": [{"note": f"approve-{seed}"}],
            "publication_payload": {
                "source_id": source_id,
                "source_family_id": source_family_id,
                "title": f"Governed document-backed source {seed}",
                "source_class": "tax_law",
                "authority_level": "statute",
                "tax_domain": "income_tax",
                "issuing_authority": "Kenya Revenue Authority",
                "point_in_time_url": f"https://example.com/document-source/{seed}",
                "source_version_form": "as_issued",
                "effective_from": "2026-01-01",
                "effective_to": None,
                "tax_year": 2026,
                "anchors": [
                    {
                        "anchor_id": f"anchor-{seed}",
                        "anchor_title": f"Anchor {seed}",
                        "anchor_path": f"path-{seed}",
                        "temporal_scope_from": "2026-01-01",
                        "temporal_scope_to": None,
                        "chunks": [{"chunk_text": "governed chunk"}],
                    }
                ],
            },
        },
        headers=admin_auth_headers(f"{seed}-approve"),
    )
    publish = client.post(
        f"/knowledge/ingestion/{ingestion_job_id}/publish",
        json={"published_by": "123e4567-e89b-12d3-a456-426614174312"},
        headers=admin_auth_headers(f"{seed}-publish"),
    )

    assert ingest.status_code == 200
    assert review.status_code == 200
    assert approve.status_code == 200
    publish_payload = response_json(publish)
    assert publish.status_code == 200
    publish_result = require_object(publish_payload["result"])
    proposed_source_record = require_object(publish_result["proposed_source_record"])
    return {
        "source_id": source_id,
        "source_family_id": source_family_id,
        "source_version_id": str(proposed_source_record["published_source_version_id"]),
        "anchor_id": f"anchor-{seed}",
    }
