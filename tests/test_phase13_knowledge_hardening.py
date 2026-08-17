"""DB-backed hardening tests for governed knowledge runtime controls."""

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

from tests.knowledge_db_test_support import load_database_url
from tests.knowledge_db_test_support import require_object_dict
from tests.knowledge_db_test_support import create_runtime_harness
from tests.knowledge_db_test_support import KnowledgeRuntimeHarness
from tests.knowledge_db_test_support import build_admin_auth_headers
from tests.knowledge_db_test_support import ensure_knowledge_migration_applied


@pytest.fixture()
def db_connection() -> Iterator[psycopg.Connection]:
    """Create DB connection for governed knowledge hardening tests."""

    database_url = load_database_url()
    if database_url is None or not database_url.strip():
        pytest.skip("DATABASE_URL is not set; skipping knowledge hardening DB tests.")

    ensure_knowledge_migration_applied(database_url=database_url)
    try:
        connection = psycopg.connect(database_url, connect_timeout=5)
    except psycopg.OperationalError:
        pytest.skip("DATABASE_URL is not reachable; skipping knowledge hardening DB tests.")

    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def harness(db_connection: psycopg.Connection) -> KnowledgeRuntimeHarness:
    """Build one runtime harness for governed knowledge hardening tests."""

    return create_runtime_harness(connection=db_connection)


def test_protected_knowledge_routes_require_internal_auth_context(
    harness: KnowledgeRuntimeHarness,
) -> None:
    requested_by = str(uuid4())
    payload = _file_ingestion_payload(
        requested_by=requested_by,
        idempotency_key=f"hardening-auth-{uuid4().hex}",
        filename="auth-check.pdf",
        file_bytes=b"auth-check",
    )

    with TestClient(harness.app) as client:
        missing_auth = client.post("/knowledge/ingestion/files", json=payload)
        forbidden_role = client.post(
            "/knowledge/ingestion/files",
            json=payload,
            headers=_role_auth_headers(role="TaxAgent", user_id=str(uuid4())),
        )
        missing_management_auth = client.get("/knowledge/sources")

    assert missing_auth.status_code == 401
    assert forbidden_role.status_code == 403
    assert missing_management_auth.status_code == 401
    assert _detail(_json(missing_auth))["reason_code"] == "auth_context_missing"
    assert _detail(_json(forbidden_role))["reason_code"] == "authorization_role_forbidden"
    assert _detail(_json(missing_management_auth))["reason_code"] == "auth_context_missing"


def test_metadata_correction_is_audited_and_rejected_after_publication(
    db_connection: psycopg.Connection,
    harness: KnowledgeRuntimeHarness,
) -> None:
    admin_user = str(uuid4())
    requested_by = str(uuid4())
    reviewer_id = str(uuid4())
    publisher_id = str(uuid4())
    seed = uuid4().hex
    file_payload = _file_ingestion_payload(
        requested_by=requested_by,
        idempotency_key=f"hardening-correction-{seed}",
        filename=f"hardening-correction-{seed}.pdf",
        file_bytes=f"hardening-correction-{seed}".encode(),
    )
    publication_payload = _publication_payload(
        seed=seed,
        source_id=f"KNW-HARD-CORR-{seed}",
        source_family_id=f"KNW-HARD-CORR-FAMILY-{seed}",
        search_token=f"hardening-correction-token-{seed}",
        effective_from="2026-01-01",
        effective_to=None,
    )
    admin_headers = build_admin_auth_headers(user_id=admin_user)

    with TestClient(harness.app) as client:
        ingest = client.post("/knowledge/ingestion/files", json=file_payload, headers=admin_headers)
        ingestion_job_id = str(cast(dict[str, object], _json(ingest)["result"])["ingestion_job_id"])
        review = client.post(
            f"/knowledge/ingestion/{ingestion_job_id}/review",
            json={
                "reviewed_by": reviewer_id,
                "review_notes": [{"note": "reviewed for correction"}],
                "proposed_source_updates": {"workflow_seed": seed},
            },
            headers=admin_headers,
        )
        approve = client.post(
            f"/knowledge/ingestion/{ingestion_job_id}/approve",
            json={
                "reviewed_by": reviewer_id,
                "review_notes": [{"note": "approved for correction"}],
                "publication_payload": publication_payload,
            },
            headers=admin_headers,
        )
        correction = client.post(
            f"/knowledge/ingestion/{ingestion_job_id}/metadata-correction",
            json={
                "corrected_by": admin_user,
                "review_notes": [{"note": "corrected metadata"}],
                "publication_payload_updates": {
                    "title": f"Corrected governed title {seed}",
                    "tax_year": 2027,
                },
            },
            headers=admin_headers,
        )
        publish = client.post(
            f"/knowledge/ingestion/{ingestion_job_id}/publish",
            json={"published_by": publisher_id},
            headers=admin_headers,
        )
        rejected_correction = client.post(
            f"/knowledge/ingestion/{ingestion_job_id}/metadata-correction",
            json={
                "corrected_by": admin_user,
                "review_notes": [{"note": "attempted post-publish correction"}],
                "publication_payload_updates": {"title": "Not allowed"},
            },
            headers=admin_headers,
        )

    correction_payload = _json(correction)
    correction_result = require_object_dict(correction_payload["result"])
    proposed_source_record = require_object_dict(correction_result["proposed_source_record"])
    corrected_publication_payload = require_object_dict(
        proposed_source_record["publication_payload"]
    )

    assert ingest.status_code == 200
    assert review.status_code == 200
    assert approve.status_code == 200
    assert correction.status_code == 200
    assert publish.status_code == 200
    assert rejected_correction.status_code == 409
    assert corrected_publication_payload["title"] == f"Corrected governed title {seed}"
    assert corrected_publication_payload["tax_year"] == 2027
    assert (
        _detail(_json(rejected_correction))["reason_code"] == "invalid_publication_state_transition"
    )

    with db_connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT details
            FROM audit_events
            WHERE event_type = 'knowledge_metadata_correction'
              AND resource_type = 'knowledge_ingestion_job'
              AND resource_id = %s::uuid
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (ingestion_job_id,),
        )
        row = cursor.fetchone()

    assert row is not None
    details = require_object_dict(cast(object, row[0]))
    updated_fields = cast(list[object], details["updated_fields"])
    assert [str(item) for item in updated_fields] == ["tax_year", "title"]


def test_search_retrieve_and_timeline_queries_emit_audit_events(
    db_connection: psycopg.Connection,
    harness: KnowledgeRuntimeHarness,
) -> None:
    seed = uuid4().hex
    published = _publish_version(
        harness=harness,
        seed=seed,
        source_id=f"KNW-HARD-AUDIT-{seed}",
        source_family_id=f"KNW-HARD-AUDIT-FAMILY-{seed}",
        search_token=f"hardening-audit-token-{seed}",
        effective_from="2025-01-01",
        effective_to=None,
    )

    with TestClient(harness.app) as client:
        search = client.post(
            "/knowledge/search",
            json={"query": published["search_token"], "tax_domain": "income_tax"},
        )
        retrieve = client.post(
            "/knowledge/retrieve",
            json={
                "source_ids": [published["source_id"]],
                "anchor_ids": [published["anchor_id"]],
            },
        )
        timeline = client.post(
            "/knowledge/timeline/search",
            json={
                "query": published["search_token"],
                "tax_domain": "income_tax",
                "start_date": "2025-01-01",
                "end_date": "2026-12-31",
            },
        )

    assert search.status_code == 200
    assert retrieve.status_code == 200
    assert timeline.status_code == 200

    with db_connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT event_type, resource_type, retention_policy_code
            FROM audit_events
            WHERE event_type = ANY(%s::text[])
            ORDER BY created_at ASC, id ASC
            """,
            (
                [
                    "knowledge_search",
                    "knowledge_retrieve",
                    "knowledge_timeline_search",
                ],
            ),
        )
        rows = cursor.fetchall()

    emitted = {(str(row[0]), str(row[1]), str(row[2])) for row in rows}
    assert (
        "knowledge_search",
        "knowledge_query",
        "knowledge_runtime_query_retention",
    ) in emitted
    assert (
        "knowledge_retrieve",
        "knowledge_query",
        "knowledge_runtime_query_retention",
    ) in emitted
    assert (
        "knowledge_timeline_search",
        "knowledge_query",
        "knowledge_runtime_query_retention",
    ) in emitted


def test_archived_lineage_remains_visible_through_source_detail_retention_summary(
    harness: KnowledgeRuntimeHarness,
) -> None:
    seed = uuid4().hex
    acting_user = str(uuid4())
    predecessor = _publish_version(
        harness=harness,
        seed=f"{seed}-pred",
        source_id=f"KNW-HARD-RET-{seed}",
        source_family_id=f"KNW-HARD-RET-FAMILY-{seed}",
        search_token=f"hardening-retention-token-{seed}",
        effective_from="2025-01-01",
        effective_to="2025-12-31",
    )
    successor = _publish_version(
        harness=harness,
        seed=f"{seed}-succ",
        source_id=predecessor["source_id"],
        source_family_id=predecessor["source_family_id"],
        search_token=f"hardening-retention-token-{seed}",
        effective_from="2026-01-01",
        effective_to=None,
    )
    admin_headers = build_admin_auth_headers(user_id=acting_user)

    with TestClient(harness.app) as client:
        supersede = client.post(
            f"/knowledge/source-versions/{predecessor['source_version_id']}/supersede",
            json={
                "successor_source_version_id": successor["source_version_id"],
                "superseded_by": acting_user,
            },
            headers=admin_headers,
        )
        archive = client.post(
            f"/knowledge/source-versions/{predecessor['source_version_id']}/archive",
            json={"archived_by": acting_user},
            headers=admin_headers,
        )
        source_detail = client.get(
            f"/knowledge/sources/{predecessor['source_id']}",
            headers=admin_headers,
        )

    assert supersede.status_code == 200
    assert archive.status_code == 200
    assert source_detail.status_code == 200

    source_result = require_object_dict(_json(source_detail)["result"])
    retention_summary = require_object_dict(source_result["retention_summary"])
    version_items = cast(list[object], source_result["versions"])
    version_states = {str(require_object_dict(item)["publication_state"]) for item in version_items}

    assert retention_summary == {
        "lineage_preserved": True,
        "has_document_lineage": True,
        "has_purged_document_lineage": False,
        "retention_policy_code": "knowledge_runtime_default_retention",
        "purge_supported": False,
    }
    assert version_states == {"archived", "published"}


def _publish_version(
    *,
    harness: KnowledgeRuntimeHarness,
    seed: str,
    source_id: str,
    source_family_id: str,
    search_token: str,
    effective_from: str,
    effective_to: str | None,
) -> dict[str, str]:
    requested_by = str(uuid4())
    reviewer_id = str(uuid4())
    publisher_id = str(uuid4())
    file_payload = _file_ingestion_payload(
        requested_by=requested_by,
        idempotency_key=f"hardening-publish-{seed}",
        filename=f"hardening-{seed}.pdf",
        file_bytes=f"hardening-{seed}".encode(),
    )
    publication_payload = _publication_payload(
        seed=seed,
        source_id=source_id,
        source_family_id=source_family_id,
        search_token=search_token,
        effective_from=effective_from,
        effective_to=effective_to,
    )
    admin_headers = build_admin_auth_headers()

    with TestClient(harness.app) as client:
        ingest = client.post("/knowledge/ingestion/files", json=file_payload, headers=admin_headers)
        ingestion_job_id = str(cast(dict[str, object], _json(ingest)["result"])["ingestion_job_id"])
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
    assert approve.status_code == 200
    assert publish.status_code == 200

    published_result = require_object_dict(_json(publish)["result"])
    proposed_source_record = require_object_dict(published_result["proposed_source_record"])
    return {
        "source_id": source_id,
        "source_family_id": source_family_id,
        "source_version_id": str(proposed_source_record["published_source_version_id"]),
        "anchor_id": f"anchor-{seed}",
        "search_token": search_token,
    }


def _publication_payload(
    *,
    seed: str,
    source_id: str,
    source_family_id: str,
    search_token: str,
    effective_from: str,
    effective_to: str | None,
) -> dict[str, object]:
    return {
        "source_id": source_id,
        "source_family_id": source_family_id,
        "title": f"Governed source {seed}",
        "source_class": "tax_law",
        "authority_level": "statute",
        "tax_domain": "income_tax",
        "issuing_authority": "Kenya Revenue Authority",
        "point_in_time_url": f"https://example.com/hardening/{seed}",
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


def _role_auth_headers(*, role: str, user_id: str) -> dict[str, str]:
    return {
        "X-Auth-Context": json.dumps(
            {
                "schema_version": "1.0.0",
                "user_id": user_id,
                "tenant_id": "default_tenant",
                "role": role,
                "session_id": "11111111-2222-3333-4444-555555555555",
                "delegation_context": {
                    "is_delegated": False,
                    "principal_user_id": None,
                    "delegate_user_id": None,
                    "delegation_id": None,
                    "granted_at": None,
                    "revoked_at": None,
                },
            },
            sort_keys=True,
        )
    }


def _json(response: Any) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


def _detail(payload: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], payload["detail"])
