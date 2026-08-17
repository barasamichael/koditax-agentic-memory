"""Reports service runtime app factory and operational route wiring."""

import json
from pathlib import Path as PathlibPath
import re
from typing import Any
from typing import cast

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi import Request
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.middleware.cors import CORSMiddleware

from services.reports.app.audit import ReportsAuditEmitter
from shared.tracing.correlation import get_correlation_id
from shared.tracing.correlation import CorrelationIdMiddleware
from services.reports.app.config import REPORTS_SERVICE_NAME
from services.reports.app.config import get_reports_service_version
from services.reports.app.errors import REPORT_NOT_FOUND
from services.reports.app.errors import ReportErrorEnvelope
from services.reports.app.errors import REPORTS_REASON_CODES
from services.reports.app.errors import INVALID_REPORT_REQUEST
from services.reports.app.errors import REPORTS_CONTRACT_VIOLATION
from services.reports.app.errors import build_report_error_envelope
from services.reports.app.routes import ROUTER
from services.reports.app.metrics import get_default_reports_metrics_emitter
from services.reports.app.repository import get_default_reports_repository
from services.reports.app.logging_policy import emit_report_structured_log
from services.storage.app.capability_tokens import StorageCapabilityService
from services.validation.app.validation_rules import evaluate_report_workflow_validation

load_dotenv(dotenv_path=PathlibPath(__file__).parent.parent.parent.parent / ".env")

_REPORT_ID_PATTERN = re.compile(r"^/v1/reports/[^/]+/artifacts/([0-9a-f-]{36})(?:/metadata)?$")


def create_app() -> FastAPI:
    """Build deterministic reports FastAPI app with baseline routes."""

    app = FastAPI(
        title=REPORTS_SERVICE_NAME,
        version=get_reports_service_version(),
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
    app.state.reports_repository = get_default_reports_repository()
    app.state.storage_capability_service = StorageCapabilityService()
    app.state.reports_audit_emitter = ReportsAuditEmitter()
    app.state.reports_metrics_emitter = get_default_reports_metrics_emitter()
    app.add_middleware(CorrelationIdMiddleware)
    app.middleware("http")(_reports_governed_validation_middleware)
    app.middleware("http")(_reports_structured_logging_middleware)
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


async def _reports_governed_validation_middleware(
    request: Request,
    call_next: Any,
) -> Any:
    if request.method != "POST":
        return await call_next(request)
    tax_domain = _reports_validation_tax_domain_for_path(request.url.path)
    if tax_domain is None:
        return await call_next(request)

    try:
        payload = await request.json()
    except Exception:
        return await call_next(request)
    if not isinstance(payload, dict):
        return await call_next(request)

    governed_validation = evaluate_report_workflow_validation(
        tax_domain=tax_domain,
        payload=cast(dict[str, object], payload),
    ).to_dict()
    request.state.governed_validation = governed_validation
    if governed_validation["validation_status"] != "accepted":
        envelope = build_report_error_envelope(
            request=request,
            error_code=INVALID_REPORT_REQUEST,
            message="Report generation blocked by governed validation.",
            reason=INVALID_REPORT_REQUEST,
            reason_code=INVALID_REPORT_REQUEST,
            context={"governed_validation": governed_validation},
        )
        return JSONResponse(status_code=409, content={"detail": envelope})
    return await call_next(request)


async def _handle_request_validation_error(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    envelope = build_report_error_envelope(
        request=request,
        error_code=INVALID_REPORT_REQUEST,
        message="Reports request payload is invalid.",
        reason=INVALID_REPORT_REQUEST,
        reason_code=INVALID_REPORT_REQUEST,
        context={"validation_errors": exc.errors()},
    )
    return JSONResponse(status_code=400, content={"detail": envelope})


async def _handle_http_exception_error(
    request: Request,
    exc: HTTPException,
) -> JSONResponse:
    normalized_error = _normalize_http_exception_detail(request=request, detail=exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"detail": normalized_error})


async def _handle_starlette_http_exception_error(
    request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    if exc.status_code == 404:
        envelope = build_report_error_envelope(
            request=request,
            error_code=REPORT_NOT_FOUND,
            message="Requested reports route was not found.",
            reason=REPORT_NOT_FOUND,
            reason_code=REPORT_NOT_FOUND,
            context={"requested_path": request.url.path},
        )
        return JSONResponse(status_code=404, content={"detail": envelope})

    envelope = build_report_error_envelope(
        request=request,
        error_code=REPORTS_CONTRACT_VIOLATION,
        message="Reports request failed.",
        reason=REPORTS_CONTRACT_VIOLATION,
        reason_code=REPORTS_CONTRACT_VIOLATION,
        context={"requested_path": request.url.path},
    )
    return JSONResponse(status_code=exc.status_code, content={"detail": envelope})


def _normalize_http_exception_detail(
    *,
    request: Request,
    detail: object,
) -> ReportErrorEnvelope:
    detail_payload = cast(dict[str, object], detail) if isinstance(detail, dict) else {}
    reason = str(detail_payload.get("reason", "")).strip()
    reason_code = str(detail_payload.get("reason_code", "")).strip()
    error_code = str(detail_payload.get("error_code", "")).strip()
    message = str(detail_payload.get("message", "")).strip()
    context = detail_payload.get("context")

    if reason not in REPORTS_REASON_CODES:
        reason = REPORTS_CONTRACT_VIOLATION
    if reason_code not in REPORTS_REASON_CODES:
        reason_code = reason
    if not error_code:
        error_code = reason_code
    if not message:
        message = "Reports request failed."

    return build_report_error_envelope(
        request=request,
        error_code=error_code,
        message=message,
        reason=reason,
        reason_code=reason_code,
        context=cast(dict[str, object], context) if isinstance(context, dict) else None,
    )


async def _reports_structured_logging_middleware(
    request: Request,
    call_next: Any,
) -> Any:
    correlation_id = get_correlation_id(request)
    tenant_id = request.headers.get("X-Tenant-ID", "").strip() or None
    report_id = _infer_report_id_from_path(path=request.url.path)
    try:
        response = await call_next(request)
    except Exception:
        emit_report_structured_log(
            level="error",
            service=REPORTS_SERVICE_NAME,
            event_type="reports_request_failed_unhandled",
            correlation_id=correlation_id,
            tenant_id=tenant_id,
            report_id=report_id,
            reason_code=REPORTS_CONTRACT_VIOLATION,
            details={
                "method": request.method,
                "path": request.url.path,
                "status_code": 500,
            },
        )
        raise

    reason_code = _extract_reason_code_from_response(response=response)
    emit_report_structured_log(
        level="info" if response.status_code < 400 else "error",
        service=REPORTS_SERVICE_NAME,
        event_type=(
            "reports_request_succeeded" if response.status_code < 400 else "reports_request_failed"
        ),
        correlation_id=correlation_id,
        tenant_id=tenant_id,
        report_id=report_id,
        reason_code=reason_code,
        details={
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
        },
    )
    return response


def _extract_reason_code_from_response(*, response: Any) -> str | None:
    body = getattr(response, "body", None)
    if not isinstance(body, (bytes, bytearray)):
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    payload_object = cast(dict[str, object], payload)
    detail = payload_object.get("detail")
    if not isinstance(detail, dict):
        return None
    detail_object = cast(dict[str, object], detail)
    reason_code = detail_object.get("reason_code")
    if not isinstance(reason_code, str):
        return None
    return reason_code.strip() or None


def _infer_report_id_from_path(*, path: str) -> str | None:
    matched = _REPORT_ID_PATTERN.fullmatch(path)
    if matched is None:
        return None
    report_id = matched.group(1).strip().lower()
    return report_id or None


def _reports_validation_tax_domain_for_path(path: str) -> str | None:
    normalized = path.strip().lower()
    if normalized == "/v1/reports/income-tax/artifacts":
        return "income_tax"
    if normalized == "/v1/reports/health-contribution/artifacts":
        return "health_contribution"
    return None


app = create_app()
