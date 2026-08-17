"""Validation runtime boundary with deterministic standalone execution."""

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
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.middleware.cors import CORSMiddleware

from shared.tracing.correlation import get_trace_id
from shared.tracing.correlation import get_correlation_id
from shared.tracing.correlation import CorrelationIdMiddleware
from services.validation.app.config import ValidationConfig
from services.validation.app.config import load_validation_config
from services.validation.app.config import VALIDATION_INTERNAL_API_KEY_HEADER
from services.validation.app.audit_events import emit_validation_audit_event
from services.validation.app.audit_events import build_validation_audit_evidence
from services.validation.app.audit_events import build_validation_failure_audit_event
from services.validation.app.audit_events import build_validation_execution_audit_event
from services.validation.app.audit_events import build_default_validation_audit_event_store
from services.validation.app.validation_rules import SUPPORTED_TAX_DOMAINS
from services.validation.app.validation_rules import ValidationRequestModel
from services.validation.app.validation_rules import supported_modes_for_domain
from services.validation.app.validation_rules import evaluate_validation_request
from services.validation.app.validation_store import ValidationStoreError
from services.validation.app.validation_store import build_default_validation_store
from services.validation.app.validation_store import build_validation_execution_record
from services.validation.app.validation_outcomes import ValidationMode

load_dotenv(dotenv_path=PathlibPath(__file__).parent.parent.parent.parent / ".env")

INVALID_VALIDATION_REQUEST = "invalid_validation_request"
UNSUPPORTED_VALIDATION_SCOPE = "unsupported_validation_scope"
REQUEST_BODY_OPTIONAL = Body(None)
ROUTER = APIRouter()


def create_app() -> FastAPI:
    """Build deterministic validation FastAPI app."""

    config = load_validation_config()
    app = FastAPI(title=config.service_name, version=config.service_version)
    app.state.validation_config = config
    app.state.validation_store = build_default_validation_store(config)
    app.state.validation_audit_store = build_default_validation_audit_event_store()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.allowed_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )
    app.add_middleware(CorrelationIdMiddleware)
    app.add_exception_handler(RequestValidationError, cast(Any, _handle_request_validation_error))
    app.add_exception_handler(HTTPException, cast(Any, _handle_http_exception_error))
    app.add_exception_handler(
        StarletteHTTPException, cast(Any, _handle_starlette_http_exception_error)
    )
    app.include_router(ROUTER)
    return app


@ROUTER.get("/healthz")
def validation_healthz(request: Request) -> dict[str, object]:
    """Expose deterministic validation health endpoint."""

    config = _config(request)
    _, persistence_mode = request.app.state.validation_store.readiness()
    return {
        "status": "ok",
        "service": config.service_name,
        "version": config.service_version,
        "runtime_mode": config.runtime_mode,
        "persistence_mode": persistence_mode,
        "correlation_id": get_correlation_id(request),
        "trace_id": get_trace_id(request),
    }


@ROUTER.get("/readyz")
def validation_readyz(request: Request) -> dict[str, object]:
    """Expose deterministic validation readiness endpoint."""

    config = _config(request)
    ready, persistence_mode = request.app.state.validation_store.readiness()
    if config.runtime_mode == "production" and not ready:
        raise _http_error(
            request=request,
            status_code=503,
            error_code="validation_persistence_unavailable",
            message="Validation persistence is unavailable.",
            reason="validation_persistence_unavailable",
        )
    return {
        "status": "ready",
        "service": config.service_name,
        "version": config.service_version,
        "runtime_mode": config.runtime_mode,
        "persistence_mode": persistence_mode,
        "correlation_id": get_correlation_id(request),
        "trace_id": get_trace_id(request),
    }


@ROUTER.post("/validate/return")
def validate_return_payload(
    request: Request,
    payload: Any = REQUEST_BODY_OPTIONAL,
) -> dict[str, object]:
    """Validate deterministic required return fields by mode."""

    _enforce_internal_validation_boundary(request=request)
    parsed_request = _parse_validation_request(request=request, payload=payload)
    evaluation = evaluate_validation_request(parsed_request)
    record = build_validation_execution_record(
        return_id=parsed_request.return_id,
        tax_domain=parsed_request.tax_domain,
        mode=parsed_request.mode,
        fields=parsed_request.fields,
        validation_status=evaluation.validation_status,
        issues=tuple(issue.to_dict() for issue in evaluation.issues),
        rule_results=tuple(rule_result.to_dict() for rule_result in evaluation.rule_results),
        correlation_id=get_correlation_id(request),
        trace_id=get_trace_id(request),
    )
    try:
        request.app.state.validation_store.record_execution(record)
    except ValidationStoreError as error:
        raise _http_error(
            request=request,
            status_code=error.status_code,
            error_code=error.error_code,
            message=error.message,
            reason=error.reason,
            context={
                "return_id": parsed_request.return_id,
                "tax_domain": parsed_request.tax_domain,
                "mode": parsed_request.mode,
            },
        ) from error
    stored_audit_event = emit_validation_audit_event(
        store=request.app.state.validation_audit_store,
        event=build_validation_execution_audit_event(record=record),
    )

    config = _config(request)
    return {
        "status": "ok",
        "service": config.service_name,
        "correlation_id": get_correlation_id(request),
        "trace_id": get_trace_id(request),
        "audit_evidence": build_validation_audit_evidence(stored_audit_event).to_dict(),
        "result": {
            "validation_id": str(record.validation_id),
            "return_id": parsed_request.return_id,
            "tax_domain": parsed_request.tax_domain,
            "mode": parsed_request.mode,
            "validation_status": evaluation.validation_status,
            "summary": evaluation.summary_dict(),
            "issues": [issue.to_dict() for issue in evaluation.issues],
            "rule_results": [rule_result.to_dict() for rule_result in evaluation.rule_results],
        },
    }


@ROUTER.api_route(
    "/v1/validation/{scope}/{remaining_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
def validation_scope_guard(
    request: Request,
    scope: str,
    remaining_path: str,
) -> dict[str, object]:
    """Fail closed for unsupported validation scope paths."""

    _ = (scope, remaining_path)
    raise _http_error(
        request=request,
        status_code=404,
        error_code=UNSUPPORTED_VALIDATION_SCOPE,
        message="Requested validation scope is not supported.",
        reason=UNSUPPORTED_VALIDATION_SCOPE,
    )


def _parse_validation_request(*, request: Request, payload: object) -> ValidationRequestModel:
    source = _as_object(request=request, payload=payload)
    return_id = _required_string(request=request, source=source, field_name="return_id")
    tax_domain = _required_string(request=request, source=source, field_name="tax_domain")
    mode_raw = _required_string(request=request, source=source, field_name="mode")
    if tax_domain not in SUPPORTED_TAX_DOMAINS:
        raise _http_error(
            request=request,
            status_code=404,
            error_code=UNSUPPORTED_VALIDATION_SCOPE,
            message="Validation domain is not supported.",
            reason=UNSUPPORTED_VALIDATION_SCOPE,
            context={"tax_domain": tax_domain},
        )
    supported_modes = supported_modes_for_domain(tax_domain)
    if mode_raw not in supported_modes:
        raise _http_error(
            request=request,
            status_code=400,
            error_code=INVALID_VALIDATION_REQUEST,
            message="Validation mode is invalid for the requested tax domain.",
            reason=INVALID_VALIDATION_REQUEST,
            context={"mode": mode_raw, "tax_domain": tax_domain},
        )
    fields_value = source.get("fields")
    if not isinstance(fields_value, Mapping):
        raise _http_error(
            request=request,
            status_code=400,
            error_code=INVALID_VALIDATION_REQUEST,
            message="Validation field `fields` must be an object.",
            reason=INVALID_VALIDATION_REQUEST,
        )
    return ValidationRequestModel(
        return_id=return_id,
        tax_domain=tax_domain,
        mode=cast(ValidationMode, mode_raw),
        fields=cast(Mapping[str, object], fields_value),
    )


def _required_string(
    *,
    request: Request,
    source: Mapping[str, object],
    field_name: str,
) -> str:
    value = source.get(field_name)
    if isinstance(value, str):
        normalized = value.strip()
        if normalized:
            return normalized
    raise _http_error(
        request=request,
        status_code=400,
        error_code=INVALID_VALIDATION_REQUEST,
        message=f"Validation request field `{field_name}` is invalid.",
        reason=INVALID_VALIDATION_REQUEST,
    )


def _as_object(*, request: Request, payload: object) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise _http_error(
            request=request,
            status_code=400,
            error_code=INVALID_VALIDATION_REQUEST,
            message="Validation request payload is invalid.",
            reason=INVALID_VALIDATION_REQUEST,
        )
    source = cast(Mapping[object, object], payload)
    normalized = {str(key): source[key] for key in source}
    request.scope["validation_request_payload"] = normalized
    return normalized


def _enforce_internal_validation_boundary(*, request: Request) -> None:
    config = _config(request)
    if config.runtime_mode != "production":
        return
    if config.internal_api_key is None:
        raise _http_error(
            request=request,
            status_code=503,
            error_code="validation_internal_boundary_unavailable",
            message="Validation internal boundary is unavailable.",
            reason="validation_internal_boundary_unavailable",
        )
    provided_key = request.headers.get(VALIDATION_INTERNAL_API_KEY_HEADER, "").strip()
    if provided_key != config.internal_api_key:
        raise _http_error(
            request=request,
            status_code=403,
            error_code="validation_internal_boundary_forbidden",
            message="Validation runtime is restricted to governed internal callers.",
            reason="validation_internal_boundary_forbidden",
        )


def _error_envelope(
    *,
    request: Request,
    error_code: str,
    message: str,
    reason: str,
    context: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "error_code": error_code,
        "message": message,
        "reason": reason,
        "reason_code": reason,
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
    context: dict[str, object] | None = None,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=_error_envelope(
            request=request,
            error_code=error_code,
            message=message,
            reason=reason,
            context=context,
        ),
    )


async def _handle_request_validation_error(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    _ = exc
    audit_evidence = _emit_validation_failure_audit(
        request=request,
        error_code=INVALID_VALIDATION_REQUEST,
        reason=INVALID_VALIDATION_REQUEST,
        context=None,
    )
    return JSONResponse(
        status_code=400,
        content={
            "detail": _error_envelope(
                request=request,
                error_code=INVALID_VALIDATION_REQUEST,
                message="Validation request payload is invalid.",
                reason=INVALID_VALIDATION_REQUEST,
                context={"audit_evidence": audit_evidence},
            )
        },
    )


async def _handle_http_exception_error(
    request: Request,
    exc: HTTPException,
) -> JSONResponse:
    detail = cast(dict[str, object], exc.detail) if isinstance(exc.detail, dict) else {}
    detail_context = cast(dict[str, object] | None, detail.get("context"))
    audit_evidence = _emit_validation_failure_audit(
        request=request,
        error_code=str(detail.get("error_code", INVALID_VALIDATION_REQUEST)),
        reason=str(detail.get("reason", INVALID_VALIDATION_REQUEST)),
        context=detail_context,
    )
    merged_context: dict[str, object] = {}
    if detail_context is not None:
        merged_context.update(detail_context)
    merged_context["audit_evidence"] = audit_evidence
    envelope = _error_envelope(
        request=request,
        error_code=str(detail.get("error_code", INVALID_VALIDATION_REQUEST)),
        message=str(detail.get("message", "Validation request failed.")),
        reason=str(detail.get("reason", INVALID_VALIDATION_REQUEST)),
        context=merged_context,
    )
    return JSONResponse(status_code=exc.status_code, content={"detail": envelope})


async def _handle_starlette_http_exception_error(
    request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    _ = exc
    audit_evidence = _emit_validation_failure_audit(
        request=request,
        error_code=UNSUPPORTED_VALIDATION_SCOPE,
        reason=UNSUPPORTED_VALIDATION_SCOPE,
        context={"requested_path": request.url.path},
    )
    return JSONResponse(
        status_code=404,
        content={
            "detail": _error_envelope(
                request=request,
                error_code=UNSUPPORTED_VALIDATION_SCOPE,
                message="Requested validation path is not supported.",
                reason=UNSUPPORTED_VALIDATION_SCOPE,
                context={
                    "requested_path": request.url.path,
                    "audit_evidence": audit_evidence,
                },
            )
        },
    )


def _config(request: Request) -> ValidationConfig:
    return cast(ValidationConfig, request.app.state.validation_config)


def _emit_validation_failure_audit(
    *,
    request: Request,
    error_code: str,
    reason: str,
    context: dict[str, object] | None,
) -> dict[str, str]:
    request_payload = cast(dict[str, object], request.scope.get("validation_request_payload", {}))
    event = build_validation_failure_audit_event(
        correlation_id=get_correlation_id(request),
        trace_id=get_trace_id(request),
        error_code=error_code,
        reason=reason,
        return_id=_optional_context_string(request_payload.get("return_id")),
        tax_domain=_optional_context_string(request_payload.get("tax_domain")),
        mode=_optional_context_string(request_payload.get("mode")),
        context=context,
    )
    stored_event = emit_validation_audit_event(
        store=request.app.state.validation_audit_store,
        event=event,
    )
    return build_validation_audit_evidence(stored_event).to_dict()


def _optional_context_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


app = create_app()
