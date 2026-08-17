"""DB-backed supersession and archive workflow tests for governed knowledge items."""

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
from tests.knowledge_db_test_support import require_object_dict
from tests.knowledge_db_test_support import create_runtime_harness
from tests.knowledge_db_test_support import KnowledgeRuntimeHarness
from tests.knowledge_db_test_support import build_admin_auth_headers
from tests.knowledge_db_test_support import ensure_knowledge_migration_applied


@pytest.fixture()
def db_connection() -> Iterator[psycopg.Connection]:
    """Create DB connection for governed knowledge supersession tests."""

    database_url = load_database_url()
    if database_url is None or not database_url.strip():
        pytest.skip("DATABASE_URL is not set; skipping knowledge supersession DB tests.")

    ensure_knowledge_migration_applied(database_url=database_url)
    try:
        connection = psycopg.connect(database_url, connect_timeout=5)
    except psycopg.OperationalError:
        pytest.skip("DATABASE_URL is not reachable; skipping knowledge supersession DB tests.")

    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def harness(db_connection: psycopg.Connection) -> KnowledgeRuntimeHarness:
    """Build one runtime harness for governed supersession tests."""

    return create_runtime_harness(connection=db_connection)


def test_same_source_versions_can_be_superseded_and_historical_search_stays_safe(
    harness: KnowledgeRuntimeHarness,
) -> None:
    seed = uuid4().hex
    shared_source_id = f"KNW-SUP-{seed}"
    shared_family_id = f"KNW-SUP-FAMILY-{seed}"
    search_token = f"supersession-token-{seed}"
    predecessor = _publish_version(
        harness=harness,
        seed=f"{seed}-pred",
        shared_source_id=shared_source_id,
        shared_family_id=shared_family_id,
        search_token=search_token,
        effective_from="2025-01-01",
        effective_to="2025-12-31",
        point_in_time_suffix="2025",
    )
    successor = _publish_version(
        harness=harness,
        seed=f"{seed}-succ",
        shared_source_id=shared_source_id,
        shared_family_id=shared_family_id,
        search_token=search_token,
        effective_from="2026-01-01",
        effective_to=None,
        point_in_time_suffix="2026",
    )
    headers = _stable_headers("knowledge-supersede-success")

    with TestClient(harness.app) as client:
        admin_headers = build_admin_auth_headers()
        first = client.post(
            f"/knowledge/source-versions/{predecessor['source_version_id']}/supersede",
            json={
                "successor_source_version_id": successor["source_version_id"],
                "superseded_by": str(uuid4()),
            },
            headers={**headers, **admin_headers},
        )
        second = client.post(
            f"/knowledge/source-versions/{predecessor['source_version_id']}/supersede",
            json={
                "successor_source_version_id": successor["source_version_id"],
                "superseded_by": str(uuid4()),
            },
            headers={**headers, **admin_headers},
        )
        present_search = client.post(
            "/knowledge/search",
            json={"query": search_token, "tax_domain": "income_tax"},
        )
        historical_search = client.post(
            "/knowledge/search",
            json={
                "query": search_token,
                "tax_domain": "income_tax",
                "effective_date": "2025-06-01",
            },
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
        retrieve = client.post(
            "/knowledge/retrieve",
            json={
                "source_ids": [],
                "anchor_ids": [predecessor["anchor_id"]],
            },
        )

    first_payload = _json(first)
    present_items = _result_items(_json(present_search))
    historical_items = _result_items(_json(historical_search))
    timeline_items = _result_items(_json(timeline))
    retrieve_items = _result_items(_json(retrieve))

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.content == second.content
    assert cast(dict[str, object], first_payload["result"])["publication_state"] == "superseded"
    assert (
        cast(dict[str, object], first_payload["result"])["superseded_by_source_version_id"]
        == successor["source_version_id"]
    )
    assert _anchor_ids(present_items) == (successor["anchor_id"],)
    assert _anchor_ids(historical_items) == (predecessor["anchor_id"],)
    assert _anchor_ids(timeline_items) == (predecessor["anchor_id"], successor["anchor_id"])
    assert _anchor_ids(retrieve_items) == (predecessor["anchor_id"],)


def test_superseded_version_can_be_archived_and_disappears_from_search(
    harness: KnowledgeRuntimeHarness,
) -> None:
    seed = uuid4().hex
    shared_source_id = f"KNW-ARCH-{seed}"
    shared_family_id = f"KNW-ARCH-FAMILY-{seed}"
    search_token = f"archive-token-{seed}"
    predecessor = _publish_version(
        harness=harness,
        seed=f"{seed}-pred",
        shared_source_id=shared_source_id,
        shared_family_id=shared_family_id,
        search_token=search_token,
        effective_from="2025-01-01",
        effective_to="2025-12-31",
        point_in_time_suffix="2025",
    )
    successor = _publish_version(
        harness=harness,
        seed=f"{seed}-succ",
        shared_source_id=shared_source_id,
        shared_family_id=shared_family_id,
        search_token=search_token,
        effective_from="2026-01-01",
        effective_to=None,
        point_in_time_suffix="2026",
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
        first_archive = client.post(
            f"/knowledge/source-versions/{predecessor['source_version_id']}/archive",
            json={"archived_by": str(uuid4())},
            headers={**_stable_headers("knowledge-archive-success"), **admin_headers},
        )
        second_archive = client.post(
            f"/knowledge/source-versions/{predecessor['source_version_id']}/archive",
            json={"archived_by": str(uuid4())},
            headers={**_stable_headers("knowledge-archive-success"), **admin_headers},
        )
        historical_search = client.post(
            "/knowledge/search",
            json={
                "query": search_token,
                "tax_domain": "income_tax",
                "effective_date": "2025-06-01",
            },
        )

    archive_payload = _json(first_archive)
    assert supersede.status_code == 200
    assert first_archive.status_code == 200
    assert second_archive.status_code == 200
    assert first_archive.content == second_archive.content
    assert cast(dict[str, object], archive_payload["result"])["publication_state"] == "archived"
    assert _result_total(_json(historical_search)) == 0


def test_cross_family_supersession_fails_canonically(harness: KnowledgeRuntimeHarness) -> None:
    seed = uuid4().hex
    predecessor = _publish_version(
        harness=harness,
        seed=f"{seed}-pred",
        shared_source_id=f"KNW-CROSS-A-{seed}",
        shared_family_id=f"KNW-CROSS-FAMILY-A-{seed}",
        search_token=f"cross-a-{seed}",
        effective_from="2025-01-01",
        effective_to="2025-12-31",
        point_in_time_suffix="2025",
    )
    successor = _publish_version(
        harness=harness,
        seed=f"{seed}-succ",
        shared_source_id=f"KNW-CROSS-B-{seed}",
        shared_family_id=f"KNW-CROSS-FAMILY-B-{seed}",
        search_token=f"cross-b-{seed}",
        effective_from="2026-01-01",
        effective_to=None,
        point_in_time_suffix="2026",
    )
    payload = {
        "successor_source_version_id": successor["source_version_id"],
        "superseded_by": str(uuid4()),
    }
    headers = _stable_headers("knowledge-cross-family")

    with TestClient(harness.app) as client:
        admin_headers = build_admin_auth_headers()
        first = client.post(
            f"/knowledge/source-versions/{predecessor['source_version_id']}/supersede",
            json=payload,
            headers={**headers, **admin_headers},
        )
        second = client.post(
            f"/knowledge/source-versions/{predecessor['source_version_id']}/supersede",
            json=payload,
            headers={**headers, **admin_headers},
        )

    assert first.status_code == 409
    assert second.status_code == 409
    assert _canonical_detail(_json(first)) == _canonical_detail(_json(second))
    assert _canonical_detail(_json(first))["reason_code"] == "knowledge_supersession_conflict"


def test_invalid_temporal_ordering_fails_canonically(harness: KnowledgeRuntimeHarness) -> None:
    seed = uuid4().hex
    shared_source_id = f"KNW-TEMP-{seed}"
    shared_family_id = f"KNW-TEMP-FAMILY-{seed}"
    predecessor = _publish_version(
        harness=harness,
        seed=f"{seed}-pred",
        shared_source_id=shared_source_id,
        shared_family_id=shared_family_id,
        search_token=f"temp-{seed}",
        effective_from="2026-01-01",
        effective_to="2026-12-31",
        point_in_time_suffix="2026",
    )
    successor = _publish_version(
        harness=harness,
        seed=f"{seed}-succ",
        shared_source_id=shared_source_id,
        shared_family_id=shared_family_id,
        search_token=f"temp-{seed}",
        effective_from="2025-01-01",
        effective_to="2025-12-31",
        point_in_time_suffix="2025",
    )

    with TestClient(harness.app) as client:
        response = client.post(
            f"/knowledge/source-versions/{predecessor['source_version_id']}/supersede",
            json={
                "successor_source_version_id": successor["source_version_id"],
                "superseded_by": str(uuid4()),
            },
            headers=build_admin_auth_headers(),
        )

    assert response.status_code == 409
    assert _canonical_detail(_json(response))["reason_code"] == "knowledge_temporal_scope_mismatch"


def test_archive_non_published_source_version_fails_canonically(
    db_connection: psycopg.Connection,
    harness: KnowledgeRuntimeHarness,
) -> None:
    seed = uuid4().hex
    published = _publish_version(
        harness=harness,
        seed=f"{seed}-published",
        shared_source_id=f"KNW-ARCHIVE-SEED-{seed}",
        shared_family_id=f"KNW-ARCHIVE-FAMILY-{seed}",
        search_token=f"archive-seed-{seed}",
        effective_from="2026-01-01",
        effective_to="2026-12-31",
        point_in_time_suffix="2026",
    )
    non_published_source_version_id = _insert_non_published_source_version(
        connection=db_connection,
        source_id=published["source_id"],
        effective_from="2027-01-01",
        effective_to="2027-12-31",
    )

    with TestClient(harness.app) as client:
        response = client.post(
            f"/knowledge/source-versions/{non_published_source_version_id}/archive",
            json={"archived_by": str(uuid4())},
            headers=build_admin_auth_headers(),
        )

    assert response.status_code == 409
    assert _canonical_detail(_json(response))["reason_code"] == "knowledge_record_not_published"


def test_customer_document_lineage_cannot_enter_managed_source_version_scope(
    db_connection: psycopg.Connection,
    harness: KnowledgeRuntimeHarness,
) -> None:
    seed = uuid4().hex
    published = _publish_version(
        harness=harness,
        seed=f"{seed}-published",
        shared_source_id=f"KNW-CUSTOMER-{seed}",
        shared_family_id=f"KNW-CUSTOMER-FAMILY-{seed}",
        search_token=f"customer-seed-{seed}",
        effective_from="2026-01-01",
        effective_to="2026-12-31",
        point_in_time_suffix="2026",
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
                        "approved",
                        "2027-01-01",
                        "2027-12-31",
                        2027,
                    ),
                )
    finally:
        db_connection.rollback()


def _publish_version(
    *,
    harness: KnowledgeRuntimeHarness,
    seed: str,
    shared_source_id: str,
    shared_family_id: str,
    search_token: str,
    effective_from: str,
    effective_to: str | None,
    point_in_time_suffix: str,
) -> dict[str, str]:
    requested_by = str(uuid4())
    reviewer_id = str(uuid4())
    publisher_id = str(uuid4())
    ingestion_payload = {
        "requested_by": requested_by,
        "idempotency_key": f"idem-publish-{seed}",
        "filename": f"finance-act-{seed}.pdf",
        "mime_type": "application/pdf",
        "file_content_base64": base64.b64encode(f"official-{seed}".encode()).decode("ascii"),
        "source_input_origin": "official_source_upload",
        "source_class": "tax_law",
    }
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
        "source_version_id": str(proposed_source_record["published_source_version_id"]),
        "anchor_id": f"anchor-{seed}",
    }


def _insert_non_published_source_version(
    *,
    connection: psycopg.Connection,
    source_id: str,
    effective_from: str,
    effective_to: str | None,
) -> str:
    source_version_id = str(uuid4())
    with connection.cursor() as cursor:
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
                UUID(source_version_id),
                source_id,
                None,
                f"https://example.com/non-published/{source_version_id}",
                f"non-published-checksum-{source_version_id}",
                "as_issued",
                "official_source_url",
                f"official-source-url://https://example.com/non-published/{source_version_id}",
                "approved",
                effective_from,
                effective_to,
                int(effective_from[:4]),
            ),
        )
    connection.commit()
    return source_version_id


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


def _stable_headers(seed: str) -> dict[str, str]:
    return {
        "X-Correlation-ID": f"{seed}-corr",
        "X-Trace-ID": f"{seed}-trace",
    }
