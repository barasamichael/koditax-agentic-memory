from __future__ import annotations

import base64

from fastapi.testclient import TestClient

from tests.knowledge.support import response_json
from tests.knowledge.support import require_object
from tests.knowledge.support import admin_auth_headers

REQUESTED_BY = "123e4567-e89b-12d3-a456-426614174000"
REVIEWER_ID = "123e4567-e89b-12d3-a456-426614174010"
PUBLISHER_ID = "123e4567-e89b-12d3-a456-426614174020"


def test_metadata_correction_updates_narrow_unpublished_fields(client: TestClient) -> None:
    ingestion_job_id = _approved_ingestion_job(client, idempotency_key="metadata-approved")

    correction = client.post(
        f"/knowledge/ingestion/{ingestion_job_id}/metadata-correction",
        json={
            "corrected_by": REVIEWER_ID,
            "review_notes": [{"note": "title correction"}],
            "publication_payload_updates": {"title": "Finance Act 2026 Revised"},
        },
        headers=admin_auth_headers("metadata-ok"),
    )

    payload = response_json(correction)
    result = require_object(payload["result"])
    proposed = require_object(result["proposed_source_record"])
    publication_payload = require_object(proposed["publication_payload"])

    assert correction.status_code == 200
    assert result["ingestion_state"] == "approved"
    assert publication_payload["title"] == "Finance Act 2026 Revised"
    assert proposed["last_corrected_by"] == REVIEWER_ID


def test_metadata_correction_rejects_immutable_lineage_fields(client: TestClient) -> None:
    ingestion_job_id = _approved_ingestion_job(client, idempotency_key="metadata-immutable")

    correction = client.post(
        f"/knowledge/ingestion/{ingestion_job_id}/metadata-correction",
        json={
            "corrected_by": REVIEWER_ID,
            "review_notes": [{"note": "bad correction"}],
            "publication_payload_updates": {"source_id": "KNW-MUTATED"},
        },
        headers=admin_auth_headers("metadata-immutable"),
    )

    detail = require_object(response_json(correction)["detail"])

    assert correction.status_code == 409
    assert detail["error_code"] == "invalid_knowledge_lineage"
    assert detail["message"] == "Knowledge metadata correction contains immutable lineage fields."
    assert detail["reason"] == "invalid_knowledge_lineage"


def test_metadata_correction_rejects_published_items(client: TestClient) -> None:
    ingestion_job_id = _approved_ingestion_job(client, idempotency_key="metadata-published")
    publish = client.post(
        f"/knowledge/ingestion/{ingestion_job_id}/publish",
        json={"published_by": PUBLISHER_ID},
        headers=admin_auth_headers("metadata-published-publish"),
    )
    correction = client.post(
        f"/knowledge/ingestion/{ingestion_job_id}/metadata-correction",
        json={
            "corrected_by": REVIEWER_ID,
            "review_notes": [{"note": "too late"}],
            "publication_payload_updates": {"title": "Should Fail"},
        },
        headers=admin_auth_headers("metadata-published"),
    )

    detail = require_object(response_json(correction)["detail"])

    assert publish.status_code == 200
    assert correction.status_code == 409
    assert detail["error_code"] == "invalid_publication_state_transition"
    assert (
        detail["message"] == "Knowledge metadata correction is allowed only for editable "
        "unpublished review-stage material."
    )
    assert detail["reason"] == "invalid_publication_state_transition"


def _approved_ingestion_job(client: TestClient, *, idempotency_key: str) -> str:
    ingest = client.post(
        "/knowledge/ingestion/files",
        json={
            "requested_by": REQUESTED_BY,
            "idempotency_key": idempotency_key,
            "filename": "finance-act.pdf",
            "mime_type": "application/pdf",
            "file_content_base64": base64.b64encode(idempotency_key.encode("utf-8")).decode(
                "utf-8"
            ),
            "legacy_import_acknowledged": True,
            "source_class": "tax_law",
        },
        headers=admin_auth_headers(f"{idempotency_key}-ingest"),
    )
    ingestion_job_id = str(require_object(response_json(ingest)["result"])["ingestion_job_id"])
    approve = client.post(
        f"/knowledge/ingestion/{ingestion_job_id}/approve",
        json={
            "reviewed_by": REVIEWER_ID,
            "review_notes": [{"note": "approved"}],
            "publication_payload": {
                "source_id": "KNW-FINANCE-2026",
                "source_family_id": "KNW-FINANCE-FAMILY",
                "source_class": "tax_law",
                "authority_level": "statute",
                "tax_domain": "income_tax",
                "issuing_authority": "National Treasury",
                "title": "Finance Act 2026",
                "point_in_time_url": "https://example.com/finance-act-2026",
                "source_version_form": "point_in_time_consolidation",
                "effective_from": "2026-01-01",
                "tax_year": 2026,
                "anchors": [
                    {
                        "anchor_id": "finance-act-2026-15-2",
                        "title": "Section 15(2)",
                        "path": "part-i/section-15-2",
                        "content": "Allowable deductions text",
                    }
                ],
                "source_version_id": "123e4567-e89b-12d3-a456-426614174720",
            },
        },
        headers=admin_auth_headers(f"{idempotency_key}-approve"),
    )

    assert ingest.status_code == 200
    assert approve.status_code == 200
    return ingestion_job_id
