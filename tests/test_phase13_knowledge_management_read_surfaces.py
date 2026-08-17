"""DB-backed management read-surface tests for governed knowledge visibility."""

from __future__ import annotations

from uuid import uuid4
import base64
from typing import Any
from typing import cast
from collections.abc import Iterator

import pytest
import psycopg
from fastapi.testclient import TestClient

from tests.knowledge_db_test_support import require_int
from tests.knowledge_db_test_support import load_database_url
from tests.knowledge_db_test_support import require_object_dict
from tests.knowledge_db_test_support import create_runtime_harness
from tests.knowledge_db_test_support import KnowledgeRuntimeHarness
from tests.knowledge_db_test_support import build_admin_auth_headers
from tests.knowledge_db_test_support import ensure_knowledge_migration_applied


@pytest.fixture()
def db_connection() -> Iterator[psycopg.Connection]:
    """Create DB connection for governed knowledge management DB tests."""

    database_url = load_database_url()
    if database_url is None or not database_url.strip():
        pytest.skip("DATABASE_URL is not set; skipping knowledge management DB tests.")

    ensure_knowledge_migration_applied(database_url=database_url)
    try:
        connection = psycopg.connect(database_url, connect_timeout=5)
    except psycopg.OperationalError:
        pytest.skip("DATABASE_URL is not reachable; skipping knowledge management DB tests.")

    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def harness(db_connection: psycopg.Connection) -> KnowledgeRuntimeHarness:
    """Build one runtime harness for governed knowledge management tests."""

    return create_runtime_harness(connection=db_connection)


def test_management_ingestion_listing_is_deterministic_and_newest_first(
    harness: KnowledgeRuntimeHarness,
) -> None:
    requested_by = str(uuid4())
    first_seed = uuid4().hex
    second_seed = uuid4().hex
    first_payload = _file_ingestion_payload(
        requested_by=requested_by,
        idempotency_key=f"mgmt-ingestion-{first_seed}",
        filename=f"finance-{first_seed}.pdf",
        file_bytes=f"official-{first_seed}".encode(),
    )
    second_payload = _url_ingestion_payload(
        requested_by=requested_by,
        idempotency_key=f"mgmt-ingestion-{second_seed}",
        url=f"https://example.com/knowledge/{second_seed}",
    )

    with TestClient(harness.app) as client:
        admin_headers = build_admin_auth_headers()
        first_ingest = client.post(
            "/knowledge/ingestion/files",
            json=first_payload,
            headers=admin_headers,
        )
        second_ingest = client.post(
            "/knowledge/ingestion/urls",
            json=second_payload,
            headers=admin_headers,
        )
        first_list = client.get(
            "/knowledge/ingestion",
            params={
                "requested_by": requested_by,
                "ingestion_state": "uploaded",
                "limit": 10,
                "offset": 0,
                "sort_by": "created_at",
                "sort_order": "desc",
            },
            headers=admin_headers,
        )
        second_list = client.get(
            "/knowledge/ingestion",
            params={
                "requested_by": requested_by,
                "ingestion_state": "uploaded",
                "limit": 10,
                "offset": 0,
                "sort_by": "created_at",
                "sort_order": "desc",
            },
            headers=admin_headers,
        )

    first_result = cast(dict[str, object], _json(first_ingest)["result"])
    second_result = cast(dict[str, object], _json(second_ingest)["result"])
    first_list_payload = _json(first_list)
    second_list_payload = _json(second_list)
    assert first_ingest.status_code == 200
    assert second_ingest.status_code == 200
    assert first_list.status_code == 200
    assert second_list.status_code == 200
    assert first_list_payload["result"] == second_list_payload["result"]
    page = require_object_dict(cast(dict[str, object], first_list_payload["result"])["page"])
    assert page == {
        "limit": 10,
        "offset": 0,
        "sort_by": "created_at",
        "sort_order": "desc",
    }
    items = _items(first_list_payload)
    assert _result_total(first_list_payload) == 2
    assert [str(item["ingestion_job_id"]) for item in items] == [
        str(second_result["ingestion_job_id"]),
        str(first_result["ingestion_job_id"]),
    ]
    assert [str(item["source_input_origin"]) for item in items] == [
        "official_source_url",
        "official_source_upload",
    ]


def test_management_ingestion_visibility_does_not_leak_into_search_or_retrieve(
    harness: KnowledgeRuntimeHarness,
) -> None:
    seed = uuid4().hex
    requested_by = str(uuid4())
    payload = _file_ingestion_payload(
        requested_by=requested_by,
        idempotency_key=f"mgmt-hidden-{seed}",
        filename=f"hidden-{seed}.pdf",
        file_bytes=f"hidden-{seed}".encode(),
    )

    with TestClient(harness.app) as client:
        admin_headers = build_admin_auth_headers()
        ingest = client.post("/knowledge/ingestion/files", json=payload, headers=admin_headers)
        ingest_result = cast(dict[str, object], _json(ingest)["result"])
        management_list = client.get(
            "/knowledge/ingestion",
            params={
                "requested_by": requested_by,
                "limit": 10,
                "offset": 0,
                "sort_by": "created_at",
                "sort_order": "desc",
            },
            headers=admin_headers,
        )
        search = client.post(
            "/knowledge/search",
            json={"query": seed, "tax_domain": "income_tax"},
        )
        retrieve = client.post(
            "/knowledge/retrieve",
            json={
                "source_ids": [str(ingest_result["ingestion_job_id"])],
                "anchor_ids": [],
            },
        )

    management_payload = _json(management_list)
    assert ingest.status_code == 200
    assert management_list.status_code == 200
    assert _result_total(management_payload) == 1
    assert str(_items(management_payload)[0]["ingestion_job_id"]) == str(
        ingest_result["ingestion_job_id"]
    )
    assert _result_total(_json(search)) == 0
    assert _result_total(_json(retrieve)) == 0


def test_management_source_version_listing_and_detail_preserve_lifecycle_visibility(
    harness: KnowledgeRuntimeHarness,
) -> None:
    seed = uuid4().hex
    predecessor = _publish_version(
        harness=harness,
        seed=f"{seed}-pred",
        shared_source_id=f"KNW-MGMT-{seed}",
        shared_family_id=f"KNW-MGMT-FAMILY-{seed}",
        search_token=f"mgmt-search-{seed}",
        effective_from="2025-01-01",
        effective_to="2025-12-31",
    )
    successor = _publish_version(
        harness=harness,
        seed=f"{seed}-succ",
        shared_source_id=f"KNW-MGMT-{seed}",
        shared_family_id=f"KNW-MGMT-FAMILY-{seed}",
        search_token=f"mgmt-search-{seed}",
        effective_from="2026-01-01",
        effective_to=None,
    )

    with TestClient(harness.app) as client:
        admin_headers = build_admin_auth_headers()
        supersede = client.post(
            f"/knowledge/source-versions/{predecessor['source_version_id']}/supersede",
            json={
                "successor_source_version_id": successor["source_version_id"],
                "superseded_by": str(uuid4()),
            },
            headers=admin_headers,
        )
        published_list = client.get(
            "/knowledge/source-versions",
            params={
                "source_family_id": predecessor["source_family_id"],
                "publication_state": "published",
                "limit": 10,
                "offset": 0,
                "sort_by": "source_family_id",
                "sort_order": "asc",
            },
            headers=admin_headers,
        )
        superseded_list = client.get(
            "/knowledge/source-versions",
            params={
                "source_family_id": predecessor["source_family_id"],
                "publication_state": "superseded",
                "limit": 10,
                "offset": 0,
                "sort_by": "source_family_id",
                "sort_order": "asc",
            },
            headers=admin_headers,
        )
        detail = client.get(
            f"/knowledge/source-versions/{predecessor['source_version_id']}",
            headers=admin_headers,
        )
        archive = client.post(
            f"/knowledge/source-versions/{predecessor['source_version_id']}/archive",
            json={"archived_by": str(uuid4())},
            headers=admin_headers,
        )
        archived_list = client.get(
            "/knowledge/source-versions",
            params={
                "source_family_id": predecessor["source_family_id"],
                "publication_state": "archived",
                "limit": 10,
                "offset": 0,
                "sort_by": "source_family_id",
                "sort_order": "asc",
            },
            headers=admin_headers,
        )
        sources = client.get("/knowledge/sources", headers=admin_headers)
        source_detail = client.get(
            f"/knowledge/sources/{predecessor['source_id']}",
            headers=admin_headers,
        )
        anchor_detail = client.get(
            f"/knowledge/anchors/anchor-{seed}-pred",
            headers=admin_headers,
        )
        historical_search = client.post(
            "/knowledge/search",
            json={
                "query": predecessor["search_token"],
                "tax_domain": "income_tax",
                "effective_date": "2025-06-01",
            },
        )

    published_items = _items(_json(published_list))
    superseded_items = _items(_json(superseded_list))
    archived_items = _items(_json(archived_list))
    detail_result = cast(dict[str, object], _json(detail)["result"])
    assert supersede.status_code == 200
    assert published_list.status_code == 200
    assert superseded_list.status_code == 200
    assert detail.status_code == 200
    assert archive.status_code == 200
    assert archived_list.status_code == 200
    assert sources.status_code == 200
    assert source_detail.status_code == 200
    assert anchor_detail.status_code == 200
    published_result = require_object_dict(_json(published_list)["result"])
    published_page = require_object_dict(published_result["page"])
    assert published_page["sort_by"] == "source_family_id"
    assert "anchors" not in detail_result
    assert "chunks" not in detail_result
    assert [str(item["source_version_id"]) for item in published_items] == [
        successor["source_version_id"]
    ]
    assert [str(item["source_version_id"]) for item in superseded_items] == [
        predecessor["source_version_id"]
    ]
    assert detail_result["publication_state"] == "superseded"
    assert detail_result["superseded_by_source_version_id"] == successor["source_version_id"]
    assert (
        cast(dict[str, object], _json(source_detail)["result"])["source_id"]
        == predecessor["source_id"]
    )
    anchor_detail_result = cast(dict[str, object], _json(anchor_detail)["result"])
    assert "chunk_text" not in str(anchor_detail_result)
    assert [str(item["source_version_id"]) for item in archived_items] == [
        predecessor["source_version_id"]
    ]
    assert _result_total(_json(historical_search)) == 0


def test_invalid_management_filters_fail_canonically(
    harness: KnowledgeRuntimeHarness,
) -> None:
    with TestClient(harness.app) as client:
        admin_headers = build_admin_auth_headers()
        bad_limit = client.get("/knowledge/ingestion", params={"limit": 0}, headers=admin_headers)
        bad_offset = client.get(
            "/knowledge/ingestion",
            params={"offset": -1},
            headers=admin_headers,
        )
        bad_sort = client.get(
            "/knowledge/ingestion",
            params={"sort_by": "requested_by"},
            headers=admin_headers,
        )
        bad_state = client.get(
            "/knowledge/source-versions",
            params={"publication_state": "not-a-state"},
            headers=admin_headers,
        )
        bad_version_sort = client.get(
            "/knowledge/source-versions",
            params={"sort_by": "title"},
            headers=admin_headers,
        )

    bad_limit_detail = _detail(_json(bad_limit))
    bad_offset_detail = _detail(_json(bad_offset))
    bad_sort_detail = _detail(_json(bad_sort))
    bad_state_detail = _detail(_json(bad_state))
    bad_version_sort_detail = _detail(_json(bad_version_sort))
    assert bad_limit.status_code == 400
    assert bad_offset.status_code == 400
    assert bad_sort.status_code == 400
    assert bad_state.status_code == 400
    assert bad_version_sort.status_code == 400
    assert bad_limit_detail["reason_code"] == "invalid_knowledge_request"
    assert bad_offset_detail["reason_code"] == "invalid_knowledge_request"
    assert bad_sort_detail["reason_code"] == "invalid_knowledge_request"
    assert bad_state_detail["reason_code"] == "invalid_knowledge_request"
    assert bad_version_sort_detail["reason_code"] == "invalid_knowledge_request"


def test_management_pagination_is_stable_for_source_versions(
    harness: KnowledgeRuntimeHarness,
) -> None:
    seed = uuid4().hex
    source_id = f"KNW-PAGE-{seed}"
    family_id = f"KNW-PAGE-FAMILY-{seed}"
    first = _publish_version(
        harness=harness,
        seed=f"{seed}-a",
        shared_source_id=source_id,
        shared_family_id=family_id,
        search_token=f"page-search-{seed}",
        effective_from="2024-01-01",
        effective_to="2024-12-31",
    )
    second = _publish_version(
        harness=harness,
        seed=f"{seed}-b",
        shared_source_id=source_id,
        shared_family_id=family_id,
        search_token=f"page-search-{seed}",
        effective_from="2025-01-01",
        effective_to="2025-12-31",
    )

    with TestClient(harness.app) as client:
        admin_headers = build_admin_auth_headers()
        first_page = client.get(
            "/knowledge/source-versions",
            params={
                "source_family_id": family_id,
                "limit": 1,
                "offset": 0,
                "sort_by": "effective_from",
                "sort_order": "asc",
            },
            headers=admin_headers,
        )
        second_page = client.get(
            "/knowledge/source-versions",
            params={
                "source_family_id": family_id,
                "limit": 1,
                "offset": 1,
                "sort_by": "effective_from",
                "sort_order": "asc",
            },
            headers=admin_headers,
        )
        repeat_second_page = client.get(
            "/knowledge/source-versions",
            params={
                "source_family_id": family_id,
                "limit": 1,
                "offset": 1,
                "sort_by": "effective_from",
                "sort_order": "asc",
            },
            headers=admin_headers,
        )

    assert first_page.status_code == 200
    assert second_page.status_code == 200
    assert repeat_second_page.status_code == 200
    assert _json(second_page)["result"] == _json(repeat_second_page)["result"]
    assert [str(item["source_version_id"]) for item in _items(_json(first_page))] == [
        first["source_version_id"]
    ]
    assert [str(item["source_version_id"]) for item in _items(_json(second_page))] == [
        second["source_version_id"]
    ]


def _publish_version(
    *,
    harness: KnowledgeRuntimeHarness,
    seed: str,
    shared_source_id: str,
    shared_family_id: str,
    search_token: str,
    effective_from: str,
    effective_to: str | None,
) -> dict[str, str]:
    requested_by = str(uuid4())
    reviewer_id = str(uuid4())
    publisher_id = str(uuid4())
    ingestion_payload = _file_ingestion_payload(
        requested_by=requested_by,
        idempotency_key=f"mgmt-publish-{seed}",
        filename=f"finance-act-{seed}.pdf",
        file_bytes=f"official-{seed}".encode(),
    )
    publication_payload = {
        "source_id": shared_source_id,
        "source_family_id": shared_family_id,
        "title": f"Finance Act governed source {shared_source_id}",
        "source_class": "tax_law",
        "authority_level": "statute",
        "tax_domain": "income_tax",
        "issuing_authority": "Kenya Revenue Authority",
        "point_in_time_url": f"https://example.com/finance-act/{shared_source_id}",
        "source_version_form": "as_issued",
        "effective_from": effective_from,
        "effective_to": effective_to,
        "tax_year": int(effective_from[:4]),
        "anchors": [
            {
                "anchor_id": f"anchor-{seed}",
                "anchor_title": f"Anchor {seed}",
                "anchor_path": f"path-{seed}",
                "anchor_text": f"Anchor text for {search_token}",
                "temporal_scope_from": effective_from,
                "temporal_scope_to": effective_to,
                "chunks": [{"chunk_text": f"Chunk text for {search_token} {seed}"}],
            }
        ],
    }

    with TestClient(harness.app) as client:
        admin_headers = build_admin_auth_headers()
        ingest = client.post(
            "/knowledge/ingestion/files",
            json=ingestion_payload,
            headers=admin_headers,
        )
        ingestion_job_id = str(cast(dict[str, object], _json(ingest)["result"])["ingestion_job_id"])
        review = client.post(
            f"/knowledge/ingestion/{ingestion_job_id}/review",
            json={
                "reviewed_by": reviewer_id,
                "review_notes": [{"note": f"reviewed-{seed}"}],
                "proposed_source_updates": {"workflow_seed": seed},
            },
            headers=admin_headers,
        )
        approve = client.post(
            f"/knowledge/ingestion/{ingestion_job_id}/approve",
            json={
                "reviewed_by": reviewer_id,
                "review_notes": [{"note": f"approved-{seed}"}],
                "publication_payload": publication_payload,
            },
            headers=admin_headers,
        )
        publish = client.post(
            f"/knowledge/ingestion/{ingestion_job_id}/publish",
            json={"published_by": publisher_id},
            headers=admin_headers,
        )

    assert ingest.status_code == 200
    assert review.status_code == 200
    assert approve.status_code == 200
    assert publish.status_code == 200
    publish_result = cast(dict[str, object], _json(publish)["result"])
    proposed_source_record = require_object_dict(publish_result["proposed_source_record"])
    return {
        "source_id": shared_source_id,
        "source_family_id": shared_family_id,
        "source_version_id": str(proposed_source_record["published_source_version_id"]),
        "search_token": search_token,
    }


def _file_ingestion_payload(
    *,
    requested_by: str,
    idempotency_key: str,
    filename: str,
    file_bytes: bytes,
) -> dict[str, object]:
    return {
        "requested_by": requested_by,
        "idempotency_key": idempotency_key,
        "filename": filename,
        "mime_type": "application/pdf",
        "file_content_base64": base64.b64encode(file_bytes).decode("ascii"),
        "source_input_origin": "official_source_upload",
        "source_class": "tax_law",
    }


def _url_ingestion_payload(
    *,
    requested_by: str,
    idempotency_key: str,
    url: str,
) -> dict[str, object]:
    return {
        "requested_by": requested_by,
        "idempotency_key": idempotency_key,
        "url": url,
        "source_input_origin": "official_source_url",
        "source_class": "guidance",
    }


def _json(response: Any) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


def _detail(payload: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], payload["detail"])


def _result_total(payload: dict[str, object]) -> int:
    result = cast(dict[str, object], payload["result"])
    return require_int(result["total"])


def _items(payload: dict[str, object]) -> tuple[dict[str, object], ...]:
    result = cast(dict[str, object], payload["result"])
    items = cast(list[object], result["items"])
    return tuple(cast(dict[str, object], item) for item in items)
