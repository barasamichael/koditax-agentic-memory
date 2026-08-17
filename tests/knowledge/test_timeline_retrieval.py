from __future__ import annotations

from fastapi.testclient import TestClient

from tests.knowledge.support import response_json
from tests.knowledge.support import require_object
from tests.knowledge.support import admin_auth_headers
from tests.knowledge.support import require_object_list
from services.knowledge.app.main import INVALID_KNOWLEDGE_REQUEST


def test_timeline_search_returns_chronology_safe_admin_payload(
    client: TestClient,
) -> None:
    response = client.post(
        "/knowledge/timeline/search",
        json={
            "query": "allowable deductions",
            "tax_domain": "income_tax",
            "start_date": "2025-01-01",
            "end_date": "2026-12-31",
        },
        headers=admin_auth_headers("timeline-ok"),
    )

    payload = response_json(response)
    result = require_object(payload["result"])
    items = require_object_list(result["items"])

    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["correlation_id"] == "timeline-ok-corr"
    assert payload["trace_id"] == "timeline-ok-trace"
    assert result["total"] == 2
    first_item = items[0]
    second_item = items[1]
    assert [first_item["timeline_position"], second_item["timeline_position"]] == [1, 2]
    assert [first_item["publication_state"], second_item["publication_state"]] == [
        "superseded",
        "published",
    ]
    assert [first_item["effective_from"], second_item["effective_from"]] == [
        "2025-01-01",
        "2026-01-01",
    ]
    assert first_item["source_version_id"] == "123e4567-e89b-12d3-a456-426614174100"
    assert second_item["source_version_id"] == "123e4567-e89b-12d3-a456-426614174101"


def test_timeline_search_is_deterministic_for_repeated_identical_requests(
    client: TestClient,
) -> None:
    request_body = {
        "query": "allowable deductions",
        "tax_domain": "income_tax",
        "start_date": "2025-01-01",
        "end_date": "2026-12-31",
    }
    headers = admin_auth_headers("timeline-repeat")

    first = client.post("/knowledge/timeline/search", json=request_body, headers=headers)
    second = client.post("/knowledge/timeline/search", json=request_body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.content == second.content


def test_timeline_search_rejects_invalid_date_range_with_canonical_error_shape(
    client: TestClient,
) -> None:
    response = client.post(
        "/knowledge/timeline/search",
        json={
            "query": "allowable deductions",
            "tax_domain": "income_tax",
            "start_date": "2026-12-31",
            "end_date": "2026-01-01",
        },
        headers=admin_auth_headers("timeline-range"),
    )

    payload = response_json(response)
    detail = require_object(payload["detail"])

    assert response.status_code == 400
    assert detail["error_code"] == INVALID_KNOWLEDGE_REQUEST
    assert detail["message"] == "Knowledge timeline date range is invalid."
    assert detail["reason"] == INVALID_KNOWLEDGE_REQUEST
    assert detail["correlation_id"] == "timeline-range-corr"
    assert detail["trace_id"] == "timeline-range-trace"


def test_timeline_search_rejects_missing_tax_domain_with_canonical_error_shape(
    client: TestClient,
) -> None:
    response = client.post(
        "/knowledge/timeline/search",
        json={
            "query": "allowable deductions",
            "start_date": "2025-01-01",
            "end_date": "2026-12-31",
        },
        headers=admin_auth_headers("timeline-invalid"),
    )

    payload = response_json(response)
    detail = require_object(payload["detail"])

    assert response.status_code == 400
    assert detail["error_code"] == INVALID_KNOWLEDGE_REQUEST
    assert detail["message"] == "Knowledge request field `tax_domain` is invalid."
    assert detail["reason"] == INVALID_KNOWLEDGE_REQUEST
