from __future__ import annotations

from fastapi.testclient import TestClient

from tests.knowledge.support import response_json
from tests.knowledge.support import require_object
from tests.knowledge.support import admin_auth_headers
from services.knowledge.app.main import INVALID_KNOWLEDGE_LINEAGE
from services.knowledge.app.main import INVALID_KNOWLEDGE_REQUEST

REQUESTED_BY = "123e4567-e89b-12d3-a456-426614174000"
DOCUMENT_ID = "123e4567-e89b-12d3-a456-426614174321"
VALID_CHECKSUM = "a" * 64


def test_document_backed_ingestion_accepts_storage_registered_handoff(
    client: TestClient,
) -> None:
    response = client.post(
        "/knowledge/ingestion/documents",
        json={
            "requested_by": REQUESTED_BY,
            "idempotency_key": "document-job-001",
            "document_id": DOCUMENT_ID,
            "storage_key": "registered-sources/finance-act-2026.pdf",
            "mime_type": "application/pdf",
            "payload_checksum_sha256": VALID_CHECKSUM,
            "source_document_system": "storage_registered",
            "source_class": "tax_law",
        },
        headers=admin_auth_headers("document-ok"),
    )

    payload = response_json(response)
    result = require_object(payload["result"])

    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert result["ingestion_job_id"] == "job-document-001"
    assert result["document_id"] == DOCUMENT_ID
    assert result["requested_by"] == REQUESTED_BY
    assert result["ingestion_state"] == "uploaded"
    assert result["source_input_origin"] == "official_source_upload"
    assert result["source_input_ref"] == (
        "official-source-upload://storage_registered/documents/123e4567-e89b-12d3-a456-426614174321"
    )
    assert result["payload_checksum_sha256"] == VALID_CHECKSUM


def test_document_backed_ingestion_rejects_unsupported_source_document_system(
    client: TestClient,
) -> None:
    response = client.post(
        "/knowledge/ingestion/documents",
        json={
            "requested_by": REQUESTED_BY,
            "idempotency_key": "document-job-unsupported-system",
            "document_id": DOCUMENT_ID,
            "storage_key": "registered-sources/finance-act-2026.pdf",
            "mime_type": "application/pdf",
            "payload_checksum_sha256": VALID_CHECKSUM,
            "source_document_system": "document_ai",
            "source_class": "tax_law",
        },
        headers=admin_auth_headers("document-system"),
    )

    payload = response_json(response)
    detail = require_object(payload["detail"])

    assert response.status_code == 400
    assert detail["error_code"] == INVALID_KNOWLEDGE_REQUEST
    assert detail["message"] == "Knowledge request field `source_document_system` is invalid."
    assert detail["reason"] == INVALID_KNOWLEDGE_REQUEST


def test_document_backed_ingestion_rejects_url_style_storage_key(
    client: TestClient,
) -> None:
    response = client.post(
        "/knowledge/ingestion/documents",
        json={
            "requested_by": REQUESTED_BY,
            "idempotency_key": "document-job-url-storage",
            "document_id": DOCUMENT_ID,
            "storage_key": "https://example.com/finance-act-2026.pdf",
            "mime_type": "application/pdf",
            "payload_checksum_sha256": VALID_CHECKSUM,
            "source_document_system": "storage_registered",
            "source_class": "tax_law",
        },
        headers=admin_auth_headers("document-storage-url"),
    )

    payload = response_json(response)
    detail = require_object(payload["detail"])

    assert response.status_code == 409
    assert detail["error_code"] == INVALID_KNOWLEDGE_LINEAGE
    assert (
        detail["message"] == "Knowledge document storage must use a local storage key, not a URL."
    )
    assert detail["reason"] == INVALID_KNOWLEDGE_LINEAGE


def test_document_backed_ingestion_surfaces_lineage_conflict_canonically(
    client: TestClient,
) -> None:
    response = client.post(
        "/knowledge/ingestion/documents",
        json={
            "requested_by": REQUESTED_BY,
            "idempotency_key": "document-lineage-conflict",
            "document_id": DOCUMENT_ID,
            "storage_key": "registered-sources/finance-act-2026.pdf",
            "mime_type": "application/pdf",
            "payload_checksum_sha256": VALID_CHECKSUM,
            "source_document_system": "storage_registered",
            "source_class": "tax_law",
        },
        headers=admin_auth_headers("document-lineage"),
    )

    payload = response_json(response)
    detail = require_object(payload["detail"])

    assert response.status_code == 409
    assert detail["error_code"] == INVALID_KNOWLEDGE_LINEAGE
    assert detail["message"] == "Knowledge ingestion document storage reference is invalid."
    assert detail["reason"] == INVALID_KNOWLEDGE_LINEAGE
