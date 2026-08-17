"""Expose dummy gateway endpoints for tool flows."""

import os
from uuid import UUID
from typing import cast
from typing import Protocol
from typing import Annotated
from pathlib import Path as PathlibPath

import httpx
from dotenv import load_dotenv
from fastapi import Depends
from fastapi import FastAPI
from fastapi import Request
from fastapi import APIRouter
from fastapi import HTTPException
from pydantic import BaseModel
from fastapi.responses import Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from fastapi.middleware.cors import CORSMiddleware

from shared.authz.rbac import Principal
from shared.authz.rbac import AUTH_CONTEXT_HEADER_NAME
from shared.authz.rbac import build_authorized_principal_dependency
from shared.tracing.correlation import get_trace_id
from shared.tracing.correlation import get_correlation_id
from shared.tracing.correlation import TRACE_ID_HEADER_NAME
from shared.tracing.correlation import CorrelationIdMiddleware
from shared.tracing.correlation import CORRELATION_ID_HEADER_NAME
from shared.idempotency.idempotency import require_idempotency_key

load_dotenv(dotenv_path=PathlibPath(__file__).parent.parent.parent.parent / ".env")

ROUTER = APIRouter()
DEFAULT_EVENT_STORE_BASE_URL = "http://event-store"
DEFAULT_ORCHESTRATION_BASE_URL = "http://orchestration"
require_gateway_auth_context = build_authorized_principal_dependency()
require_orchestration_gateway_principal = build_authorized_principal_dependency(
    allowed_roles=frozenset({"IndividualTaxpayer", "TaxAgent", "Accountant"}),
    allowed_delegated_roles=frozenset({"TaxAgent", "Accountant"}),
    required_tenant_id=None,
    allow_delegation=True,
)
INVALID_TAX_DOMAIN = "invalid_tax_domain"
UNSUPPORTED_TAX_DOMAIN_PATH = "unsupported_tax_domain_path"
ACTIVE_ORCHESTRATION_LED_BOUNDARY = "active_orchestration_led_boundary"
RECOGNIZED_GATEWAY_TAX_DOMAINS: dict[str, str] = {
    "income-tax": "income-tax",
    "income_tax": "income-tax",
    "health-contribution": "health-contribution",
    "health_contribution": "health-contribution",
    "vat": "vat",
    "withholding-tax": "withholding-tax",
    "withholding_tax": "withholding-tax",
    "corporate-tax": "corporate-tax",
    "corporate_tax": "corporate-tax",
    "payroll": "payroll",
    "paye": "payroll",
}


class AuditEventAppendRequest(BaseModel):
    """Represent the gateway-to-event-store audit payload.

    :param event_type: Event type identifier.
    :param user_id: Subject user ID.
    :param correlation_id: Correlation ID for traceability.
    :param idempotency_key: Idempotency key from incoming request.
    """

    event_type: str
    user_id: UUID
    trace_id: str
    correlation_id: str
    idempotency_key: str


class AuditEventAppendResponse(BaseModel):
    """Represent event-store append response payload.

    :param event_id: Generated audit event ID.
    :param correlation_id: Correlation ID for traceability.
    """

    event_id: UUID
    correlation_id: str


class ToolPingResponse(BaseModel):
    """Represent the dummy tool ping response payload.

    :param ok: Whether the flow succeeded.
    :param event_id: Generated audit event ID.
    :param correlation_id: Correlation ID for traceability.
    """

    ok: bool
    event_id: UUID
    correlation_id: str


class AuditClientProtocol(Protocol):
    """Define the audit append client contract for dependency injection."""

    async def append_audit_event(
        self,
        payload: AuditEventAppendRequest,
        auth_context_header: str,
    ) -> AuditEventAppendResponse:
        """Append an audit event via event-store.

        :param payload: Audit append request payload.
        :param auth_context_header: Canonical auth context header value.
        :return: Event-store append response.
        """

        ...


class HttpEventStoreAuditClient:
    """Call event-store over HTTP for audit append operations.

    :param base_url: Event-store base URL.
    """

    def __init__(self, base_url: str = DEFAULT_EVENT_STORE_BASE_URL) -> None:
        self._base_url = base_url

    async def append_audit_event(
        self,
        payload: AuditEventAppendRequest,
        auth_context_header: str,
    ) -> AuditEventAppendResponse:
        """Append an audit event in event-store over HTTP.

        :param payload: Audit append request payload.
        :param auth_context_header: Canonical auth context header value.
        :return: Parsed event-store append response.
        """

        headers = {
            AUTH_CONTEXT_HEADER_NAME: auth_context_header,
            CORRELATION_ID_HEADER_NAME: payload.correlation_id,
            TRACE_ID_HEADER_NAME: payload.trace_id,
        }
        async with httpx.AsyncClient(base_url=self._base_url, timeout=5.0) as client:
            response = await client.post(
                "/audit/append",
                json=payload.model_dump(mode="json"),
                headers=headers,
            )
        response.raise_for_status()
        return AuditEventAppendResponse.model_validate(response.json())


def get_audit_client(request: Request) -> AuditClientProtocol:
    """Resolve the configured audit client dependency.

    :param request: Active HTTP request.
    :return: Audit client implementation.
    """

    configured_client = getattr(request.app.state, "audit_client", None)
    if configured_client is not None:
        return cast(AuditClientProtocol, configured_client)

    return HttpEventStoreAuditClient()


@ROUTER.post("/tools/ping", response_model=ToolPingResponse)
async def ping_tool(
    request: Request,
    principal: Annotated[Principal, Depends(require_gateway_auth_context)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    audit_client: Annotated[AuditClientProtocol, Depends(get_audit_client)],
) -> ToolPingResponse:
    """Execute a dummy tool call and append an audit event.

    :param request: Active HTTP request.
    :param principal: Parsed authenticated principal dependency.
    :param idempotency_key: Validated idempotency key dependency.
    :param audit_client: Injected audit append client.
    :return: Dummy tool ping response.
    """

    correlation_id = get_correlation_id(request)
    trace_id = get_trace_id(request)
    auth_context_header = request.headers.get(AUTH_CONTEXT_HEADER_NAME, "")
    append_payload = AuditEventAppendRequest(
        event_type="tool.ping",
        user_id=principal.user_id,
        trace_id=trace_id,
        correlation_id=correlation_id,
        idempotency_key=idempotency_key,
    )
    append_response = await audit_client.append_audit_event(
        payload=append_payload,
        auth_context_header=auth_context_header,
    )
    return ToolPingResponse(
        ok=True,
        event_id=append_response.event_id,
        correlation_id=correlation_id,
    )


@ROUTER.post("/v1/orchestration/prompt/decide")
async def forward_orchestration_decide(
    request: Request,
    principal: Annotated[Principal, Depends(require_orchestration_gateway_principal)],
) -> Response:
    """Validate and forward a trusted decision request without changing its context."""

    _ = principal
    return await _forward_orchestration_request(request=request, stream=False)


@ROUTER.post("/v1/orchestration/prompt/execute")
async def forward_orchestration_execute(
    request: Request,
    principal: Annotated[Principal, Depends(require_orchestration_gateway_principal)],
) -> Response:
    """Validate and forward a trusted execution request without changing its context."""

    _ = principal
    return await _forward_orchestration_request(request=request, stream=False)


@ROUTER.post("/v1/orchestration/prompt/execute/stream")
async def forward_orchestration_execute_stream(
    request: Request,
    principal: Annotated[Principal, Depends(require_orchestration_gateway_principal)],
) -> StreamingResponse:
    """Validate and relay orchestration server-sent events without buffering."""

    _ = principal
    response = await _forward_orchestration_stream(request=request)
    return response


def _orchestration_forward_headers(request: Request) -> dict[str, str]:
    """Select only trusted forwarding headers, retaining the auth envelope verbatim."""

    names = (
        AUTH_CONTEXT_HEADER_NAME,
        CORRELATION_ID_HEADER_NAME,
        TRACE_ID_HEADER_NAME,
        "Idempotency-Key",
        "Content-Type",
        "Accept",
    )
    return {name: request.headers[name] for name in names if name in request.headers}


def _orchestration_target_path(request: Request) -> str:
    base_url = os.getenv("GATEWAY_ORCHESTRATION_BASE_URL", DEFAULT_ORCHESTRATION_BASE_URL)
    return f"{base_url.rstrip('/')}{request.url.path}"


async def _forward_orchestration_request(*, request: Request, stream: bool) -> Response:
    """Forward a non-streaming request while retaining the downstream envelope."""

    _ = stream
    async with httpx.AsyncClient(timeout=30.0) as client:
        upstream = await client.request(
            method=request.method,
            url=_orchestration_target_path(request),
            content=await request.body(),
            headers=_orchestration_forward_headers(request),
        )
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )


async def _forward_orchestration_stream(*, request: Request) -> StreamingResponse:
    """Open an upstream SSE relay and close its client after the response ends."""

    client = httpx.AsyncClient(timeout=None)
    upstream_request = client.build_request(
        method=request.method,
        url=_orchestration_target_path(request),
        content=await request.body(),
        headers=_orchestration_forward_headers(request),
    )
    upstream = await client.send(upstream_request, stream=True)
    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "text/event-stream"),
        background=BackgroundTask(_close_orchestration_stream, client, upstream),
    )


async def _close_orchestration_stream(client: httpx.AsyncClient, upstream: httpx.Response) -> None:
    await upstream.aclose()
    await client.aclose()


@ROUTER.api_route(
    "/v1/gateway/{scope}/{remaining_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
async def gateway_scope_guard(
    request: Request,
    scope: str,
    remaining_path: str,
) -> dict[str, object]:
    """Fail closed for recognized but non-implemented direct gateway tax-domain paths."""

    normalized_scope = _normalize_gateway_tax_domain(scope)
    requested_path = f"/v1/gateway/{scope}/{remaining_path}".rstrip("/")
    if normalized_scope is None:
        raise _gateway_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_TAX_DOMAIN,
            message="Requested tax domain is not recognized by the gateway boundary.",
            reason=INVALID_TAX_DOMAIN,
            details={
                "requested_path": requested_path,
                "tax_domain": scope.strip().lower() or "unknown",
            },
        )

    details: dict[str, object] = {
        "requested_path": requested_path,
        "tax_domain": normalized_scope,
    }
    if normalized_scope == "health-contribution":
        details["supported_execution_boundary"] = "orchestration"
        raise _gateway_http_error(
            request=request,
            status_code=501,
            error_code=UNSUPPORTED_TAX_DOMAIN_PATH,
            message=(
                "Gateway does not execute health-contribution tax-domain paths directly. "
                "The governed supported path is orchestration-led."
            ),
            reason=ACTIVE_ORCHESTRATION_LED_BOUNDARY,
            details=details,
        )

    raise _gateway_http_error(
        request=request,
        status_code=501,
        error_code=UNSUPPORTED_TAX_DOMAIN_PATH,
        message="Requested tax-domain path is not implemented at the gateway boundary.",
        reason=UNSUPPORTED_TAX_DOMAIN_PATH,
        details=details,
    )


def create_app() -> FastAPI:
    """Build the gateway FastAPI application.

    :return: Configured FastAPI app.
    """

    app = FastAPI()
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
    app.include_router(ROUTER)
    return app


app = create_app()


def _normalize_gateway_tax_domain(scope: str) -> str | None:
    return RECOGNIZED_GATEWAY_TAX_DOMAINS.get(scope.strip().lower())


def _gateway_http_error(
    *,
    request: Request,
    status_code: int,
    error_code: str,
    message: str,
    reason: str,
    details: dict[str, object] | None = None,
) -> HTTPException:
    payload: dict[str, object] = {
        "error_code": error_code,
        "message": message,
        "reason": reason,
        "trace_id": get_trace_id(request),
        "correlation_id": get_correlation_id(request),
    }
    if details:
        payload["details"] = details
    return HTTPException(status_code=status_code, detail=payload)
