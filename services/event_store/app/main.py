"""Event-store runtime with persistent append-only audit storage."""

import base64
import json
from pathlib import Path as PathlibPath
from uuid import UUID
from typing import Any
from typing import cast
from typing import Annotated
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from fastapi import Depends
from fastapi import FastAPI
from fastapi import Request
from fastapi import APIRouter
from fastapi import HTTPException
from pydantic import BaseModel
from pydantic import field_validator
from dotenv import load_dotenv
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.middleware.cors import CORSMiddleware

from shared.authz.rbac import Principal
from shared.authz.rbac import build_authorized_principal_dependency
from shared.tracing.correlation import get_trace_id
from shared.tracing.correlation import get_correlation_id
from shared.tracing.correlation import TRACE_ID_HEADER_NAME
from shared.tracing.correlation import CorrelationIdMiddleware
from shared.tracing.correlation import CORRELATION_ID_HEADER_NAME
from services.event_store.app.models import PersistedAuditEvent
from services.event_store.app.repository import APPEND_CONFLICT
from services.event_store.app.repository import ARCHIVAL_FORBIDDEN
from services.event_store.app.repository import ARCHIVAL_NOT_FOUND
from services.event_store.app.repository import ARCHIVAL_INELIGIBLE
from services.event_store.app.repository import EventStoreRepository
from services.event_store.app.repository import QUERY_CURSOR_INVALID
from services.event_store.app.repository import QUERY_SCOPE_FORBIDDEN
from services.event_store.app.repository import INTEGRITY_CHECK_FAILED
from services.event_store.app.repository import RETENTION_POLICY_INVALID
from services.event_store.app.repository import EventStoreRepositoryError
from services.event_store.app.repository import PERSISTENCE_NOT_CONFIGURED
from services.event_store.app.repository import get_default_event_store_repository

load_dotenv(dotenv_path=PathlibPath(__file__).parent.parent.parent.parent / ".env")

ROUTER = APIRouter()
SERVICE_NAME = "event_store"
SERVICE_VERSION = "0.1.0"
EVENT_STORE_ALLOWED_ROLES = frozenset({"IndividualTaxpayer", "TaxAgent", "Accountant"})
EVENT_STORE_ALLOWED_DELEGATED_ROLES = frozenset({"TaxAgent", "Accountant"})
require_event_store_principal = build_authorized_principal_dependency(
    allowed_roles=EVENT_STORE_ALLOWED_ROLES,
    allowed_delegated_roles=EVENT_STORE_ALLOWED_DELEGATED_ROLES,
    allow_delegation=True,
)
_visible_events_floor = datetime.fromtimestamp(0, tz=UTC)


class AuditEventAppendRequest(BaseModel):
    """Represent one append request payload for audit event persistence."""

    event_type: str
    user_id: UUID | None
    trace_id: str | None = None
    correlation_id: str
    idempotency_key: str
    resource_id: UUID | None = None
    details: dict[str, object] | None = None

    @field_validator("idempotency_key")
    @classmethod
    def _validate_idempotency_key(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("idempotency_key must be a non-empty string.")
        if len(normalized) > 128:
            raise ValueError("idempotency_key must be at most 128 characters.")
        return normalized


class AuditEventAppendResponse(BaseModel):
    """Represent append response payload."""

    event_id: UUID
    correlation_id: str


class AuditRetentionEligibleEvent(BaseModel):
    """Represent one retention-eligible immutable event."""

    event_id: UUID
    user_id: UUID
    correlation_id: str
    trace_id: str
    created_at: str
    retention_expires_at: str
    retention_policy_code: str
    retention_days: int


class AuditRetentionEligibleResponse(BaseModel):
    """Represent deterministic retention-eligibility response payload."""

    as_of: str
    events: tuple[AuditRetentionEligibleEvent, ...]


class AuditEventArchiveRequest(BaseModel):
    """Represent one archival transition request payload."""

    event_id: UUID
    reason_code: str
    archived_at: datetime | None = None


class AuditEventArchiveResponse(BaseModel):
    """Represent deterministic archival transition response payload."""

    status: str
    event_id: UUID
    user_id: UUID
    correlation_id: str
    archived_at: str
    archival_reason_code: str


class AuditEventQueryEnvelope(BaseModel):
    """Represent one query/replay event item."""

    event_id: UUID
    event_type: str
    action_type: str
    user_id: UUID | None
    trace_id: str
    correlation_id: str
    idempotency_key: str
    created_at: str
    previous_event_checksum: str | None
    event_checksum: str
    is_delegated: bool
    principal_user_id: UUID | None
    delegate_user_id: UUID | None
    delegation_id: UUID | None


class AuditEventQueryResponse(BaseModel):
    """Represent deterministic event query/replay response payload."""

    tenant_id: str
    user_id: UUID | None
    correlation_id: str | None
    limit: int
    next_cursor: str | None
    events: tuple[AuditEventQueryEnvelope, ...]


class AuditEventIntegrityVerificationResponse(BaseModel):
    """Represent deterministic integrity verification response payload."""

    tenant_id: str
    user_id: UUID | None
    correlation_id: str | None
    limit: int
    algorithm: str
    verified_event_count: int
    verified_through_event_id: UUID | None
    next_cursor: str | None


def create_app() -> FastAPI:
    """Build event-store runtime app."""

    app = FastAPI(title=SERVICE_NAME, version=SERVICE_VERSION)
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
    app.include_router(ROUTER)
    return app


def get_event_store_repository(request: Request) -> EventStoreRepository:
    """Resolve configured repository dependency."""

    configured = getattr(request.app.state, "event_store_repository", None)
    if isinstance(configured, EventStoreRepository):
        return configured
    repository = get_default_event_store_repository()
    request.app.state.event_store_repository = repository
    return repository


@ROUTER.post("/audit/append", response_model=AuditEventAppendResponse)
async def append_audit_event(
    request: Request,
    payload: AuditEventAppendRequest,
    principal: Annotated[Principal, Depends(require_event_store_principal)],
    repository: Annotated[EventStoreRepository, Depends(get_event_store_repository)],
) -> AuditEventAppendResponse:
    """Append immutable audit event in persistent append-only storage."""

    inbound_correlation_id = request.headers.get(CORRELATION_ID_HEADER_NAME)
    inbound_trace_id = request.headers.get(TRACE_ID_HEADER_NAME)
    if inbound_correlation_id is not None or inbound_trace_id is not None:
        correlation_id = get_correlation_id(request)
        trace_id = get_trace_id(request)
    else:
        correlation_id = payload.correlation_id.strip() or get_correlation_id(request)
        trace_id = (payload.trace_id or "").strip() or get_trace_id(request)

    try:
        persisted = repository.append_event(
            event_type=payload.event_type,
            user_id=payload.user_id,
            role_at_time=principal.role,
            trace_id=trace_id,
            correlation_id=correlation_id,
            idempotency_key=payload.idempotency_key,
            is_delegated=principal.delegation_context.is_delegated,
            principal_user_id=principal.delegation_context.principal_user_id,
            delegate_user_id=principal.delegation_context.delegate_user_id,
            delegation_id=principal.delegation_context.delegation_id,
            event_timestamp=datetime.now(UTC),
            details=payload.details,
            resource_id=payload.resource_id,
        )
    except EventStoreRepositoryError as error:
        status_code = 503
        if error.reason_code == APPEND_CONFLICT:
            status_code = 409
        if error.reason_code == PERSISTENCE_NOT_CONFIGURED:
            status_code = 500
        if error.reason_code == RETENTION_POLICY_INVALID:
            status_code = 500
        raise _http_error(
            request=request,
            status_code=status_code,
            error_code=error.reason_code,
            message=error.message,
            reason=error.reason_code,
            reason_code=error.reason_code,
        ) from error

    return AuditEventAppendResponse(
        event_id=persisted.event_id,
        correlation_id=correlation_id,
    )


@ROUTER.get("/audit/retention/eligible", response_model=AuditRetentionEligibleResponse)
async def list_retention_eligible_events(
    request: Request,
    principal: Annotated[Principal, Depends(require_event_store_principal)],
    repository: Annotated[EventStoreRepository, Depends(get_event_store_repository)],
    limit: int = 50,
    as_of: datetime | None = None,
) -> AuditRetentionEligibleResponse:
    """List deterministic archival-eligible events in scoped principal view."""

    scoped_as_of = (as_of or datetime.now(UTC)).astimezone(UTC)
    try:
        events = repository.list_retention_eligible_events(
            as_of=scoped_as_of,
            limit=limit,
            allowed_user_ids=_allowed_user_ids(principal=principal),
        )
    except EventStoreRepositoryError as error:
        status_code = 503
        if error.reason_code in {"invalid_event_store_request", ARCHIVAL_FORBIDDEN}:
            status_code = 400 if error.reason_code == "invalid_event_store_request" else 403
        if error.reason_code == PERSISTENCE_NOT_CONFIGURED:
            status_code = 500
        raise _http_error(
            request=request,
            status_code=status_code,
            error_code=error.reason_code,
            message=error.message,
            reason=error.reason_code,
            reason_code=error.reason_code,
        ) from error

    return AuditRetentionEligibleResponse(
        as_of=scoped_as_of.isoformat().replace("+00:00", "Z"),
        events=tuple(
            AuditRetentionEligibleEvent(
                event_id=item.event_id,
                user_id=cast(UUID, item.user_id),
                correlation_id=item.correlation_id,
                trace_id=item.trace_id,
                created_at=item.created_at,
                retention_expires_at=item.retention_expires_at,
                retention_policy_code=item.retention_policy_code,
                retention_days=item.retention_days,
            )
            for item in events
        ),
    )


@ROUTER.post("/audit/archival/mark", response_model=AuditEventArchiveResponse)
async def mark_event_archived(
    request: Request,
    payload: AuditEventArchiveRequest,
    principal: Annotated[Principal, Depends(require_event_store_principal)],
    repository: Annotated[EventStoreRepository, Depends(get_event_store_repository)],
) -> AuditEventArchiveResponse:
    """Mark one immutable event as archived when retention eligibility is satisfied."""

    archived_at = (payload.archived_at or datetime.now(UTC)).astimezone(UTC)
    try:
        archived = repository.mark_event_archived(
            event_id=payload.event_id,
            archived_at=archived_at,
            reason_code=payload.reason_code.strip() or "retention_expired",
            archived_by_user_id=principal.user_id,
            allowed_user_ids=_allowed_user_ids(principal=principal),
        )
    except EventStoreRepositoryError as error:
        status_code = 503
        if error.reason_code == ARCHIVAL_INELIGIBLE:
            status_code = 409
        if error.reason_code == ARCHIVAL_NOT_FOUND:
            status_code = 404
        if error.reason_code == ARCHIVAL_FORBIDDEN:
            status_code = 403
        if error.reason_code == PERSISTENCE_NOT_CONFIGURED:
            status_code = 500
        raise _http_error(
            request=request,
            status_code=status_code,
            error_code=error.reason_code,
            message=error.message,
            reason=error.reason_code,
            reason_code=error.reason_code,
        ) from error

    return AuditEventArchiveResponse(
        status=archived.status,
        event_id=archived.event_id,
        user_id=archived.user_id,
        correlation_id=archived.correlation_id,
        archived_at=archived.archived_at,
        archival_reason_code=archived.archival_reason_code,
    )


@ROUTER.get("/audit/events", response_model=AuditEventQueryResponse)
async def query_audit_events(
    request: Request,
    principal: Annotated[Principal, Depends(require_event_store_principal)],
    repository: Annotated[EventStoreRepository, Depends(get_event_store_repository)],
    tenant_id: str,
    user_id: UUID | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> AuditEventQueryResponse:
    """Query deterministic scoped events with stable pagination semantics."""

    return _query_events_response(
        request=request,
        principal=principal,
        repository=repository,
        tenant_id=tenant_id,
        user_id=user_id,
        correlation_id=None,
        limit=limit,
        cursor=cursor,
    )


@ROUTER.get("/audit/replay/{correlation_id}", response_model=AuditEventQueryResponse)
async def replay_audit_events_by_correlation(
    request: Request,
    correlation_id: str,
    principal: Annotated[Principal, Depends(require_event_store_principal)],
    repository: Annotated[EventStoreRepository, Depends(get_event_store_repository)],
    tenant_id: str,
    user_id: UUID | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> AuditEventQueryResponse:
    """Replay deterministic scoped events for one correlation identifier."""

    normalized_correlation = correlation_id.strip()
    if not normalized_correlation:
        raise _http_error(
            request=request,
            status_code=400,
            error_code="invalid_event_store_request",
            message="Correlation identifier must be a non-empty string.",
            reason="invalid_event_store_request",
            reason_code="invalid_event_store_request",
        )
    return _query_events_response(
        request=request,
        principal=principal,
        repository=repository,
        tenant_id=tenant_id,
        user_id=user_id,
        correlation_id=normalized_correlation,
        limit=limit,
        cursor=cursor,
    )


@ROUTER.get("/audit/integrity/verify", response_model=AuditEventIntegrityVerificationResponse)
async def verify_audit_event_integrity(
    request: Request,
    principal: Annotated[Principal, Depends(require_event_store_principal)],
    repository: Annotated[EventStoreRepository, Depends(get_event_store_repository)],
    tenant_id: str,
    user_id: UUID | None = None,
    correlation_id: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> AuditEventIntegrityVerificationResponse:
    """Verify deterministic integrity checksum/hash-chain for one scoped page."""

    if tenant_id.strip() != principal.tenant_id:
        raise _http_error(
            request=request,
            status_code=403,
            error_code=QUERY_SCOPE_FORBIDDEN,
            message="Requested tenant scope is forbidden for this principal.",
            reason=QUERY_SCOPE_FORBIDDEN,
            reason_code=QUERY_SCOPE_FORBIDDEN,
        )

    try:
        cursor_created_at, cursor_event_id = _decode_query_cursor(cursor=cursor)
    except ValueError as error:
        raise _http_error(
            request=request,
            status_code=400,
            error_code=QUERY_CURSOR_INVALID,
            message="Pagination cursor is invalid.",
            reason=QUERY_CURSOR_INVALID,
            reason_code=QUERY_CURSOR_INVALID,
            details={"cursor": cursor or ""},
        ) from error

    try:
        result = repository.verify_integrity_page(
            allowed_user_ids=_allowed_user_ids(principal=principal),
            user_id=user_id,
            correlation_id=correlation_id,
            limit=limit,
            cursor_created_at=cursor_created_at,
            cursor_event_id=cursor_event_id,
        )
    except EventStoreRepositoryError as error:
        status_code = 503
        if error.reason_code in {"invalid_event_store_request", QUERY_CURSOR_INVALID}:
            status_code = 400
        if error.reason_code == QUERY_SCOPE_FORBIDDEN:
            status_code = 403
        if error.reason_code == INTEGRITY_CHECK_FAILED:
            status_code = 409
        if error.reason_code == PERSISTENCE_NOT_CONFIGURED:
            status_code = 500
        raise _http_error(
            request=request,
            status_code=status_code,
            error_code=error.reason_code,
            message=error.message,
            reason=error.reason_code,
            reason_code=error.reason_code,
        ) from error

    return AuditEventIntegrityVerificationResponse(
        tenant_id=principal.tenant_id,
        user_id=user_id,
        correlation_id=correlation_id,
        limit=limit,
        algorithm=result.algorithm,
        verified_event_count=result.verified_event_count,
        verified_through_event_id=result.verified_through_event_id,
        next_cursor=_encode_query_cursor(
            created_at=result.next_cursor_created_at,
            event_id=result.next_cursor_event_id,
        ),
    )


def get_audit_events() -> tuple[PersistedAuditEvent, ...]:
    """Return deterministic snapshot of persisted events after reset floor."""

    repository = get_default_event_store_repository()
    return repository.list_events_since(created_at_floor=_visible_events_floor)


def reset_audit_events() -> None:
    """Advance visibility floor to preserve append-only semantics in tests."""

    global _visible_events_floor
    repository = get_default_event_store_repository()
    try:
        latest_created_at = repository.latest_created_at()
    except EventStoreRepositoryError:
        latest_created_at = None
    if latest_created_at is None:
        _visible_events_floor = datetime.now(UTC)
        return
    _visible_events_floor = latest_created_at + timedelta(microseconds=1)


def _allowed_user_ids(*, principal: Principal) -> tuple[UUID, ...]:
    ids = {principal.user_id}
    delegation_principal = principal.delegation_context.principal_user_id
    if delegation_principal is not None:
        ids.add(delegation_principal)
    return tuple(sorted(ids, key=str))


def _query_events_response(
    *,
    request: Request,
    principal: Principal,
    repository: EventStoreRepository,
    tenant_id: str,
    user_id: UUID | None,
    correlation_id: str | None,
    limit: int,
    cursor: str | None,
) -> AuditEventQueryResponse:
    if tenant_id.strip() != principal.tenant_id:
        raise _http_error(
            request=request,
            status_code=403,
            error_code=QUERY_SCOPE_FORBIDDEN,
            message="Requested tenant scope is forbidden for this principal.",
            reason=QUERY_SCOPE_FORBIDDEN,
            reason_code=QUERY_SCOPE_FORBIDDEN,
        )

    cursor_created_at: datetime | None
    cursor_event_id: UUID | None
    try:
        cursor_created_at, cursor_event_id = _decode_query_cursor(cursor=cursor)
    except ValueError as error:
        raise _http_error(
            request=request,
            status_code=400,
            error_code=QUERY_CURSOR_INVALID,
            message="Pagination cursor is invalid.",
            reason=QUERY_CURSOR_INVALID,
            reason_code=QUERY_CURSOR_INVALID,
            details={"cursor": cursor or ""},
        ) from error

    try:
        page = repository.query_events_page(
            allowed_user_ids=_allowed_user_ids(principal=principal),
            user_id=user_id,
            correlation_id=correlation_id,
            limit=limit,
            cursor_created_at=cursor_created_at,
            cursor_event_id=cursor_event_id,
        )
    except EventStoreRepositoryError as error:
        status_code = 503
        if error.reason_code in {"invalid_event_store_request", QUERY_CURSOR_INVALID}:
            status_code = 400
        if error.reason_code == QUERY_SCOPE_FORBIDDEN:
            status_code = 403
        if error.reason_code == PERSISTENCE_NOT_CONFIGURED:
            status_code = 500
        raise _http_error(
            request=request,
            status_code=status_code,
            error_code=error.reason_code,
            message=error.message,
            reason=error.reason_code,
            reason_code=error.reason_code,
        ) from error

    return AuditEventQueryResponse(
        tenant_id=principal.tenant_id,
        user_id=user_id,
        correlation_id=correlation_id,
        limit=limit,
        next_cursor=_encode_query_cursor(
            created_at=page.next_cursor_created_at,
            event_id=page.next_cursor_event_id,
        ),
        events=tuple(
            AuditEventQueryEnvelope(
                event_id=item.event_id,
                event_type=item.event_type,
                action_type=item.action_type,
                user_id=item.user_id,
                trace_id=item.trace_id,
                correlation_id=item.correlation_id,
                idempotency_key=item.idempotency_key,
                created_at=item.created_at,
                previous_event_checksum=item.previous_event_checksum,
                event_checksum=item.event_checksum,
                is_delegated=item.is_delegated,
                principal_user_id=item.principal_user_id,
                delegate_user_id=item.delegate_user_id,
                delegation_id=item.delegation_id,
            )
            for item in page.events
        ),
    )


def _encode_query_cursor(*, created_at: str | None, event_id: UUID | None) -> str | None:
    if created_at is None or event_id is None:
        return None
    raw = json.dumps(
        {"created_at": created_at, "event_id": str(event_id)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("utf-8")


def _decode_query_cursor(*, cursor: str | None) -> tuple[datetime | None, UUID | None]:
    if cursor is None or not cursor.strip():
        return None, None
    try:
        payload = base64.urlsafe_b64decode(cursor.encode("utf-8")).decode("utf-8")
        parsed = json.loads(payload)
    except (ValueError, json.JSONDecodeError) as error:
        raise ValueError("Invalid cursor payload.") from error
    if not isinstance(parsed, dict):
        raise ValueError("Invalid cursor payload.")
    parsed_map = cast(dict[str, object], parsed)
    created_at_raw = parsed_map.get("created_at")
    event_id_raw = parsed_map.get("event_id")
    if not isinstance(created_at_raw, str) or not isinstance(event_id_raw, str):
        raise ValueError("Invalid cursor payload.")
    try:
        created_at = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00")).astimezone(UTC)
        event_id = UUID(event_id_raw)
    except ValueError as error:
        raise ValueError("Invalid cursor payload.") from error
    return created_at, event_id


def _error_envelope(
    *,
    request: Request,
    error_code: str,
    message: str,
    reason: str,
    reason_code: str,
    details: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "error_code": error_code,
        "message": message,
        "reason": reason,
        "reason_code": reason_code,
        "trace_id": get_trace_id(request),
        "correlation_id": get_correlation_id(request),
        "details": details or {},
    }
    return payload


def _http_error(
    *,
    request: Request,
    status_code: int,
    error_code: str,
    message: str,
    reason: str,
    reason_code: str,
    details: dict[str, object] | None = None,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=_error_envelope(
            request=request,
            error_code=error_code,
            message=message,
            reason=reason,
            reason_code=reason_code,
            details=details,
        ),
    )


async def _handle_request_validation_error(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    _ = exc
    envelope = _error_envelope(
        request=request,
        error_code="invalid_event_store_request",
        message="Event-store request payload is invalid.",
        reason="invalid_event_store_request",
        reason_code="invalid_event_store_request",
    )
    return JSONResponse(status_code=400, content={"detail": envelope})


async def _handle_http_exception_error(
    request: Request,
    exc: HTTPException,
) -> JSONResponse:
    detail = cast(dict[str, object], exc.detail) if isinstance(exc.detail, dict) else {}
    envelope = _error_envelope(
        request=request,
        error_code=str(detail.get("error_code", "invalid_event_store_request")),
        message=str(detail.get("message", "Event-store request failed.")),
        reason=str(detail.get("reason", "invalid_event_store_request")),
        reason_code=str(
            detail.get("reason_code", detail.get("reason", "invalid_event_store_request"))
        ),
        details=cast(dict[str, object], detail.get("details", {})),
    )
    return JSONResponse(status_code=exc.status_code, content={"detail": envelope})


async def _handle_starlette_http_exception_error(
    request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    envelope = _error_envelope(
        request=request,
        error_code="unsupported_event_store_scope",
        message="Requested event-store path is not supported.",
        reason="unsupported_event_store_scope",
        reason_code="unsupported_event_store_scope",
    )
    return JSONResponse(status_code=exc.status_code, content={"detail": envelope})


app = create_app()
