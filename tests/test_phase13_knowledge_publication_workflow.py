"""DB-backed review and publication workflow tests for governed knowledge items."""

from __future__ import annotations

import json
from uuid import uuid4
import base64
from typing import Any
from typing import cast
from collections.abc import Iterator
from collections.abc import Sequence

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
from services.knowledge.app.embeddings import KnowledgeEmbeddingProvider


@pytest.fixture()
def db_connection() -> Iterator[psycopg.Connection]:
    """Create DB connection for governed knowledge publication tests."""

    database_url = load_database_url()
    if database_url is None or not database_url.strip():
        pytest.skip("DATABASE_URL is not set; skipping knowledge publication DB tests.")

    ensure_knowledge_migration_applied(database_url=database_url)
    try:
        connection = psycopg.connect(database_url, connect_timeout=5)
    except psycopg.OperationalError:
        pytest.skip("DATABASE_URL is not reachable; skipping knowledge publication DB tests.")

    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def harness(db_connection: psycopg.Connection) -> KnowledgeRuntimeHarness:
    """Build one runtime harness for governed publication tests."""

    return create_runtime_harness(connection=db_connection)


class _StubEmbeddingProvider(KnowledgeEmbeddingProvider):
    @property
    def model_name(self) -> str:
        return "test-embedding-model"

    def embed_texts(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        vectors: list[tuple[float, ...]] = []
        for index, _text in enumerate(texts):
            vectors.append((1.0, float(index + 1), 0.0))
        return tuple(vectors)


def test_review_fetch_approve_publish_and_searchable_promotion_work_end_to_end(
    harness: KnowledgeRuntimeHarness,
) -> None:
    seed = uuid4().hex
    requested_by = str(uuid4())
    reviewer_id = str(uuid4())
    publisher_id = str(uuid4())
    search_token = f"knowledge-search-{seed}"
    file_payload = _file_ingestion_payload(
        requested_by=requested_by,
        idempotency_key=f"idem-publish-{seed}",
        filename="finance-act-2026.pdf",
        file_bytes=f"official-file-{seed}".encode(),
        source_class="tax_law",
    )
    publication_payload = _publication_payload(
        seed=seed,
        source_class="tax_law",
        authority_level="statute",
        search_token=search_token,
    )

    with TestClient(harness.app) as client:
        admin_headers = build_admin_auth_headers()
        ingest = client.post("/knowledge/ingestion/files", json=file_payload, headers=admin_headers)
        ingestion_job_id = str(cast(dict[str, object], _json(ingest)["result"])["ingestion_job_id"])

        fetch = client.get(f"/knowledge/ingestion/{ingestion_job_id}", headers=admin_headers)
        review = client.post(
            f"/knowledge/ingestion/{ingestion_job_id}/review",
            json={
                "reviewed_by": reviewer_id,
                "review_notes": [{"note": "reviewed for publication readiness"}],
                "proposed_source_updates": {"review_tag": f"tag-{seed}"},
            },
            headers=admin_headers,
        )
        approve = client.post(
            f"/knowledge/ingestion/{ingestion_job_id}/approve",
            json={
                "reviewed_by": reviewer_id,
                "review_notes": [{"note": "approved for governed publication"}],
                "publication_payload": publication_payload,
            },
            headers=admin_headers,
        )
        first_publish = client.post(
            f"/knowledge/ingestion/{ingestion_job_id}/publish",
            json={"published_by": publisher_id},
            headers={**_stable_headers("knowledge-publish"), **admin_headers},
        )
        second_publish = client.post(
            f"/knowledge/ingestion/{ingestion_job_id}/publish",
            json={"published_by": publisher_id},
            headers={**_stable_headers("knowledge-publish"), **admin_headers},
        )
        search = client.post(
            "/knowledge/search",
            json={"query": search_token, "tax_domain": "income_tax"},
        )
        effective_search = client.post(
            "/knowledge/search",
            json={
                "query": search_token,
                "tax_domain": "income_tax",
                "effective_date": "2026-04-19",
            },
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
        retrieve = client.post(
            "/knowledge/retrieve",
            json={
                "source_ids": [publication_payload["source_id"]],
                "anchor_ids": [
                    cast(list[dict[str, object]], publication_payload["anchors"])[0]["anchor_id"]
                ],
            },
        )

    fetch_body = _json(fetch)
    review_body = _json(review)
    approve_body = _json(approve)
    first_publish_body = _json(first_publish)
    search_body = _json(search)
    effective_search_body = _json(effective_search)
    timeline_body = _json(timeline)
    retrieve_body = _json(retrieve)

    assert ingest.status_code == 200
    assert fetch.status_code == 200
    assert review.status_code == 200
    assert approve.status_code == 200
    assert first_publish.status_code == 200
    assert second_publish.status_code == 200
    assert first_publish.content == second_publish.content
    assert cast(dict[str, object], fetch_body["result"])["ingestion_state"] == "uploaded"
    assert cast(dict[str, object], review_body["result"])["ingestion_state"] == "review_pending"
    assert (
        cast(dict[str, object], approve_body["result"])["ingestion_state"]
        == "approved_for_publication"
    )
    assert cast(dict[str, object], first_publish_body["result"])["ingestion_state"] == "published"
    assert _result_total(search_body) >= 1
    assert _result_total(effective_search_body) >= 1
    assert _result_total(timeline_body) >= 1
    assert publication_payload["source_id"] in _result_source_ids(search_body)
    assert publication_payload["source_id"] in _result_source_ids(effective_search_body)
    assert publication_payload["source_id"] in _result_source_ids(timeline_body)
    assert publication_payload["source_id"] in _result_source_ids(retrieve_body)


def test_publish_persists_chunk_embeddings_when_provider_is_configured(
    db_connection: psycopg.Connection,
) -> None:
    harness = create_runtime_harness(
        connection=db_connection,
        embedding_provider=_StubEmbeddingProvider(),
    )
    seed = uuid4().hex
    requested_by = str(uuid4())
    reviewer_id = str(uuid4())
    publisher_id = str(uuid4())
    file_payload = _file_ingestion_payload(
        requested_by=requested_by,
        idempotency_key=f"idem-publish-embed-{seed}",
        filename="finance-act-embeddings.pdf",
        file_bytes=f"official-file-embed-{seed}".encode(),
        source_class="tax_law",
    )
    publication_payload = _publication_payload(
        seed=seed,
        source_class="tax_law",
        authority_level="statute",
        search_token=f"knowledge-embed-{seed}",
    )

    with TestClient(harness.app) as client:
        admin_headers = build_admin_auth_headers()
        ingest = client.post("/knowledge/ingestion/files", json=file_payload, headers=admin_headers)
        ingestion_job_id = str(cast(dict[str, object], _json(ingest)["result"])["ingestion_job_id"])
        approve = client.post(
            f"/knowledge/ingestion/{ingestion_job_id}/approve",
            json={
                "reviewed_by": reviewer_id,
                "review_notes": [{"note": "approved for embedding publication"}],
                "publication_payload": publication_payload,
            },
            headers=admin_headers,
        )
        publish = client.post(
            f"/knowledge/ingestion/{ingestion_job_id}/publish",
            json={"published_by": publisher_id},
            headers=admin_headers,
        )

    assert approve.status_code == 200
    assert publish.status_code == 200

    published_result = cast(dict[str, object], _json(publish)["result"])
    published_source_version_id = str(
        require_object_dict(published_result["proposed_source_record"])[
            "published_source_version_id"
        ]
    )
    with db_connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                kce.embedding_model,
                kce.embedding_dimensions,
                kc.embedding_vector_ref
            FROM knowledge_chunk_embeddings AS kce
            JOIN knowledge_chunks AS kc
              ON kc.id = kce.chunk_id
            JOIN knowledge_anchors AS ka
              ON ka.anchor_id = kc.anchor_id
            WHERE ka.source_version_id = %s::uuid
            ORDER BY kc.chunk_index ASC, kce.id ASC
            """,
            (published_source_version_id,),
        )
        rows = cursor.fetchall()

    assert rows
    assert all(str(row[0]) == "test-embedding-model" for row in rows)
    assert all(int(row[1]) == 3 for row in rows)
    assert all(str(row[2]).startswith("openai-embedding://test-embedding-model/") for row in rows)


def test_publish_without_approval_fails_canonically(harness: KnowledgeRuntimeHarness) -> None:
    seed = uuid4().hex
    requested_by = str(uuid4())
    publisher_id = str(uuid4())
    payload = _file_ingestion_payload(
        requested_by=requested_by,
        idempotency_key=f"idem-no-approval-{seed}",
        filename="not-approved.pdf",
        file_bytes=f"not-approved-{seed}".encode(),
        source_class="tax_law",
    )

    with TestClient(harness.app) as client:
        admin_headers = build_admin_auth_headers()
        ingest = client.post("/knowledge/ingestion/files", json=payload, headers=admin_headers)
        ingestion_job_id = str(cast(dict[str, object], _json(ingest)["result"])["ingestion_job_id"])
        publish = client.post(
            f"/knowledge/ingestion/{ingestion_job_id}/publish",
            json={"published_by": publisher_id},
            headers=admin_headers,
        )

    detail = _detail(_json(publish))
    assert ingest.status_code == 200
    assert publish.status_code == 409
    assert detail["reason_code"] == "invalid_publication_state_transition"


def test_approve_with_invalid_authority_binding_fails_canonically(
    harness: KnowledgeRuntimeHarness,
) -> None:
    seed = uuid4().hex
    requested_by = str(uuid4())
    reviewer_id = str(uuid4())
    payload = _file_ingestion_payload(
        requested_by=requested_by,
        idempotency_key=f"idem-invalid-binding-{seed}",
        filename="binding.pdf",
        file_bytes=f"binding-{seed}".encode(),
        source_class="tax_law",
    )
    publication_payload = _publication_payload(
        seed=seed,
        source_class="tax_law",
        authority_level="guidance",
        search_token=f"binding-token-{seed}",
    )

    with TestClient(harness.app) as client:
        admin_headers = build_admin_auth_headers()
        ingest = client.post("/knowledge/ingestion/files", json=payload, headers=admin_headers)
        ingestion_job_id = str(cast(dict[str, object], _json(ingest)["result"])["ingestion_job_id"])
        approve = client.post(
            f"/knowledge/ingestion/{ingestion_job_id}/approve",
            json={
                "reviewed_by": reviewer_id,
                "review_notes": [{"note": "invalid binding"}],
                "publication_payload": publication_payload,
            },
            headers=admin_headers,
        )

    detail = _detail(_json(approve))
    assert approve.status_code == 400
    assert detail["reason_code"] == "invalid_authority_source_class_binding"


def test_publish_with_missing_approval_metadata_fails_canonically(
    db_connection: psycopg.Connection,
    harness: KnowledgeRuntimeHarness,
) -> None:
    seed = uuid4().hex
    requested_by = str(uuid4())
    publisher_id = str(uuid4())
    payload = _file_ingestion_payload(
        requested_by=requested_by,
        idempotency_key=f"idem-missing-metadata-{seed}",
        filename="missing-metadata.pdf",
        file_bytes=f"missing-metadata-{seed}".encode(),
        source_class="tax_law",
    )

    with TestClient(harness.app) as client:
        admin_headers = build_admin_auth_headers()
        ingest = client.post("/knowledge/ingestion/files", json=payload, headers=admin_headers)
        ingestion_result = cast(dict[str, object], _json(ingest)["result"])
        ingestion_job_id = str(ingestion_result["ingestion_job_id"])

    _force_job_state(
        db_connection,
        ingestion_job_id=ingestion_job_id,
        ingestion_state="approved_for_publication",
        proposed_source_record_update={},
    )

    with TestClient(harness.app) as client:
        publish = client.post(
            f"/knowledge/ingestion/{ingestion_job_id}/publish",
            json={"published_by": publisher_id},
            headers=build_admin_auth_headers(),
        )

    detail = _detail(_json(publish))
    assert publish.status_code == 409
    assert detail["reason_code"] == "knowledge_publication_safety_rejected"


def test_rejected_jobs_remain_non_searchable(harness: KnowledgeRuntimeHarness) -> None:
    seed = uuid4().hex
    requested_by = str(uuid4())
    reviewer_id = str(uuid4())
    payload = _file_ingestion_payload(
        requested_by=requested_by,
        idempotency_key=f"idem-rejected-{seed}",
        filename="rejected.pdf",
        file_bytes=f"rejected-{seed}".encode(),
        source_class="guidance",
    )

    with TestClient(harness.app) as client:
        admin_headers = build_admin_auth_headers()
        ingest = client.post("/knowledge/ingestion/files", json=payload, headers=admin_headers)
        ingestion_job_id = str(cast(dict[str, object], _json(ingest)["result"])["ingestion_job_id"])
        reject = client.post(
            f"/knowledge/ingestion/{ingestion_job_id}/reject",
            json={
                "reviewed_by": reviewer_id,
                "review_notes": [{"note": "not suitable for publication"}],
            },
            headers=admin_headers,
        )
        search = client.post(
            "/knowledge/search",
            json={"query": f"rejected-{seed}", "tax_domain": "income_tax"},
        )
        retrieve = client.post(
            "/knowledge/retrieve",
            json={"source_ids": [ingestion_job_id], "anchor_ids": []},
        )

    assert reject.status_code == 200
    assert _result_total(_json(search)) == 0
    assert _result_total(_json(retrieve)) == 0


def test_customer_document_lineage_cannot_be_published(
    db_connection: psycopg.Connection,
    harness: KnowledgeRuntimeHarness,
) -> None:
    seed = uuid4().hex
    requested_by = str(uuid4())
    reviewer_id = str(uuid4())
    publisher_id = str(uuid4())
    payload = _file_ingestion_payload(
        requested_by=requested_by,
        idempotency_key=f"idem-customer-lineage-{seed}",
        filename="customer-lineage.pdf",
        file_bytes=f"customer-lineage-{seed}".encode(),
        source_class="tax_law",
    )
    publication_payload = _publication_payload(
        seed=seed,
        source_class="tax_law",
        authority_level="statute",
        search_token=f"customer-lineage-{seed}",
    )

    with TestClient(harness.app) as client:
        admin_headers = build_admin_auth_headers()
        ingest = client.post("/knowledge/ingestion/files", json=payload, headers=admin_headers)
        ingestion_job_id = str(cast(dict[str, object], _json(ingest)["result"])["ingestion_job_id"])
        approve = client.post(
            f"/knowledge/ingestion/{ingestion_job_id}/approve",
            json={
                "reviewed_by": reviewer_id,
                "review_notes": [{"note": "approved then tampered for lineage test"}],
                "publication_payload": publication_payload,
            },
            headers=admin_headers,
        )

    assert approve.status_code == 200
    _force_job_state(
        db_connection,
        ingestion_job_id=ingestion_job_id,
        ingestion_state="approved_for_publication",
        proposed_source_record_update={"source_input_origin": "customer_uploaded_document"},
    )

    with TestClient(harness.app) as client:
        publish = client.post(
            f"/knowledge/ingestion/{ingestion_job_id}/publish",
            json={"published_by": publisher_id},
            headers=build_admin_auth_headers(),
        )

    detail = _detail(_json(publish))
    assert publish.status_code == 409
    assert detail["reason_code"] == "invalid_knowledge_lineage"


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
    source_class: str,
    authority_level: str,
    search_token: str,
) -> dict[str, object]:
    return {
        "source_id": f"KNW-PH13-{seed}",
        "source_family_id": f"KNW-PH13-FAMILY-{seed}",
        "title": f"Finance Act publication {seed}",
        "source_class": source_class,
        "authority_level": authority_level,
        "tax_domain": "income_tax",
        "issuing_authority": "Kenya Revenue Authority",
        "point_in_time_url": f"https://example.com/finance-act/{seed}",
        "source_version_form": "as_issued",
        "effective_from": "2026-01-01",
        "effective_to": None,
        "tax_year": 2026,
        "anchors": [
            {
                "anchor_id": f"anchor-{seed}",
                "anchor_title": f"Anchor {seed}",
                "anchor_path": f"anchor-path-{seed}",
                "anchor_text": f"Anchor text for {search_token}",
                "temporal_scope_from": "2026-01-01",
                "temporal_scope_to": None,
                "chunks": [
                    {"chunk_text": f"Chunk one {search_token}"},
                    {"chunk_text": f"Chunk two {search_token}"},
                ],
            }
        ],
    }


def _force_job_state(
    connection: psycopg.Connection,
    *,
    ingestion_job_id: str,
    ingestion_state: str,
    proposed_source_record_update: dict[str, object],
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT proposed_source_record FROM knowledge_ingestion_jobs WHERE id = %s",
            (ingestion_job_id,),
        )
        row = cursor.fetchone()
        assert row is not None
        proposed_source_record = require_object_dict(row[0])
        proposed_source_record.update(proposed_source_record_update)
        cursor.execute(
            """
            UPDATE knowledge_ingestion_jobs
            SET ingestion_state = %s,
                proposed_source_record = %s::jsonb
            WHERE id = %s
            """,
            (
                ingestion_state,
                json.dumps(proposed_source_record, sort_keys=True),
                ingestion_job_id,
            ),
        )
    connection.commit()


def _stable_headers(seed: str) -> dict[str, str]:
    return {
        "X-Correlation-ID": f"{seed}-corr",
        "X-Trace-ID": f"{seed}-trace",
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


def _result_source_ids(payload: dict[str, object]) -> tuple[str, ...]:
    result = cast(dict[str, object], payload["result"])
    items = cast(list[object], result["items"])
    return tuple(str(cast(dict[str, object], item)["source_id"]) for item in items)
