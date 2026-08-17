"""DB-backed bulk ingestion tests for governed knowledge intake."""

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
from tests.knowledge_db_test_support import ensure_knowledge_migration_applied


@pytest.fixture()
def db_connection() -> Iterator[psycopg.Connection]:
    """Create DB connection for governed knowledge bulk-ingestion tests."""

    database_url = load_database_url()
    if database_url is None or not database_url.strip():
        pytest.skip("DATABASE_URL is not set; skipping knowledge bulk-ingestion DB tests.")

    ensure_knowledge_migration_applied(database_url=database_url)
    try:
        connection = psycopg.connect(database_url, connect_timeout=5)
    except psycopg.OperationalError:
        pytest.skip("DATABASE_URL is not reachable; skipping knowledge bulk-ingestion DB tests.")

    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def harness(db_connection: psycopg.Connection) -> Iterator[KnowledgeRuntimeHarness]:
    """Build runtime harness and clean up any created ingestion artifacts."""

    runtime_harness = create_runtime_harness(connection=db_connection)
    try:
        yield runtime_harness
    finally:
        _cleanup_ingestion_records(db_connection, runtime_harness)


def test_bulk_file_ingestion_persists_multiple_governed_jobs(
    db_connection: psycopg.Connection,
    harness: KnowledgeRuntimeHarness,
) -> None:
    acting_user = str(uuid4())
    payload = {
        "acting_user": acting_user,
        "items": [
            _bulk_file_item(
                _file_payload(
                    requested_by=acting_user,
                    idempotency_key=f"bulk-file-{uuid4()}",
                    filename="finance-act.pdf",
                    mime_type="application/pdf",
                    file_bytes=b"%PDF-1.7 governed bulk finance act",
                    source_class="tax_law",
                )
            ),
            _bulk_file_item(
                _file_payload(
                    requested_by=acting_user,
                    idempotency_key=f"bulk-file-{uuid4()}",
                    filename="guidance.html",
                    mime_type="text/html",
                    file_bytes=b"<html>governed bulk guidance</html>",
                    source_class="guidance",
                )
            ),
        ],
    }

    with TestClient(harness.app) as client:
        response = client.post(
            "/knowledge/ingestion/files/bulk",
            json=payload,
            headers=_stable_headers("knowledge-bulk-files-001"),
        )

    body = _json(response)
    result = require_object_dict(body["result"])
    items = cast(list[object], result["items"])
    normalized_items = [require_object_dict(item) for item in items]

    assert response.status_code == 200
    assert result["bulk_status"] == "full_success"
    assert require_int(result["total"]) == 2
    assert [item["index"] for item in normalized_items] == [0, 1]
    assert all(item["status"] == "ok" for item in normalized_items)
    for item in normalized_items:
        ingestion_job_id = str(item["ingestion_job_id"])
        stored = _fetch_ingestion_job(db_connection, ingestion_job_id=ingestion_job_id)
        _track_created(
            harness,
            job_id=ingestion_job_id,
            document_id=str(stored["document_id"]),
            requested_by=str(stored["requested_by"]),
        )
        assert stored["requested_by"] == acting_user
        proposed_source_record = require_object_dict(stored["proposed_source_record"])
        assert proposed_source_record["ingestion_kind"] == "file"
        assert proposed_source_record["source_input_origin"] == "official_source_upload"


def test_bulk_url_ingestion_persists_multiple_governed_jobs(
    db_connection: psycopg.Connection,
    harness: KnowledgeRuntimeHarness,
) -> None:
    acting_user = str(uuid4())
    payload = {
        "acting_user": acting_user,
        "items": [
            _bulk_url_item(
                _url_payload(
                    requested_by=acting_user,
                    idempotency_key=f"bulk-url-{uuid4()}",
                    url=f"https://Example.com/law/{uuid4().hex}?lang=en#ignored",
                    source_class="guidance",
                )
            ),
            _bulk_url_item(
                _url_payload(
                    requested_by=acting_user,
                    idempotency_key=f"bulk-url-{uuid4()}",
                    url=f"https://example.com/law/{uuid4().hex}",
                    source_class="tax_law",
                )
            ),
        ],
    }

    with TestClient(harness.app) as client:
        response = client.post(
            "/knowledge/ingestion/urls/bulk",
            json=payload,
            headers=_stable_headers("knowledge-bulk-urls-001"),
        )

    body = _json(response)
    result = require_object_dict(body["result"])
    items = [require_object_dict(item) for item in cast(list[object], result["items"])]

    assert response.status_code == 200
    assert result["bulk_status"] == "full_success"
    assert require_int(result["total"]) == 2
    for item in items:
        ingestion_job_id = str(item["ingestion_job_id"])
        stored = _fetch_ingestion_job(db_connection, ingestion_job_id=ingestion_job_id)
        _track_created(
            harness,
            job_id=ingestion_job_id,
            document_id=str(stored["document_id"]),
            requested_by=str(stored["requested_by"]),
        )
        proposed_source_record = require_object_dict(stored["proposed_source_record"])
        assert proposed_source_record["ingestion_kind"] == "url"
        assert proposed_source_record["source_input_origin"] == "official_source_url"


def test_identical_bulk_file_ingestion_replay_is_byte_equivalent(
    db_connection: psycopg.Connection,
    harness: KnowledgeRuntimeHarness,
) -> None:
    acting_user = str(uuid4())
    payload = {
        "acting_user": acting_user,
        "items": [
            _bulk_file_item(
                _file_payload(
                    requested_by=acting_user,
                    idempotency_key=f"bulk-replay-file-{uuid4()}",
                    filename="replay-one.pdf",
                    mime_type="application/pdf",
                    file_bytes=b"bulk replay one",
                )
            ),
            _bulk_file_item(
                _file_payload(
                    requested_by=acting_user,
                    idempotency_key=f"bulk-replay-file-{uuid4()}",
                    filename="replay-two.txt",
                    mime_type="text/plain",
                    file_bytes=b"bulk replay two",
                )
            ),
        ],
    }
    headers = _stable_headers("knowledge-bulk-files-replay")

    with TestClient(harness.app) as client:
        first = client.post("/knowledge/ingestion/files/bulk", json=payload, headers=headers)
        second = client.post("/knowledge/ingestion/files/bulk", json=payload, headers=headers)

    first_body = _json(first)
    items = [
        require_object_dict(item)
        for item in cast(list[object], require_object_dict(first_body["result"])["items"])
    ]
    for item in items:
        stored = _fetch_ingestion_job(db_connection, ingestion_job_id=str(item["ingestion_job_id"]))
        _track_created(
            harness,
            job_id=str(item["ingestion_job_id"]),
            document_id=str(stored["document_id"]),
            requested_by=str(stored["requested_by"]),
        )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.content == second.content


def test_bulk_ingestion_mixed_success_is_canonical_and_non_searchable(
    db_connection: psycopg.Connection,
    harness: KnowledgeRuntimeHarness,
) -> None:
    acting_user = str(uuid4())
    success_token = f"bulk-unpublished-{uuid4().hex}"
    payload = {
        "acting_user": acting_user,
        "items": [
            _bulk_file_item(
                _file_payload(
                    requested_by=acting_user,
                    idempotency_key=f"bulk-mixed-{uuid4()}",
                    filename=f"{success_token}.pdf",
                    mime_type="application/pdf",
                    file_bytes=success_token.encode(),
                    source_class="tax_law",
                )
            ),
            _bulk_file_item(
                _file_payload(
                    requested_by=acting_user,
                    idempotency_key=f"bulk-mixed-{uuid4()}",
                    filename="bad.bin",
                    mime_type="application/octet-stream",
                    file_bytes=b"bad",
                    source_class="guidance",
                )
            ),
        ],
    }

    with TestClient(harness.app) as client:
        response = client.post("/knowledge/ingestion/files/bulk", json=payload)
        search = client.post(
            "/knowledge/search",
            json={"query": success_token, "tax_domain": "income_tax"},
        )

    body = _json(response)
    result = require_object_dict(body["result"])
    items = [require_object_dict(item) for item in cast(list[object], result["items"])]
    accepted_item = next(item for item in items if item["status"] == "ok")
    rejected_item = next(item for item in items if item["status"] == "error")
    stored = _fetch_ingestion_job(
        db_connection,
        ingestion_job_id=str(accepted_item["ingestion_job_id"]),
    )
    _track_created(
        harness,
        job_id=str(accepted_item["ingestion_job_id"]),
        document_id=str(stored["document_id"]),
        requested_by=str(stored["requested_by"]),
    )

    assert response.status_code == 200
    assert result["bulk_status"] == "partial_failure"
    assert rejected_item["error_code"] == "invalid_knowledge_request"
    assert rejected_item["reason"] == "invalid_knowledge_request"
    assert require_int(require_object_dict(_json(search)["result"])["total"]) == 0


def test_bulk_ingestion_conflicting_repeated_idempotency_keys_fail_canonically(
    harness: KnowledgeRuntimeHarness,
) -> None:
    acting_user = str(uuid4())
    conflicting_key = f"bulk-conflict-{uuid4()}"
    payload = {
        "acting_user": acting_user,
        "items": [
            _bulk_url_item(
                _url_payload(
                    requested_by=acting_user,
                    idempotency_key=conflicting_key,
                    url="https://example.com/conflict/one",
                    source_class="guidance",
                )
            ),
            _bulk_url_item(
                _url_payload(
                    requested_by=acting_user,
                    idempotency_key=conflicting_key,
                    url="https://example.com/conflict/two",
                    source_class="guidance",
                )
            ),
        ],
    }
    headers = _stable_headers("knowledge-bulk-conflict")

    with TestClient(harness.app) as client:
        first = client.post("/knowledge/ingestion/urls/bulk", json=payload, headers=headers)
        second = client.post("/knowledge/ingestion/urls/bulk", json=payload, headers=headers)

    first_items = [
        require_object_dict(item)
        for item in cast(list[object], require_object_dict(_json(first)["result"])["items"])
    ]
    assert first.status_code == 200
    assert second.status_code == 200
    assert _canonical_detail(_json(first)) == _canonical_detail(_json(second))
    assert first_items[0]["status"] == "ok"
    assert first_items[1]["status"] == "error"
    assert first_items[1]["error_code"] == "knowledge_idempotency_conflict"


def test_bulk_url_ingestion_rejects_malformed_and_forbidden_origin_items_canonically(
    harness: KnowledgeRuntimeHarness,
) -> None:
    acting_user = str(uuid4())
    payload = {
        "acting_user": acting_user,
        "items": [
            _bulk_url_item(
                _url_payload(
                    requested_by=acting_user,
                    idempotency_key=f"bulk-bad-url-{uuid4()}",
                    url="https:///missing-host",
                )
            ),
            _bulk_url_item(
                _url_payload(
                    requested_by=acting_user,
                    idempotency_key=f"bulk-bad-origin-{uuid4()}",
                    url="https://example.com/forbidden",
                    source_input_origin="customer_uploaded_document",
                )
            ),
        ],
    }

    with TestClient(harness.app) as client:
        response = client.post("/knowledge/ingestion/urls/bulk", json=payload)

    body = _json(response)
    result = require_object_dict(body["result"])
    items = [require_object_dict(item) for item in cast(list[object], result["items"])]

    assert response.status_code == 200
    assert result["bulk_status"] == "full_rejection"
    assert items[0]["error_code"] == "invalid_knowledge_request"
    assert items[1]["error_code"] == "unsupported_source_input_origin"


def _bulk_file_item(item: dict[str, object]) -> dict[str, object]:
    return {
        "idempotency_key": item["idempotency_key"],
        "filename": item["filename"],
        "mime_type": item["mime_type"],
        "file_content_base64": item["file_content_base64"],
        "source_input_origin": item.get("source_input_origin"),
        "source_class": item.get("source_class"),
    }


def _bulk_url_item(item: dict[str, object]) -> dict[str, object]:
    return {
        "idempotency_key": item["idempotency_key"],
        "url": item["url"],
        "source_input_origin": item.get("source_input_origin"),
        "source_class": item.get("source_class"),
    }


def _track_created(
    harness: KnowledgeRuntimeHarness,
    *,
    job_id: str,
    document_id: str,
    requested_by: str,
) -> None:
    harness.job_ids.append(job_id)
    harness.document_ids.append(document_id)
    harness.user_ids.append(requested_by)


def _cleanup_ingestion_records(
    connection: psycopg.Connection,
    harness: KnowledgeRuntimeHarness,
) -> None:
    job_ids = sorted(set(harness.job_ids))
    document_ids = sorted(set(harness.document_ids))
    user_ids = sorted(set(harness.user_ids))
    with connection.cursor() as cursor:
        if job_ids:
            cursor.execute(
                "DELETE FROM knowledge_ingestion_jobs WHERE id = ANY(%s::uuid[])",
                ([UUID(value) for value in job_ids],),
            )
        if document_ids:
            cursor.execute(
                """
                UPDATE documents
                SET state = 'eligible_for_purge',
                    purge_eligible_at = CURRENT_TIMESTAMP
                WHERE id = ANY(%s::uuid[])
                  AND state IN ('uploaded', 'processing', 'validated')
                """,
                ([UUID(value) for value in document_ids],),
            )
            cursor.execute(
                "DELETE FROM documents WHERE id = ANY(%s::uuid[])",
                ([UUID(value) for value in document_ids],),
            )
        if user_ids:
            cursor.execute(
                "DELETE FROM users WHERE id = ANY(%s::uuid[])",
                ([UUID(value) for value in user_ids],),
            )
    connection.commit()


def _file_payload(
    *,
    requested_by: str,
    idempotency_key: str,
    filename: str,
    mime_type: str,
    file_bytes: bytes,
    source_input_origin: str | None = "official_source_upload",
    source_class: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "requested_by": requested_by,
        "idempotency_key": idempotency_key,
        "filename": filename,
        "mime_type": mime_type,
        "file_content_base64": base64.b64encode(file_bytes).decode("ascii"),
    }
    if source_input_origin is not None:
        payload["source_input_origin"] = source_input_origin
    if source_class is not None:
        payload["source_class"] = source_class
    return payload


def _url_payload(
    *,
    requested_by: str,
    idempotency_key: str,
    url: str,
    source_input_origin: str | None = "official_source_url",
    source_class: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "requested_by": requested_by,
        "idempotency_key": idempotency_key,
        "url": url,
    }
    if source_input_origin is not None:
        payload["source_input_origin"] = source_input_origin
    if source_class is not None:
        payload["source_class"] = source_class
    return payload


def _stable_headers(seed: str) -> dict[str, str]:
    return {
        "X-Correlation-ID": f"{seed}-corr",
        "X-Trace-ID": f"{seed}-trace",
    }


def _fetch_ingestion_job(
    connection: psycopg.Connection,
    *,
    ingestion_job_id: str,
) -> dict[str, object]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                id::text,
                document_id::text,
                requested_by::text,
                ingestion_state,
                extracted_metadata,
                proposed_source_record
            FROM knowledge_ingestion_jobs
            WHERE id = %s
            """,
            (UUID(ingestion_job_id),),
        )
        row = cursor.fetchone()
    assert row is not None
    return {
        "ingestion_job_id": str(row[0]),
        "document_id": str(row[1]),
        "requested_by": str(row[2]),
        "ingestion_state": str(row[3]),
        "extracted_metadata": require_object_dict(row[4]),
        "proposed_source_record": require_object_dict(row[5]),
    }


def _json(response: Any) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


def _canonical_detail(payload: dict[str, object]) -> dict[str, object]:
    detail = require_object_dict(payload["detail"])
    return {
        "error_code": detail["error_code"],
        "message": detail["message"],
        "reason": detail["reason"],
        "reason_code": detail["reason_code"],
    }
