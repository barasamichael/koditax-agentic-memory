from __future__ import annotations

from fastapi.testclient import TestClient

from tests.knowledge.support import response_json
from tests.knowledge.support import require_object
from tests.knowledge.support import role_auth_headers
from tests.knowledge.support import admin_auth_headers


def test_protected_search_rejects_oversized_query_canonically(client: TestClient) -> None:
    response = client.post(
        "/knowledge/search",
        json={"query": "a" * 513, "tax_domain": "income_tax"},
        headers=admin_auth_headers("search-too-long"),
    )

    payload = response_json(response)
    detail = require_object(payload["detail"])

    assert response.status_code == 400
    assert detail["error_code"] == "invalid_knowledge_request"
    assert detail["message"] == "Knowledge request field `query` is invalid."
    assert detail["reason"] == "invalid_knowledge_request"
    assert detail["correlation_id"] == "search-too-long-corr"
    assert detail["trace_id"] == "search-too-long-trace"


def test_protected_retrieve_rejects_excessive_identifier_count_canonically(
    client: TestClient,
) -> None:
    response = client.post(
        "/knowledge/retrieve",
        json={
            "source_ids": [f"KNW-{index:03d}" for index in range(51)],
            "anchor_ids": [],
        },
        headers=admin_auth_headers("retrieve-too-many"),
    )

    payload = response_json(response)
    detail = require_object(payload["detail"])

    assert response.status_code == 400
    assert detail["error_code"] == "invalid_knowledge_request"
    assert detail["message"] == "Knowledge request field `source_ids` is invalid."
    assert detail["reason"] == "invalid_knowledge_request"
    assert detail["correlation_id"] == "retrieve-too-many-corr"
    assert detail["trace_id"] == "retrieve-too-many-trace"


def test_management_route_rejects_forbidden_non_admin_role_deterministically(
    client: TestClient,
) -> None:
    response = client.get(
        "/knowledge/sources",
        headers=role_auth_headers(role="TaxAgent", seed="forbidden-role"),
    )

    payload = response_json(response)
    detail = require_object(payload["detail"])

    assert response.status_code == 403
    assert detail["error_code"] == "authorization_role_forbidden"
    assert detail["reason"] == "authorization_role_forbidden"
    assert detail["correlation_id"] == "forbidden-role-corr"
    assert detail["trace_id"] == "forbidden-role-trace"


def test_management_filter_rejects_unsupported_customer_uploaded_origin(
    client: TestClient,
) -> None:
    response = client.get(
        "/knowledge/ingestion",
        params={"source_input_origin": "customer_uploaded_document"},
        headers=role_auth_headers(role="Administrator", seed="bad-origin"),
    )

    payload = response_json(response)
    detail = require_object(payload["detail"])

    assert response.status_code == 400
    assert detail["error_code"] == "invalid_knowledge_request"
    assert detail["message"] == "Knowledge management source input origin filter is unsupported."
    assert detail["reason"] == "invalid_knowledge_request"
