"""DB-backed ingestion boundary tests for governed knowledge intake."""

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
from tests.knowledge_db_test_support import build_admin_auth_headers


@pytest.fixture()
def db_connection() -> Iterator[psycopg.Connection]:
    """Create DB connection for governed knowledge ingestion tests."""

    database_url = load_database_url()
    if database_url is None or not database_url.strip():
        pytest.skip("DATABASE_URL is not set; skipping knowledge ingestion DB tests.")

    ensure_knowledge_migration_applied(database_url=database_url)
    try:
        connection = psycopg.connect(database_url, connect_timeout=5)
    except psycopg.OperationalError:
        pytest.skip("DATABASE_URL is not reachable; skipping knowledge ingestion DB tests.")

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


def test_single_file_ingestion_persists_one_governed_job(
    db_connection: psycopg.Connection,
    harness: KnowledgeRuntimeHarness,
) -> None:
    unique_seed = uuid4().hex
    requested_by = str(uuid4())
    payload = _file_payload(
        requested_by=requested_by,
        idempotency_key=f"idem-file-{uuid4()}",
        filename="finance-act.pdf",
        mime_type="application/pdf",
        file_bytes=f"%PDF-1.7 official source {unique_seed}".encode(),
        source_class="tax_law",
    )

    with TestClient(harness.app) as client:
        response = client.post(
            "/knowledge/ingestion/files",
            json=payload,
            headers=_authenticated_headers("knowledge-file-001", user_id=requested_by),
        )

    body = _json(response)
    result = cast(dict[str, object], body["result"])
    _track_created(
        harness,
        job_id=str(result["ingestion_job_id"]),
        document_id=str(result["document_id"]),
        requested_by=str(result["requested_by"]),
    )
    stored = _fetch_ingestion_job(
        db_connection,
        ingestion_job_id=str(result["ingestion_job_id"]),
    )

    assert response.status_code == 200
    assert result["source_input_origin"] == "official_source_upload"
    assert result["ingestion_state"] == "uploaded"
    assert stored["document_id"] == result["document_id"]
    assert stored["requested_by"] == payload["requested_by"]
    proposed_source_record = require_object_dict(stored["proposed_source_record"])
    extracted_metadata = require_object_dict(stored["extracted_metadata"])
    assert proposed_source_record["source_input_origin"] == "official_source_upload"
    assert extracted_metadata["mime_type"] == "application/pdf"


def test_single_url_ingestion_persists_one_governed_job(
    db_connection: psycopg.Connection,
    harness: KnowledgeRuntimeHarness,
) -> None:
    unique_seed = uuid4().hex
    requested_by = str(uuid4())
    payload = _url_payload(
        requested_by=requested_by,
        idempotency_key=f"idem-url-{uuid4()}",
        url=f"https://Example.com/acts/finance/{unique_seed}?lang=en#ignored",
        source_class="guidance",
    )
    expected_normalized_url = f"https://example.com/acts/finance/{unique_seed}?lang=en"

    with TestClient(harness.app) as client:
        response = client.post(
            "/knowledge/ingestion/urls",
            json=payload,
            headers=_authenticated_headers("knowledge-url-001", user_id=requested_by),
        )

    body = _json(response)
    result = cast(dict[str, object], body["result"])
    _track_created(
        harness,
        job_id=str(result["ingestion_job_id"]),
        document_id=str(result["document_id"]),
        requested_by=str(result["requested_by"]),
    )
    stored = _fetch_ingestion_job(
        db_connection,
        ingestion_job_id=str(result["ingestion_job_id"]),
    )

    assert response.status_code == 200
    assert result["source_input_origin"] == "official_source_url"
    assert result["source_input_ref"] == f"official-source-url://{expected_normalized_url}"
    proposed_source_record = require_object_dict(stored["proposed_source_record"])
    assert proposed_source_record["normalized_url"] == expected_normalized_url


@pytest.mark.parametrize(
    ("mime_type", "suffix"),
    (
        ("application/pdf", "pdf"),
        ("text/html", "html"),
        ("text/plain", "txt"),
        ("text/markdown", "md"),
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
        ("application/xml", "xml"),
    ),
)
def test_supported_mime_types_are_accepted_deterministically(
    harness: KnowledgeRuntimeHarness,
    mime_type: str,
    suffix: str,
) -> None:
    unique_seed = uuid4().hex
    requested_by = str(uuid4())
    payload = _file_payload(
        requested_by=requested_by,
        idempotency_key=f"idem-mime-{suffix}-{uuid4()}",
        filename=f"official-source.{suffix}",
        mime_type=mime_type,
        file_bytes=f"official-{suffix}-{unique_seed}".encode(),
    )

    with TestClient(harness.app) as client:
        response = client.post(
            "/knowledge/ingestion/files",
            json=payload,
            headers=_authenticated_headers("knowledge-mime", user_id=requested_by),
        )

    body = _json(response)
    result = cast(dict[str, object], body["result"])
    _track_created(
        harness,
        job_id=str(result["ingestion_job_id"]),
        document_id=str(result["document_id"]),
        requested_by=str(result["requested_by"]),
    )
    assert response.status_code == 200
    assert result["ingestion_state"] == "uploaded"


def test_identical_file_ingestion_replay_is_byte_equivalent(
    harness: KnowledgeRuntimeHarness,
) -> None:
    unique_seed = uuid4().hex
    requested_by = str(uuid4())
    payload = _file_payload(
        requested_by=requested_by,
        idempotency_key=f"idem-file-replay-{uuid4()}",
        filename="replay.pdf",
        mime_type="application/pdf",
        file_bytes=f"replay-pdf-{unique_seed}".encode(),
    )
    headers = _stable_headers("knowledge-file-replay")
    headers.update(build_admin_auth_headers(user_id=requested_by))

    with TestClient(harness.app) as client:
        first = client.post("/knowledge/ingestion/files", json=payload, headers=headers)
        second = client.post("/knowledge/ingestion/files", json=payload, headers=headers)

    first_body = _json(first)
    result = cast(dict[str, object], first_body["result"])
    _track_created(
        harness,
        job_id=str(result["ingestion_job_id"]),
        document_id=str(result["document_id"]),
        requested_by=str(result["requested_by"]),
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.content == second.content


def test_identical_url_ingestion_replay_is_byte_equivalent(
    harness: KnowledgeRuntimeHarness,
) -> None:
    unique_seed = uuid4().hex
    requested_by = str(uuid4())
    payload = _url_payload(
        requested_by=requested_by,
        idempotency_key=f"idem-url-replay-{uuid4()}",
        url=f"https://example.com/replay/{unique_seed}",
    )
    headers = _stable_headers("knowledge-url-replay")
    headers.update(build_admin_auth_headers(user_id=requested_by))

    with TestClient(harness.app) as client:
        first = client.post("/knowledge/ingestion/urls", json=payload, headers=headers)
        second = client.post("/knowledge/ingestion/urls", json=payload, headers=headers)

    first_body = _json(first)
    result = cast(dict[str, object], first_body["result"])
    _track_created(
        harness,
        job_id=str(result["ingestion_job_id"]),
        document_id=str(result["document_id"]),
        requested_by=str(result["requested_by"]),
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.content == second.content


def test_single_item_file_ingestion_shape_remains_stable_after_bulk_extension(
    harness: KnowledgeRuntimeHarness,
) -> None:
    payload = _file_payload(
        requested_by=str(uuid4()),
        idempotency_key=f"idem-file-stable-{uuid4()}",
        filename="stable-shape.pdf",
        mime_type="application/pdf",
        file_bytes=b"stable single-item ingestion",
        source_class="tax_law",
    )

    with TestClient(harness.app) as client:
        response = client.post(
            "/knowledge/ingestion/files",
            json=payload,
            headers=_authenticated_headers("knowledge-file-stable", user_id=str(payload["requested_by"])),
        )

    body = _json(response)
    result = cast(dict[str, object], body["result"])
    _track_created(
        harness,
        job_id=str(result["ingestion_job_id"]),
        document_id=str(result["document_id"]),
        requested_by=str(result["requested_by"]),
    )
    assert response.status_code == 200
    assert result["ingestion_state"] == "uploaded"
    assert result["source_input_origin"] == "official_source_upload"


def test_duplicate_file_payload_without_conflicting_metadata_reuses_existing_job(
    db_connection: psycopg.Connection,
    harness: KnowledgeRuntimeHarness,
) -> None:
    requested_by = str(uuid4())
    duplicate_seed = uuid4().hex
    first_payload = _file_payload(
        requested_by=requested_by,
        idempotency_key=f"idem-dup-a-{uuid4()}",
        filename="duplicate.pdf",
        mime_type="application/pdf",
        file_bytes=f"same-content-{duplicate_seed}".encode(),
        source_class="tax_law",
    )
    second_payload = _file_payload(
        requested_by=requested_by,
        idempotency_key=f"idem-dup-b-{uuid4()}",
        filename="duplicate.pdf",
        mime_type="application/pdf",
        file_bytes=f"same-content-{duplicate_seed}".encode(),
        source_class="tax_law",
    )

    with TestClient(harness.app) as client:
        first = client.post(
            "/knowledge/ingestion/files",
            json=first_payload,
            headers=_authenticated_headers("knowledge-dup-a", user_id=requested_by),
        )
        second = client.post(
            "/knowledge/ingestion/files",
            json=second_payload,
            headers=_authenticated_headers("knowledge-dup-b", user_id=requested_by),
        )

    first_body = _json(first)
    second_body = _json(second)
    first_result = cast(dict[str, object], first_body["result"])
    second_result = cast(dict[str, object], second_body["result"])
    _track_created(
        harness,
        job_id=str(first_result["ingestion_job_id"]),
        document_id=str(first_result["document_id"]),
        requested_by=str(first_result["requested_by"]),
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first_result == second_result
    assert (
        _count_ingestion_jobs_for_document(
            db_connection,
            document_id=str(first_result["document_id"]),
        )
        == 1
    )


def test_unsupported_file_type_is_rejected_canonically(
    harness: KnowledgeRuntimeHarness,
) -> None:
    payload = _file_payload(
        requested_by=str(uuid4()),
        idempotency_key=f"idem-bad-mime-{uuid4()}",
        filename="bad.bin",
        mime_type="application/octet-stream",
        file_bytes=b"bad",
    )
    headers = _authenticated_headers("knowledge-bad-mime", user_id=str(payload["requested_by"]))

    with TestClient(harness.app) as client:
        first = client.post("/knowledge/ingestion/files", json=payload, headers=headers)
        second = client.post("/knowledge/ingestion/files", json=payload, headers=headers)

    assert first.status_code == 400
    assert second.status_code == 400
    assert _canonical_detail(_json(first)) == _canonical_detail(_json(second))


def test_malformed_url_is_rejected_canonically(
    harness: KnowledgeRuntimeHarness,
) -> None:
    payload = _url_payload(
        requested_by=str(uuid4()),
        idempotency_key=f"idem-bad-url-{uuid4()}",
        url="https:///missing-host",
    )

    with TestClient(harness.app) as client:
        response = client.post(
            "/knowledge/ingestion/urls",
            json=payload,
            headers=_authenticated_headers("knowledge-bad-url", user_id=str(payload["requested_by"])),
        )

    detail = _canonical_detail(_json(response))
    assert response.status_code == 400
    assert detail["reason_code"] == "invalid_knowledge_request"


def test_non_http_url_is_rejected_canonically(
    harness: KnowledgeRuntimeHarness,
) -> None:
    payload = _url_payload(
        requested_by=str(uuid4()),
        idempotency_key=f"idem-ftp-url-{uuid4()}",
        url="ftp://example.com/acts/finance",
    )

    with TestClient(harness.app) as client:
        response = client.post(
            "/knowledge/ingestion/urls",
            json=payload,
            headers=_authenticated_headers("knowledge-ftp-url", user_id=str(payload["requested_by"])),
        )

    detail = _canonical_detail(_json(response))
    assert response.status_code == 400
    assert detail["reason_code"] == "invalid_knowledge_request"


def test_forbidden_customer_document_lineage_is_rejected_canonically(
    harness: KnowledgeRuntimeHarness,
) -> None:
    payload = _file_payload(
        requested_by=str(uuid4()),
        idempotency_key=f"idem-customer-origin-{uuid4()}",
        filename="private.pdf",
        mime_type="application/pdf",
        file_bytes=b"private",
        source_input_origin="customer_uploaded_document",
    )

    with TestClient(harness.app) as client:
        response = client.post(
            "/knowledge/ingestion/files",
            json=payload,
            headers=_authenticated_headers("knowledge-customer-origin", user_id=str(payload["requested_by"])),
        )

    detail = _canonical_detail(_json(response))
    assert response.status_code == 400
    assert detail["reason_code"] == "unsupported_source_input_origin"


def test_same_idempotency_key_with_different_payload_fails_canonically(
    harness: KnowledgeRuntimeHarness,
) -> None:
    requested_by = str(uuid4())
    idempotency_key = f"idem-conflict-{uuid4()}"
    conflict_seed = uuid4().hex
    first_payload = _file_payload(
        requested_by=requested_by,
        idempotency_key=idempotency_key,
        filename="conflict-a.pdf",
        mime_type="application/pdf",
        file_bytes=f"payload-a-{conflict_seed}".encode(),
    )
    second_payload = _file_payload(
        requested_by=requested_by,
        idempotency_key=idempotency_key,
        filename="conflict-b.pdf",
        mime_type="application/pdf",
        file_bytes=f"payload-b-{conflict_seed}".encode(),
    )

    with TestClient(harness.app) as client:
        first = client.post(
            "/knowledge/ingestion/files",
            json=first_payload,
            headers=_authenticated_headers("knowledge-idem-conflict-a", user_id=requested_by),
        )
        second = client.post(
            "/knowledge/ingestion/files",
            json=second_payload,
            headers=_authenticated_headers("knowledge-idem-conflict-b", user_id=requested_by),
        )

    first_result = cast(dict[str, object], _json(first)["result"])
    _track_created(
        harness,
        job_id=str(first_result["ingestion_job_id"]),
        document_id=str(first_result["document_id"]),
        requested_by=str(first_result["requested_by"]),
    )
    detail = _canonical_detail(_json(second))
    assert first.status_code == 200
    assert second.status_code == 409
    assert detail["reason_code"] == "knowledge_idempotency_conflict"


def test_ingested_unpublished_records_do_not_appear_in_search_or_retrieve(
    harness: KnowledgeRuntimeHarness,
) -> None:
    unique_seed = uuid4().hex
    requested_by = str(uuid4())
    payload = _url_payload(
        requested_by=requested_by,
        idempotency_key=f"idem-hidden-{uuid4()}",
        url=f"https://example.com/unpublished-source/{unique_seed}",
        source_class="guidance",
    )

    with TestClient(harness.app) as client:
        ingest = client.post(
            "/knowledge/ingestion/urls",
            json=payload,
            headers=_authenticated_headers("knowledge-hidden", user_id=requested_by),
        )
        ingest_body = _json(ingest)
        result = cast(dict[str, object], ingest_body["result"])
        search = client.post(
            "/knowledge/search",
            json={"query": "unpublished-source", "tax_domain": "income_tax"},
            headers=_authenticated_headers("knowledge-hidden-search", user_id=requested_by),
        )
        retrieve = client.post(
            "/knowledge/retrieve",
            json={
                "source_ids": [str(result["ingestion_job_id"])],
                "anchor_ids": [],
            },
            headers=_authenticated_headers("knowledge-hidden-retrieve", user_id=requested_by),
        )

    _track_created(
        harness,
        job_id=str(result["ingestion_job_id"]),
        document_id=str(result["document_id"]),
        requested_by=str(result["requested_by"]),
    )
    assert ingest.status_code == 200
    assert _result_total(_json(search)) == 0
    assert _result_total(_json(retrieve)) == 0


def test_ingested_job_is_review_fetchable_but_still_non_searchable(
    harness: KnowledgeRuntimeHarness,
) -> None:
    unique_seed = uuid4().hex
    requested_by = str(uuid4())
    payload = _file_payload(
        requested_by=requested_by,
        idempotency_key=f"idem-review-fetch-{uuid4()}",
        filename="review-fetch.pdf",
        mime_type="application/pdf",
        file_bytes=f"review-fetch-{unique_seed}".encode(),
        source_class="tax_law",
    )

    with TestClient(harness.app) as client:
        ingest = client.post(
            "/knowledge/ingestion/files",
            json=payload,
            headers=_authenticated_headers("knowledge-review-fetch", user_id=requested_by),
        )
        ingest_body = _json(ingest)
        result = cast(dict[str, object], ingest_body["result"])
        fetch = client.get(
            f"/knowledge/ingestion/{result['ingestion_job_id']}",
            headers=_authenticated_headers("knowledge-review-fetch-get", user_id=requested_by),
        )
        search = client.post(
            "/knowledge/search",
            json={"query": "review-fetch", "tax_domain": "income_tax"},
            headers=_authenticated_headers("knowledge-review-search", user_id=requested_by),
        )

    _track_created(
        harness,
        job_id=str(result["ingestion_job_id"]),
        document_id=str(result["document_id"]),
        requested_by=str(result["requested_by"]),
    )
    fetch_result = cast(dict[str, object], _json(fetch)["result"])
    assert ingest.status_code == 200
    assert fetch.status_code == 200
    assert fetch_result["ingestion_state"] == "uploaded"
    assert fetch_result["source_input_origin"] == "official_source_upload"
    assert _result_total(_json(search)) == 0


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
    legacy_import_acknowledged: bool = True,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "requested_by": requested_by,
        "idempotency_key": idempotency_key,
        "filename": filename,
        "mime_type": mime_type,
        "file_content_base64": base64.b64encode(file_bytes).decode("ascii"),
        "legacy_import_acknowledged": legacy_import_acknowledged,
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


def _authenticated_headers(seed: str, *, user_id: str) -> dict[str, str]:
    headers = _stable_headers(seed)
    headers.update(build_admin_auth_headers(user_id=user_id))
    return headers


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


def _count_ingestion_jobs_for_document(
    connection: psycopg.Connection,
    *,
    document_id: str,
) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM knowledge_ingestion_jobs WHERE document_id = %s",
            (UUID(document_id),),
        )
        row = cursor.fetchone()
    assert row is not None
    return require_int(row[0])


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
