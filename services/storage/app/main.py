"""Storage service runtime app factory and capability issuance routes."""

from dataclasses import asdict
from pathlib import Path as PathlibPath
from typing import Any
from typing import cast
from collections.abc import Mapping

from fastapi import Body
from fastapi import FastAPI
from fastapi import Request
from fastapi import APIRouter
from fastapi import HTTPException
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from shared.tracing.correlation import get_trace_id
from shared.tracing.correlation import get_correlation_id
from shared.tracing.correlation import CorrelationIdMiddleware
from services.storage.app.config import STORAGE_SERVICE_NAME
from services.storage.app.config import get_storage_service_version
from services.storage.app.errors import CLEANUP_NOT_ELIGIBLE
from services.storage.app.errors import STORAGE_CLEANUP_FAILED
from services.storage.app.errors import INVALID_STORAGE_REQUEST
from services.storage.app.errors import create_storage_http_error
from services.storage.app.errors import UNSUPPORTED_STORAGE_SCOPE
from services.storage.app.errors import RETENTION_POLICY_VIOLATION
from services.storage.app.errors import STORAGE_CAPABILITY_EXPIRED
from services.storage.app.errors import STORAGE_CONTRACT_VIOLATION
from services.storage.app.errors import build_storage_error_envelope
from services.storage.app.errors import STORAGE_CAPABILITY_NOT_FOUND
from services.storage.app.models import UploadCapabilityRequestModel
from services.storage.app.models import DownloadCapabilityRequestModel
from services.reports.app.metrics import get_default_reports_metrics_emitter
from services.reports.app.metrics import REPORTS_DOWNLOAD_EXPIRY_REJECT_TOTAL
from services.storage.app.retention import cleanup_one_record
from services.storage.app.retention import cleanup_reference_time
from services.storage.app.retention import run_retention_cleanup_hook
from services.storage.app.retention import compute_retention_expires_at
from services.storage.app.retention import retention_class_for_object_key
from services.storage.app.repository import StorageRetentionRepository
from services.storage.app.repository import StorageRetentionRepositoryError
from services.reports.app.logging_policy import emit_report_structured_log
from services.storage.app.capability_tokens import StorageCapabilityService
from services.storage.app.capability_tokens import StorageCapabilityResolutionError

load_dotenv(dotenv_path=PathlibPath(__file__).parent.parent.parent.parent / ".env")

ROUTER = APIRouter()
REQUEST_BODY_OPTIONAL = Body(None)


def create_app() -> FastAPI:
    """Build deterministic storage FastAPI app with capability routes."""

    app = FastAPI(
        title=STORAGE_SERVICE_NAME,
        version=get_storage_service_version(),
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
    app.state.storage_capability_service = StorageCapabilityService()
    app.state.storage_retention_repository = StorageRetentionRepository()
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
    app.include_router(ROUTER)
    return app


@ROUTER.get("/healthz")
def storage_health_status(request: Request) -> dict[str, str]:
    """Expose deterministic storage-service health endpoint."""

    return {
        "status": "ok",
        "service": STORAGE_SERVICE_NAME,
        "version": get_storage_service_version(),
        "correlation_id": get_correlation_id(request),
    }


@ROUTER.get("/readyz")
def storage_readiness_status(request: Request) -> dict[str, str]:
    """Expose deterministic storage-service readiness endpoint."""

    return {
        "status": "ready",
        "service": STORAGE_SERVICE_NAME,
        "version": get_storage_service_version(),
        "correlation_id": get_correlation_id(request),
    }


@ROUTER.post("/v1/storage/upload-capabilities", status_code=201)
def create_upload_capability(
    request: Request,
    payload: Any = REQUEST_BODY_OPTIONAL,
) -> dict[str, object]:
    """Issue deterministic upload capability for one governed object key."""

    request_model = _parse_upload_request(payload=payload)
    idempotency_key = _required_header(request=request, name="Idempotency-Key")
    service = _service(request=request)
    issued = service.issue_upload_capability(
        request_model=request_model,
        idempotency_key=idempotency_key,
    )
    metadata = service.get_object_metadata(object_key=request_model.object_key)
    if metadata is None:
        raise create_storage_http_error(
            request=request,
            status_code=503,
            error_code=STORAGE_CONTRACT_VIOLATION,
            message="Storage metadata persistence failed.",
            reason=STORAGE_CONTRACT_VIOLATION,
        )
    repository = _retention_repository(request=request)
    try:
        retention_class = retention_class_for_object_key(object_key=metadata.object_key)
        retention_expires_at = compute_retention_expires_at(
            created_at=metadata.created_at,
            retention_class=retention_class,
        )
        repository.upsert_record(
            object_key=metadata.object_key,
            tenant_id=metadata.tenant_id,
            owner_user_id=metadata.owner_user_id,
            content_type=metadata.content_type,
            size_bytes=metadata.size_bytes,
            checksum_sha256=metadata.checksum_sha256,
            created_at=metadata.created_at,
            retention_class=retention_class,
            retention_expires_at=retention_expires_at,
        )
    except StorageRetentionRepositoryError as error:
        raise create_storage_http_error(
            request=request,
            status_code=_status_code_for_retention_error(reason_code=error.reason_code),
            error_code=error.reason_code,
            message=error.message,
            reason=error.reason_code,
        ) from error

    return {
        "status": issued.status,
        "capability": asdict(issued.capability),
        "trace_id": get_trace_id(request),
        "correlation_id": get_correlation_id(request),
    }


@ROUTER.post("/v1/storage/download-capabilities", status_code=201)
def create_download_capability(
    request: Request,
    payload: Any = REQUEST_BODY_OPTIONAL,
) -> dict[str, object]:
    """Issue deterministic download capability for one governed object key."""

    request_model = _parse_download_request(payload=payload)
    idempotency_key = _required_header(request=request, name="Idempotency-Key")
    service = _service(request=request)
    issued = service.issue_download_capability(
        request_model=request_model,
        idempotency_key=idempotency_key,
    )
    if issued is None:
        emit_report_structured_log(
            level="error",
            service=STORAGE_SERVICE_NAME,
            event_type="storage_download_capability_failed",
            correlation_id=get_correlation_id(request),
            tenant_id=request_model.tenant_id,
            report_id=None,
            reason_code=STORAGE_CAPABILITY_NOT_FOUND,
            details={
                "method": request.method,
                "path": request.url.path,
                "status_code": 404,
            },
        )
        raise create_storage_http_error(
            request=request,
            status_code=404,
            error_code=STORAGE_CAPABILITY_NOT_FOUND,
            message="Storage object metadata not found for download capability issuance.",
            reason=STORAGE_CAPABILITY_NOT_FOUND,
        )
    try:
        capability = service.resolve_download_capability(
            capability_id=issued.capability.capability_id
        )
    except StorageCapabilityResolutionError as error:
        if error.reason_code == STORAGE_CAPABILITY_EXPIRED:
            get_default_reports_metrics_emitter().increment_counter_non_blocking(
                REPORTS_DOWNLOAD_EXPIRY_REJECT_TOTAL,
                dimensions={
                    "event_type": "report_downloaded",
                    "reason_code": STORAGE_CAPABILITY_EXPIRED,
                },
            )
        status_code = _status_code_for_capability_resolution(reason_code=error.reason_code)
        emit_report_structured_log(
            level="error",
            service=STORAGE_SERVICE_NAME,
            event_type="storage_download_capability_failed",
            correlation_id=get_correlation_id(request),
            tenant_id=request_model.tenant_id,
            report_id=None,
            reason_code=error.reason_code,
            details={
                "method": request.method,
                "path": request.url.path,
                "status_code": status_code,
            },
        )
        raise create_storage_http_error(
            request=request,
            status_code=status_code,
            error_code=error.reason_code,
            message=error.message,
            reason=error.reason_code,
        ) from error
    emit_report_structured_log(
        level="info",
        service=STORAGE_SERVICE_NAME,
        event_type="storage_download_capability_issued",
        correlation_id=get_correlation_id(request),
        tenant_id=request_model.tenant_id,
        report_id=None,
        reason_code=None,
        details={
            "method": request.method,
            "path": request.url.path,
            "status_code": 201,
            "capability_id": capability.capability_id,
            "download_url": capability.url,
        },
    )
    return {
        "status": issued.status,
        "capability": asdict(capability),
        "trace_id": get_trace_id(request),
        "correlation_id": get_correlation_id(request),
    }


@ROUTER.get("/v1/storage/objects/{object_key}/metadata")
def get_storage_object_metadata(request: Request, object_key: str) -> dict[str, object]:
    """Return deterministic storage object metadata for one object key."""

    normalized_object_key = object_key.strip()
    if normalized_object_key == "":
        raise create_storage_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_STORAGE_REQUEST,
            message="Storage object key must be provided.",
            reason=INVALID_STORAGE_REQUEST,
        )
    service = _service(request=request)
    metadata = service.get_object_metadata(object_key=normalized_object_key)
    if metadata is None:
        raise create_storage_http_error(
            request=request,
            status_code=404,
            error_code=STORAGE_CAPABILITY_NOT_FOUND,
            message="Storage object metadata was not found.",
            reason=STORAGE_CAPABILITY_NOT_FOUND,
        )

    service.build_metadata_capability(
        object_key=normalized_object_key, tenant_id=metadata.tenant_id
    )
    return {
        "status": "ok",
        "metadata": asdict(metadata),
        "trace_id": get_trace_id(request),
        "correlation_id": get_correlation_id(request),
    }


@ROUTER.post("/v1/storage/internal/retention/cleanup-hooks/run")
def run_storage_retention_cleanup_hook(
    request: Request,
    payload: Any = REQUEST_BODY_OPTIONAL,
) -> dict[str, object]:
    source = _as_object(payload=payload) if payload is not None else {}
    limit_raw = source.get("limit", 100)
    if not isinstance(limit_raw, int):
        raise create_storage_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_STORAGE_REQUEST,
            message="Cleanup hook field `limit` is invalid.",
            reason=INVALID_STORAGE_REQUEST,
        )
    repository = _retention_repository(request=request)
    summary = run_retention_cleanup_hook(
        repository=repository,
        limit=limit_raw,
        reference_time=cleanup_reference_time(),
    )
    return {
        "status": "ok",
        "summary": summary,
        "trace_id": get_trace_id(request),
        "correlation_id": get_correlation_id(request),
    }


@ROUTER.post("/v1/storage/internal/retention/cleanup-hooks/{object_key}")
def run_storage_retention_cleanup_for_one(
    request: Request,
    object_key: str,
) -> dict[str, object]:
    repository = _retention_repository(request=request)
    try:
        cleaned = cleanup_one_record(
            repository=repository,
            object_key=object_key,
            reference_time=cleanup_reference_time(),
        )
    except StorageRetentionRepositoryError as error:
        raise create_storage_http_error(
            request=request,
            status_code=_status_code_for_retention_error(reason_code=error.reason_code),
            error_code=error.reason_code,
            message=error.message,
            reason=error.reason_code,
        ) from error
    return {
        "status": "ok",
        "record": cleaned.to_payload(),
        "trace_id": get_trace_id(request),
        "correlation_id": get_correlation_id(request),
    }


@ROUTER.api_route(
    "/v1/storage/{scope}/{remaining_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
def storage_runtime_scaffold(
    request: Request,
    scope: str,
    remaining_path: str,
) -> dict[str, object]:
    """Fail closed for unsupported storage scope paths."""

    _ = (scope, remaining_path)
    raise create_storage_http_error(
        request=request,
        status_code=404,
        error_code=UNSUPPORTED_STORAGE_SCOPE,
        message="Requested storage scope is not supported.",
        reason=UNSUPPORTED_STORAGE_SCOPE,
    )


async def _handle_request_validation_error(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    envelope = build_storage_error_envelope(
        request=request,
        error_code=INVALID_STORAGE_REQUEST,
        message="Storage request payload is invalid.",
        reason=INVALID_STORAGE_REQUEST,
    )
    return JSONResponse(status_code=400, content={"detail": envelope})


async def _handle_http_exception_error(
    request: Request,
    exc: HTTPException,
) -> JSONResponse:
    detail = cast(dict[str, object], exc.detail) if isinstance(exc.detail, dict) else {}
    envelope = build_storage_error_envelope(
        request=request,
        error_code=str(detail.get("error_code", "")),
        message=str(detail.get("message", "")),
        reason=str(detail.get("reason", "")),
    )
    return JSONResponse(status_code=exc.status_code, content={"detail": envelope})


async def _handle_starlette_http_exception_error(
    request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    envelope = build_storage_error_envelope(
        request=request,
        error_code=STORAGE_CONTRACT_VIOLATION,
        message="Storage request failed.",
        reason=STORAGE_CONTRACT_VIOLATION,
    )
    return JSONResponse(status_code=exc.status_code, content={"detail": envelope})


def _service(*, request: Request) -> StorageCapabilityService:
    configured = getattr(request.app.state, "storage_capability_service", None)
    if isinstance(configured, StorageCapabilityService):
        return configured
    fallback = StorageCapabilityService()
    request.app.state.storage_capability_service = fallback
    return fallback


def _retention_repository(*, request: Request) -> StorageRetentionRepository:
    configured = getattr(request.app.state, "storage_retention_repository", None)
    if isinstance(configured, StorageRetentionRepository):
        return configured
    fallback = StorageRetentionRepository()
    request.app.state.storage_retention_repository = fallback
    return fallback


def _required_header(*, request: Request, name: str) -> str:
    value = request.headers.get(name, "").strip()
    if value:
        return value
    raise create_storage_http_error(
        request=request,
        status_code=400,
        error_code=INVALID_STORAGE_REQUEST,
        message=f"Required header `{name}` is missing.",
        reason=INVALID_STORAGE_REQUEST,
    )


def _parse_upload_request(*, payload: object) -> UploadCapabilityRequestModel:
    source = _as_object(payload=payload)
    required_string = (
        "tenant_id",
        "owner_user_id",
        "object_key",
        "content_type",
        "checksum_sha256",
    )
    for field_name in required_string:
        if not isinstance(source.get(field_name), str) or str(source[field_name]).strip() == "":
            raise _invalid_payload_error(field_name=field_name)
    expected_size_raw = source.get("expected_size_bytes")
    if not isinstance(expected_size_raw, int) or expected_size_raw < 1:
        raise _invalid_payload_error(field_name="expected_size_bytes")
    document_id = source.get("document_id")
    if document_id is not None and not isinstance(document_id, str):
        raise _invalid_payload_error(field_name="document_id")
    return UploadCapabilityRequestModel(
        tenant_id=str(source["tenant_id"]).strip(),
        owner_user_id=str(source["owner_user_id"]).strip(),
        object_key=str(source["object_key"]).strip(),
        content_type=str(source["content_type"]).strip(),
        expected_size_bytes=expected_size_raw,
        checksum_sha256=str(source["checksum_sha256"]).strip(),
        document_id=str(document_id).strip() if isinstance(document_id, str) else None,
    )


def _parse_download_request(*, payload: object) -> DownloadCapabilityRequestModel:
    source = _as_object(payload=payload)
    required_string = ("tenant_id", "owner_user_id", "object_key")
    for field_name in required_string:
        if not isinstance(source.get(field_name), str) or str(source[field_name]).strip() == "":
            raise _invalid_payload_error(field_name=field_name)
    document_id = source.get("document_id")
    if document_id is not None and not isinstance(document_id, str):
        raise _invalid_payload_error(field_name="document_id")
    return DownloadCapabilityRequestModel(
        tenant_id=str(source["tenant_id"]).strip(),
        owner_user_id=str(source["owner_user_id"]).strip(),
        object_key=str(source["object_key"]).strip(),
        document_id=str(document_id).strip() if isinstance(document_id, str) else None,
    )


def _as_object(*, payload: object) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise _invalid_payload_error(field_name="payload")
    source = cast(Mapping[object, object], payload)
    return {str(key): source[key] for key in source}


def _invalid_payload_error(*, field_name: str) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={
            "error_code": INVALID_STORAGE_REQUEST,
            "message": f"Storage request field `{field_name}` is invalid.",
            "reason": INVALID_STORAGE_REQUEST,
        },
    )


def _status_code_for_capability_resolution(*, reason_code: str) -> int:
    if reason_code == INVALID_STORAGE_REQUEST:
        return 400
    if reason_code == STORAGE_CAPABILITY_NOT_FOUND:
        return 404
    if reason_code == STORAGE_CAPABILITY_EXPIRED:
        return 410
    return 500


def _status_code_for_retention_error(*, reason_code: str) -> int:
    if reason_code == RETENTION_POLICY_VIOLATION:
        return 409
    if reason_code == CLEANUP_NOT_ELIGIBLE:
        return 409
    if reason_code == STORAGE_CLEANUP_FAILED:
        return 503
    return 500


app = create_app()
