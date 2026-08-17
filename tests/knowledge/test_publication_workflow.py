from __future__ import annotations

import base64

from fastapi.testclient import TestClient

from tests.knowledge.support import response_json
from tests.knowledge.support import require_object
from tests.knowledge.support import stable_headers
from tests.knowledge.support import admin_auth_headers

REQUESTED_BY = "123e4567-e89b-12d3-a456-426614174000"
REVIEWER_ID = "123e4567-e89b-12d3-a456-426614174010"
PUBLISHER_ID = "123e4567-e89b-12d3-a456-426614174020"


def test_review_route_rejects_missing_auth_context(client: TestClient) -> None:
    ingestion_job_id = _create_ingestion_job(client)

    response = client.post(
        f"/knowledge/ingestion/{ingestion_job_id}/review",
        json={
            "reviewed_by": REVIEWER_ID,
            "review_notes": [{"note": "reviewed for publication"}],
        },
        headers=stable_headers("review-auth"),
    )

    payload = response_json(response)
    detail = require_object(payload["detail"])

    assert response.status_code == 401
    assert detail["error_code"] == "auth_context_missing"
    assert detail["message"] == "Auth context header is required."
    assert detail["reason"] == "auth_context_missing"


def test_review_approve_and_publish_follow_current_canonical_states(
    client: TestClient,
) -> None:
    ingestion_job_id = _create_ingestion_job(client)

    review = client.post(
        f"/knowledge/ingestion/{ingestion_job_id}/review",
        json={
            "reviewed_by": REVIEWER_ID,
            "review_notes": [{"note": "reviewed for publication"}],
            "proposed_source_updates": {"review_tag": "finance-2026"},
        },
        headers=admin_auth_headers("review-ok"),
    )
    approve = client.post(
        f"/knowledge/ingestion/{ingestion_job_id}/approve",
        json={
            "reviewed_by": REVIEWER_ID,
            "review_notes": [{"note": "approved for publication"}],
            "publication_payload": _publication_payload(
                source_version_id="123e4567-e89b-12d3-a456-426614174700",
                effective_from="2026-01-01",
                effective_to=None,
            ),
        },
        headers=admin_auth_headers("approve-ok"),
    )
    publish = client.post(
        f"/knowledge/ingestion/{ingestion_job_id}/publish",
        json={"published_by": PUBLISHER_ID},
        headers=admin_auth_headers("publish-ok"),
    )

    review_payload = response_json(review)
    approve_payload = response_json(approve)
    publish_payload = response_json(publish)
    review_result = require_object(review_payload["result"])
    approve_result = require_object(approve_payload["result"])
    publish_result = require_object(publish_payload["result"])
    publish_proposed = require_object(publish_result["proposed_source_record"])

    assert review.status_code == 200
    assert approve.status_code == 200
    assert publish.status_code == 200
    assert review_result["ingestion_state"] == "review_pending"
    assert approve_result["ingestion_state"] == "approved"
    assert publish_result["ingestion_state"] == "published"
    assert publish_proposed["published_by"] == PUBLISHER_ID
    assert publish_proposed["published_source_version_id"] == "123e4567-e89b-12d3-a456-426614174700"


def test_publish_requires_distinct_publisher_from_approving_reviewer(
    client: TestClient,
) -> None:
    ingestion_job_id = _create_ingestion_job(client)
    approve = client.post(
        f"/knowledge/ingestion/{ingestion_job_id}/approve",
        json={
            "reviewed_by": REVIEWER_ID,
            "review_notes": [{"note": "approved for publication"}],
            "publication_payload": _publication_payload(
                source_version_id="123e4567-e89b-12d3-a456-426614174701",
                effective_from="2026-01-01",
                effective_to=None,
            ),
        },
        headers=admin_auth_headers("approve-distinct"),
    )
    publish = client.post(
        f"/knowledge/ingestion/{ingestion_job_id}/publish",
        json={"published_by": REVIEWER_ID},
        headers=admin_auth_headers("publish-distinct"),
    )

    detail = require_object(response_json(publish)["detail"])

    assert approve.status_code == 200
    assert publish.status_code == 409
    assert detail["error_code"] == "knowledge_publication_safety_rejected"
    assert (
        detail["message"]
        == "Knowledge publication requires a publisher distinct from the approving reviewer."
    )
    assert detail["reason"] == "knowledge_publication_safety_rejected"


def test_rejecting_published_ingestion_job_fails_canonically(client: TestClient) -> None:
    ingestion_job_id = _create_ingestion_job(client)
    client.post(
        f"/knowledge/ingestion/{ingestion_job_id}/approve",
        json={
            "reviewed_by": REVIEWER_ID,
            "review_notes": [{"note": "approved for publication"}],
            "publication_payload": _publication_payload(
                source_version_id="123e4567-e89b-12d3-a456-426614174702",
                effective_from="2026-01-01",
                effective_to=None,
            ),
        },
        headers=admin_auth_headers("approve-published"),
    )
    client.post(
        f"/knowledge/ingestion/{ingestion_job_id}/publish",
        json={"published_by": PUBLISHER_ID},
        headers=admin_auth_headers("publish-published"),
    )
    reject = client.post(
        f"/knowledge/ingestion/{ingestion_job_id}/reject",
        json={
            "reviewed_by": REVIEWER_ID,
            "review_notes": [{"note": "too late to reject"}],
        },
        headers=admin_auth_headers("reject-published"),
    )

    detail = require_object(response_json(reject)["detail"])

    assert reject.status_code == 409
    assert detail["error_code"] == "invalid_publication_state_transition"
    assert detail["message"] == "Published knowledge ingestion jobs cannot be rejected."
    assert detail["reason"] == "invalid_publication_state_transition"


def _create_ingestion_job(client: TestClient) -> str:
    ingest = client.post(
        "/knowledge/ingestion/files",
        json={
            "requested_by": REQUESTED_BY,
            "idempotency_key": "publication-file-001",
            "filename": "finance-act.pdf",
            "mime_type": "application/pdf",
            "file_content_base64": base64.b64encode(b"pdf-bytes").decode("utf-8"),
            "legacy_import_acknowledged": True,
            "source_class": "tax_law",
        },
        headers=admin_auth_headers("ingest-publication"),
    )
    result = require_object(response_json(ingest)["result"])
    assert ingest.status_code == 200
    return str(result["ingestion_job_id"])


def _publication_payload(
    *,
    source_version_id: str,
    effective_from: str,
    effective_to: str | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "source_id": "KNW-FINANCE-2026",
        "source_family_id": "KNW-FINANCE-FAMILY",
        "source_class": "tax_law",
        "authority_level": "statute",
        "tax_domain": "income_tax",
        "issuing_authority": "National Treasury",
        "title": "Finance Act 2026",
        "point_in_time_url": "https://example.com/finance-act-2026",
        "source_version_form": "point_in_time_consolidation",
        "effective_from": effective_from,
        "tax_year": 2026,
        "anchors": [
            {
                "anchor_id": "finance-act-2026-15-2",
                "title": "Section 15(2)",
                "path": "part-i/section-15-2",
                "content": "Allowable deductions text",
            }
        ],
        "source_version_id": source_version_id,
    }
    if effective_to is not None:
        payload["effective_to"] = effective_to
    return payload
