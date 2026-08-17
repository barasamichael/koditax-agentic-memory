"""Performance-smoke tests for governed knowledge runtime determinism."""

from __future__ import annotations

from uuid import uuid4
import base64
from typing import Any
from typing import cast
from collections.abc import Iterator

import pytest
import psycopg
from fastapi.testclient import TestClient

from tests.knowledge_db_test_support import load_database_url
from tests.knowledge_db_test_support import require_object_dict
from tests.knowledge_db_test_support import create_runtime_harness
from tests.knowledge_db_test_support import KnowledgeRuntimeHarness
from tests.knowledge_db_test_support import build_admin_auth_headers
from tests.knowledge_db_test_support import ensure_knowledge_migration_applied


@pytest.fixture()
def db_connection() -> Iterator[psycopg.Connection]:
    """Create DB connection for governed knowledge performance-smoke tests."""

    database_url = load_database_url()
    if database_url is None or not database_url.strip():
        pytest.skip("DATABASE_URL is not set; skipping knowledge performance-smoke DB tests.")

    ensure_knowledge_migration_applied(database_url=database_url)
    try:
        connection = psycopg.connect(database_url, connect_timeout=5)
    except psycopg.OperationalError:
        pytest.skip("DATABASE_URL is not reachable; skipping knowledge performance-smoke DB tests.")

    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def harness(db_connection: psycopg.Connection) -> KnowledgeRuntimeHarness:
    """Build one runtime harness for governed knowledge performance-smoke tests."""

    return create_runtime_harness(connection=db_connection)


def test_search_is_deterministic_on_a_modest_governed_dataset(
    harness: KnowledgeRuntimeHarness,
) -> None:
    family_seed = uuid4().hex
    query_token = f"performance-smoke-token-{family_seed}"
    for index in range(8):
        _publish_version(
            harness=harness,
            seed=f"{family_seed}-{index}",
            source_id=f"KNW-PERF-{family_seed}-{index}",
            source_family_id=f"KNW-PERF-FAMILY-{family_seed}-{index}",
            search_token=query_token,
            effective_from=f"202{index % 3 + 4}-01-01",
        )

    with TestClient(harness.app) as client:
        first_search = client.post(
            "/knowledge/search",
            json={"query": query_token, "tax_domain": "income_tax"},
        )
        second_search = client.post(
            "/knowledge/search",
            json={"query": query_token, "tax_domain": "income_tax"},
        )

    first_payload = _json(first_search)
    second_payload = _json(second_search)
    assert first_search.status_code == 200
    assert second_search.status_code == 200
    assert first_payload["result"] == second_payload["result"]
    result = require_object_dict(first_payload["result"])
    items = cast(list[object], result["items"])
    assert len(items) >= 8


def test_bulk_ingestion_preserves_stable_outcome_order_for_larger_batches(
    harness: KnowledgeRuntimeHarness,
) -> None:
    acting_user = str(uuid4())
    batch_seed = uuid4().hex
    items = [
        {
            "idempotency_key": f"perf-bulk-{batch_seed}-{index}",
            "filename": f"perf-bulk-{batch_seed}-{index}.pdf",
            "mime_type": "application/pdf",
            "file_content_base64": base64.b64encode(
                f"performance-bulk-{batch_seed}-{index}".encode()
            ).decode("ascii"),
            "source_input_origin": "official_source_upload",
            "source_class": "tax_law",
        }
        for index in range(20)
    ]
    payload = {"acting_user": acting_user, "items": items}

    with TestClient(harness.app) as client:
        admin_headers = build_admin_auth_headers()
        first_response = client.post(
            "/knowledge/ingestion/files/bulk",
            json=payload,
            headers=admin_headers,
        )
        second_response = client.post(
            "/knowledge/ingestion/files/bulk",
            json=payload,
            headers=admin_headers,
        )

    first_payload = _json(first_response)
    second_payload = _json(second_response)
    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_payload["result"] == second_payload["result"]

    result = require_object_dict(first_payload["result"])
    batch_items = cast(list[object], result["items"])
    assert [require_object_dict(item)["idempotency_key"] for item in batch_items] == [
        entry["idempotency_key"] for entry in items
    ]


def _publish_version(
    *,
    harness: KnowledgeRuntimeHarness,
    seed: str,
    source_id: str,
    source_family_id: str,
    search_token: str,
    effective_from: str,
) -> None:
    file_payload = {
        "requested_by": str(uuid4()),
        "idempotency_key": f"perf-publish-{seed}",
        "filename": f"perf-{seed}.pdf",
        "mime_type": "application/pdf",
        "file_content_base64": base64.b64encode(f"perf-{seed}".encode()).decode("ascii"),
        "source_input_origin": "official_source_upload",
        "source_class": "tax_law",
    }
    publication_payload = {
        "source_id": source_id,
        "source_family_id": source_family_id,
        "title": f"Performance governed source {seed}",
        "source_class": "tax_law",
        "authority_level": "statute",
        "tax_domain": "income_tax",
        "issuing_authority": "Kenya Revenue Authority",
        "point_in_time_url": f"https://example.com/performance/{seed}",
        "source_version_form": "as_issued",
        "effective_from": effective_from,
        "effective_to": None,
        "tax_year": int(effective_from[:4]),
        "anchors": [
            {
                "anchor_id": f"anchor-{seed}",
                "anchor_title": f"Anchor {seed}",
                "anchor_path": f"path-{seed}",
                "anchor_text": f"Anchor text for {search_token}",
                "temporal_scope_from": effective_from,
                "temporal_scope_to": None,
                "chunks": [{"chunk_text": f"Chunk text for {search_token} {seed}"}],
            }
        ],
    }

    with TestClient(harness.app) as client:
        admin_headers = build_admin_auth_headers()
        ingest = client.post("/knowledge/ingestion/files", json=file_payload, headers=admin_headers)
        ingestion_job_id = str(cast(dict[str, object], _json(ingest)["result"])["ingestion_job_id"])
        approve = client.post(
            f"/knowledge/ingestion/{ingestion_job_id}/approve",
            json={
                "reviewed_by": str(uuid4()),
                "review_notes": [{"note": f"approved-{seed}"}],
                "publication_payload": publication_payload,
            },
            headers=admin_headers,
        )
        publish = client.post(
            f"/knowledge/ingestion/{ingestion_job_id}/publish",
            json={"published_by": str(uuid4())},
            headers=admin_headers,
        )

    assert ingest.status_code == 200
    assert approve.status_code == 200
    assert publish.status_code == 200


def _json(response: Any) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)
