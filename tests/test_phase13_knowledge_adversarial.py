"""DB-backed adversarial tests for governed knowledge runtime boundaries."""

from __future__ import annotations

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
    """Create DB connection for governed knowledge adversarial tests."""

    database_url = load_database_url()
    if database_url is None or not database_url.strip():
        pytest.skip("DATABASE_URL is not set; skipping knowledge adversarial DB tests.")

    ensure_knowledge_migration_applied(database_url=database_url)
    try:
        connection = psycopg.connect(database_url, connect_timeout=5)
    except psycopg.OperationalError:
        pytest.skip("DATABASE_URL is not reachable; skipping knowledge adversarial DB tests.")

    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def harness(db_connection: psycopg.Connection) -> KnowledgeRuntimeHarness:
    """Build one runtime harness for governed knowledge adversarial tests."""

    return create_runtime_harness(connection=db_connection)


def test_malformed_file_ingestion_payload_fails_canonically(
    harness: KnowledgeRuntimeHarness,
) -> None:
    with TestClient(harness.app) as client:
        response = client.post(
            "/knowledge/ingestion/files",
            json={
                "requested_by": str(uuid4()),
                "idempotency_key": f"bad-base64-{uuid4().hex}",
                "filename": "bad.pdf",
                "mime_type": "application/pdf",
                "file_content_base64": "%%%not-base64%%%",
                "source_input_origin": "official_source_upload",
                "source_class": "tax_law",
            },
            headers=build_admin_auth_headers(),
        )

    assert response.status_code == 400
    assert _detail(_json(response))["reason_code"] == "invalid_knowledge_request"


def test_metadata_correction_rejects_immutable_lineage_fields(
    harness: KnowledgeRuntimeHarness,
) -> None:
    seed = uuid4().hex
    admin_headers = build_admin_auth_headers()
    file_payload = _file_ingestion_payload(
        requested_by=str(uuid4()),
        idempotency_key=f"adversarial-lineage-{seed}",
        filename=f"adversarial-lineage-{seed}.pdf",
        file_bytes=f"adversarial-lineage-{seed}".encode(),
    )
    publication_payload = _publication_payload(
        seed=seed,
        source_id=f"KNW-ADV-LINEAGE-{seed}",
        source_family_id=f"KNW-ADV-LINEAGE-FAMILY-{seed}",
        search_token=f"adversarial-lineage-token-{seed}",
    )

    with TestClient(harness.app) as client:
        ingest = client.post("/knowledge/ingestion/files", json=file_payload, headers=admin_headers)
        ingestion_job_id = str(cast(dict[str, object], _json(ingest)["result"])["ingestion_job_id"])
        approve = client.post(
            f"/knowledge/ingestion/{ingestion_job_id}/approve",
            json={
                "reviewed_by": str(uuid4()),
                "review_notes": [{"note": "approved for immutable-lineage test"}],
                "publication_payload": publication_payload,
            },
            headers=admin_headers,
        )
        correction = client.post(
            f"/knowledge/ingestion/{ingestion_job_id}/metadata-correction",
            json={
                "corrected_by": str(uuid4()),
                "review_notes": [{"note": "attempted lineage break"}],
                "publication_payload_updates": {"source_id": "KNW-BROKEN-LINEAGE"},
            },
            headers=admin_headers,
        )

    assert approve.status_code == 200
    assert correction.status_code == 409
    assert _detail(_json(correction))["reason_code"] == "invalid_knowledge_lineage"


def test_cross_domain_and_unpublished_material_do_not_leak_through_retrieval(
    harness: KnowledgeRuntimeHarness,
) -> None:
    seed = uuid4().hex
    unpublished_payload = _file_ingestion_payload(
        requested_by=str(uuid4()),
        idempotency_key=f"adversarial-unpublished-{seed}",
        filename=f"adversarial-unpublished-{seed}.pdf",
        file_bytes=f"adversarial-hidden-token-{seed}".encode(),
    )
    published = _publish_version(
        harness=harness,
        seed=seed,
        source_id=f"KNW-ADV-DOMAIN-{seed}",
        source_family_id=f"KNW-ADV-DOMAIN-FAMILY-{seed}",
        search_token=f"adversarial-domain-token-{seed}",
    )

    with TestClient(harness.app) as client:
        ingest = client.post(
            "/knowledge/ingestion/files",
            json=unpublished_payload,
            headers=build_admin_auth_headers(),
        )
        wrong_domain_search = client.post(
            "/knowledge/search",
            json={"query": published["search_token"], "tax_domain": "vat"},
        )
        wrong_domain_timeline = client.post(
            "/knowledge/timeline/search",
            json={
                "query": published["search_token"],
                "tax_domain": "vat",
                "start_date": "2025-01-01",
                "end_date": "2026-12-31",
            },
        )
        unpublished_search = client.post(
            "/knowledge/search",
            json={"query": f"adversarial-hidden-token-{seed}", "tax_domain": "income_tax"},
        )

    assert ingest.status_code == 200
    assert wrong_domain_search.status_code == 200
    assert wrong_domain_timeline.status_code == 200
    assert unpublished_search.status_code == 200
    assert _result_total(_json(wrong_domain_search)) == 0
    assert _result_total(_json(wrong_domain_timeline)) == 0
    assert _result_total(_json(unpublished_search)) == 0


def test_supersession_self_reference_fails_canonically(
    harness: KnowledgeRuntimeHarness,
) -> None:
    seed = uuid4().hex
    published = _publish_version(
        harness=harness,
        seed=f"{seed}-self",
        source_id=f"KNW-ADV-SUPER-{seed}",
        source_family_id=f"KNW-ADV-SUPER-FAMILY-{seed}",
        search_token=f"adversarial-supersede-token-{seed}",
    )

    with TestClient(harness.app) as client:
        response = client.post(
            f"/knowledge/source-versions/{published['source_version_id']}/supersede",
            json={
                "successor_source_version_id": published["source_version_id"],
                "superseded_by": str(uuid4()),
            },
            headers=build_admin_auth_headers(),
        )

    assert response.status_code == 409
    assert _detail(_json(response))["reason_code"] == "knowledge_supersession_conflict"


def _publish_version(
    *,
    harness: KnowledgeRuntimeHarness,
    seed: str,
    source_id: str,
    source_family_id: str,
    search_token: str,
) -> dict[str, str]:
    file_payload = _file_ingestion_payload(
        requested_by=str(uuid4()),
        idempotency_key=f"adversarial-publish-{seed}",
        filename=f"adversarial-{seed}.pdf",
        file_bytes=f"adversarial-{seed}".encode(),
    )
    publication_payload = _publication_payload(
        seed=seed,
        source_id=source_id,
        source_family_id=source_family_id,
        search_token=search_token,
    )
    admin_headers = build_admin_auth_headers()

    with TestClient(harness.app) as client:
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

    published_result = require_object_dict(_json(publish)["result"])
    proposed_source_record = require_object_dict(published_result["proposed_source_record"])
    return {
        "source_id": source_id,
        "source_version_id": str(proposed_source_record["published_source_version_id"]),
        "search_token": search_token,
    }


def _publication_payload(
    *,
    seed: str,
    source_id: str,
    source_family_id: str,
    search_token: str,
) -> dict[str, object]:
    return {
        "source_id": source_id,
        "source_family_id": source_family_id,
        "title": f"Adversarial governed source {seed}",
        "source_class": "tax_law",
        "authority_level": "statute",
        "tax_domain": "income_tax",
        "issuing_authority": "Kenya Revenue Authority",
        "point_in_time_url": f"https://example.com/adversarial/{seed}",
        "source_version_form": "as_issued",
        "effective_from": "2025-01-01",
        "effective_to": None,
        "tax_year": 2025,
        "anchors": [
            {
                "anchor_id": f"anchor-{seed}",
                "anchor_title": f"Anchor {seed}",
                "anchor_path": f"path-{seed}",
                "anchor_text": f"Anchor text for {search_token}",
                "temporal_scope_from": "2025-01-01",
                "temporal_scope_to": None,
                "chunks": [{"chunk_text": f"Chunk text for {search_token}"}],
            }
        ],
    }


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


def _detail(payload: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], payload["detail"])


def _result_total(payload: dict[str, object]) -> int:
    result = require_object_dict(payload["result"])
    return require_int(result["total"])
