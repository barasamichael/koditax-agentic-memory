"""OpenAPI contract checks for knowledge runtime boundary."""

from __future__ import annotations

from typing import cast
from pathlib import Path
from datetime import date

import yaml

from services.knowledge.app.main import create_app
from services.knowledge.app.repository import KnowledgeSearchRecord
from services.knowledge.app.repository import KnowledgeTimelineRecord
from services.knowledge.app.repository import KnowledgeIngestionRecord
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

CONTRACT_PATH = Path("contracts/openapi/knowledge.yaml")
REQUIRED_PATHS = {
    "/healthz",
    "/readyz",
    "/knowledge/search",
    "/knowledge/retrieve",
    "/knowledge/timeline/search",
    "/knowledge/ingestion/files",
    "/knowledge/ingestion/files/bulk",
    "/knowledge/ingestion/urls",
    "/knowledge/ingestion/urls/bulk",
    "/knowledge/ingestion",
    "/knowledge/ingestion/{ingestion_job_id}",
    "/knowledge/ingestion/bulk/reject",
    "/knowledge/ingestion/bulk/publish",
    "/knowledge/ingestion/{ingestion_job_id}/review",
    "/knowledge/ingestion/{ingestion_job_id}/approve",
    "/knowledge/ingestion/{ingestion_job_id}/reject",
    "/knowledge/ingestion/{ingestion_job_id}/publish",
    "/knowledge/source-versions",
    "/knowledge/source-versions/{source_version_id}",
    "/knowledge/sources",
    "/knowledge/sources/{source_id}",
    "/knowledge/anchors/{anchor_id}",
    "/knowledge/source-versions/bulk/archive",
    "/knowledge/source-versions/{source_version_id}/supersede",
    "/knowledge/source-versions/{source_version_id}/archive",
    "/knowledge/ingestion/{ingestion_job_id}/metadata-correction",
}
REQUIRED_SCHEMAS = {
    "ErrorEnvelope",
    "KnowledgeSearchRequest",
    "KnowledgeRetrieveRequest",
    "KnowledgeTimelineSearchRequest",
    "KnowledgeSearchResponse",
    "KnowledgeTimelineResponse",
    "KnowledgeTimelineResult",
    "KnowledgeTimelineItem",
    "KnowledgeFileIngestionRequest",
    "KnowledgeUrlIngestionRequest",
    "KnowledgeIngestionResponse",
    "KnowledgeIngestionResult",
    "KnowledgeBulkIngestionFileRequest",
    "KnowledgeBulkIngestionUrlRequest",
    "KnowledgeBulkIngestionFileItem",
    "KnowledgeBulkIngestionUrlItem",
    "KnowledgeBulkIngestionItem",
    "KnowledgeBulkIngestionResult",
    "KnowledgeBulkIngestionResponse",
    "KnowledgeIngestionDetailResponse",
    "KnowledgeIngestionDetailResult",
    "KnowledgeIngestionSummaryItem",
    "KnowledgeIngestionManagementListResult",
    "KnowledgeIngestionManagementListResponse",
    "KnowledgeBulkActionItem",
    "KnowledgeBulkActionResult",
    "KnowledgeBulkActionResponse",
    "KnowledgeIngestionReviewRequest",
    "KnowledgeIngestionApproveRequest",
    "KnowledgeIngestionRejectRequest",
    "KnowledgeIngestionPublishRequest",
    "KnowledgeIngestionBulkRejectRequest",
    "KnowledgeIngestionBulkPublishRequest",
    "KnowledgeSourceVersionSupersedeRequest",
    "KnowledgeSourceVersionArchiveRequest",
    "KnowledgeSourceVersionBulkArchiveRequest",
    "KnowledgeSourceVersionLifecycleResponse",
    "KnowledgeSourceVersionLifecycleResult",
    "KnowledgeSourceVersionSummaryItem",
    "KnowledgeSourceVersionManagementListResult",
    "KnowledgeSourceVersionManagementListResponse",
    "KnowledgeSourceSummaryItem",
    "KnowledgeSourceManagementListResult",
    "KnowledgeSourceManagementListResponse",
    "KnowledgeSourceDetailResult",
    "KnowledgeSourceDetailResponse",
    "KnowledgeAnchorChunkSummary",
    "KnowledgeAnchorDetailResult",
    "KnowledgeAnchorDetailResponse",
    "KnowledgeMetadataCorrectionRequest",
}


class _StubKnowledgeRepository:
    def search_records(
        self,
        *,
        query: str,
        source_type: str | None,
        tax_domain: str | None,
        effective_date: date | None,
    ) -> tuple[KnowledgeSearchRecord, ...]:
        _ = (query, source_type, tax_domain, effective_date)
        return ()

    def retrieve_records(
        self,
        *,
        source_ids: tuple[str, ...],
        anchor_ids: tuple[str, ...],
    ) -> tuple[KnowledgeSearchRecord, ...]:
        _ = (source_ids, anchor_ids)
        return ()

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
        return (_timeline_record(),)

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
            requested_by="123e4567-e89b-12d3-a456-426614174000",
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
        _ = (requested_by, idempotency_key, url, source_input_origin, source_class)
        return KnowledgeIngestionRecord(
            ingestion_job_id="job-url-001",
            document_id="doc-url-001",
            requested_by="123e4567-e89b-12d3-a456-426614174000",
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
        return (_ingestion_summary_record(),)

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
        return (_source_version_summary_record(),)

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
        return (_source_summary_record(),)

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


def test_knowledge_openapi_contract_parses_and_declares_required_paths() -> None:
    document = _load_contract()
    assert document.get("openapi") == "3.1.0"
    paths = _paths(document)
    missing = sorted(REQUIRED_PATHS - set(paths))
    assert not missing
    assert "post" in paths["/knowledge/search"]
    assert "post" in paths["/knowledge/retrieve"]
    assert "post" in paths["/knowledge/timeline/search"]
    assert "post" in paths["/knowledge/ingestion/files"]
    assert "post" in paths["/knowledge/ingestion/files/bulk"]
    assert "post" in paths["/knowledge/ingestion/urls"]
    assert "post" in paths["/knowledge/ingestion/urls/bulk"]
    assert "get" in paths["/knowledge/ingestion/{ingestion_job_id}"]
    assert "post" in paths["/knowledge/ingestion/bulk/reject"]
    assert "post" in paths["/knowledge/ingestion/bulk/publish"]
    assert "post" in paths["/knowledge/ingestion/{ingestion_job_id}/review"]
    assert "post" in paths["/knowledge/ingestion/{ingestion_job_id}/approve"]
    assert "post" in paths["/knowledge/ingestion/{ingestion_job_id}/reject"]
    assert "post" in paths["/knowledge/ingestion/{ingestion_job_id}/publish"]
    assert "post" in paths["/knowledge/ingestion/{ingestion_job_id}/metadata-correction"]
    assert "post" in paths["/knowledge/source-versions/bulk/archive"]
    assert "get" in paths["/knowledge/sources"]
    assert "get" in paths["/knowledge/sources/{source_id}"]
    assert "get" in paths["/knowledge/anchors/{anchor_id}"]
    assert "post" in paths["/knowledge/source-versions/{source_version_id}/supersede"]
    assert "post" in paths["/knowledge/source-versions/{source_version_id}/archive"]


def test_knowledge_openapi_contract_contains_canonical_error_envelope_fields() -> None:
    schemas = _schemas(_load_contract())
    error_schema = cast(dict[str, object], schemas["ErrorEnvelope"])
    required_list = cast(list[object], error_schema["required"])
    required = {str(item) for item in required_list}
    expected_fields = {
        "error_code",
        "message",
        "reason",
        "reason_code",
        "correlation_id",
        "trace_id",
    }
    assert expected_fields.issubset(required)


def test_knowledge_openapi_contract_contains_required_schemas() -> None:
    schemas = _schemas(_load_contract())
    missing = sorted(REQUIRED_SCHEMAS - set(schemas))
    assert not missing


def test_knowledge_runtime_routes_match_required_openapi_surface() -> None:
    runtime_routes = _runtime_route_methods()
    assert "/healthz" in runtime_routes
    assert "get" in runtime_routes["/healthz"]
    assert "/readyz" in runtime_routes
    assert "get" in runtime_routes["/readyz"]
    assert "/knowledge/search" in runtime_routes
    assert "post" in runtime_routes["/knowledge/search"]
    assert "/knowledge/retrieve" in runtime_routes
    assert "post" in runtime_routes["/knowledge/retrieve"]
    assert "/knowledge/timeline/search" in runtime_routes
    assert "post" in runtime_routes["/knowledge/timeline/search"]
    assert "/knowledge/ingestion/files" in runtime_routes
    assert "post" in runtime_routes["/knowledge/ingestion/files"]
    assert "/knowledge/ingestion/files/bulk" in runtime_routes
    assert "post" in runtime_routes["/knowledge/ingestion/files/bulk"]
    assert "/knowledge/ingestion/urls" in runtime_routes
    assert "post" in runtime_routes["/knowledge/ingestion/urls"]
    assert "/knowledge/ingestion/urls/bulk" in runtime_routes
    assert "post" in runtime_routes["/knowledge/ingestion/urls/bulk"]
    assert "/knowledge/ingestion" in runtime_routes
    assert "get" in runtime_routes["/knowledge/ingestion"]
    assert "/knowledge/ingestion/{ingestion_job_id}" in runtime_routes
    assert "get" in runtime_routes["/knowledge/ingestion/{ingestion_job_id}"]
    assert "/knowledge/ingestion/bulk/reject" in runtime_routes
    assert "post" in runtime_routes["/knowledge/ingestion/bulk/reject"]
    assert "/knowledge/ingestion/bulk/publish" in runtime_routes
    assert "post" in runtime_routes["/knowledge/ingestion/bulk/publish"]
    assert "/knowledge/ingestion/{ingestion_job_id}/review" in runtime_routes
    assert "post" in runtime_routes["/knowledge/ingestion/{ingestion_job_id}/review"]
    assert "/knowledge/ingestion/{ingestion_job_id}/approve" in runtime_routes
    assert "post" in runtime_routes["/knowledge/ingestion/{ingestion_job_id}/approve"]
    assert "/knowledge/ingestion/{ingestion_job_id}/reject" in runtime_routes
    assert "post" in runtime_routes["/knowledge/ingestion/{ingestion_job_id}/reject"]
    assert "/knowledge/ingestion/{ingestion_job_id}/publish" in runtime_routes
    assert "post" in runtime_routes["/knowledge/ingestion/{ingestion_job_id}/publish"]
    assert "/knowledge/source-versions" in runtime_routes
    assert "get" in runtime_routes["/knowledge/source-versions"]
    assert "/knowledge/source-versions/{source_version_id}" in runtime_routes
    assert "get" in runtime_routes["/knowledge/source-versions/{source_version_id}"]
    assert "/knowledge/sources" in runtime_routes
    assert "get" in runtime_routes["/knowledge/sources"]
    assert "/knowledge/sources/{source_id}" in runtime_routes
    assert "get" in runtime_routes["/knowledge/sources/{source_id}"]
    assert "/knowledge/anchors/{anchor_id}" in runtime_routes
    assert "get" in runtime_routes["/knowledge/anchors/{anchor_id}"]
    assert "/knowledge/source-versions/bulk/archive" in runtime_routes
    assert "post" in runtime_routes["/knowledge/source-versions/bulk/archive"]
    assert "/knowledge/source-versions/{source_version_id}/supersede" in runtime_routes
    assert "post" in runtime_routes["/knowledge/source-versions/{source_version_id}/supersede"]
    assert "/knowledge/source-versions/{source_version_id}/archive" in runtime_routes
    assert "post" in runtime_routes["/knowledge/source-versions/{source_version_id}/archive"]
    assert "/knowledge/ingestion/{ingestion_job_id}/metadata-correction" in runtime_routes
    assert "post" in runtime_routes["/knowledge/ingestion/{ingestion_job_id}/metadata-correction"]


def _detail_record(state: str) -> KnowledgeIngestionDetailRecord:
    return KnowledgeIngestionDetailRecord(
        ingestion_job_id="job-file-001",
        document_id="doc-file-001",
        requested_by="123e4567-e89b-12d3-a456-426614174000",
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


def _ingestion_summary_record() -> KnowledgeIngestionSummaryRecord:
    return KnowledgeIngestionSummaryRecord(
        ingestion_job_id="job-file-001",
        document_id="doc-file-001",
        requested_by="123e4567-e89b-12d3-a456-426614174000",
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


def _source_version_summary_record() -> KnowledgeSourceVersionSummaryRecord:
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


def _source_summary_record() -> KnowledgeSourceSummaryRecord:
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
        versions=(_source_version_summary_record(),),
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


def _timeline_record() -> KnowledgeTimelineRecord:
    return KnowledgeTimelineRecord(
        source_id="KNW-FINANCE-2026",
        source_version_id="123e4567-e89b-12d3-a456-426614174100",
        anchor_id="anchor-finance-2026",
        title="Finance Act governed source",
        source_type="tax_law",
        authority_level="statute",
        tax_domain="income_tax",
        effective_from="2026-01-01",
        effective_to=None,
        publication_state="published",
        timeline_position=1,
        content="Timeline content",
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


def _load_contract() -> dict[str, object]:
    loaded = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return cast(dict[str, object], loaded)


def _paths(document: dict[str, object]) -> dict[str, dict[str, object]]:
    raw = document.get("paths")
    assert isinstance(raw, dict)
    return cast(dict[str, dict[str, object]], raw)


def _schemas(document: dict[str, object]) -> dict[str, object]:
    components = cast(dict[str, object], document.get("components", {}))
    raw = components.get("schemas")
    assert isinstance(raw, dict)
    return cast(dict[str, object], raw)


def _runtime_route_methods() -> dict[str, set[str]]:
    app = create_app(repository=_StubKnowledgeRepository())
    route_methods: dict[str, set[str]] = {}
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not isinstance(path, str) or not isinstance(methods, set):
            continue
        normalized = {str(method).lower() for method in cast(set[object], methods)}
        route_methods.setdefault(path, set()).update(normalized)
    return route_methods
