from __future__ import annotations

import base64

from fastapi.testclient import TestClient

from tests.knowledge.support import response_json
from tests.knowledge.support import require_object
from tests.knowledge.support import admin_auth_headers

REQUESTED_BY = "123e4567-e89b-12d3-a456-426614174000"
REVIEWER_ID = "123e4567-e89b-12d3-a456-426614174010"
PUBLISHER_ID = "123e4567-e89b-12d3-a456-426614174020"


def test_same_family_published_versions_can_be_superseded(client: TestClient) -> None:
    predecessor = _publish_version(
        client,
        ingestion_key="supersede-predecessor",
        source_version_id="123e4567-e89b-12d3-a456-426614174710",
        source_family_id="KNW-FINANCE-FAMILY",
        effective_from="2025-01-01",
        effective_to="2025-12-31",
    )
    successor = _publish_version(
        client,
        ingestion_key="supersede-successor",
        source_version_id="123e4567-e89b-12d3-a456-426614174711",
        source_family_id="KNW-FINANCE-FAMILY",
        effective_from="2026-01-01",
        effective_to=None,
    )

    supersede = client.post(
        f"/knowledge/source-versions/{predecessor}/supersede",
        json={
            "successor_source_version_id": successor,
            "superseded_by": PUBLISHER_ID,
        },
        headers=admin_auth_headers("supersede-ok"),
    )

    payload = response_json(supersede)
    result = require_object(payload["result"])

    assert supersede.status_code == 200
    assert result["publication_state"] == "superseded"
    assert result["superseded_by_source_version_id"] == successor


def test_cross_family_supersession_fails_canonically(client: TestClient) -> None:
    predecessor = _publish_version(
        client,
        ingestion_key="cross-predecessor",
        source_version_id="123e4567-e89b-12d3-a456-426614174712",
        source_family_id="KNW-FINANCE-FAMILY-A",
        effective_from="2025-01-01",
        effective_to="2025-12-31",
    )
    successor = _publish_version(
        client,
        ingestion_key="cross-successor",
        source_version_id="123e4567-e89b-12d3-a456-426614174713",
        source_family_id="KNW-FINANCE-FAMILY-B",
        effective_from="2026-01-01",
        effective_to=None,
    )

    supersede = client.post(
        f"/knowledge/source-versions/{predecessor}/supersede",
        json={
            "successor_source_version_id": successor,
            "superseded_by": PUBLISHER_ID,
        },
        headers=admin_auth_headers("supersede-cross"),
    )

    detail = require_object(response_json(supersede)["detail"])

    assert supersede.status_code == 409
    assert detail["error_code"] == "knowledge_supersession_conflict"
    assert (
        detail["message"] == "Knowledge supersession requires predecessor and successor "
        "from the same governed source family."
    )
    assert detail["reason"] == "knowledge_supersession_conflict"


def test_superseded_version_can_be_archived(client: TestClient) -> None:
    predecessor = _publish_version(
        client,
        ingestion_key="archive-predecessor",
        source_version_id="123e4567-e89b-12d3-a456-426614174714",
        source_family_id="KNW-FINANCE-FAMILY",
        effective_from="2025-01-01",
        effective_to="2025-12-31",
    )
    successor = _publish_version(
        client,
        ingestion_key="archive-successor",
        source_version_id="123e4567-e89b-12d3-a456-426614174715",
        source_family_id="KNW-FINANCE-FAMILY",
        effective_from="2026-01-01",
        effective_to=None,
    )
    client.post(
        f"/knowledge/source-versions/{predecessor}/supersede",
        json={
            "successor_source_version_id": successor,
            "superseded_by": PUBLISHER_ID,
        },
        headers=admin_auth_headers("archive-prime"),
    )

    archive = client.post(
        f"/knowledge/source-versions/{predecessor}/archive",
        json={"archived_by": PUBLISHER_ID},
        headers=admin_auth_headers("archive-ok"),
    )

    payload = response_json(archive)
    result = require_object(payload["result"])

    assert archive.status_code == 200
    assert result["publication_state"] == "archived"


def test_active_published_version_cannot_be_archived(client: TestClient) -> None:
    active_version = _publish_version(
        client,
        ingestion_key="archive-active",
        source_version_id="123e4567-e89b-12d3-a456-426614174716",
        source_family_id="KNW-FINANCE-FAMILY",
        effective_from="2026-01-01",
        effective_to=None,
    )

    archive = client.post(
        f"/knowledge/source-versions/{active_version}/archive",
        json={"archived_by": PUBLISHER_ID},
        headers=admin_auth_headers("archive-active"),
    )

    detail = require_object(response_json(archive)["detail"])

    assert archive.status_code == 409
    assert detail["error_code"] == "invalid_publication_state_transition"
    assert (
        detail["message"] == "Knowledge source version cannot be archived while it remains "
        "the active published version."
    )
    assert detail["reason"] == "invalid_publication_state_transition"


def _publish_version(
    client: TestClient,
    *,
    ingestion_key: str,
    source_version_id: str,
    source_family_id: str,
    effective_from: str,
    effective_to: str | None,
) -> str:
    ingest = client.post(
        "/knowledge/ingestion/files",
        json={
            "requested_by": REQUESTED_BY,
            "idempotency_key": ingestion_key,
            "filename": "finance-act.pdf",
            "mime_type": "application/pdf",
            "file_content_base64": base64.b64encode(ingestion_key.encode("utf-8")).decode("utf-8"),
            "legacy_import_acknowledged": True,
            "source_class": "tax_law",
        },
        headers=admin_auth_headers(f"{ingestion_key}-ingest"),
    )
    ingestion_job_id = str(require_object(response_json(ingest)["result"])["ingestion_job_id"])
    approve = client.post(
        f"/knowledge/ingestion/{ingestion_job_id}/approve",
        json={
            "reviewed_by": REVIEWER_ID,
            "review_notes": [{"note": "approved"}],
            "publication_payload": _publication_payload(
                source_version_id=source_version_id,
                source_family_id=source_family_id,
                effective_from=effective_from,
                effective_to=effective_to,
            ),
        },
        headers=admin_auth_headers(f"{ingestion_key}-approve"),
    )
    publish = client.post(
        f"/knowledge/ingestion/{ingestion_job_id}/publish",
        json={"published_by": PUBLISHER_ID},
        headers=admin_auth_headers(f"{ingestion_key}-publish"),
    )

    assert ingest.status_code == 200
    assert approve.status_code == 200
    assert publish.status_code == 200
    return source_version_id


def _publication_payload(
    *,
    source_version_id: str,
    source_family_id: str,
    effective_from: str,
    effective_to: str | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "source_id": f"KNW-{source_version_id[-4:]}",
        "source_family_id": source_family_id,
        "source_class": "tax_law",
        "authority_level": "statute",
        "tax_domain": "income_tax",
        "issuing_authority": "National Treasury",
        "title": "Finance Act",
        "point_in_time_url": "https://example.com/finance-act",
        "source_version_form": "point_in_time_consolidation",
        "effective_from": effective_from,
        "tax_year": 2026,
        "anchors": [
            {
                "anchor_id": f"anchor-{source_version_id[-4:]}",
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
