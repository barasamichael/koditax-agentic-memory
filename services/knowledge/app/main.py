"""Knowledge runtime boundary with deterministic search and retrieval endpoints."""

from uuid import UUID
from typing import Any
from typing import cast
from typing import Protocol
from pathlib import Path as PathlibPath
from datetime import date
from collections.abc import Mapping

from dotenv import load_dotenv
from fastapi import Body
from fastapi import Depends
from fastapi import FastAPI
from fastapi import Request
from fastapi import APIRouter
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.middleware.cors import CORSMiddleware

from shared.authz.rbac import Principal
from shared.authz.rbac import build_authorized_principal_dependency
from shared.tracing.correlation import get_trace_id
from shared.tracing.correlation import get_correlation_id
from shared.tracing.correlation import CorrelationIdMiddleware
from services.knowledge.app.config import KNOWLEDGE_SERVICE_NAME
from services.knowledge.app.config import KNOWLEDGE_SERVICE_VERSION
from services.knowledge.app.repository import KnowledgeSearchRecord
from services.knowledge.app.repository import KnowledgeTimelineRecord
from services.knowledge.app.repository import KnowledgeIngestionRecord
from services.knowledge.app.repository import KnowledgeRepositoryError
from services.knowledge.app.repository import KnowledgeAnchorDetailRecord
from services.knowledge.app.repository import KnowledgeSourceDetailRecord
from services.knowledge.app.repository import KnowledgeSourceSummaryRecord
from services.knowledge.app.repository import KnowledgeIngestionDetailRecord
from services.knowledge.app.repository import KnowledgeIngestionSummaryRecord
from services.knowledge.app.repository import get_default_knowledge_repository
from services.knowledge.app.repository import KnowledgeBulkIngestionItemRecord
from services.knowledge.app.repository import KnowledgeBulkOperationItemRecord
from services.knowledge.app.repository import KnowledgeSourceVersionSummaryRecord
from services.knowledge.app.repository import KnowledgeSourceVersionLifecycleRecord

load_dotenv(dotenv_path=PathlibPath(__file__).parent.parent.parent.parent / ".env")

INVALID_KNOWLEDGE_REQUEST = "invalid_knowledge_request"
UNSUPPORTED_KNOWLEDGE_SCOPE = "unsupported_knowledge_scope"
UNSUPPORTED_SOURCE_INPUT_ORIGIN = "unsupported_source_input_origin"
UNSUPPORTED_SOURCE_CLASS = "unsupported_source_class"
INVALID_KNOWLEDGE_LINEAGE = "invalid_knowledge_lineage"
KNOWLEDGE_IDEMPOTENCY_CONFLICT = "knowledge_idempotency_conflict"
INVALID_PUBLICATION_STATE_TRANSITION = "invalid_publication_state_transition"
INVALID_AUTHORITY_SOURCE_CLASS_BINDING = "invalid_authority_source_class_binding"
INVALID_EFFECTIVE_WINDOW_METADATA = "invalid_effective_window_metadata"
KNOWLEDGE_PUBLICATION_SAFETY_REJECTED = "knowledge_publication_safety_rejected"
KNOWLEDGE_SUPERSESSION_CONFLICT = "knowledge_supersession_conflict"
KNOWLEDGE_TEMPORAL_SCOPE_MISMATCH = "knowledge_temporal_scope_mismatch"
KNOWLEDGE_RECORD_NOT_PUBLISHED = "knowledge_record_not_published"
MAX_PUBLIC_QUERY_LENGTH = 512
MAX_RETRIEVE_IDENTIFIER_COUNT = 50
MAX_RETRIEVE_IDENTIFIER_LENGTH = 255
REQUEST_BODY_OPTIONAL = Body(None)
ROUTER = APIRouter()
require_internal_knowledge_principal = build_authorized_principal_dependency(
    allowed_roles=frozenset({"Administrator"}),
    allow_delegation=False,
)
INTERNAL_KNOWLEDGE_PRINCIPAL = Depends(require_internal_knowledge_principal)
PUBLIC_KNOWLEDGE_ROUTE_SPECS = frozenset(
    {
        ("GET", "/healthz"),
        ("GET", "/readyz"),
        ("DELETE", "/v1/knowledge/{scope}/{remaining_path:path}"),
        ("GET", "/v1/knowledge/{scope}/{remaining_path:path}"),
        ("PATCH", "/v1/knowledge/{scope}/{remaining_path:path}"),
        ("POST", "/v1/knowledge/{scope}/{remaining_path:path}"),
        ("PUT", "/v1/knowledge/{scope}/{remaining_path:path}"),
    }
)
PROTECTED_KNOWLEDGE_ROUTE_SPECS = frozenset(
    {
        ("GET", "/knowledge/anchors/{anchor_id}"),
        ("GET", "/knowledge/ingestion"),
        ("GET", "/knowledge/ingestion/{ingestion_job_id}"),
        ("POST", "/knowledge/search"),
        ("POST", "/knowledge/retrieve"),
        ("GET", "/knowledge/source-versions"),
        ("GET", "/knowledge/source-versions/{source_version_id}"),
        ("GET", "/knowledge/sources"),
        ("GET", "/knowledge/sources/{source_id}"),
        ("POST", "/knowledge/timeline/search"),
        ("POST", "/knowledge/ingestion/bulk/publish"),
        ("POST", "/knowledge/ingestion/bulk/reject"),
        ("POST", "/knowledge/ingestion/documents"),
        ("POST", "/knowledge/ingestion/documents/bulk"),
        ("POST", "/knowledge/ingestion/files"),
        ("POST", "/knowledge/ingestion/files/bulk"),
        ("POST", "/knowledge/ingestion/urls"),
        ("POST", "/knowledge/ingestion/urls/bulk"),
        ("POST", "/knowledge/ingestion/{ingestion_job_id}/approve"),
        ("POST", "/knowledge/ingestion/{ingestion_job_id}/metadata-correction"),
        ("POST", "/knowledge/ingestion/{ingestion_job_id}/publish"),
        ("POST", "/knowledge/ingestion/{ingestion_job_id}/reject"),
        ("POST", "/knowledge/ingestion/{ingestion_job_id}/review"),
        ("POST", "/knowledge/source-versions/bulk/archive"),
        ("POST", "/knowledge/source-versions/{source_version_id}/archive"),
        ("POST", "/knowledge/source-versions/{source_version_id}/supersede"),
    }
)


class KnowledgeRepositoryProtocol(Protocol):
    """Describe the governed repository operations required by the runtime."""

    def search_records(
        self,
        *,
        query: str,
        source_type: str | None,
        tax_domain: str | None,
        effective_date: date | None,
    ) -> tuple[KnowledgeSearchRecord, ...]: ...

    def retrieve_records(
        self,
        *,
        source_ids: tuple[str, ...],
        anchor_ids: tuple[str, ...],
    ) -> tuple[KnowledgeSearchRecord, ...]: ...

    def timeline_search_records(
        self,
        *,
        query: str,
        source_type: str | None,
        tax_domain: str,
        start_date: date,
        end_date: date,
    ) -> tuple[KnowledgeTimelineRecord, ...]: ...

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
        legacy_import_acknowledged: bool,
    ) -> KnowledgeIngestionRecord: ...

    def ingest_registered_document_source(
        self,
        *,
        requested_by: str,
        idempotency_key: str,
        document_id: str,
        storage_key: str,
        mime_type: str,
        payload_checksum_sha256: str,
        source_document_system: str,
        source_input_origin: str | None,
        source_class: str | None,
    ) -> KnowledgeIngestionRecord: ...

    def ingest_url_source(
        self,
        *,
        requested_by: str,
        idempotency_key: str,
        url: str,
        source_input_origin: str | None,
        source_class: str | None,
    ) -> KnowledgeIngestionRecord: ...

    def bulk_ingest_file_sources(
        self,
        *,
        requested_by: str,
        items: tuple[dict[str, object], ...],
    ) -> tuple[KnowledgeBulkIngestionItemRecord, ...]: ...

    def bulk_ingest_registered_document_sources(
        self,
        *,
        requested_by: str,
        items: tuple[dict[str, object], ...],
    ) -> tuple[KnowledgeBulkIngestionItemRecord, ...]: ...

    def bulk_ingest_url_sources(
        self,
        *,
        requested_by: str,
        items: tuple[dict[str, object], ...],
    ) -> tuple[KnowledgeBulkIngestionItemRecord, ...]: ...

    def get_ingestion_job(
        self,
        *,
        ingestion_job_id: str,
    ) -> KnowledgeIngestionDetailRecord: ...

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
    ) -> tuple[KnowledgeIngestionSummaryRecord, ...]: ...

    def review_ingestion_job(
        self,
        *,
        ingestion_job_id: str,
        reviewed_by: str,
        review_notes: tuple[dict[str, object], ...],
        proposed_source_updates: dict[str, object] | None,
    ) -> KnowledgeIngestionDetailRecord: ...

    def approve_ingestion_job(
        self,
        *,
        ingestion_job_id: str,
        reviewed_by: str,
        publication_payload: dict[str, object],
        review_notes: tuple[dict[str, object], ...],
    ) -> KnowledgeIngestionDetailRecord: ...

    def reject_ingestion_job(
        self,
        *,
        ingestion_job_id: str,
        reviewed_by: str,
        review_notes: tuple[dict[str, object], ...],
    ) -> KnowledgeIngestionDetailRecord: ...

    def publish_ingestion_job(
        self,
        *,
        ingestion_job_id: str,
        published_by: str,
    ) -> KnowledgeIngestionDetailRecord: ...

    def bulk_reject_ingestion_jobs(
        self,
        *,
        reviewed_by: str,
        ingestion_job_ids: tuple[str, ...],
        review_notes: tuple[dict[str, object], ...],
    ) -> tuple[KnowledgeBulkOperationItemRecord, ...]: ...

    def bulk_publish_ingestion_jobs(
        self,
        *,
        published_by: str,
        ingestion_job_ids: tuple[str, ...],
    ) -> tuple[KnowledgeBulkOperationItemRecord, ...]: ...

    def supersede_source_version(
        self,
        *,
        source_version_id: str,
        successor_source_version_id: str,
        superseded_by: str,
    ) -> KnowledgeSourceVersionLifecycleRecord: ...

    def archive_source_version(
        self,
        *,
        source_version_id: str,
        archived_by: str,
    ) -> KnowledgeSourceVersionLifecycleRecord: ...

    def bulk_archive_source_versions(
        self,
        *,
        archived_by: str,
        source_version_ids: tuple[str, ...],
    ) -> tuple[KnowledgeBulkOperationItemRecord, ...]: ...

    def get_source_version_lifecycle(
        self,
        *,
        source_version_id: str,
    ) -> KnowledgeSourceVersionLifecycleRecord: ...

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
    ) -> tuple[KnowledgeSourceVersionSummaryRecord, ...]: ...

    def list_sources(
        self,
        *,
        source_class: str | None,
        tax_domain: str | None,
        limit: int,
        offset: int,
        sort_by: str | None,
        sort_order: str | None,
    ) -> tuple[KnowledgeSourceSummaryRecord, ...]: ...

    def get_source(
        self,
        *,
        source_id: str,
    ) -> KnowledgeSourceDetailRecord: ...

    def get_anchor(
        self,
        *,
        anchor_id: str,
    ) -> KnowledgeAnchorDetailRecord: ...

    def correct_ingestion_metadata(
        self,
        *,
        ingestion_job_id: str,
        corrected_by: str,
        review_notes: tuple[dict[str, object], ...],
        publication_payload_updates: dict[str, object],
    ) -> KnowledgeIngestionDetailRecord: ...


def create_app(*, repository: KnowledgeRepositoryProtocol | None = None) -> FastAPI:
    """Build deterministic knowledge FastAPI app."""

    app = FastAPI(title=KNOWLEDGE_SERVICE_NAME, version=KNOWLEDGE_SERVICE_VERSION)
    app.state.knowledge_repository = (
        get_default_knowledge_repository() if repository is None else repository
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5174",
            "http://localhost:5174",
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )
    app.add_middleware(CorrelationIdMiddleware)
    app.add_exception_handler(
        RequestValidationError,
        cast(Any, _handle_request_validation_error),
    )
    app.add_exception_handler(HTTPException, cast(Any, _handle_http_exception_error))
    app.add_exception_handler(
        StarletteHTTPException,
        cast(Any, _handle_starlette_http_exception_error),
    )
    app.add_exception_handler(
        KnowledgeRepositoryError,
        cast(Any, _handle_repository_error),
    )
    app.include_router(ROUTER)
    _verify_knowledge_route_boundary(app)
    return app


@ROUTER.get("/healthz")
def knowledge_healthz(request: Request) -> dict[str, str]:
    """Expose deterministic knowledge health endpoint."""

    return {
        "status": "ok",
        "service": KNOWLEDGE_SERVICE_NAME,
        "version": KNOWLEDGE_SERVICE_VERSION,
        "correlation_id": get_correlation_id(request),
        "trace_id": get_trace_id(request),
    }


@ROUTER.get("/readyz")
def knowledge_readyz(request: Request) -> dict[str, str]:
    """Expose deterministic knowledge readiness endpoint."""

    return {
        "status": "ready",
        "service": KNOWLEDGE_SERVICE_NAME,
        "version": KNOWLEDGE_SERVICE_VERSION,
        "correlation_id": get_correlation_id(request),
        "trace_id": get_trace_id(request),
    }


@ROUTER.post("/knowledge/search")
def search_knowledge(
    request: Request,
    payload: Any = REQUEST_BODY_OPTIONAL,
    principal: Principal = INTERNAL_KNOWLEDGE_PRINCIPAL,
) -> dict[str, object]:
    """Search deterministic knowledge catalog with stable ranking and filters."""

    _ = principal
    source = _as_object(payload)
    query = _public_query_text(source, "query")
    source_type = _optional_string(source.get("source_type"))
    tax_domain = _optional_string(source.get("tax_domain"))
    effective_date_value = _optional_string(source.get("effective_date"))
    effective_date = _parse_iso_date(effective_date_value) if effective_date_value else None

    repository = _get_repository(request)
    try:
        response_items = [
            record.to_public_payload()
            for record in repository.search_records(
                query=query,
                source_type=source_type,
                tax_domain=tax_domain,
                effective_date=effective_date,
            )
        ]
    except KnowledgeRepositoryError as error:
        raise _repository_http_error(request, error) from error
    return {
        "status": "ok",
        "service": KNOWLEDGE_SERVICE_NAME,
        "correlation_id": get_correlation_id(request),
        "trace_id": get_trace_id(request),
        "result": {
            "total": len(response_items),
            "items": response_items,
        },
    }


@ROUTER.post("/knowledge/retrieve")
def retrieve_knowledge(
    request: Request,
    payload: Any = REQUEST_BODY_OPTIONAL,
    principal: Principal = INTERNAL_KNOWLEDGE_PRINCIPAL,
) -> dict[str, object]:
    """Retrieve deterministic knowledge records by source IDs or anchors."""

    _ = principal
    source = _as_object(payload)
    source_ids = tuple(
        _string_list(
            source,
            "source_ids",
            max_items=MAX_RETRIEVE_IDENTIFIER_COUNT,
            max_item_length=MAX_RETRIEVE_IDENTIFIER_LENGTH,
        )
    )
    anchor_ids = tuple(
        _string_list(
            source,
            "anchor_ids",
            max_items=MAX_RETRIEVE_IDENTIFIER_COUNT,
            max_item_length=MAX_RETRIEVE_IDENTIFIER_LENGTH,
        )
    )
    repository = _get_repository(request)
    try:
        items = repository.retrieve_records(source_ids=source_ids, anchor_ids=anchor_ids)
    except KnowledgeRepositoryError as error:
        raise _repository_http_error(request, error) from error
    return {
        "status": "ok",
        "service": KNOWLEDGE_SERVICE_NAME,
        "correlation_id": get_correlation_id(request),
        "trace_id": get_trace_id(request),
        "result": {
            "total": len(items),
            "items": [record.to_public_payload() for record in items],
        },
    }


@ROUTER.post("/knowledge/timeline/search")
def timeline_search_knowledge(
    request: Request,
    payload: Any = REQUEST_BODY_OPTIONAL,
    principal: Principal = INTERNAL_KNOWLEDGE_PRINCIPAL,
) -> dict[str, object]:
    """Retrieve deterministic knowledge records across governed effective windows."""

    _ = principal
    source = _as_object(payload)
    query = _public_query_text(source, "query")
    tax_domain = _required_string(source, "tax_domain")
    source_type = _optional_string(source.get("source_type"))
    start_date = _parse_iso_date(_required_string(source, "start_date"), field_name="start_date")
    end_date = _parse_iso_date(_required_string(source, "end_date"), field_name="end_date")
    if end_date < start_date:
        raise _http_error(
            request=request,
            status_code=400,
            error_code=INVALID_KNOWLEDGE_REQUEST,
            message="Knowledge timeline date range is invalid.",
            reason=INVALID_KNOWLEDGE_REQUEST,
            reason_code=INVALID_KNOWLEDGE_REQUEST,
        )

    repository = _get_repository(request)
    try:
        items = repository.timeline_search_records(
            query=query,
            source_type=source_type,
            tax_domain=tax_domain,
            start_date=start_date,
            end_date=end_date,
        )
    except KnowledgeRepositoryError as error:
        raise _repository_http_error(request, error) from error
    return _collection_success_envelope(
        request=request,
        items=[record.to_public_payload() for record in items],
    )


@ROUTER.post("/knowledge/ingestion/files")
def ingest_knowledge_file(
    request: Request,
    payload: Any = REQUEST_BODY_OPTIONAL,
    principal: Principal = INTERNAL_KNOWLEDGE_PRINCIPAL,
) -> dict[str, object]:
    """Persist one governed official-source file ingestion job."""

    _ = principal
    source = _as_object(payload)
    repository = _get_repository(request)
    try:
        record = repository.ingest_file_source(
            requested_by=_required_uuid_string(source, "requested_by"),
            idempotency_key=_required_string(source, "idempotency_key"),
            filename=_required_string(source, "filename"),
            mime_type=_required_string(source, "mime_type"),
            file_content_base64=_required_string(source, "file_content_base64"),
            source_input_origin=_optional_string(source.get("source_input_origin")),
            source_class=_optional_string(source.get("source_class")),
            legacy_import_acknowledged=_required_true_boolean(
                source,
                "legacy_import_acknowledged",
            ),
        )
    except KnowledgeRepositoryError as error:
        raise _repository_http_error(request, error) from error
    return _ingestion_success_envelope(request=request, record=record)


@ROUTER.post("/knowledge/ingestion/documents")
def ingest_knowledge_registered_document(
    request: Request,
    payload: Any = REQUEST_BODY_OPTIONAL,
    principal: Principal = INTERNAL_KNOWLEDGE_PRINCIPAL,
) -> dict[str, object]:
    """Persist one governed official-source document handoff ingestion job."""

    _ = principal
    source = _as_object(payload)
    repository = _get_repository(request)
    try:
        record = repository.ingest_registered_document_source(
            requested_by=_required_uuid_string(source, "requested_by"),
            idempotency_key=_required_string(source, "idempotency_key"),
            document_id=_required_uuid_string(source, "document_id"),
            storage_key=_required_string(source, "storage_key"),
            mime_type=_required_string(source, "mime_type"),
            payload_checksum_sha256=_required_string(source, "payload_checksum_sha256"),
            source_document_system=_required_string(source, "source_document_system"),
            source_input_origin=_optional_string(source.get("source_input_origin")),
            source_class=_optional_string(source.get("source_class")),
        )
    except KnowledgeRepositoryError as error:
        raise _repository_http_error(request, error) from error
    return _ingestion_success_envelope(request=request, record=record)


@ROUTER.post("/knowledge/ingestion/urls")
def ingest_knowledge_url(
    request: Request,
    payload: Any = REQUEST_BODY_OPTIONAL,
    principal: Principal = INTERNAL_KNOWLEDGE_PRINCIPAL,
) -> dict[str, object]:
    """Persist one governed official-source URL ingestion job."""

    _ = principal
    source = _as_object(payload)
    repository = _get_repository(request)
    try:
        record = repository.ingest_url_source(
            requested_by=_required_uuid_string(source, "requested_by"),
            idempotency_key=_required_string(source, "idempotency_key"),
            url=_required_string(source, "url"),
            source_input_origin=_optional_string(source.get("source_input_origin")),
            source_class=_optional_string(source.get("source_class")),
        )
    except KnowledgeRepositoryError as error:
        raise _repository_http_error(request, error) from error
    return _ingestion_success_envelope(request=request, record=record)


@ROUTER.post("/knowledge/ingestion/files/bulk")
def bulk_ingest_knowledge_files(
    request: Request,
    payload: Any = REQUEST_BODY_OPTIONAL,
    principal: Principal = INTERNAL_KNOWLEDGE_PRINCIPAL,
) -> dict[str, object]:
    """Persist multiple governed official-source file ingestion jobs deterministically."""

    _ = principal
    source = _as_object(payload)
    repository = _get_repository(request)
    try:
        records = repository.bulk_ingest_file_sources(
            requested_by=_required_uuid_string(source, "acting_user"),
            items=_required_non_empty_object_list(source, "items"),
        )
    except KnowledgeRepositoryError as error:
        raise _repository_http_error(request, error) from error
    return _bulk_ingestion_success_envelope(request=request, items=records)


@ROUTER.post("/knowledge/ingestion/documents/bulk")
def bulk_ingest_knowledge_registered_documents(
    request: Request,
    payload: Any = REQUEST_BODY_OPTIONAL,
    principal: Principal = INTERNAL_KNOWLEDGE_PRINCIPAL,
) -> dict[str, object]:
    """Persist multiple governed official-source document handoff ingestion jobs."""

    _ = principal
    source = _as_object(payload)
    repository = _get_repository(request)
    try:
        records = repository.bulk_ingest_registered_document_sources(
            requested_by=_required_uuid_string(source, "acting_user"),
            items=_required_non_empty_object_list(source, "items"),
        )
    except KnowledgeRepositoryError as error:
        raise _repository_http_error(request, error) from error
    return _bulk_ingestion_success_envelope(request=request, items=records)


@ROUTER.post("/knowledge/ingestion/urls/bulk")
def bulk_ingest_knowledge_urls(
    request: Request,
    payload: Any = REQUEST_BODY_OPTIONAL,
    principal: Principal = INTERNAL_KNOWLEDGE_PRINCIPAL,
) -> dict[str, object]:
    """Persist multiple governed official-source URL ingestion jobs deterministically."""

    _ = principal
    source = _as_object(payload)
    repository = _get_repository(request)
    try:
        records = repository.bulk_ingest_url_sources(
            requested_by=_required_uuid_string(source, "acting_user"),
            items=_required_non_empty_object_list(source, "items"),
        )
    except KnowledgeRepositoryError as error:
        raise _repository_http_error(request, error) from error
    return _bulk_ingestion_success_envelope(request=request, items=records)


@ROUTER.get("/knowledge/ingestion/{ingestion_job_id}")
def get_knowledge_ingestion_job(
    request: Request,
    ingestion_job_id: str,
    principal: Principal = INTERNAL_KNOWLEDGE_PRINCIPAL,
) -> dict[str, object]:
    """Fetch one deterministic governed ingestion job for review."""

    _ = principal
    repository = _get_repository(request)
    try:
        record = repository.get_ingestion_job(ingestion_job_id=ingestion_job_id)
    except KnowledgeRepositoryError as error:
        raise _repository_http_error(request, error) from error
    return _ingestion_detail_success_envelope(request=request, record=record)


@ROUTER.get("/knowledge/ingestion")
def list_knowledge_ingestion_jobs(
    request: Request,
    ingestion_state: str | None = None,
    source_input_origin: str | None = None,
    source_class: str | None = None,
    requested_by: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
    principal: Principal = INTERNAL_KNOWLEDGE_PRINCIPAL,
) -> dict[str, object]:
    """List governed ingestion jobs for deterministic management visibility."""

    _ = principal
    repository = _get_repository(request)
    try:
        records = repository.list_ingestion_jobs(
            ingestion_state=_optional_string(ingestion_state),
            source_input_origin=_optional_string(source_input_origin),
            source_class=_optional_string(source_class),
            requested_by=_optional_uuid_string(requested_by),
            limit=_management_limit(limit),
            offset=_management_offset(offset),
            sort_by=_optional_string(sort_by),
            sort_order=_optional_string(sort_order),
        )
    except KnowledgeRepositoryError as error:
        raise _repository_http_error(request, error) from error
    return _collection_success_envelope(
        request=request,
        items=[record.to_public_payload() for record in records],
        limit=_management_limit(limit),
        offset=_management_offset(offset),
        sort_by=_optional_string(sort_by) or "created_at",
        sort_order=_optional_string(sort_order) or "desc",
    )


@ROUTER.post("/knowledge/ingestion/{ingestion_job_id}/metadata-correction")
def correct_knowledge_ingestion_metadata(
    request: Request,
    ingestion_job_id: str,
    payload: Any = REQUEST_BODY_OPTIONAL,
    principal: Principal = INTERNAL_KNOWLEDGE_PRINCIPAL,
) -> dict[str, object]:
    """Apply one narrow pre-publication metadata correction for editable unpublished material."""

    _ = principal
    source = _as_object(payload)
    repository = _get_repository(request)
    try:
        record = repository.correct_ingestion_metadata(
            ingestion_job_id=ingestion_job_id,
            corrected_by=_required_uuid_string(source, "corrected_by"),
            review_notes=_required_object_list(source, "review_notes"),
            publication_payload_updates=_required_object(source, "publication_payload_updates"),
        )
    except KnowledgeRepositoryError as error:
        raise _repository_http_error(request, error) from error
    return _ingestion_detail_success_envelope(request=request, record=record)


@ROUTER.post("/knowledge/ingestion/bulk/reject")
def bulk_reject_knowledge_ingestion_jobs(
    request: Request,
    payload: Any = REQUEST_BODY_OPTIONAL,
    principal: Principal = INTERNAL_KNOWLEDGE_PRINCIPAL,
) -> dict[str, object]:
    """Reject multiple reviewable ingestion jobs with deterministic outcomes."""

    _ = principal
    source = _as_object(payload)
    repository = _get_repository(request)
    try:
        records = repository.bulk_reject_ingestion_jobs(
            reviewed_by=_required_uuid_string(source, "acting_user"),
            ingestion_job_ids=_required_non_empty_string_list(source, "ids"),
            review_notes=_required_object_list(source, "review_notes"),
        )
    except KnowledgeRepositoryError as error:
        raise _repository_http_error(request, error) from error
    return _bulk_success_envelope(request=request, items=records)


@ROUTER.post("/knowledge/ingestion/bulk/publish")
def bulk_publish_knowledge_ingestion_jobs(
    request: Request,
    payload: Any = REQUEST_BODY_OPTIONAL,
    principal: Principal = INTERNAL_KNOWLEDGE_PRINCIPAL,
) -> dict[str, object]:
    """Publish multiple approved ingestion jobs with deterministic outcomes."""

    _ = principal
    source = _as_object(payload)
    repository = _get_repository(request)
    try:
        records = repository.bulk_publish_ingestion_jobs(
            published_by=_required_uuid_string(source, "acting_user"),
            ingestion_job_ids=_required_non_empty_string_list(source, "ids"),
        )
    except KnowledgeRepositoryError as error:
        raise _repository_http_error(request, error) from error
    return _bulk_success_envelope(request=request, items=records)


@ROUTER.post("/knowledge/ingestion/{ingestion_job_id}/review")
def review_knowledge_ingestion_job(
    request: Request,
    ingestion_job_id: str,
    payload: Any = REQUEST_BODY_OPTIONAL,
    principal: Principal = INTERNAL_KNOWLEDGE_PRINCIPAL,
) -> dict[str, object]:
    """Persist review notes and proposed metadata updates for one ingestion job."""

    _ = principal
    source = _as_object(payload)
    repository = _get_repository(request)
    try:
        record = repository.review_ingestion_job(
            ingestion_job_id=ingestion_job_id,
            reviewed_by=_required_uuid_string(source, "reviewed_by"),
            review_notes=_required_object_list(source, "review_notes"),
            proposed_source_updates=_optional_object(source.get("proposed_source_updates")),
        )
    except KnowledgeRepositoryError as error:
        raise _repository_http_error(request, error) from error
    return _ingestion_detail_success_envelope(request=request, record=record)


@ROUTER.post("/knowledge/ingestion/{ingestion_job_id}/approve")
def approve_knowledge_ingestion_job(
    request: Request,
    ingestion_job_id: str,
    payload: Any = REQUEST_BODY_OPTIONAL,
    principal: Principal = INTERNAL_KNOWLEDGE_PRINCIPAL,
) -> dict[str, object]:
    """Approve one ingestion job for governed publication."""

    _ = principal
    source = _as_object(payload)
    repository = _get_repository(request)
    try:
        record = repository.approve_ingestion_job(
            ingestion_job_id=ingestion_job_id,
            reviewed_by=_required_uuid_string(source, "reviewed_by"),
            publication_payload=_required_object(source, "publication_payload"),
            review_notes=_required_object_list(source, "review_notes"),
        )
    except KnowledgeRepositoryError as error:
        raise _repository_http_error(request, error) from error
    return _ingestion_detail_success_envelope(request=request, record=record)


@ROUTER.post("/knowledge/ingestion/{ingestion_job_id}/reject")
def reject_knowledge_ingestion_job(
    request: Request,
    ingestion_job_id: str,
    payload: Any = REQUEST_BODY_OPTIONAL,
    principal: Principal = INTERNAL_KNOWLEDGE_PRINCIPAL,
) -> dict[str, object]:
    """Reject one ingestion job and keep it fail-closed."""

    _ = principal
    source = _as_object(payload)
    repository = _get_repository(request)
    try:
        record = repository.reject_ingestion_job(
            ingestion_job_id=ingestion_job_id,
            reviewed_by=_required_uuid_string(source, "reviewed_by"),
            review_notes=_required_object_list(source, "review_notes"),
        )
    except KnowledgeRepositoryError as error:
        raise _repository_http_error(request, error) from error
    return _ingestion_detail_success_envelope(request=request, record=record)


@ROUTER.post("/knowledge/ingestion/{ingestion_job_id}/publish")
def publish_knowledge_ingestion_job(
    request: Request,
    ingestion_job_id: str,
    payload: Any = REQUEST_BODY_OPTIONAL,
    principal: Principal = INTERNAL_KNOWLEDGE_PRINCIPAL,
) -> dict[str, object]:
    """Publish one approved ingestion job into searchable governed records."""

    _ = principal
    source = _as_object(payload)
    repository = _get_repository(request)
    try:
        record = repository.publish_ingestion_job(
            ingestion_job_id=ingestion_job_id,
            published_by=_required_uuid_string(source, "published_by"),
        )
    except KnowledgeRepositoryError as error:
        raise _repository_http_error(request, error) from error
    return _ingestion_detail_success_envelope(request=request, record=record)


@ROUTER.post("/knowledge/source-versions/{source_version_id}/supersede")
def supersede_knowledge_source_version(
    request: Request,
    source_version_id: str,
    payload: Any = REQUEST_BODY_OPTIONAL,
    principal: Principal = INTERNAL_KNOWLEDGE_PRINCIPAL,
) -> dict[str, object]:
    """Supersede one published source version with a governed successor."""

    _ = principal
    source = _as_object(payload)
    repository = _get_repository(request)
    try:
        record = repository.supersede_source_version(
            source_version_id=source_version_id,
            successor_source_version_id=_required_string(
                source,
                "successor_source_version_id",
            ),
            superseded_by=_required_uuid_string(source, "superseded_by"),
        )
    except KnowledgeRepositoryError as error:
        raise _repository_http_error(request, error) from error
    return _source_version_lifecycle_success_envelope(request=request, record=record)


@ROUTER.get("/knowledge/source-versions")
def list_knowledge_source_versions(
    request: Request,
    publication_state: str | None = None,
    source_id: str | None = None,
    source_family_id: str | None = None,
    tax_domain: str | None = None,
    source_class: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
    principal: Principal = INTERNAL_KNOWLEDGE_PRINCIPAL,
) -> dict[str, object]:
    """List governed source versions for deterministic management visibility."""

    _ = principal
    repository = _get_repository(request)
    try:
        records = repository.list_source_versions(
            publication_state=_optional_string(publication_state),
            source_id=_optional_identifier_string(source_id),
            source_family_id=_optional_identifier_string(source_family_id),
            tax_domain=_optional_string(tax_domain),
            source_class=_optional_string(source_class),
            limit=_management_limit(limit),
            offset=_management_offset(offset),
            sort_by=_optional_string(sort_by),
            sort_order=_optional_string(sort_order),
        )
    except KnowledgeRepositoryError as error:
        raise _repository_http_error(request, error) from error
    return _collection_success_envelope(
        request=request,
        items=[record.to_public_payload() for record in records],
        limit=_management_limit(limit),
        offset=_management_offset(offset),
        sort_by=_optional_string(sort_by) or "source_family_id",
        sort_order=_optional_string(sort_order) or "asc",
    )


@ROUTER.get("/knowledge/sources")
def list_knowledge_sources(
    request: Request,
    source_class: str | None = None,
    tax_domain: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
    principal: Principal = INTERNAL_KNOWLEDGE_PRINCIPAL,
) -> dict[str, object]:
    """List governed sources for deterministic management visibility."""

    _ = principal
    repository = _get_repository(request)
    try:
        records = repository.list_sources(
            source_class=_optional_string(source_class),
            tax_domain=_optional_string(tax_domain),
            limit=_management_limit(limit),
            offset=_management_offset(offset),
            sort_by=_optional_string(sort_by),
            sort_order=_optional_string(sort_order),
        )
    except KnowledgeRepositoryError as error:
        raise _repository_http_error(request, error) from error
    return _collection_success_envelope(
        request=request,
        items=[record.to_public_payload() for record in records],
        limit=_management_limit(limit),
        offset=_management_offset(offset),
        sort_by=_optional_string(sort_by) or "source_family_id",
        sort_order=_optional_string(sort_order) or "asc",
    )


@ROUTER.get("/knowledge/sources/{source_id}")
def get_knowledge_source(
    request: Request,
    source_id: str,
    principal: Principal = INTERNAL_KNOWLEDGE_PRINCIPAL,
) -> dict[str, object]:
    """Fetch one governed source detail record for management visibility."""

    _ = principal
    repository = _get_repository(request)
    try:
        record = repository.get_source(source_id=source_id)
    except KnowledgeRepositoryError as error:
        raise _repository_http_error(request, error) from error
    return _single_success_envelope(request=request, result=record.to_public_payload())


@ROUTER.get("/knowledge/anchors/{anchor_id}")
def get_knowledge_anchor(
    request: Request,
    anchor_id: str,
    principal: Principal = INTERNAL_KNOWLEDGE_PRINCIPAL,
) -> dict[str, object]:
    """Fetch one governed anchor detail record for management visibility."""

    _ = principal
    repository = _get_repository(request)
    try:
        record = repository.get_anchor(anchor_id=anchor_id)
    except KnowledgeRepositoryError as error:
        raise _repository_http_error(request, error) from error
    return _single_success_envelope(request=request, result=record.to_public_payload())


@ROUTER.post("/knowledge/source-versions/bulk/archive")
def bulk_archive_knowledge_source_versions(
    request: Request,
    payload: Any = REQUEST_BODY_OPTIONAL,
    principal: Principal = INTERNAL_KNOWLEDGE_PRINCIPAL,
) -> dict[str, object]:
    """Archive multiple eligible source versions with deterministic outcomes."""

    _ = principal
    source = _as_object(payload)
    repository = _get_repository(request)
    try:
        records = repository.bulk_archive_source_versions(
            archived_by=_required_uuid_string(source, "acting_user"),
            source_version_ids=_required_non_empty_string_list(source, "ids"),
        )
    except KnowledgeRepositoryError as error:
        raise _repository_http_error(request, error) from error
    return _bulk_success_envelope(request=request, items=records)


@ROUTER.get("/knowledge/source-versions/{source_version_id}")
def get_knowledge_source_version_lifecycle(
    request: Request,
    source_version_id: str,
    principal: Principal = INTERNAL_KNOWLEDGE_PRINCIPAL,
) -> dict[str, object]:
    """Fetch one governed source-version lifecycle record for management visibility."""

    _ = principal
    repository = _get_repository(request)
    try:
        record = repository.get_source_version_lifecycle(source_version_id=source_version_id)
    except KnowledgeRepositoryError as error:
        raise _repository_http_error(request, error) from error
    return _source_version_lifecycle_success_envelope(request=request, record=record)


@ROUTER.post("/knowledge/source-versions/{source_version_id}/archive")
def archive_knowledge_source_version(
    request: Request,
    source_version_id: str,
    payload: Any = REQUEST_BODY_OPTIONAL,
    principal: Principal = INTERNAL_KNOWLEDGE_PRINCIPAL,
) -> dict[str, object]:
    """Archive one governed published or superseded source version."""

    _ = principal
    source = _as_object(payload)
    repository = _get_repository(request)
    try:
        record = repository.archive_source_version(
            source_version_id=source_version_id,
            archived_by=_required_uuid_string(source, "archived_by"),
        )
    except KnowledgeRepositoryError as error:
        raise _repository_http_error(request, error) from error
    return _source_version_lifecycle_success_envelope(request=request, record=record)


@ROUTER.api_route(
    "/v1/knowledge/{scope}/{remaining_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
def knowledge_scope_guard(
    request: Request,
    scope: str,
    remaining_path: str,
) -> dict[str, object]:
    """Fail closed for unsupported knowledge scope paths."""

    _ = (scope, remaining_path)
    raise _http_error(
        request=request,
        status_code=404,
        error_code=UNSUPPORTED_KNOWLEDGE_SCOPE,
        message="Requested knowledge scope is not supported.",
        reason=UNSUPPORTED_KNOWLEDGE_SCOPE,
        reason_code=UNSUPPORTED_KNOWLEDGE_SCOPE,
    )


def _parse_iso_date(value: str, *, field_name: str = "effective_date") -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": INVALID_KNOWLEDGE_REQUEST,
                "message": f"Knowledge request field `{field_name}` must be an ISO date.",
                "reason": INVALID_KNOWLEDGE_REQUEST,
                "reason_code": INVALID_KNOWLEDGE_REQUEST,
            },
        ) from error


def _string_list(
    source: Mapping[str, object],
    field_name: str,
    *,
    max_items: int | None = None,
    max_item_length: int | None = None,
) -> list[str]:
    value = source.get(field_name, [])
    if not isinstance(value, list):
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": INVALID_KNOWLEDGE_REQUEST,
                "message": f"Knowledge request field `{field_name}` is invalid.",
                "reason": INVALID_KNOWLEDGE_REQUEST,
                "reason_code": INVALID_KNOWLEDGE_REQUEST,
            },
        )
    value_list = cast(list[object], value)
    output: list[str] = []
    for item in value_list:
        if isinstance(item, str) and item.strip():
            normalized = item.strip()
            if max_item_length is not None and len(normalized) > max_item_length:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error_code": INVALID_KNOWLEDGE_REQUEST,
                        "message": f"Knowledge request field `{field_name}` is invalid.",
                        "reason": INVALID_KNOWLEDGE_REQUEST,
                        "reason_code": INVALID_KNOWLEDGE_REQUEST,
                    },
                )
            output.append(normalized)
        else:
            raise HTTPException(
                status_code=400,
                detail={
                    "error_code": INVALID_KNOWLEDGE_REQUEST,
                    "message": f"Knowledge request field `{field_name}` is invalid.",
                    "reason": INVALID_KNOWLEDGE_REQUEST,
                    "reason_code": INVALID_KNOWLEDGE_REQUEST,
                },
            )
    if max_items is not None and len(output) > max_items:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": INVALID_KNOWLEDGE_REQUEST,
                "message": f"Knowledge request field `{field_name}` is invalid.",
                "reason": INVALID_KNOWLEDGE_REQUEST,
                "reason_code": INVALID_KNOWLEDGE_REQUEST,
            },
        )
    return output


def _required_non_empty_string_list(
    source: Mapping[str, object],
    field_name: str,
) -> tuple[str, ...]:
    values = tuple(_string_list(source, field_name))
    if values:
        return values
    raise HTTPException(
        status_code=400,
        detail={
            "error_code": INVALID_KNOWLEDGE_REQUEST,
            "message": f"Knowledge request field `{field_name}` is invalid.",
            "reason": INVALID_KNOWLEDGE_REQUEST,
            "reason_code": INVALID_KNOWLEDGE_REQUEST,
        },
    )


def _required_string(source: Mapping[str, object], field_name: str) -> str:
    value = source.get(field_name)
    if isinstance(value, str):
        normalized = value.strip()
        if normalized:
            return normalized
    raise HTTPException(
        status_code=400,
        detail={
            "error_code": INVALID_KNOWLEDGE_REQUEST,
            "message": f"Knowledge request field `{field_name}` is invalid.",
            "reason": INVALID_KNOWLEDGE_REQUEST,
            "reason_code": INVALID_KNOWLEDGE_REQUEST,
        },
    )


def _required_uuid_string(source: Mapping[str, object], field_name: str) -> str:
    normalized = _required_string(source, field_name)
    try:
        return str(UUID(normalized))
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": INVALID_KNOWLEDGE_REQUEST,
                "message": f"Knowledge request field `{field_name}` is invalid.",
                "reason": INVALID_KNOWLEDGE_REQUEST,
                "reason_code": INVALID_KNOWLEDGE_REQUEST,
            },
        ) from error


def _public_query_text(source: Mapping[str, object], field_name: str) -> str:
    normalized = _required_string(source, field_name)
    if len(normalized) > MAX_PUBLIC_QUERY_LENGTH:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": INVALID_KNOWLEDGE_REQUEST,
                "message": f"Knowledge request field `{field_name}` is invalid.",
                "reason": INVALID_KNOWLEDGE_REQUEST,
                "reason_code": INVALID_KNOWLEDGE_REQUEST,
            },
        )
    return normalized.lower()


def _required_true_boolean(source: Mapping[str, object], field_name: str) -> bool:
    value = source.get(field_name)
    if value is True:
        return True
    raise HTTPException(
        status_code=400,
        detail={
            "error_code": INVALID_KNOWLEDGE_REQUEST,
            "message": f"Knowledge request field `{field_name}` is invalid.",
            "reason": INVALID_KNOWLEDGE_REQUEST,
            "reason_code": INVALID_KNOWLEDGE_REQUEST,
        },
    )


def _required_object(source: Mapping[str, object], field_name: str) -> dict[str, object]:
    value = source.get(field_name)
    if isinstance(value, Mapping):
        nested = cast(Mapping[object, object], value)
        return {str(key): nested[key] for key in nested}
    raise HTTPException(
        status_code=400,
        detail={
            "error_code": INVALID_KNOWLEDGE_REQUEST,
            "message": f"Knowledge request field `{field_name}` is invalid.",
            "reason": INVALID_KNOWLEDGE_REQUEST,
            "reason_code": INVALID_KNOWLEDGE_REQUEST,
        },
    )


def _optional_object(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        nested = cast(Mapping[object, object], value)
        return {str(key): nested[key] for key in nested}
    raise HTTPException(
        status_code=400,
        detail={
            "error_code": INVALID_KNOWLEDGE_REQUEST,
            "message": "Knowledge request field is invalid.",
            "reason": INVALID_KNOWLEDGE_REQUEST,
            "reason_code": INVALID_KNOWLEDGE_REQUEST,
        },
    )


def _required_object_list(
    source: Mapping[str, object],
    field_name: str,
) -> tuple[dict[str, object], ...]:
    value = source.get(field_name)
    if not isinstance(value, list):
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": INVALID_KNOWLEDGE_REQUEST,
                "message": f"Knowledge request field `{field_name}` is invalid.",
                "reason": INVALID_KNOWLEDGE_REQUEST,
                "reason_code": INVALID_KNOWLEDGE_REQUEST,
            },
        )
    output: list[dict[str, object]] = []
    for item in cast(list[object], value):
        if not isinstance(item, Mapping):
            raise HTTPException(
                status_code=400,
                detail={
                    "error_code": INVALID_KNOWLEDGE_REQUEST,
                    "message": f"Knowledge request field `{field_name}` is invalid.",
                    "reason": INVALID_KNOWLEDGE_REQUEST,
                    "reason_code": INVALID_KNOWLEDGE_REQUEST,
                },
            )
        nested = cast(Mapping[object, object], item)
        output.append({str(key): nested[key] for key in nested})
    return tuple(output)


def _required_non_empty_object_list(
    source: Mapping[str, object],
    field_name: str,
) -> tuple[dict[str, object], ...]:
    values = _required_object_list(source, field_name)
    if values:
        return values
    raise HTTPException(
        status_code=400,
        detail={
            "error_code": INVALID_KNOWLEDGE_REQUEST,
            "message": f"Knowledge request field `{field_name}` is invalid.",
            "reason": INVALID_KNOWLEDGE_REQUEST,
            "reason_code": INVALID_KNOWLEDGE_REQUEST,
        },
    )


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.strip()
        if normalized:
            return normalized.lower()
    raise HTTPException(
        status_code=400,
        detail={
            "error_code": INVALID_KNOWLEDGE_REQUEST,
            "message": "Knowledge request filter is invalid.",
            "reason": INVALID_KNOWLEDGE_REQUEST,
            "reason_code": INVALID_KNOWLEDGE_REQUEST,
        },
    )


def _optional_identifier_string(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.strip()
        if normalized:
            return normalized
    raise HTTPException(
        status_code=400,
        detail={
            "error_code": INVALID_KNOWLEDGE_REQUEST,
            "message": "Knowledge request filter is invalid.",
            "reason": INVALID_KNOWLEDGE_REQUEST,
            "reason_code": INVALID_KNOWLEDGE_REQUEST,
        },
    )


def _optional_uuid_string(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.strip()
        if normalized:
            try:
                return str(UUID(normalized))
            except ValueError as error:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error_code": INVALID_KNOWLEDGE_REQUEST,
                        "message": "Knowledge request filter is invalid.",
                        "reason": INVALID_KNOWLEDGE_REQUEST,
                        "reason_code": INVALID_KNOWLEDGE_REQUEST,
                    },
                ) from error
    raise HTTPException(
        status_code=400,
        detail={
            "error_code": INVALID_KNOWLEDGE_REQUEST,
            "message": "Knowledge request filter is invalid.",
            "reason": INVALID_KNOWLEDGE_REQUEST,
            "reason_code": INVALID_KNOWLEDGE_REQUEST,
        },
    )


def _management_limit(value: int | None) -> int:
    if value is None:
        return 100
    if value > 0:
        return value
    raise HTTPException(
        status_code=400,
        detail={
            "error_code": INVALID_KNOWLEDGE_REQUEST,
            "message": "Knowledge request filter is invalid.",
            "reason": INVALID_KNOWLEDGE_REQUEST,
            "reason_code": INVALID_KNOWLEDGE_REQUEST,
        },
    )


def _management_offset(value: int | None) -> int:
    if value is None:
        return 0
    if value >= 0:
        return value
    raise HTTPException(
        status_code=400,
        detail={
            "error_code": INVALID_KNOWLEDGE_REQUEST,
            "message": "Knowledge request filter is invalid.",
            "reason": INVALID_KNOWLEDGE_REQUEST,
            "reason_code": INVALID_KNOWLEDGE_REQUEST,
        },
    )


def _as_object(payload: object) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": INVALID_KNOWLEDGE_REQUEST,
                "message": "Knowledge request payload is invalid.",
                "reason": INVALID_KNOWLEDGE_REQUEST,
                "reason_code": INVALID_KNOWLEDGE_REQUEST,
            },
        )
    source = cast(Mapping[object, object], payload)
    return {str(key): source[key] for key in source}


def _ingestion_success_envelope(
    *,
    request: Request,
    record: KnowledgeIngestionRecord,
) -> dict[str, object]:
    return {
        "status": "ok",
        "service": KNOWLEDGE_SERVICE_NAME,
        "correlation_id": get_correlation_id(request),
        "trace_id": get_trace_id(request),
        "result": record.to_public_payload(),
    }


def _single_success_envelope(
    *,
    request: Request,
    result: dict[str, object],
) -> dict[str, object]:
    return {
        "status": "ok",
        "service": KNOWLEDGE_SERVICE_NAME,
        "correlation_id": get_correlation_id(request),
        "trace_id": get_trace_id(request),
        "result": result,
    }


def _collection_success_envelope(
    *,
    request: Request,
    items: list[dict[str, object]],
    limit: int | None = None,
    offset: int | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "total": len(items),
        "items": items,
    }
    if limit is not None or offset is not None or sort_by is not None or sort_order is not None:
        result["page"] = {
            "limit": limit,
            "offset": offset,
            "sort_by": sort_by,
            "sort_order": sort_order,
        }
    return {
        "status": "ok",
        "service": KNOWLEDGE_SERVICE_NAME,
        "correlation_id": get_correlation_id(request),
        "trace_id": get_trace_id(request),
        "result": result,
    }


def _bulk_success_envelope(
    *,
    request: Request,
    items: tuple[KnowledgeBulkOperationItemRecord, ...],
) -> dict[str, object]:
    if items and all(item.status == "ok" for item in items):
        bulk_status = "full_success"
    elif any(item.status == "ok" for item in items):
        bulk_status = "partial_failure"
    else:
        bulk_status = "full_rejection"
    return {
        "status": "ok",
        "service": KNOWLEDGE_SERVICE_NAME,
        "correlation_id": get_correlation_id(request),
        "trace_id": get_trace_id(request),
        "result": {
            "bulk_status": bulk_status,
            "total": len(items),
            "items": [item.to_public_payload() for item in items],
        },
    }


def _bulk_ingestion_success_envelope(
    *,
    request: Request,
    items: tuple[KnowledgeBulkIngestionItemRecord, ...],
) -> dict[str, object]:
    if items and all(item.status == "ok" for item in items):
        bulk_status = "full_success"
    elif any(item.status == "ok" for item in items):
        bulk_status = "partial_failure"
    else:
        bulk_status = "full_rejection"
    return {
        "status": "ok",
        "service": KNOWLEDGE_SERVICE_NAME,
        "correlation_id": get_correlation_id(request),
        "trace_id": get_trace_id(request),
        "result": {
            "bulk_status": bulk_status,
            "total": len(items),
            "items": [item.to_public_payload() for item in items],
        },
    }


def _ingestion_detail_success_envelope(
    *,
    request: Request,
    record: KnowledgeIngestionDetailRecord,
) -> dict[str, object]:
    return {
        "status": "ok",
        "service": KNOWLEDGE_SERVICE_NAME,
        "correlation_id": get_correlation_id(request),
        "trace_id": get_trace_id(request),
        "result": record.to_public_payload(),
    }


def _source_version_lifecycle_success_envelope(
    *,
    request: Request,
    record: KnowledgeSourceVersionLifecycleRecord,
) -> dict[str, object]:
    return {
        "status": "ok",
        "service": KNOWLEDGE_SERVICE_NAME,
        "correlation_id": get_correlation_id(request),
        "trace_id": get_trace_id(request),
        "result": record.to_public_payload(),
    }


def _error_envelope(
    *,
    request: Request,
    error_code: str,
    message: str,
    reason: str,
    reason_code: str,
    context: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "error_code": error_code,
        "message": message,
        "reason": reason,
        "reason_code": reason_code,
        "correlation_id": get_correlation_id(request),
        "trace_id": get_trace_id(request),
    }
    if context is not None:
        payload["context"] = context
    return payload


def _http_error(
    *,
    request: Request,
    status_code: int,
    error_code: str,
    message: str,
    reason: str,
    reason_code: str,
    context: dict[str, object] | None = None,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=_error_envelope(
            request=request,
            error_code=error_code,
            message=message,
            reason=reason,
            reason_code=reason_code,
            context=context,
        ),
    )


def _get_repository(request: Request) -> KnowledgeRepositoryProtocol:
    repository = getattr(request.app.state, "knowledge_repository", None)
    if repository is None:
        return cast(KnowledgeRepositoryProtocol, get_default_knowledge_repository())
    return cast(KnowledgeRepositoryProtocol, repository)


def _verify_knowledge_route_boundary(app: FastAPI) -> None:
    route_index: dict[tuple[str, str], APIRoute] = {}
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            route_index[(method, route.path)] = route

    for route_spec in PROTECTED_KNOWLEDGE_ROUTE_SPECS:
        route = route_index.get(route_spec)
        if route is None:
            raise RuntimeError(f"knowledge_protected_route_missing:{route_spec[0]} {route_spec[1]}")
        if not _route_requires_internal_knowledge_principal(route):
            raise RuntimeError(
                f"knowledge_protected_route_unsecured:{route_spec[0]} {route_spec[1]}"
            )

    for route_spec in PUBLIC_KNOWLEDGE_ROUTE_SPECS:
        route = route_index.get(route_spec)
        if route is None:
            raise RuntimeError(f"knowledge_public_route_missing:{route_spec[0]} {route_spec[1]}")
        if _route_requires_internal_knowledge_principal(route):
            raise RuntimeError(
                f"knowledge_public_route_oversecured:{route_spec[0]} {route_spec[1]}"
            )


def _route_requires_internal_knowledge_principal(route: APIRoute) -> bool:
    return any(
        dependency.call is require_internal_knowledge_principal
        for dependency in route.dependant.dependencies
    )


async def _handle_request_validation_error(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    _ = exc
    return JSONResponse(
        status_code=400,
        content={
            "detail": _error_envelope(
                request=request,
                error_code=INVALID_KNOWLEDGE_REQUEST,
                message="Knowledge request payload is invalid.",
                reason=INVALID_KNOWLEDGE_REQUEST,
                reason_code=INVALID_KNOWLEDGE_REQUEST,
            )
        },
    )


async def _handle_http_exception_error(
    request: Request,
    exc: HTTPException,
) -> JSONResponse:
    detail = cast(dict[str, object], exc.detail) if isinstance(exc.detail, dict) else {}
    envelope = _error_envelope(
        request=request,
        error_code=str(detail.get("error_code", INVALID_KNOWLEDGE_REQUEST)),
        message=str(detail.get("message", "Knowledge request failed.")),
        reason=str(detail.get("reason", INVALID_KNOWLEDGE_REQUEST)),
        reason_code=str(detail.get("reason_code", detail.get("reason", INVALID_KNOWLEDGE_REQUEST))),
        context=cast(dict[str, object] | None, detail.get("context")),
    )
    return JSONResponse(status_code=exc.status_code, content={"detail": envelope})


async def _handle_starlette_http_exception_error(
    request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": _error_envelope(
                request=request,
                error_code=UNSUPPORTED_KNOWLEDGE_SCOPE,
                message="Requested knowledge path is not supported.",
                reason=UNSUPPORTED_KNOWLEDGE_SCOPE,
                reason_code=UNSUPPORTED_KNOWLEDGE_SCOPE,
            )
        },
    )


def _repository_http_error(
    request: Request,
    error: KnowledgeRepositoryError,
) -> HTTPException:
    status_code = 503
    if error.reason_code in {
        INVALID_KNOWLEDGE_REQUEST,
        UNSUPPORTED_SOURCE_INPUT_ORIGIN,
        UNSUPPORTED_SOURCE_CLASS,
        INVALID_AUTHORITY_SOURCE_CLASS_BINDING,
        INVALID_EFFECTIVE_WINDOW_METADATA,
    }:
        status_code = 400
    elif error.reason_code in {
        INVALID_KNOWLEDGE_LINEAGE,
        KNOWLEDGE_IDEMPOTENCY_CONFLICT,
        INVALID_PUBLICATION_STATE_TRANSITION,
        KNOWLEDGE_PUBLICATION_SAFETY_REJECTED,
        KNOWLEDGE_SUPERSESSION_CONFLICT,
        KNOWLEDGE_TEMPORAL_SCOPE_MISMATCH,
        KNOWLEDGE_RECORD_NOT_PUBLISHED,
    }:
        status_code = 409
    return _http_error(
        request=request,
        status_code=status_code,
        error_code=error.reason_code,
        message=error.message,
        reason=error.reason_code,
        reason_code=error.reason_code,
    )


async def _handle_repository_error(
    request: Request,
    exc: KnowledgeRepositoryError,
) -> JSONResponse:
    http_error = _repository_http_error(request, exc)
    return await _handle_http_exception_error(request, http_error)


app = create_app()
