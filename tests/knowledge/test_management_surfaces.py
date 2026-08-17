from __future__ import annotations

import base64

from fastapi.testclient import TestClient

from tests.knowledge.support import response_json
from tests.knowledge.support import require_object
from tests.knowledge.support import stable_headers
from tests.knowledge.support import admin_auth_headers
from tests.knowledge.support import require_object_list


def test_management_listing_routes_require_admin_auth(client: TestClient) -> None:
    ingestion = client.get("/knowledge/ingestion", headers=stable_headers("mgmt-ingestion"))
    source_versions = client.get(
        "/knowledge/source-versions",
        headers=stable_headers("mgmt-versions"),
    )
    sources = client.get("/knowledge/sources", headers=stable_headers("mgmt-sources"))

    for response, correlation_id in (
        (ingestion, "mgmt-ingestion-corr"),
        (source_versions, "mgmt-versions-corr"),
        (sources, "mgmt-sources-corr"),
    ):
        payload = response_json(response)
        detail = require_object(payload["detail"])
        assert response.status_code == 401
        assert detail["error_code"] == "auth_context_missing"
        assert detail["reason"] == "auth_context_missing"
        assert detail["correlation_id"] == correlation_id


def test_management_ingestion_and_source_listings_are_deterministic(client: TestClient) -> None:
    _ingest_url_source(client=client, seed="mgmt-url")
    _ingest_legacy_file_source(client=client, seed="mgmt-file")
    published = _publish_source_version(
        client=client,
        seed="mgmt-published",
        source_id="KNW-MGMT-001",
        source_family_id="KNW-MGMT-FAMILY-001",
        effective_from="2026-01-01",
        effective_to=None,
    )

    ingestion_first = client.get(
        "/knowledge/ingestion",
        params={
            "limit": 10,
            "offset": 0,
            "sort_by": "created_at",
            "sort_order": "desc",
        },
        headers=admin_auth_headers("ingestion-first"),
    )
    ingestion_second = client.get(
        "/knowledge/ingestion",
        params={
            "limit": 10,
            "offset": 0,
            "sort_by": "created_at",
            "sort_order": "desc",
        },
        headers=admin_auth_headers("ingestion-second"),
    )
    source_versions = client.get(
        "/knowledge/source-versions",
        params={
            "source_family_id": published["source_family_id"],
            "publication_state": "published",
            "limit": 10,
            "offset": 0,
            "sort_by": "effective_from",
            "sort_order": "asc",
        },
        headers=admin_auth_headers("versions"),
    )
    sources = client.get(
        "/knowledge/sources",
        params={
            "source_class": "tax_law",
            "tax_domain": "income_tax",
            "limit": 10,
            "offset": 0,
            "sort_by": "source_family_id",
            "sort_order": "asc",
        },
        headers=admin_auth_headers("sources"),
    )

    first_payload = response_json(ingestion_first)
    second_payload = response_json(ingestion_second)
    version_payload = response_json(source_versions)
    source_payload = response_json(sources)

    assert ingestion_first.status_code == 200
    assert ingestion_second.status_code == 200
    assert first_payload["result"] == second_payload["result"]

    ingestion_result = require_object(first_payload["result"])
    ingestion_page = require_object(ingestion_result["page"])
    ingestion_items = require_object_list(ingestion_result["items"])
    assert ingestion_result["total"] == 3
    assert ingestion_page == {
        "limit": 10,
        "offset": 0,
        "sort_by": "created_at",
        "sort_order": "desc",
    }
    assert [item["source_input_origin"] for item in ingestion_items] == [
        "official_source_upload",
        "official_source_upload",
        "official_source_url",
    ]

    assert source_versions.status_code == 200
    version_result = require_object(version_payload["result"])
    version_page = require_object(version_result["page"])
    version_items = require_object_list(version_result["items"])
    assert version_result["total"] == 1
    assert version_page == {
        "limit": 10,
        "offset": 0,
        "sort_by": "effective_from",
        "sort_order": "asc",
    }
    assert version_items[0]["source_version_id"] == published["source_version_id"]
    assert version_items[0]["publication_state"] == "published"

    assert sources.status_code == 200
    source_result = require_object(source_payload["result"])
    source_page = require_object(source_result["page"])
    source_items = require_object_list(source_result["items"])
    assert source_result["total"] == 1
    assert source_page == {
        "limit": 10,
        "offset": 0,
        "sort_by": "source_family_id",
        "sort_order": "asc",
    }
    assert source_items[0]["source_id"] == published["source_id"]
    assert source_items[0]["version_count"] == 1
    assert source_items[0]["anchor_count"] == 1


def test_management_invalid_filters_fail_with_canonical_error_shape(
    client: TestClient,
) -> None:
    bad_limit = client.get(
        "/knowledge/ingestion",
        params={"limit": 0},
        headers=admin_auth_headers("bad-limit"),
    )
    bad_publication_state = client.get(
        "/knowledge/source-versions",
        params={"publication_state": "draft"},
        headers=admin_auth_headers("bad-state"),
    )
    bad_sort = client.get(
        "/knowledge/sources",
        params={"sort_by": "requested_by"},
        headers=admin_auth_headers("bad-sort"),
    )

    for response in (bad_limit, bad_publication_state, bad_sort):
        payload = response_json(response)
        detail = require_object(payload["detail"])
        assert response.status_code == 400
        assert detail["error_code"] == "invalid_knowledge_request"
        assert detail["message"]
        assert detail["reason"] == "invalid_knowledge_request"


def _ingest_url_source(*, client: TestClient, seed: str) -> dict[str, object]:
    response = client.post(
        "/knowledge/ingestion/urls",
        json={
            "requested_by": "123e4567-e89b-12d3-a456-426614174210",
            "idempotency_key": f"{seed}-url-key",
            "url": f"https://example.com/{seed}",
            "source_input_origin": "official_source_url",
            "source_class": "guidance",
        },
        headers=admin_auth_headers(seed),
    )
    payload = response_json(response)
    assert response.status_code == 200
    return require_object(payload["result"])


def _ingest_legacy_file_source(*, client: TestClient, seed: str) -> dict[str, object]:
    response = client.post(
        "/knowledge/ingestion/files",
        json={
            "requested_by": "123e4567-e89b-12d3-a456-426614174211",
            "idempotency_key": f"{seed}-file-key",
            "filename": f"{seed}.pdf",
            "mime_type": "application/pdf",
            "file_content_base64": base64.b64encode(f"{seed}-content".encode()).decode("ascii"),
            "source_input_origin": "official_source_upload",
            "source_class": "tax_law",
            "legacy_import_acknowledged": True,
        },
        headers=admin_auth_headers(seed),
    )
    payload = response_json(response)
    assert response.status_code == 200
    return require_object(payload["result"])


def _publish_source_version(
    *,
    client: TestClient,
    seed: str,
    source_id: str,
    source_family_id: str,
    effective_from: str,
    effective_to: str | None,
) -> dict[str, str]:
    ingested = _ingest_legacy_file_source(client=client, seed=seed)
    ingestion_job_id = str(ingested["ingestion_job_id"])
    reviewer_id = "123e4567-e89b-12d3-a456-426614174212"
    publisher_id = "123e4567-e89b-12d3-a456-426614174213"

    review = client.post(
        f"/knowledge/ingestion/{ingestion_job_id}/review",
        json={
            "reviewed_by": reviewer_id,
            "review_notes": [{"note": f"review-{seed}"}],
            "proposed_source_updates": {"workflow_seed": seed},
        },
        headers=admin_auth_headers(f"{seed}-review"),
    )
    approve = client.post(
        f"/knowledge/ingestion/{ingestion_job_id}/approve",
        json={
            "reviewed_by": reviewer_id,
            "review_notes": [{"note": f"approve-{seed}"}],
            "publication_payload": {
                "source_id": source_id,
                "source_family_id": source_family_id,
                "title": f"Governed source {seed}",
                "source_class": "tax_law",
                "authority_level": "statute",
                "tax_domain": "income_tax",
                "issuing_authority": "Kenya Revenue Authority",
                "point_in_time_url": f"https://example.com/sources/{seed}",
                "source_version_form": "point_in_time_consolidation",
                "effective_from": effective_from,
                "effective_to": effective_to,
                "tax_year": 2026,
                "anchors": [
                    {
                        "anchor_id": f"anchor-{seed}",
                        "anchor_title": f"Anchor {seed}",
                        "anchor_path": f"path-{seed}",
                        "temporal_scope_from": effective_from,
                        "temporal_scope_to": effective_to,
                        "chunks": [{"chunk_text": f"chunk for {seed}"}],
                    }
                ],
            },
        },
        headers=admin_auth_headers(f"{seed}-approve"),
    )
    publish = client.post(
        f"/knowledge/ingestion/{ingestion_job_id}/publish",
        json={"published_by": publisher_id},
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
