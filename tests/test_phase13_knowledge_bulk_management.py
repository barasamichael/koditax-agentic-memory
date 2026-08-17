"""DB-backed tests for governed bulk knowledge management behavior."""

from __future__ import annotations

import json
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
    """Create DB connection for governed knowledge bulk-management tests."""

    database_url = load_database_url()
    if database_url is None or not database_url.strip():
        pytest.skip("DATABASE_URL is not set; skipping knowledge bulk DB tests.")

    ensure_knowledge_migration_applied(database_url=database_url)
    try:
        connection = psycopg.connect(database_url, connect_timeout=5)
    except psycopg.OperationalError:
        pytest.skip("DATABASE_URL is not reachable; skipping knowledge bulk DB tests.")

    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def harness(db_connection: psycopg.Connection) -> KnowledgeRuntimeHarness:
    """Build one runtime harness for governed knowledge bulk tests."""

    return create_runtime_harness(connection=db_connection)


def test_bulk_publish_is_deterministic_and_partial_failures_are_canonical(
    harness: KnowledgeRuntimeHarness,
) -> None:
    publisher_id = str(uuid4())
    first = _prepare_approved_ingestion(
        harness=harness,
        seed=f"{uuid4().hex}-one",
        shared_source_id=f"KNW-BULK-PUB-{uuid4().hex}",
        shared_family_id=f"KNW-BULK-PUB-FAMILY-{uuid4().hex}",
        search_token=f"bulk-publish-search-{uuid4().hex}",
        effective_from="2026-01-01",
        effective_to=None,
    )
    second = _prepare_approved_ingestion(
        harness=harness,
        seed=f"{uuid4().hex}-two",
        shared_source_id=f"KNW-BULK-PUB-{uuid4().hex}",
        shared_family_id=f"KNW-BULK-PUB-FAMILY-{uuid4().hex}",
        search_token=f"bulk-publish-search-{uuid4().hex}",
        effective_from="2025-01-01",
        effective_to="2025-12-31",
    )
    non_approved = _ingest_only(
        harness=harness,
        seed=f"{uuid4().hex}-raw",
    )
    bulk_payload = {
        "acting_user": publisher_id,
        "ids": [
            second["ingestion_job_id"],
            non_approved["ingestion_job_id"],
            first["ingestion_job_id"],
            second["ingestion_job_id"],
        ],
    }

    with TestClient(harness.app) as client:
        admin_headers = build_admin_auth_headers()
        first_publish = client.post(
            "/knowledge/ingestion/bulk/publish",
            json=bulk_payload,
            headers=admin_headers,
        )
        second_publish = client.post(
            "/knowledge/ingestion/bulk/publish",
            json=bulk_payload,
            headers=admin_headers,
        )
        first_search = client.post(
            "/knowledge/search",
            json={"query": first["search_token"], "tax_domain": "income_tax"},
        )
        second_search = client.post(
            "/knowledge/search",
            json={
                "query": second["search_token"],
                "tax_domain": "income_tax",
                "effective_date": "2025-06-01",
            },
        )
        non_approved_search = client.post(
            "/knowledge/search",
            json={"query": non_approved["search_token"], "tax_domain": "income_tax"},
        )

    first_payload = _json(first_publish)
    second_payload = _json(second_publish)
    first_result = require_object_dict(first_payload["result"])
    items = _items(first_payload)
    assert first_publish.status_code == 200
    assert second_publish.status_code == 200
    assert first_payload["result"] == second_payload["result"]
    assert first_result["bulk_status"] == "partial_failure"
    assert [str(item["id"]) for item in items] == [
        second["ingestion_job_id"],
        non_approved["ingestion_job_id"],
        first["ingestion_job_id"],
    ]
    assert [str(item["outcome"]) for item in items] == [
        "published",
        "failed",
        "published",
    ]
    assert _items(_json(first_publish))[1]["error_code"] == "invalid_publication_state_transition"
    assert _result_total(_json(first_search)) == 1
    assert _result_total(_json(second_search)) == 1
    assert _result_total(_json(non_approved_search)) == 0


def test_bulk_reject_is_replay_safe_and_keeps_jobs_non_searchable(
    harness: KnowledgeRuntimeHarness,
) -> None:
    acting_user = str(uuid4())
    first = _ingest_only(harness=harness, seed=f"{uuid4().hex}-reject-a")
    second = _ingest_only(harness=harness, seed=f"{uuid4().hex}-reject-b")
    payload = {
        "acting_user": acting_user,
        "ids": [
            first["ingestion_job_id"],
            second["ingestion_job_id"],
            first["ingestion_job_id"],
        ],
        "review_notes": [{"note": "bulk reject"}],
    }

    with TestClient(harness.app) as client:
        admin_headers = build_admin_auth_headers()
        first_reject = client.post(
            "/knowledge/ingestion/bulk/reject",
            json=payload,
            headers=admin_headers,
        )
        second_reject = client.post(
            "/knowledge/ingestion/bulk/reject",
            json=payload,
            headers=admin_headers,
        )
        rejected_list = client.get(
            "/knowledge/ingestion",
            params={
                "requested_by": first["requested_by"],
                "ingestion_state": "rejected",
                "limit": 10,
                "offset": 0,
                "sort_by": "created_at",
                "sort_order": "desc",
            },
            headers=admin_headers,
        )
        search = client.post(
            "/knowledge/search",
            json={"query": first["search_token"], "tax_domain": "income_tax"},
        )

    first_payload = _json(first_reject)
    second_payload = _json(second_reject)
    assert first_reject.status_code == 200
    assert second_reject.status_code == 200
    assert first_payload["result"] == second_payload["result"]
    assert require_object_dict(first_payload["result"])["bulk_status"] == "full_success"
    assert [str(item["outcome"]) for item in _items(first_payload)] == ["rejected", "rejected"]
    assert _result_total(_json(rejected_list)) >= 1
    assert _result_total(_json(search)) == 0


def test_bulk_archive_is_replay_safe_for_superseded_versions(
    harness: KnowledgeRuntimeHarness,
) -> None:
    seed = uuid4().hex
    first_predecessor = _publish_version(
        harness=harness,
        seed=f"{seed}-pred-a",
        shared_source_id=f"KNW-BULK-ARCH-{seed}-A",
        shared_family_id=f"KNW-BULK-ARCH-FAM-{seed}-A",
        search_token=f"bulk-archive-search-{seed}-a",
        effective_from="2024-01-01",
        effective_to="2024-12-31",
    )
    first_successor = _publish_version(
        harness=harness,
        seed=f"{seed}-succ-a",
        shared_source_id=first_predecessor["source_id"],
        shared_family_id=first_predecessor["source_family_id"],
        search_token=f"bulk-archive-search-{seed}-a",
        effective_from="2025-01-01",
        effective_to=None,
    )
    second_predecessor = _publish_version(
        harness=harness,
        seed=f"{seed}-pred-b",
        shared_source_id=f"KNW-BULK-ARCH-{seed}-B",
        shared_family_id=f"KNW-BULK-ARCH-FAM-{seed}-B",
        search_token=f"bulk-archive-search-{seed}-b",
        effective_from="2023-01-01",
        effective_to="2023-12-31",
    )
    second_successor = _publish_version(
        harness=harness,
        seed=f"{seed}-succ-b",
        shared_source_id=second_predecessor["source_id"],
        shared_family_id=second_predecessor["source_family_id"],
        search_token=f"bulk-archive-search-{seed}-b",
        effective_from="2024-01-01",
        effective_to=None,
    )
    acting_user = str(uuid4())

    with TestClient(harness.app) as client:
        admin_headers = build_admin_auth_headers()
        first_supersede = client.post(
            f"/knowledge/source-versions/{first_predecessor['source_version_id']}/supersede",
            json={
                "successor_source_version_id": first_successor["source_version_id"],
                "superseded_by": acting_user,
            },
            headers=admin_headers,
        )
        second_supersede = client.post(
            f"/knowledge/source-versions/{second_predecessor['source_version_id']}/supersede",
            json={
                "successor_source_version_id": second_successor["source_version_id"],
                "superseded_by": acting_user,
            },
            headers=admin_headers,
        )
        bulk_payload = {
            "acting_user": acting_user,
            "ids": [
                second_predecessor["source_version_id"],
                first_predecessor["source_version_id"],
                second_predecessor["source_version_id"],
            ],
        }
        first_archive = client.post(
            "/knowledge/source-versions/bulk/archive",
            json=bulk_payload,
            headers=admin_headers,
        )
        second_archive = client.post(
            "/knowledge/source-versions/bulk/archive",
            json=bulk_payload,
            headers=admin_headers,
        )
        first_detail = client.get(
            f"/knowledge/source-versions/{first_predecessor['source_version_id']}",
            headers=admin_headers,
        )
        second_detail = client.get(
            f"/knowledge/source-versions/{second_predecessor['source_version_id']}",
            headers=admin_headers,
        )
        historical_search = client.post(
            "/knowledge/search",
            json={
                "query": first_predecessor["search_token"],
                "tax_domain": "income_tax",
                "effective_date": "2024-06-01",
            },
        )

    assert first_supersede.status_code == 200
    assert second_supersede.status_code == 200
    assert first_archive.status_code == 200
    assert second_archive.status_code == 200
    assert _json(first_archive)["result"] == _json(second_archive)["result"]
    assert require_object_dict(_json(first_archive)["result"])["bulk_status"] == "full_success"
    assert [str(item["outcome"]) for item in _items(_json(first_archive))] == [
        "archived",
        "archived",
    ]
    first_detail_result = require_object_dict(_json(first_detail)["result"])
    second_detail_result = require_object_dict(_json(second_detail)["result"])
    assert first_detail_result["publication_state"] == "archived"
    assert second_detail_result["publication_state"] == "archived"
    assert _result_total(_json(historical_search)) == 0


def test_bulk_archive_rejects_invalid_active_published_versions_canonically(
    harness: KnowledgeRuntimeHarness,
) -> None:
    published = _publish_version(
        harness=harness,
        seed=f"{uuid4().hex}-active",
        shared_source_id=f"KNW-ACTIVE-{uuid4().hex}",
        shared_family_id=f"KNW-ACTIVE-FAMILY-{uuid4().hex}",
        search_token=f"active-search-{uuid4().hex}",
        effective_from="2026-01-01",
        effective_to=None,
    )

    with TestClient(harness.app) as client:
        response = client.post(
            "/knowledge/source-versions/bulk/archive",
            json={"acting_user": str(uuid4()), "ids": [published["source_version_id"]]},
            headers=build_admin_auth_headers(),
        )

    payload = _json(response)
    assert response.status_code == 200
    assert require_object_dict(payload["result"])["bulk_status"] == "full_rejection"
    item = _items(payload)[0]
    assert item["error_code"] == "invalid_publication_state_transition"


def test_bulk_publish_rejects_customer_document_lineage_canonically(
    harness: KnowledgeRuntimeHarness,
) -> None:
    approved = _prepare_approved_ingestion(
        harness=harness,
        seed=f"{uuid4().hex}-cust",
        shared_source_id=f"KNW-CUST-{uuid4().hex}",
        shared_family_id=f"KNW-CUST-FAMILY-{uuid4().hex}",
        search_token=f"customer-lineage-{uuid4().hex}",
        effective_from="2026-01-01",
        effective_to=None,
    )
    _mutate_ingestion_source_origin(
        harness=harness,
        ingestion_job_id=approved["ingestion_job_id"],
        source_input_origin="customer_uploaded_document",
        source_input_ref=f"customer-upload://{approved['ingestion_job_id']}",
    )

    with TestClient(harness.app) as client:
        publish = client.post(
            "/knowledge/ingestion/bulk/publish",
            json={
                "acting_user": str(uuid4()),
                "ids": [approved["ingestion_job_id"]],
            },
            headers=build_admin_auth_headers(),
        )
        search = client.post(
            "/knowledge/search",
            json={"query": approved["search_token"], "tax_domain": "income_tax"},
        )

    payload = _json(publish)
    assert publish.status_code == 200
    assert require_object_dict(payload["result"])["bulk_status"] == "full_rejection"
    assert _items(payload)[0]["error_code"] == "invalid_knowledge_lineage"
    assert _result_total(_json(search)) == 0


def _prepare_approved_ingestion(
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
    ingestion_payload = _file_ingestion_payload(
        requested_by=requested_by,
        idempotency_key=f"bulk-approve-{seed}",
        filename=f"finance-act-{seed}.pdf",
        file_bytes=f"official-{seed}".encode(),
    )
    publication_payload = _publication_payload(
        seed=seed,
        shared_source_id=shared_source_id,
        shared_family_id=shared_family_id,
        search_token=search_token,
        effective_from=effective_from,
        effective_to=effective_to,
    )

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

    assert ingest.status_code == 200
    assert review.status_code == 200
    assert approve.status_code == 200
    return {
        "ingestion_job_id": ingestion_job_id,
        "requested_by": requested_by,
        "search_token": search_token,
    }


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
    approved = _prepare_approved_ingestion(
        harness=harness,
        seed=seed,
        shared_source_id=shared_source_id,
        shared_family_id=shared_family_id,
        search_token=search_token,
        effective_from=effective_from,
        effective_to=effective_to,
    )

    with TestClient(harness.app) as client:
        publish = client.post(
            f"/knowledge/ingestion/{approved['ingestion_job_id']}/publish",
            json={"published_by": str(uuid4())},
            headers=build_admin_auth_headers(),
        )

    assert publish.status_code == 200
    publish_result = cast(dict[str, object], _json(publish)["result"])
    proposed_source_record = require_object_dict(publish_result["proposed_source_record"])
    return {
        "source_id": shared_source_id,
        "source_family_id": shared_family_id,
        "source_version_id": str(proposed_source_record["published_source_version_id"]),
        "search_token": search_token,
    }


def _ingest_only(*, harness: KnowledgeRuntimeHarness, seed: str) -> dict[str, str]:
    requested_by = str(uuid4())
    payload = _file_ingestion_payload(
        requested_by=requested_by,
        idempotency_key=f"bulk-ingest-{seed}",
        filename=f"raw-{seed}.pdf",
        file_bytes=f"raw-{seed}".encode(),
    )
    with TestClient(harness.app) as client:
        ingest = client.post(
            "/knowledge/ingestion/files",
            json=payload,
            headers=build_admin_auth_headers(),
        )

    assert ingest.status_code == 200
    result = cast(dict[str, object], _json(ingest)["result"])
    return {
        "ingestion_job_id": str(result["ingestion_job_id"]),
        "requested_by": requested_by,
        "search_token": seed,
    }


def _publication_payload(
    *,
    seed: str,
    shared_source_id: str,
    shared_family_id: str,
    search_token: str,
    effective_from: str,
    effective_to: str | None,
) -> dict[str, object]:
    return {
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


def _mutate_ingestion_source_origin(
    *,
    harness: KnowledgeRuntimeHarness,
    ingestion_job_id: str,
    source_input_origin: str,
    source_input_ref: str,
) -> None:
    with harness.connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT proposed_source_record
            FROM knowledge_ingestion_jobs
            WHERE id = %s
            """,
            (ingestion_job_id,),
        )
        row = cursor.fetchone()
        assert row is not None
        proposed_source_record = require_object_dict(row[0])
        proposed_source_record["source_input_origin"] = source_input_origin
        proposed_source_record["source_input_ref"] = source_input_ref
        cursor.execute(
            """
            UPDATE knowledge_ingestion_jobs
            SET proposed_source_record = %s::jsonb
            WHERE id = %s
            """,
            (json.dumps(proposed_source_record, sort_keys=True), ingestion_job_id),
        )
    harness.connection.commit()


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


def _json(response: Any) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


def _result_total(payload: dict[str, object]) -> int:
    result = require_object_dict(payload["result"])
    return require_int(result["total"])


def _items(payload: dict[str, object]) -> tuple[dict[str, object], ...]:
    result = require_object_dict(payload["result"])
    items = cast(list[object], result["items"])
    return tuple(cast(dict[str, object], item) for item in items)
