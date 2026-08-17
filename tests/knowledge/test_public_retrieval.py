from __future__ import annotations

from fastapi.testclient import TestClient

from tests.knowledge.support import response_json
from tests.knowledge.support import require_object
from tests.knowledge.support import admin_auth_headers
from tests.knowledge.support import require_object_list
from services.knowledge.app.main import INVALID_KNOWLEDGE_REQUEST


def test_search_returns_deterministic_admin_result_shape(client: TestClient) -> None:
    response = client.post(
        "/knowledge/search",
        json={
            "query": "income tax act",
            "source_type": "tax_law",
            "tax_domain": "income_tax",
            "effective_date": "2026-01-01",
        },
        headers=admin_auth_headers("search-ok"),
    )

    payload = response_json(response)
    result = require_object(payload["result"])
    items = require_object_list(result["items"])

    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["correlation_id"] == "search-ok-corr"
    assert payload["trace_id"] == "search-ok-trace"
    assert result["total"] == 2
    assert [item["source_id"] for item in items] == [
        "KNW-ITA-15-2",
        "KNW-ITA-5-1-B",
    ]


def test_retrieve_returns_matching_records_in_repository_order_for_admin(
    client: TestClient,
) -> None:
    response = client.post(
        "/knowledge/retrieve",
        json={
            "source_ids": ["KNW-ITA-5-1-B", "KNW-ITA-15-2"],
            "anchor_ids": [],
        },
        headers=admin_auth_headers("retrieve-ok"),
    )

    payload = response_json(response)
    result = require_object(payload["result"])
    items = require_object_list(result["items"])

    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert result["total"] == 2
    assert [item["source_id"] for item in items] == [
        "KNW-ITA-15-2",
        "KNW-ITA-5-1-B",
    ]


def test_search_rejects_query_above_limit_with_canonical_error_shape_for_admin(
    client: TestClient,
) -> None:
    too_long_query = "q" * 513

    first = client.post(
        "/knowledge/search",
        json={"query": too_long_query},
        headers=admin_auth_headers("long-query"),
    )
    second = client.post(
        "/knowledge/search",
        json={"query": too_long_query},
        headers=admin_auth_headers("long-query"),
    )

    first_payload = response_json(first)
    second_payload = response_json(second)
    first_detail = require_object(first_payload["detail"])
    second_detail = require_object(second_payload["detail"])

    assert first.status_code == 400
    assert second.status_code == 400
    assert first_detail == second_detail
    assert first_detail["error_code"] == INVALID_KNOWLEDGE_REQUEST
    assert first_detail["message"] == "Knowledge request field `query` is invalid."
    assert first_detail["reason"] == INVALID_KNOWLEDGE_REQUEST


def test_retrieve_rejects_identifier_list_above_limit_for_admin(client: TestClient) -> None:
    response = client.post(
        "/knowledge/retrieve",
        json={
            "source_ids": [f"source-{index}" for index in range(51)],
            "anchor_ids": [],
        },
        headers=admin_auth_headers("too-many-ids"),
    )

    payload = response_json(response)
    detail = require_object(payload["detail"])

    assert response.status_code == 400
    assert detail["error_code"] == INVALID_KNOWLEDGE_REQUEST
    assert detail["message"] == "Knowledge request field `source_ids` is invalid."
    assert detail["reason"] == INVALID_KNOWLEDGE_REQUEST
