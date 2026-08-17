"""Smoke tests for knowledge runtime boundary."""

from __future__ import annotations

import json
from uuid import UUID
from uuid import uuid4
from typing import Any
from typing import cast
from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.knowledge.app.main import create_app
from services.knowledge.app.repository import KnowledgeSearchRecord
from services.knowledge.app.repository import KnowledgeTimelineRecord
from services.knowledge.app.repository import KnowledgeIngestionRecord
from services.knowledge.app.repository import KnowledgeRepositoryError
from services.knowledge.app.repository import KnowledgeAnchorDetailRecord
from services.knowledge.app.repository import KnowledgeChunkSummaryRecord
from services.knowledge.app.repository import KnowledgeSourceDetailRecord
from services.knowledge.app.repository import KnowledgeSourceSummaryRecord
from services.knowledge.app.repository import KnowledgeIngestionDetailRecord
from services.knowledge.app.repository import KnowledgeIngestionSummaryRecord
from services.knowledge.app.repository import KnowledgeBulkIngestionItemRecord
from services.knowledge.app.repository import KnowledgeBulkOperationItemRecord
from services.knowledge.app.repository import KnowledgeSourceVersionSummaryRecord
from services.knowledge.app.repository import KnowledgeSourceVersionLifecycleRecord

REQUESTED_BY = "123e4567-e89b-12d3-a456-426614174000"


class _StubKnowledgeRepository:
    def __init__(self) -> None:
        self._records = (
            KnowledgeSearchRecord(
                source_id="KNW-ITA-15-2",
                title="Income Tax Act (Cap. 470), Section 15(2)",
                url="https://new.kenyalaw.org/akn/ke/act/1973/16/eng@2024-12-27",
                source_type="tax_law",
                tax_domain="income_tax",
                authority_level="statute",
                effective_from="1974-01-01",
                effective_to=None,
                tax_year=None,
                anchor_id="income-tax-act-15-2",
                content="Allowable deductions in production of income under section 15(2).",
            ),
            KnowledgeSearchRecord(
                source_id="KNW-ITA-5-1-B",
                title="Income Tax Act (Cap. 470), Section 5(1)(b)",
                url="https://new.kenyalaw.org/akn/ke/act/1973/16/eng@2024-12-27",
                source_type="tax_law",
                tax_domain="income_tax",
                authority_level="statute",
                effective_from="1974-01-01",
                effective_to=None,
                tax_year=None,
                anchor_id="income-tax-act-5-1-b",
                content="Chargeability for non-resident employment income under section 5(1)(b).",
            ),
        )

    def search_records(
        self,
        *,
        query: str,
        source_type: str | None,
        tax_domain: str | None,
        effective_date: date | None,
    ) -> tuple[KnowledgeSearchRecord, ...]:
        _ = (query, source_type, tax_domain, effective_date)
        return self._records

    def retrieve_records(
        self,
        *,
        source_ids: tuple[str, ...],
        anchor_ids: tuple[str, ...],
    ) -> tuple[KnowledgeSearchRecord, ...]:
        return tuple(
            record
            for record in self._records
            if record.source_id in source_ids or record.anchor_id in anchor_ids
        )

    def timeline_search_records(
        self,
        *,
        query: str,
        source_type: str | None,
        tax_domain: str,
        start_date: date,
        end_date: date,
    ) -> tuple[KnowledgeTimelineRecord, ...]:
        _ = (query, source_type, tax_domain, start_date, end_date)
        return (
            _timeline_record(
                source_version_id="123e4567-e89b-12d3-a456-426614174100",
                anchor_id="income-tax-act-15-2-2025",
                effective_from="2025-01-01",
                effective_to="2025-12-31",
                publication_state="superseded",
                timeline_position=1,
            ),
            _timeline_record(
                source_version_id="123e4567-e89b-12d3-a456-426614174101",
                anchor_id="income-tax-act-15-2-2026",
                effective_from="2026-01-01",
                effective_to=None,
                publication_state="published",
                timeline_position=2,
            ),
        )

    def ingest_file_source(
        self,
        *,
        requested_by: str,
        idempotency_key: str,
        filename: str,
        mime_type: str,
        file_content_base64: str,
        source_input_origin: str | None,
        source_class: str | None,
    ) -> KnowledgeIngestionRecord:
        _ = (
            requested_by,
            idempotency_key,
            filename,
            mime_type,
            file_content_base64,
            source_input_origin,
            source_class,
        )
        return KnowledgeIngestionRecord(
            ingestion_job_id="job-file-001",
            document_id="doc-file-001",
            requested_by=REQUESTED_BY,
            ingestion_state="uploaded",
            source_input_origin="official_source_upload",
            source_input_ref="official-source-upload://sha256/abc123",
            payload_checksum_sha256="abc123",
            source_class="tax_law",
        )

    def ingest_url_source(
        self,
        *,
        requested_by: str,
        idempotency_key: str,
        url: str,
        source_input_origin: str | None,
        source_class: str | None,
    ) -> KnowledgeIngestionRecord:
        if idempotency_key == "conflict-url-key":
            raise KnowledgeRepositoryError(
                reason_code="knowledge_idempotency_conflict",
                message="Knowledge ingestion idempotency key conflicts with existing payload.",
            )
        _ = (requested_by, url, source_input_origin, source_class)
        return KnowledgeIngestionRecord(
            ingestion_job_id="job-url-001",
            document_id="doc-url-001",
            requested_by=REQUESTED_BY,
            ingestion_state="uploaded",
            source_input_origin="official_source_url",
            source_input_ref="official-source-url://https://example.com/source",
            payload_checksum_sha256="def456",
            source_class="guidance",
        )

    def bulk_ingest_file_sources(
        self,
        *,
        requested_by: str,
        items: tuple[dict[str, object], ...],
    ) -> tuple[KnowledgeBulkIngestionItemRecord, ...]:
        _ = requested_by
        return tuple(
            _bulk_ingestion_item(index=index, idempotency_key=str(item["idempotency_key"]))
            for index, item in enumerate(items)
        )

    def bulk_ingest_url_sources(
        self,
        *,
        requested_by: str,
        items: tuple[dict[str, object], ...],
    ) -> tuple[KnowledgeBulkIngestionItemRecord, ...]:
        _ = requested_by
        return tuple(
            _bulk_ingestion_item(index=index, idempotency_key=str(item["idempotency_key"]))
            for index, item in enumerate(items)
        )

    def get_ingestion_job(
        self,
        *,
        ingestion_job_id: str,
    ) -> KnowledgeIngestionDetailRecord:
        _ = ingestion_job_id
        return _detail_record("review_pending")

    def list_ingestion_jobs(
        self,
        *,
        ingestion_state: str | None,
        source_input_origin: str | None,
        source_class: str | None,
        requested_by: str | None,
        limit: int,
        offset: int,
        sort_by: str | None,
        sort_order: str | None,
    ) -> tuple[KnowledgeIngestionSummaryRecord, ...]:
        _ = (
            ingestion_state,
            source_input_origin,
            source_class,
            requested_by,
            limit,
            offset,
            sort_by,
            sort_order,
        )
        return (_summary_ingestion_record(),)

    def review_ingestion_job(
        self,
        *,
        ingestion_job_id: str,
        reviewed_by: str,
        review_notes: tuple[dict[str, object], ...],
        proposed_source_updates: dict[str, object] | None,
    ) -> KnowledgeIngestionDetailRecord:
        _ = (ingestion_job_id, reviewed_by, review_notes, proposed_source_updates)
        return _detail_record("review_pending")

    def approve_ingestion_job(
        self,
        *,
        ingestion_job_id: str,
        reviewed_by: str,
        publication_payload: dict[str, object],
        review_notes: tuple[dict[str, object], ...],
    ) -> KnowledgeIngestionDetailRecord:
        _ = (ingestion_job_id, reviewed_by, publication_payload, review_notes)
        return _detail_record("approved_for_publication")

    def reject_ingestion_job(
        self,
        *,
        ingestion_job_id: str,
        reviewed_by: str,
        review_notes: tuple[dict[str, object], ...],
    ) -> KnowledgeIngestionDetailRecord:
        _ = (ingestion_job_id, reviewed_by, review_notes)
        return _detail_record("rejected")

    def publish_ingestion_job(
        self,
        *,
        ingestion_job_id: str,
        published_by: str,
    ) -> KnowledgeIngestionDetailRecord:
        _ = (ingestion_job_id, published_by)
        return _detail_record("published")

    def bulk_reject_ingestion_jobs(
        self,
        *,
        reviewed_by: str,
        ingestion_job_ids: tuple[str, ...],
        review_notes: tuple[dict[str, object], ...],
    ) -> tuple[KnowledgeBulkOperationItemRecord, ...]:
        _ = (reviewed_by, review_notes)
        return tuple(_bulk_item(item_id, "rejected") for item_id in ingestion_job_ids)

    def bulk_publish_ingestion_jobs(
        self,
        *,
        published_by: str,
        ingestion_job_ids: tuple[str, ...],
    ) -> tuple[KnowledgeBulkOperationItemRecord, ...]:
        _ = published_by
        return tuple(_bulk_item(item_id, "published") for item_id in ingestion_job_ids)

    def supersede_source_version(
        self,
        *,
        source_version_id: str,
        successor_source_version_id: str,
        superseded_by: str,
    ) -> KnowledgeSourceVersionLifecycleRecord:
        _ = (source_version_id, successor_source_version_id, superseded_by)
        return _lifecycle_record("superseded")

    def archive_source_version(
        self,
        *,
        source_version_id: str,
        archived_by: str,
    ) -> KnowledgeSourceVersionLifecycleRecord:
        _ = (source_version_id, archived_by)
        return _lifecycle_record("archived")

    def bulk_archive_source_versions(
        self,
        *,
        archived_by: str,
        source_version_ids: tuple[str, ...],
    ) -> tuple[KnowledgeBulkOperationItemRecord, ...]:
        _ = archived_by
        return tuple(_bulk_item(item_id, "archived") for item_id in source_version_ids)

    def get_source_version_lifecycle(
        self,
        *,
        source_version_id: str,
    ) -> KnowledgeSourceVersionLifecycleRecord:
        _ = source_version_id
        return _lifecycle_record("published")

    def list_source_versions(
        self,
        *,
        publication_state: str | None,
        source_id: str | None,
        source_family_id: str | None,
        tax_domain: str | None,
        source_class: str | None,
        limit: int,
        offset: int,
        sort_by: str | None,
        sort_order: str | None,
    ) -> tuple[KnowledgeSourceVersionSummaryRecord, ...]:
        _ = (
            publication_state,
            source_id,
            source_family_id,
            tax_domain,
            source_class,
            limit,
            offset,
            sort_by,
            sort_order,
        )
        return (_summary_source_version_record(),)

    def list_sources(
        self,
        *,
        source_class: str | None,
        tax_domain: str | None,
        limit: int,
        offset: int,
        sort_by: str | None,
        sort_order: str | None,
    ) -> tuple[KnowledgeSourceSummaryRecord, ...]:
        _ = (source_class, tax_domain, limit, offset, sort_by, sort_order)
        return (_summary_source_record(),)

    def get_source(
        self,
        *,
        source_id: str,
    ) -> KnowledgeSourceDetailRecord:
        _ = source_id
        return _source_detail_record()

    def get_anchor(
        self,
        *,
        anchor_id: str,
    ) -> KnowledgeAnchorDetailRecord:
        _ = anchor_id
        return _anchor_detail_record()

    def correct_ingestion_metadata(
        self,
        *,
        ingestion_job_id: str,
        corrected_by: str,
        review_notes: tuple[dict[str, object], ...],
        publication_payload_updates: dict[str, object],
    ) -> KnowledgeIngestionDetailRecord:
        _ = (ingestion_job_id, corrected_by, review_notes, publication_payload_updates)
        return _detail_record("review_pending")


def test_knowledge_app_boots_and_operational_routes_are_available() -> None:
    app = create_app(repository=_StubKnowledgeRepository())
    assert isinstance(app, FastAPI)

    with TestClient(app) as client:
        health = client.get("/healthz", headers={"X-Correlation-ID": "knw-health"})
        ready = client.get("/readyz", headers={"X-Correlation-ID": "knw-ready"})

    health_payload = _json(health)
    ready_payload = _json(ready)
    assert health.status_code == 200
    assert ready.status_code == 200
    assert health_payload["service"] == "knowledge"
    assert ready_payload["service"] == "knowledge"
    assert health_payload["status"] == "ok"
    assert ready_payload["status"] == "ready"


def test_knowledge_search_returns_deterministic_order_for_identical_input() -> None:
    app = create_app(repository=_StubKnowledgeRepository())
    request_payload = {"query": "income tax section"}
    with TestClient(app) as client:
        first = client.post("/knowledge/search", json=request_payload)
        second = client.post("/knowledge/search", json=request_payload)

    first_payload = _json(first)
    second_payload = _json(second)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first_payload["result"] == second_payload["result"]


def test_knowledge_rejects_invalid_payload_with_canonical_error_envelope() -> None:
    app = create_app(repository=_StubKnowledgeRepository())
    with TestClient(app) as client:
        response = client.post("/knowledge/search", json={"query": ""})
    payload = _json(response)
    detail = cast(dict[str, object], payload["detail"])
    assert response.status_code == 400
    expected_fields = {
        "error_code",
        "message",
        "reason",
        "reason_code",
        "correlation_id",
        "trace_id",
    }
    assert expected_fields.issubset(detail.keys())
    assert detail["reason"] == "invalid_knowledge_request"


def test_knowledge_timeline_search_returns_deterministic_order_for_identical_input() -> None:
    app = create_app(repository=_StubKnowledgeRepository())
    request_payload = {
        "query": "income tax section",
        "tax_domain": "income_tax",
        "start_date": "2025-01-01",
        "end_date": "2026-12-31",
    }
    with TestClient(app) as client:
        first = client.post(
            "/knowledge/timeline/search",
            json=request_payload,
            headers=_stable_headers("knowledge-timeline"),
        )
        second = client.post(
            "/knowledge/timeline/search",
            json=request_payload,
            headers=_stable_headers("knowledge-timeline"),
        )

    first_payload = _json(first)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.content == second.content
    assert cast(dict[str, object], first_payload["result"])["total"] == 2


def test_knowledge_file_ingestion_returns_deterministic_success_shape() -> None:
    app = create_app(repository=_StubKnowledgeRepository())
    request_payload = {
        "requested_by": REQUESTED_BY,
        "idempotency_key": "idem-file-001",
        "filename": "finance-act.pdf",
        "mime_type": "application/pdf",
        "file_content_base64": "cGRm",
        "source_input_origin": "official_source_upload",
        "source_class": "tax_law",
    }
    headers = {
        "X-Correlation-ID": "knowledge-file-ingest-001",
        "X-Trace-ID": "knowledge-file-ingest-001-trace",
    }
    with TestClient(app) as client:
        protected_headers = {**headers, **_admin_headers()}
        first = client.post(
            "/knowledge/ingestion/files",
            json=request_payload,
            headers=protected_headers,
        )
        second = client.post(
            "/knowledge/ingestion/files",
            json=request_payload,
            headers=protected_headers,
        )

    first_payload = _json(first)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.content == second.content
    assert cast(dict[str, object], first_payload["result"])["source_input_origin"] == (
        "official_source_upload"
    )


def test_knowledge_url_ingestion_surfaces_repository_conflict_canonically() -> None:
    app = create_app(repository=_StubKnowledgeRepository())
    request_payload = {
        "requested_by": REQUESTED_BY,
        "idempotency_key": "conflict-url-key",
        "url": "https://example.com/source",
        "source_input_origin": "official_source_url",
    }
    with TestClient(app) as client:
        response = client.post(
            "/knowledge/ingestion/urls",
            json=request_payload,
            headers=_admin_headers(),
        )

    payload = _json(response)
    detail = cast(dict[str, object], payload["detail"])
    assert response.status_code == 409
    assert detail["reason_code"] == "knowledge_idempotency_conflict"


def test_knowledge_bulk_ingestion_routes_return_deterministic_shapes() -> None:
    app = create_app(repository=_StubKnowledgeRepository())
    payload = {
        "acting_user": REQUESTED_BY,
        "items": [
            {
                "idempotency_key": "bulk-file-001",
                "filename": "finance-act.pdf",
                "mime_type": "application/pdf",
                "file_content_base64": "cGRm",
                "source_input_origin": "official_source_upload",
                "source_class": "tax_law",
            },
            {
                "idempotency_key": "bulk-file-002",
                "filename": "guidance.html",
                "mime_type": "text/html",
                "file_content_base64": "aHRtbA==",
                "source_input_origin": "official_source_upload",
                "source_class": "guidance",
            },
        ],
    }
    with TestClient(app) as client:
        first_files = client.post(
            "/knowledge/ingestion/files/bulk",
            json=payload,
            headers={**_stable_headers("bulk-files"), **_admin_headers()},
        )
        second_files = client.post(
            "/knowledge/ingestion/files/bulk",
            json=payload,
            headers={**_stable_headers("bulk-files"), **_admin_headers()},
        )
        first_urls = client.post(
            "/knowledge/ingestion/urls/bulk",
            json={
                "acting_user": REQUESTED_BY,
                "items": [
                    {
                        "idempotency_key": "bulk-url-001",
                        "url": "https://example.com/law-1",
                        "source_input_origin": "official_source_url",
                    },
                    {
                        "idempotency_key": "bulk-url-002",
                        "url": "https://example.com/law-2",
                        "source_input_origin": "official_source_url",
                    },
                ],
            },
            headers={**_stable_headers("bulk-urls"), **_admin_headers()},
        )
        second_urls = client.post(
            "/knowledge/ingestion/urls/bulk",
            json={
                "acting_user": REQUESTED_BY,
                "items": [
                    {
                        "idempotency_key": "bulk-url-001",
                        "url": "https://example.com/law-1",
                        "source_input_origin": "official_source_url",
                    },
                    {
                        "idempotency_key": "bulk-url-002",
                        "url": "https://example.com/law-2",
                        "source_input_origin": "official_source_url",
                    },
                ],
            },
            headers={**_stable_headers("bulk-urls"), **_admin_headers()},
        )

    first_files_payload = _json(first_files)
    first_urls_payload = _json(first_urls)
    assert first_files.status_code == 200
    assert second_files.status_code == 200
    assert first_urls.status_code == 200
    assert second_urls.status_code == 200
    assert first_files.content == second_files.content
    assert first_urls.content == second_urls.content
    assert cast(dict[str, object], first_files_payload["result"])["bulk_status"] == "full_success"
    assert cast(dict[str, object], first_urls_payload["result"])["bulk_status"] == "full_success"


def test_knowledge_review_fetch_and_publish_routes_return_deterministic_shapes() -> None:
    app = create_app(repository=_StubKnowledgeRepository())
    publish_payload = {"published_by": REQUESTED_BY}
    with TestClient(app) as client:
        fetch = client.get("/knowledge/ingestion/job-file-001", headers=_admin_headers())
        publish = client.post(
            "/knowledge/ingestion/job-file-001/publish",
            json=publish_payload,
            headers=_admin_headers(),
        )

    fetch_payload = _json(fetch)
    publish_payload_body = _json(publish)
    assert fetch.status_code == 200
    assert publish.status_code == 200
    assert cast(dict[str, object], fetch_payload["result"])["ingestion_state"] == "review_pending"
    assert cast(dict[str, object], publish_payload_body["result"])["ingestion_state"] == "published"


def test_knowledge_management_listing_routes_return_deterministic_shapes() -> None:
    app = create_app(repository=_StubKnowledgeRepository())
    with TestClient(app) as client:
        first_ingestion = client.get(
            "/knowledge/ingestion",
            params={
                "ingestion_state": "review_pending",
                "offset": 0,
                "sort_by": "created_at",
                "sort_order": "desc",
            },
            headers=_admin_headers(),
        )
        second_ingestion = client.get(
            "/knowledge/ingestion",
            params={
                "ingestion_state": "review_pending",
                "offset": 0,
                "sort_by": "created_at",
                "sort_order": "desc",
            },
            headers=_admin_headers(),
        )
        first_versions = client.get(
            "/knowledge/source-versions",
            params={
                "publication_state": "published",
                "offset": 0,
                "sort_by": "source_family_id",
                "sort_order": "asc",
            },
            headers=_admin_headers(),
        )
        second_versions = client.get(
            "/knowledge/source-versions",
            params={
                "publication_state": "published",
                "offset": 0,
                "sort_by": "source_family_id",
                "sort_order": "asc",
            },
            headers=_admin_headers(),
        )
        detail = client.get(
            "/knowledge/source-versions/123e4567-e89b-12d3-a456-426614174100",
            headers=_admin_headers(),
        )
        sources = client.get("/knowledge/sources", headers=_admin_headers())
        source_detail = client.get(
            "/knowledge/sources/KNW-FINANCE-2026",
            headers=_admin_headers(),
        )
        anchor_detail = client.get(
            "/knowledge/anchors/anchor-finance-2026",
            headers=_admin_headers(),
        )

    first_ingestion_payload = _json(first_ingestion)
    second_ingestion_payload = _json(second_ingestion)
    first_versions_payload = _json(first_versions)
    second_versions_payload = _json(second_versions)
    detail_payload = _json(detail)
    assert first_ingestion.status_code == 200
    assert second_ingestion.status_code == 200
    assert first_versions.status_code == 200
    assert second_versions.status_code == 200
    assert detail.status_code == 200
    assert sources.status_code == 200
    assert source_detail.status_code == 200
    assert anchor_detail.status_code == 200
    assert first_ingestion_payload["result"] == second_ingestion_payload["result"]
    assert first_versions_payload["result"] == second_versions_payload["result"]
    ingestion_result = cast(dict[str, object], first_ingestion_payload["result"])
    versions_result = cast(dict[str, object], first_versions_payload["result"])
    ingestion_page = cast(dict[str, object], ingestion_result["page"])
    versions_page = cast(dict[str, object], versions_result["page"])
    assert ingestion_page["sort_by"] == "created_at"
    assert versions_page["sort_by"] == "source_family_id"
    assert cast(dict[str, object], detail_payload["result"])["publication_state"] == "published"
    assert cast(dict[str, object], _json(sources)["result"])["total"] == 1
    assert "versions" in cast(dict[str, object], _json(source_detail)["result"])
    assert "chunks" in cast(dict[str, object], _json(anchor_detail)["result"])


def test_knowledge_bulk_management_routes_return_deterministic_shapes() -> None:
    app = create_app(repository=_StubKnowledgeRepository())
    with TestClient(app) as client:
        reject = client.post(
            "/knowledge/ingestion/bulk/reject",
            json={
                "acting_user": REQUESTED_BY,
                "ids": ["job-file-001", "job-url-001"],
                "review_notes": [{"note": "bulk reject"}],
            },
            headers=_admin_headers(),
        )
        publish = client.post(
            "/knowledge/ingestion/bulk/publish",
            json={
                "acting_user": REQUESTED_BY,
                "ids": ["job-file-001", "job-url-001"],
            },
            headers=_admin_headers(),
        )
        archive = client.post(
            "/knowledge/source-versions/bulk/archive",
            json={
                "acting_user": REQUESTED_BY,
                "ids": [
                    "123e4567-e89b-12d3-a456-426614174100",
                    "123e4567-e89b-12d3-a456-426614174101",
                ],
            },
            headers=_admin_headers(),
        )

    reject_payload = _json(reject)
    publish_payload = _json(publish)
    archive_payload = _json(archive)
    assert reject.status_code == 200
    assert publish.status_code == 200
    assert archive.status_code == 200
    assert cast(dict[str, object], reject_payload["result"])["bulk_status"] == "full_success"
    assert cast(dict[str, object], publish_payload["result"])["bulk_status"] == "full_success"
    assert cast(dict[str, object], archive_payload["result"])["bulk_status"] == "full_success"


def test_knowledge_source_version_lifecycle_routes_return_deterministic_shapes() -> None:
    app = create_app(repository=_StubKnowledgeRepository())
    with TestClient(app) as client:
        supersede = client.post(
            "/knowledge/source-versions/123e4567-e89b-12d3-a456-426614174100/supersede",
            json={
                "successor_source_version_id": "123e4567-e89b-12d3-a456-426614174101",
                "superseded_by": REQUESTED_BY,
            },
            headers=_admin_headers(),
        )
        archive = client.post(
            "/knowledge/source-versions/123e4567-e89b-12d3-a456-426614174100/archive",
            json={"archived_by": REQUESTED_BY},
            headers=_admin_headers(),
        )

    supersede_payload = _json(supersede)
    archive_payload = _json(archive)
    assert supersede.status_code == 200
    assert archive.status_code == 200
    assert cast(dict[str, object], supersede_payload["result"])["publication_state"] == "superseded"
    assert cast(dict[str, object], archive_payload["result"])["publication_state"] == "archived"


def _detail_record(state: str) -> KnowledgeIngestionDetailRecord:
    return KnowledgeIngestionDetailRecord(
        ingestion_job_id="job-file-001",
        document_id="doc-file-001",
        requested_by=REQUESTED_BY,
        ingestion_state=state,
        source_input_origin="official_source_upload",
        source_input_ref="official-source-upload://sha256/abc123",
        payload_checksum_sha256="abc123",
        source_class="tax_law",
        extracted_metadata={"filename": "finance-act.pdf"},
        proposed_source_record={"source_id": "KNW-FINANCE-2026"},
        review_notes=({"note": "reviewed"},),
        completed_at=None,
    )


def _summary_ingestion_record() -> KnowledgeIngestionSummaryRecord:
    return KnowledgeIngestionSummaryRecord(
        ingestion_job_id="job-file-001",
        document_id="doc-file-001",
        requested_by=REQUESTED_BY,
        ingestion_state="review_pending",
        source_input_origin="official_source_upload",
        source_input_ref="official-source-upload://sha256/abc123",
        payload_checksum_sha256="abc123",
        source_class="tax_law",
        created_at="2026-04-19T12:00:00+00:00",
        completed_at=None,
    )


def _lifecycle_record(state: str) -> KnowledgeSourceVersionLifecycleRecord:
    return KnowledgeSourceVersionLifecycleRecord(
        source_version_id="123e4567-e89b-12d3-a456-426614174100",
        source_id="KNW-FINANCE-2026",
        source_family_id="KNW-FINANCE-FAMILY",
        publication_state=state,
        source_input_origin="official_source_upload",
        source_version_form="as_issued",
        effective_from="2026-01-01",
        effective_to=None,
        tax_year=2026,
        supersedes_source_version_id=None,
        superseded_by_source_version_id=(
            "123e4567-e89b-12d3-a456-426614174101" if state == "superseded" else None
        ),
    )


def _summary_source_version_record() -> KnowledgeSourceVersionSummaryRecord:
    return KnowledgeSourceVersionSummaryRecord(
        source_version_id="123e4567-e89b-12d3-a456-426614174100",
        source_id="KNW-FINANCE-2026",
        source_family_id="KNW-FINANCE-FAMILY",
        title="Finance Act governed source",
        source_class="tax_law",
        tax_domain="income_tax",
        authority_level="statute",
        publication_state="published",
        source_input_origin="official_source_upload",
        source_version_form="as_issued",
        effective_from="2026-01-01",
        effective_to=None,
        tax_year=2026,
        supersedes_source_version_id=None,
        superseded_by_source_version_id=None,
    )


def _summary_source_record() -> KnowledgeSourceSummaryRecord:
    return KnowledgeSourceSummaryRecord(
        source_id="KNW-FINANCE-2026",
        source_family_id="KNW-FINANCE-FAMILY",
        title="Finance Act governed source",
        canonical_url="https://example.com/finance-act",
        source_class="tax_law",
        tax_domain="income_tax",
        authority_level="statute",
        issuing_authority="Kenya Revenue Authority",
        version_count=1,
        anchor_count=1,
        created_at="2026-04-19T12:00:00+00:00",
        retired_at=None,
    )


def _source_detail_record() -> KnowledgeSourceDetailRecord:
    return KnowledgeSourceDetailRecord(
        source_id="KNW-FINANCE-2026",
        source_family_id="KNW-FINANCE-FAMILY",
        title="Finance Act governed source",
        canonical_url="https://example.com/finance-act",
        source_class="tax_law",
        tax_domain="income_tax",
        authority_level="statute",
        issuing_authority="Kenya Revenue Authority",
        version_count=1,
        anchor_count=1,
        chunk_count=1,
        created_at="2026-04-19T12:00:00+00:00",
        retired_at=None,
        versions=(_summary_source_version_record(),),
        retention_summary={
            "lineage_preserved": True,
            "has_document_lineage": True,
            "has_purged_document_lineage": False,
            "retention_policy_code": "knowledge_runtime_default_retention",
            "purge_supported": False,
        },
    )


def _anchor_detail_record() -> KnowledgeAnchorDetailRecord:
    return KnowledgeAnchorDetailRecord(
        anchor_id="anchor-finance-2026",
        source_id="KNW-FINANCE-2026",
        source_family_id="KNW-FINANCE-FAMILY",
        source_version_id="123e4567-e89b-12d3-a456-426614174100",
        source_title="Finance Act governed source",
        source_type="tax_law",
        tax_domain="income_tax",
        authority_level="statute",
        publication_state="published",
        anchor_title="Finance anchor",
        anchor_path="finance/anchor",
        temporal_scope_from="2026-01-01",
        temporal_scope_to=None,
        chunk_count=1,
        chunks=(
            KnowledgeChunkSummaryRecord(
                chunk_id="123e4567-e89b-12d3-a456-426614174102",
                chunk_index=0,
                has_embedding=True,
            ),
        ),
    )


def _timeline_record(
    *,
    source_version_id: str,
    anchor_id: str,
    effective_from: str,
    effective_to: str | None,
    publication_state: str,
    timeline_position: int,
) -> KnowledgeTimelineRecord:
    return KnowledgeTimelineRecord(
        source_id="KNW-FINANCE-2026",
        source_version_id=source_version_id,
        anchor_id=anchor_id,
        title="Finance Act governed source",
        source_type="tax_law",
        authority_level="statute",
        tax_domain="income_tax",
        effective_from=effective_from,
        effective_to=effective_to,
        publication_state=publication_state,
        timeline_position=timeline_position,
        content=f"Timeline content for {anchor_id}",
    )


def _bulk_item(item_id: str, outcome: str) -> KnowledgeBulkOperationItemRecord:
    return KnowledgeBulkOperationItemRecord(
        id=item_id,
        status="ok",
        outcome=outcome,
        error_code=None,
        reason=None,
    )


def _bulk_ingestion_item(
    *,
    index: int,
    idempotency_key: str,
) -> KnowledgeBulkIngestionItemRecord:
    return KnowledgeBulkIngestionItemRecord(
        index=index,
        idempotency_key=idempotency_key,
        status="ok",
        outcome="accepted",
        ingestion_job_id=f"job-{idempotency_key}",
        error_code=None,
        reason=None,
    )


def _json(response: Any) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


def _stable_headers(seed: str) -> dict[str, str]:
    return {
        "X-Correlation-ID": f"{seed}-corr",
        "X-Trace-ID": f"{seed}-trace",
    }


def _admin_headers() -> dict[str, str]:
    return {
        "X-Auth-Context": json.dumps(
            {
                "schema_version": "1.0.0",
                "user_id": str(uuid4()),
                "tenant_id": "default_tenant",
                "role": "Administrator",
                "session_id": str(UUID("11111111-2222-3333-4444-555555555555")),
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
