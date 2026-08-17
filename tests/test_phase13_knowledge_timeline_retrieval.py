"""DB-backed timeline retrieval tests for governed knowledge chronology."""

from __future__ import annotations

from uuid import UUID
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
from tests.knowledge_db_test_support import create_runtime_harness
from tests.knowledge_db_test_support import KnowledgeRuntimeHarness
from tests.knowledge_db_test_support import build_admin_auth_headers
from tests.knowledge_db_test_support import ensure_knowledge_migration_applied


@pytest.fixture()
def db_connection() -> Iterator[psycopg.Connection]:
    """Create DB connection for governed knowledge timeline tests."""

    database_url = load_database_url()
    if database_url is None or not database_url.strip():
        pytest.skip("DATABASE_URL is not set; skipping knowledge timeline DB tests.")

    ensure_knowledge_migration_applied(database_url=database_url)
    try:
        connection = psycopg.connect(database_url, connect_timeout=5)
    except psycopg.OperationalError:
        pytest.skip("DATABASE_URL is not reachable; skipping knowledge timeline DB tests.")

    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def harness(db_connection: psycopg.Connection) -> KnowledgeRuntimeHarness:
    """Build one runtime harness for governed timeline tests."""

    return create_runtime_harness(connection=db_connection)


def test_timeline_search_returns_historically_ordered_records_across_windows(
    harness: KnowledgeRuntimeHarness,
) -> None:
    seed = uuid4().hex
    shared_source_id = f"KNW-TL-{seed}"
    shared_family_id = f"KNW-TL-FAMILY-{seed}"
    search_token = f"timeline-token-{seed}"
    predecessor = _publish_version(
        harness=harness,
        seed=f"{seed}-pred",
        shared_source_id=shared_source_id,
        shared_family_id=shared_family_id,
        search_token=search_token,
        effective_from="2025-01-01",
        effective_to="2025-12-31",
    )
    successor = _publish_version(
        harness=harness,
        seed=f"{seed}-succ",
        shared_source_id=shared_source_id,
        shared_family_id=shared_family_id,
        search_token=search_token,
        effective_from="2026-01-01",
        effective_to=None,
    )

    with TestClient(harness.app) as client:
        first = client.post(
            "/knowledge/timeline/search",
            json={
                "query": search_token,
                "tax_domain": "income_tax",
                "start_date": "2025-01-01",
                "end_date": "2026-12-31",
            },
            headers=_stable_headers("knowledge-timeline-range"),
        )
        second = client.post(
            "/knowledge/timeline/search",
            json={
                "query": search_token,
                "tax_domain": "income_tax",
                "start_date": "2025-01-01",
                "end_date": "2026-12-31",
            },
            headers=_stable_headers("knowledge-timeline-range"),
        )

    first_items = _result_items(_json(first))
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.content == second.content
    assert _timeline_positions(first_items) == (1, 2)
    assert _anchor_ids(first_items) == (predecessor["anchor_id"], successor["anchor_id"])


def test_superseded_versions_remain_historically_visible_in_timeline_results(
    harness: KnowledgeRuntimeHarness,
) -> None:
    seed = uuid4().hex
    shared_source_id = f"KNW-TL-SUP-{seed}"
    shared_family_id = f"KNW-TL-SUP-FAMILY-{seed}"
    search_token = f"timeline-superseded-{seed}"
    predecessor = _publish_version(
        harness=harness,
        seed=f"{seed}-pred",
        shared_source_id=shared_source_id,
        shared_family_id=shared_family_id,
        search_token=search_token,
        effective_from="2025-01-01",
        effective_to="2025-12-31",
    )
    successor = _publish_version(
        harness=harness,
        seed=f"{seed}-succ",
        shared_source_id=shared_source_id,
        shared_family_id=shared_family_id,
        search_token=search_token,
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
        timeline = client.post(
            "/knowledge/timeline/search",
            json={
                "query": search_token,
                "tax_domain": "income_tax",
                "start_date": "2025-01-01",
                "end_date": "2026-12-31",
            },
        )
        current_only = client.post(
            "/knowledge/timeline/search",
            json={
                "query": search_token,
                "tax_domain": "income_tax",
                "start_date": "2026-01-01",
                "end_date": "2026-12-31",
            },
        )

    timeline_items = _result_items(_json(timeline))
    current_items = _result_items(_json(current_only))
    assert supersede.status_code == 200
    assert _anchor_ids(timeline_items) == (predecessor["anchor_id"], successor["anchor_id"])
    assert tuple(str(item["publication_state"]) for item in timeline_items) == (
        "superseded",
        "published",
    )
    assert _anchor_ids(current_items) == (successor["anchor_id"],)


def test_archived_versions_are_excluded_from_timeline_search(
    harness: KnowledgeRuntimeHarness,
) -> None:
    seed = uuid4().hex
    shared_source_id = f"KNW-TL-ARCH-{seed}"
    shared_family_id = f"KNW-TL-ARCH-FAMILY-{seed}"
    search_token = f"timeline-archive-{seed}"
    predecessor = _publish_version(
        harness=harness,
        seed=f"{seed}-pred",
        shared_source_id=shared_source_id,
        shared_family_id=shared_family_id,
        search_token=search_token,
        effective_from="2025-01-01",
        effective_to="2025-12-31",
    )
    successor = _publish_version(
        harness=harness,
        seed=f"{seed}-succ",
        shared_source_id=shared_source_id,
        shared_family_id=shared_family_id,
        search_token=search_token,
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
        archive = client.post(
            f"/knowledge/source-versions/{predecessor['source_version_id']}/archive",
            json={"archived_by": str(uuid4())},
            headers=admin_headers,
        )
        timeline = client.post(
            "/knowledge/timeline/search",
            json={
                "query": search_token,
                "tax_domain": "income_tax",
                "start_date": "2025-01-01",
                "end_date": "2026-12-31",
            },
        )

    assert supersede.status_code == 200
    assert archive.status_code == 200
    assert _anchor_ids(_result_items(_json(timeline))) == (successor["anchor_id"],)


def test_invalid_timeline_date_range_fails_canonically(harness: KnowledgeRuntimeHarness) -> None:
    with TestClient(harness.app) as client:
        response = client.post(
            "/knowledge/timeline/search",
            json={
                "query": "income tax",
                "tax_domain": "income_tax",
                "start_date": "2026-12-31",
                "end_date": "2026-01-01",
            },
            headers=_stable_headers("knowledge-timeline-invalid"),
        )

    detail = _canonical_detail(_json(response))
    assert response.status_code == 400
    assert detail["reason_code"] == "invalid_knowledge_request"


def test_unpublished_records_do_not_appear_in_timeline_results(
    harness: KnowledgeRuntimeHarness,
) -> None:
    seed = uuid4().hex
    requested_by = str(uuid4())
    reviewer_id = str(uuid4())
    search_token = f"timeline-unpublished-{seed}"
    file_payload = _file_ingestion_payload(
        requested_by=requested_by,
        idempotency_key=f"timeline-unpublished-{seed}",
        filename="timeline-unpublished.pdf",
        file_bytes=f"timeline-unpublished-{seed}".encode(),
        source_class="tax_law",
    )
    publication_payload = _publication_payload(
        seed=seed,
        shared_source_id=f"KNW-TL-UNPUB-{seed}",
        shared_family_id=f"KNW-TL-UNPUB-FAMILY-{seed}",
        search_token=search_token,
        effective_from="2026-01-01",
        effective_to=None,
    )

    with TestClient(harness.app) as client:
        admin_headers = build_admin_auth_headers()
        ingest = client.post("/knowledge/ingestion/files", json=file_payload, headers=admin_headers)
        ingestion_job_id = str(cast(dict[str, object], _json(ingest)["result"])["ingestion_job_id"])
        approve = client.post(
            f"/knowledge/ingestion/{ingestion_job_id}/approve",
            json={
                "reviewed_by": reviewer_id,
                "review_notes": [{"note": "approved but intentionally unpublished"}],
                "publication_payload": publication_payload,
            },
            headers=admin_headers,
        )
        timeline = client.post(
            "/knowledge/timeline/search",
            json={
                "query": search_token,
                "tax_domain": "income_tax",
                "start_date": "2026-01-01",
                "end_date": "2026-12-31",
            },
        )

    assert approve.status_code == 200
    assert _result_total(_json(timeline)) == 0


def test_customer_document_lineage_cannot_surface_through_timeline_retrieval(
    db_connection: psycopg.Connection,
    harness: KnowledgeRuntimeHarness,
) -> None:
    seed = uuid4().hex
    search_token = f"timeline-customer-{seed}"
    published = _publish_version(
        harness=harness,
        seed=f"{seed}-published",
        shared_source_id=f"KNW-TL-CUSTOMER-{seed}",
        shared_family_id=f"KNW-TL-CUSTOMER-FAMILY-{seed}",
        search_token=search_token,
        effective_from="2026-01-01",
        effective_to=None,
    )

    try:
        with pytest.raises(psycopg.Error):
            with db_connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO knowledge_source_versions (
                        id,
                        source_id,
                        document_id,
                        point_in_time_url,
                        source_checksum_sha256,
                        source_version_form,
                        source_input_origin,
                        source_input_ref,
                        publication_state,
                        effective_from,
                        effective_to,
                        tax_year
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        UUID(str(uuid4())),
                        published["source_id"],
                        None,
                        f"https://example.com/customer/{seed}",
                        f"customer-checksum-{seed}",
                        "as_issued",
                        "customer_uploaded_document",
                        f"customer-ref-{seed}",
                        "published",
                        "2027-01-01",
                        None,
                        2027,
                    ),
                )
    finally:
        db_connection.rollback()

    with TestClient(harness.app) as client:
        timeline = client.post(
            "/knowledge/timeline/search",
            json={
                "query": search_token,
                "tax_domain": "income_tax",
                "start_date": "2026-01-01",
                "end_date": "2026-12-31",
            },
        )

    assert _anchor_ids(_result_items(_json(timeline))) == (published["anchor_id"],)


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
        idempotency_key=f"idem-timeline-{seed}",
        filename=f"timeline-{seed}.pdf",
        file_bytes=f"timeline-file-{seed}".encode(),
        source_class="tax_law",
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
                "review_notes": [{"note": f"timeline-review-{seed}"}],
                "proposed_source_updates": {"workflow_seed": seed},
            },
            headers=admin_headers,
        )
        approve = client.post(
            f"/knowledge/ingestion/{ingestion_job_id}/approve",
            json={
                "reviewed_by": reviewer_id,
                "review_notes": [{"note": f"timeline-approved-{seed}"}],
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
    proposed_source_record = cast(dict[str, object], publish_result["proposed_source_record"])
    return {
        "source_id": shared_source_id,
        "source_version_id": str(proposed_source_record["published_source_version_id"]),
        "anchor_id": f"anchor-{seed}",
    }


def _file_ingestion_payload(
    *,
    requested_by: str,
    idempotency_key: str,
    filename: str,
    file_bytes: bytes,
    source_class: str,
) -> dict[str, object]:
    return {
        "requested_by": requested_by,
        "idempotency_key": idempotency_key,
        "filename": filename,
        "mime_type": "application/pdf",
        "file_content_base64": base64.b64encode(file_bytes).decode("ascii"),
        "source_input_origin": "official_source_upload",
        "source_class": source_class,
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
        "title": f"Finance Act timeline source {shared_source_id}",
        "source_class": "tax_law",
        "authority_level": "statute",
        "tax_domain": "income_tax",
        "issuing_authority": "Kenya Revenue Authority",
        "point_in_time_url": f"https://example.com/timeline/{shared_source_id}",
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


def _json(response: Any) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


def _canonical_detail(payload: dict[str, object]) -> dict[str, object]:
    detail = cast(dict[str, object], payload["detail"])
    return {
        "error_code": detail["error_code"],
        "message": detail["message"],
        "reason": detail["reason"],
        "reason_code": detail["reason_code"],
    }


def _result_total(payload: dict[str, object]) -> int:
    result = cast(dict[str, object], payload["result"])
    return require_int(result["total"])


def _result_items(payload: dict[str, object]) -> tuple[dict[str, object], ...]:
    result = cast(dict[str, object], payload["result"])
    items = cast(list[object], result["items"])
    return tuple(cast(dict[str, object], item) for item in items)


def _anchor_ids(items: tuple[dict[str, object], ...]) -> tuple[str, ...]:
    return tuple(str(item["anchor_id"]) for item in items)


def _timeline_positions(items: tuple[dict[str, object], ...]) -> tuple[int, ...]:
    return tuple(require_int(item["timeline_position"]) for item in items)


def _stable_headers(seed: str) -> dict[str, str]:
    return {
        "X-Correlation-ID": f"{seed}-corr",
        "X-Trace-ID": f"{seed}-trace",
    }
