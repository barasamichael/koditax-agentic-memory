from __future__ import annotations

import base64
from typing import Any

from fastapi.testclient import TestClient

from tests.knowledge.support import response_json
from tests.knowledge.support import require_object
from tests.knowledge.support import admin_auth_headers
from tests.knowledge.support import require_object_list


def test_legacy_import_source_detail_reports_legacy_lineage_and_no_purge(
    client: TestClient,
) -> None:
    published = _publish_legacy_import_source(
        client=client,
        seed="retention-legacy",
        source_id="KNW-RET-LEGACY-001",
        source_family_id="KNW-RET-LEGACY-FAMILY-001",
    )

    response = client.get(
        f"/knowledge/sources/{published['source_id']}",
        headers=admin_auth_headers("legacy-detail"),
    )

    payload = response_json(response)
    result = require_object(payload["result"])
    retention_summary = require_object(result["retention_summary"])

    assert response.status_code == 200
    assert result["source_id"] == published["source_id"]
    assert retention_summary == {
        "lineage_preserved": True,
        "has_document_lineage": False,
        "has_purged_document_lineage": False,
        "has_historical_compatibility_lineage": False,
        "has_legacy_import_lineage": True,
        "has_url_lineage": False,
        "retention_policy_code": "knowledge-shared-corpus-retention-v1",
        "purge_supported": False,
    }


def test_document_backed_source_detail_reports_document_lineage(client: TestClient) -> None:
    published = _publish_document_backed_source(
        client=client,
        seed="retention-document",
        source_id="KNW-RET-DOC-001",
        source_family_id="KNW-RET-DOC-FAMILY-001",
    )

    response = client.get(
        f"/knowledge/sources/{published['source_id']}",
        headers=admin_auth_headers("document-detail"),
    )

    payload = response_json(response)
    result = require_object(payload["result"])
    retention_summary = require_object(result["retention_summary"])
    versions = require_object_list(result["versions"])

    assert response.status_code == 200
    assert versions[0]["source_version_id"] == published["source_version_id"]
    assert retention_summary["has_document_lineage"] is True
    assert retention_summary["has_legacy_import_lineage"] is False
    assert retention_summary["has_url_lineage"] is False
    assert retention_summary["purge_supported"] is False


def test_url_backed_source_detail_reports_url_lineage(client: TestClient) -> None:
    published = _publish_url_source(
        client=client,
        seed="retention-url",
        source_id="KNW-RET-URL-001",
        source_family_id="KNW-RET-URL-FAMILY-001",
    )

    response = client.get(
        f"/knowledge/sources/{published['source_id']}",
        headers=admin_auth_headers("url-detail"),
    )

    payload = response_json(response)
    result = require_object(payload["result"])
    retention_summary = require_object(result["retention_summary"])

    assert response.status_code == 200
    assert result["source_id"] == published["source_id"]
    assert retention_summary["has_document_lineage"] is False
    assert retention_summary["has_legacy_import_lineage"] is False
    assert retention_summary["has_url_lineage"] is True
    assert retention_summary["purge_supported"] is False


def _publish_legacy_import_source(
    *,
    client: TestClient,
    seed: str,
    source_id: str,
    source_family_id: str,
) -> dict[str, str]:
    ingest = client.post(
        "/knowledge/ingestion/files",
        json={
            "requested_by": "123e4567-e89b-12d3-a456-426614174410",
            "idempotency_key": f"{seed}-file-key",
            "filename": f"{seed}.pdf",
            "mime_type": "application/pdf",
            "file_content_base64": base64.b64encode(f"{seed}-content".encode()).decode("ascii"),
            "legacy_import_acknowledged": True,
            "source_input_origin": "official_source_upload",
            "source_class": "tax_law",
        },
        headers=admin_auth_headers(f"{seed}-ingest"),
    )
    return _approve_and_publish(
        client=client,
        seed=seed,
        ingest_response=ingest,
        source_id=source_id,
        source_family_id=source_family_id,
    )


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
            "requested_by": "123e4567-e89b-12d3-a456-426614174411",
            "idempotency_key": f"{seed}-document-key",
            "document_id": "123e4567-e89b-12d3-a456-426614174412",
            "storage_key": f"knowledge/{seed}.pdf",
            "mime_type": "application/pdf",
            "payload_checksum_sha256": f"{seed}-sha256",
            "source_document_system": "storage_registered",
            "source_input_origin": "official_source_upload",
            "source_class": "tax_law",
        },
        headers=admin_auth_headers(f"{seed}-ingest"),
    )
    return _approve_and_publish(
        client=client,
        seed=seed,
        ingest_response=ingest,
        source_id=source_id,
        source_family_id=source_family_id,
    )


def _publish_url_source(
    *,
    client: TestClient,
    seed: str,
    source_id: str,
    source_family_id: str,
) -> dict[str, str]:
    ingest = client.post(
        "/knowledge/ingestion/urls",
        json={
            "requested_by": "123e4567-e89b-12d3-a456-426614174413",
            "idempotency_key": f"{seed}-url-key",
            "url": f"https://example.com/{seed}",
            "source_input_origin": "official_source_url",
            "source_class": "guidance",
        },
        headers=admin_auth_headers(f"{seed}-ingest"),
    )
    return _approve_and_publish(
        client=client,
        seed=seed,
        ingest_response=ingest,
        source_id=source_id,
        source_family_id=source_family_id,
        source_class="guidance",
        authority_level="regulatory_guidance",
    )


def _approve_and_publish(
    *,
    client: TestClient,
    seed: str,
    ingest_response: Any,
    source_id: str,
    source_family_id: str,
    source_class: str = "tax_law",
    authority_level: str = "statute",
) -> dict[str, str]:
    response = ingest_response
    payload = response_json(response)
    assert response.status_code == 200
    ingestion_job_id = str(require_object(payload["result"])["ingestion_job_id"])

    review = client.post(
        f"/knowledge/ingestion/{ingestion_job_id}/review",
        json={
            "reviewed_by": "123e4567-e89b-12d3-a456-426614174414",
            "review_notes": [{"note": f"review-{seed}"}],
            "proposed_source_updates": {"workflow_seed": seed},
        },
        headers=admin_auth_headers(f"{seed}-review"),
    )
    approve = client.post(
        f"/knowledge/ingestion/{ingestion_job_id}/approve",
        json={
            "reviewed_by": "123e4567-e89b-12d3-a456-426614174414",
            "review_notes": [{"note": f"approve-{seed}"}],
            "publication_payload": {
                "source_id": source_id,
                "source_family_id": source_family_id,
                "title": f"Governed retained source {seed}",
                "source_class": source_class,
                "authority_level": authority_level,
                "tax_domain": "income_tax",
                "issuing_authority": "Kenya Revenue Authority",
                "point_in_time_url": f"https://example.com/retention/{seed}",
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
                        "chunks": [{"chunk_text": f"chunk-{seed}"}],
                    }
                ],
            },
        },
        headers=admin_auth_headers(f"{seed}-approve"),
    )
    publish = client.post(
        f"/knowledge/ingestion/{ingestion_job_id}/publish",
        json={"published_by": "123e4567-e89b-12d3-a456-426614174415"},
        headers=admin_auth_headers(f"{seed}-publish"),
    )

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
    }
