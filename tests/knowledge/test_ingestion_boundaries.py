from __future__ import annotations

import base64

from fastapi.testclient import TestClient

from tests.knowledge.support import response_json
from tests.knowledge.support import require_object
from tests.knowledge.support import stable_headers
from tests.knowledge.support import admin_auth_headers
from services.knowledge.app.main import INVALID_KNOWLEDGE_REQUEST
from services.knowledge.app.main import KNOWLEDGE_IDEMPOTENCY_CONFLICT

REQUESTED_BY = "123e4567-e89b-12d3-a456-426614174000"


def test_ingestion_url_route_rejects_missing_auth_context(client: TestClient) -> None:
    response = client.post(
        "/knowledge/ingestion/urls",
        json={
            "requested_by": REQUESTED_BY,
            "idempotency_key": "url-job-001",
            "url": "https://example.com/source",
        },
        headers=stable_headers("ingest-auth"),
    )

    payload = response_json(response)
    detail = require_object(payload["detail"])

    assert response.status_code == 401
    assert detail["error_code"] == "auth_context_missing"
    assert detail["message"] == "Auth context header is required."
    assert detail["reason"] == "auth_context_missing"


def test_admin_url_ingestion_returns_deterministic_success_shape(client: TestClient) -> None:
    response = client.post(
        "/knowledge/ingestion/urls",
        json={
            "requested_by": REQUESTED_BY,
            "idempotency_key": "url-job-001",
            "url": "https://example.com/source",
            "source_class": "guidance",
        },
        headers=admin_auth_headers("url-ingest"),
    )

    payload = response_json(response)
    result = require_object(payload["result"])

    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["correlation_id"] == "url-ingest-corr"
    assert payload["trace_id"] == "url-ingest-trace"
    assert result["ingestion_job_id"] == "job-url-001"
    assert result["document_id"] == "doc-url-001"
    assert result["requested_by"] == REQUESTED_BY
    assert result["ingestion_state"] == "uploaded"
    assert result["source_input_origin"] == "official_source_url"
    assert result["source_class"] == "guidance"


def test_file_ingestion_requires_explicit_legacy_import_acknowledgement(
    client: TestClient,
) -> None:
    response = client.post(
        "/knowledge/ingestion/files",
        json={
            "requested_by": REQUESTED_BY,
            "idempotency_key": "file-job-001",
            "filename": "finance-act.pdf",
            "mime_type": "application/pdf",
            "file_content_base64": base64.b64encode(b"pdf-bytes").decode("utf-8"),
            "source_class": "tax_law",
        },
        headers=admin_auth_headers("file-gate"),
    )

    payload = response_json(response)
    detail = require_object(payload["detail"])

    assert response.status_code == 400
    assert detail["error_code"] == INVALID_KNOWLEDGE_REQUEST
    assert detail["message"] == "Knowledge request field `legacy_import_acknowledged` is invalid."
    assert detail["reason"] == INVALID_KNOWLEDGE_REQUEST


def test_admin_file_ingestion_accepts_legacy_import_when_explicitly_acknowledged(
    client: TestClient,
) -> None:
    response = client.post(
        "/knowledge/ingestion/files",
        json={
            "requested_by": REQUESTED_BY,
            "idempotency_key": "file-job-001",
            "filename": "finance-act.pdf",
            "mime_type": "application/pdf",
            "file_content_base64": base64.b64encode(b"pdf-bytes").decode("utf-8"),
            "legacy_import_acknowledged": True,
            "source_class": "tax_law",
        },
        headers=admin_auth_headers("file-ingest"),
    )

    payload = response_json(response)
    result = require_object(payload["result"])

    assert response.status_code == 200
    assert result["ingestion_job_id"] == "job-file-001"
    assert result["document_id"] == "doc-file-001"
    assert result["requested_by"] == REQUESTED_BY
    assert result["ingestion_state"] == "uploaded"
    assert result["source_input_origin"] == "official_source_upload"
    assert result["source_class"] == "tax_law"


def test_admin_url_ingestion_surfaces_repository_conflict_canonically(
    client: TestClient,
) -> None:
    response = client.post(
        "/knowledge/ingestion/urls",
        json={
            "requested_by": REQUESTED_BY,
            "idempotency_key": "conflict-url-key",
            "url": "https://example.com/conflict",
            "source_class": "guidance",
        },
        headers=admin_auth_headers("url-conflict"),
    )

    payload = response_json(response)
    detail = require_object(payload["detail"])

    assert response.status_code == 409
    assert detail["error_code"] == KNOWLEDGE_IDEMPOTENCY_CONFLICT
    assert (
        detail["message"] == "Knowledge ingestion idempotency key conflicts with existing payload."
    )
    assert detail["reason"] == KNOWLEDGE_IDEMPOTENCY_CONFLICT
