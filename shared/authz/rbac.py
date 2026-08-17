"""Parse authenticated principal details from Authorization headers."""

from __future__ import annotations

import json
from uuid import UUID
from uuid import uuid4
from typing import cast
from datetime import datetime
from dataclasses import field
from dataclasses import dataclass
from collections.abc import Callable

from fastapi import Header
from fastapi import Request
from fastapi import HTTPException

from shared.errors import codes
from shared.errors.envelope import create_request_http_error
from shared.authz.delegation import DelegationContext
from shared.authz.delegation import DelegationPolicyError
from shared.authz.delegation import validate_delegation_context
from shared.tracing.correlation import get_trace_id
from shared.tracing.correlation import get_correlation_id
from shared.tracing.correlation import get_trace_context_reason

AUTHORIZATION_SCHEME = "Bearer"
AUTHORIZATION_HEADER_NAME = "Authorization"
AUTH_CONTEXT_HEADER_NAME = "X-Auth-Context"
AUTH_CONTEXT_SCHEMA_VERSION = "1.0.0"
_DEFAULT_TENANT_ID = "default_tenant"
_SUPPORTED_ROLES = {
    "IndividualTaxpayer",
    "TaxAgent",
    "Accountant",
    "Administrator",
}


class AuthContextValidationError(ValueError):
    """Represent deterministic canonical auth-context validation failure."""

    def __init__(
        self,
        *,
        error_code: str,
        message: str,
        reason: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.reason = reason
        self.details = details or {}

    def to_error_detail(self) -> dict[str, object]:
        """Return deterministic error detail payload."""

        return {
            "error_code": self.error_code,
            "message": self.message,
            "reason": self.reason,
            "details": self.details,
        }


class InvalidAuthorizationError(ValueError):
    """Represent an invalid authorization token.

    :param error_code: Stable machine-readable error code.
    :param message: Human-readable error description.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class Principal:
    """Represent the authenticated principal.

    :param user_id: Parsed user identifier.
    :param role: Parsed user role.
    """

    user_id: UUID
    role: str
    tenant_id: str = _DEFAULT_TENANT_ID
    session_id: UUID | None = None
    delegation_context: DelegationContext = field(default_factory=DelegationContext.not_delegated)


@dataclass(frozen=True)
class AuthContextEnvelope:
    """Represent canonical trusted auth context for service propagation."""

    user_id: UUID
    tenant_id: str
    role: str
    session_id: UUID
    delegation_context: DelegationContext


def build_authorized_principal_dependency(
    *,
    allowed_roles: frozenset[str] | None = None,
    allowed_delegated_roles: frozenset[str] | None = None,
    required_tenant_id: str | None = _DEFAULT_TENANT_ID,
    allow_delegation: bool = True,
) -> Callable[[Request, str | None], Principal]:
    """Build a canonical tenant/role authorization dependency for protected endpoints."""

    def _dependency(
        request: Request,
        auth_context_header: str | None = Header(default=None, alias=AUTH_CONTEXT_HEADER_NAME),
    ) -> Principal:
        if auth_context_header is None or not auth_context_header.strip():
            raise _build_authz_http_error(
                request=request,
                status_code=401,
                reason="auth_context_missing",
                message="Auth context header is required.",
                details={"header": AUTH_CONTEXT_HEADER_NAME},
            )

        try:
            payload = json.loads(auth_context_header)
        except json.JSONDecodeError as error:
            raise _build_authz_http_error(
                request=request,
                status_code=401,
                reason="auth_context_malformed",
                message="Auth context header is malformed JSON.",
                details={"header": AUTH_CONTEXT_HEADER_NAME},
            ) from error

        if not isinstance(payload, dict):
            raise _build_authz_http_error(
                request=request,
                status_code=401,
                reason="auth_context_invalid_claim",
                message="Auth context is invalid.",
                details={"claim": "auth_context", "expected_type": "object"},
            )

        payload_map = cast(dict[str, object], payload)
        schema_version = payload_map.get("schema_version")
        if not isinstance(schema_version, str):
            raise _build_authz_http_error(
                request=request,
                status_code=401,
                reason="auth_context_invalid_claim",
                message="Auth context is invalid.",
                details={"claim": "schema_version"},
            )
        if schema_version != AUTH_CONTEXT_SCHEMA_VERSION:
            raise _build_authz_http_error(
                request=request,
                status_code=403,
                reason="unsupported_auth_context_scope",
                message="Auth context scope is unsupported.",
                details={"schema_version": schema_version},
            )

        try:
            envelope = parse_auth_context_envelope(payload_map)
        except AuthContextValidationError as error:
            raise _build_authz_http_error(
                request=request,
                status_code=401,
                reason="auth_context_invalid_claim",
                message="Auth context is invalid.",
                details=error.details,
            ) from error

        if required_tenant_id is not None and envelope.tenant_id != required_tenant_id:
            reason = "authorization_tenant_forbidden"
            message = "Tenant access is forbidden."
            if envelope.delegation_context.is_delegated:
                reason = "delegation_tenant_mismatch"
                message = "Delegation tenant is invalid."
            raise _build_authz_http_error(
                request=request,
                status_code=403,
                reason=reason,
                message=message,
                details={"tenant_id": envelope.tenant_id},
            )

        principal = Principal(
            user_id=envelope.user_id,
            role=envelope.role,
            tenant_id=envelope.tenant_id,
            session_id=envelope.session_id,
            delegation_context=envelope.delegation_context,
        )

        try:
            validate_delegation_context(
                delegation_context=principal.delegation_context,
                user_id=principal.user_id,
                tenant_id=principal.tenant_id,
                # Dynamic-tenant callers still validate delegation against the
                # tenant cryptographically carried by the authenticated context.
                required_tenant_id=required_tenant_id or principal.tenant_id,
                allow_delegation=allow_delegation,
            )
        except DelegationPolicyError as error:
            raise _build_authz_http_error(
                request=request,
                status_code=403,
                reason=error.reason,
                message=error.message,
                details=error.details,
            ) from error

        if (
            principal.delegation_context.is_delegated
            and allowed_delegated_roles is not None
            and principal.role not in allowed_delegated_roles
        ):
            raise _build_authz_http_error(
                request=request,
                status_code=403,
                reason="delegation_role_forbidden",
                message="Delegated role is forbidden for this endpoint.",
                details={"role": principal.role},
            )

        if allowed_roles is not None and principal.role not in allowed_roles:
            raise _build_authz_http_error(
                request=request,
                status_code=403,
                reason="authorization_role_forbidden",
                message="Role is forbidden for this endpoint.",
                details={"role": principal.role},
            )

        return principal

    return _dependency


def _build_authz_http_error(
    *,
    request: Request,
    status_code: int,
    reason: str,
    message: str,
    details: dict[str, object] | None = None,
) -> HTTPException:
    trace_id = get_trace_id(request)
    correlation_id = get_correlation_id(request)
    trace_context_reason = get_trace_context_reason(request)
    error_details: dict[str, object] = dict(details or {})
    if trace_context_reason in {"trace_context_missing", "trace_context_invalid"}:
        error_details.setdefault("trace_context_reason", trace_context_reason)
    return HTTPException(
        status_code=status_code,
        detail={
            "error_code": reason,
            "message": message,
            "reason": reason,
            "trace_id": trace_id,
            "correlation_id": correlation_id,
            "details": error_details,
        },
    )


def parse_auth_context_envelope(payload: object) -> AuthContextEnvelope:
    """Parse and validate canonical auth-context envelope fail-closed."""

    if not isinstance(payload, dict):
        raise AuthContextValidationError(
            error_code="auth_context_invalid_claim_type",
            message="Auth context envelope is invalid.",
            reason="auth_context_invalid_claim_type",
            details={"claim": "auth_context", "expected_type": "object"},
        )

    payload_map = cast(dict[str, object], payload)
    user_id_raw = _require_string_claim(payload=payload_map, claim="user_id")
    tenant_id_raw = _require_string_claim(payload=payload_map, claim="tenant_id")
    role_raw = _require_string_claim(payload=payload_map, claim="role")
    session_id_raw = _require_string_claim(payload=payload_map, claim="session_id")
    delegation_payload = _require_claim(payload=payload_map, claim="delegation_context")

    try:
        user_id = UUID(user_id_raw)
    except ValueError as error:
        raise AuthContextValidationError(
            error_code="auth_context_invalid_claim_type",
            message="Auth context claim type is invalid.",
            reason="auth_context_invalid_claim_type",
            details={"claim": "user_id", "expected_type": "uuid"},
        ) from error

    tenant_id = tenant_id_raw.strip()
    if not tenant_id:
        raise AuthContextValidationError(
            error_code="auth_context_invalid_claim_type",
            message="Auth context claim type is invalid.",
            reason="auth_context_invalid_claim_type",
            details={"claim": "tenant_id", "expected_type": "non_empty_string"},
        )

    role = role_raw.strip()
    if role not in _SUPPORTED_ROLES:
        raise AuthContextValidationError(
            error_code="auth_context_invalid_role",
            message="Auth context role is invalid.",
            reason="auth_context_invalid_role",
            details={"claim": "role"},
        )

    try:
        session_id = UUID(session_id_raw)
    except ValueError as error:
        raise AuthContextValidationError(
            error_code="auth_context_invalid_session_id",
            message="Auth context session_id is invalid.",
            reason="auth_context_invalid_session_id",
            details={"claim": "session_id"},
        ) from error

    delegation_context = _parse_delegation_context(delegation_payload)
    return AuthContextEnvelope(
        user_id=user_id,
        tenant_id=tenant_id,
        role=role,
        session_id=session_id,
        delegation_context=delegation_context,
    )


def parse_stub_bearer_token(token: str) -> Principal:
    """Parse a deterministic stub bearer token into a principal.

    :param token: Bearer token in ``<uuid>:<role>`` format.
    :return: Parsed principal.
    :raises InvalidAuthorizationError: If token format is invalid.
    """

    user_id_part, separator, role_part = token.partition(":")
    if separator != ":":
        raise InvalidAuthorizationError(
            error_code=codes.INVALID_BEARER_TOKEN,
            message="Bearer token must use '<uuid>:<role>' format.",
        )

    normalized_role = role_part.strip()
    if not normalized_role:
        raise InvalidAuthorizationError(
            error_code=codes.INVALID_BEARER_TOKEN,
            message="Bearer token role must not be empty.",
        )

    try:
        user_id = UUID(user_id_part)
    except ValueError as error:
        raise InvalidAuthorizationError(
            error_code=codes.INVALID_BEARER_TOKEN,
            message="Bearer token user_id must be a valid UUID.",
        ) from error

    return Principal(
        user_id=user_id,
        role=normalized_role,
        tenant_id=_DEFAULT_TENANT_ID,
        session_id=uuid4(),
        delegation_context=DelegationContext.not_delegated(),
    )


def require_authenticated_principal(
    request: Request,
    authorization: str | None = Header(default=None, alias=AUTHORIZATION_HEADER_NAME),
) -> Principal:
    """FastAPI dependency requiring a valid bearer principal token.

    :param request: Active HTTP request.
    :param authorization: Raw Authorization header value.
    :return: Parsed principal.
    :raises HTTPException: If header is missing or malformed.
    """

    if authorization is None:
        raise create_request_http_error(
            request=request,
            status_code=401,
            error_code=codes.MISSING_AUTHORIZATION_HEADER,
            message="Authorization header is required.",
            details={"header": AUTHORIZATION_HEADER_NAME},
        )

    scheme_part, separator, token_part = authorization.partition(" ")
    if separator != " " or scheme_part != AUTHORIZATION_SCHEME or not token_part.strip():
        raise create_request_http_error(
            request=request,
            status_code=401,
            error_code=codes.INVALID_AUTHORIZATION_SCHEME,
            message="Authorization header must be in 'Bearer <token>' format.",
            details={"header": AUTHORIZATION_HEADER_NAME},
        )

    try:
        return parse_stub_bearer_token(token_part.strip())
    except InvalidAuthorizationError as error:
        raise create_request_http_error(
            request=request,
            status_code=401,
            error_code=error.error_code,
            message=str(error),
            details={"header": AUTHORIZATION_HEADER_NAME},
        ) from error


def _require_claim(*, payload: dict[str, object], claim: str) -> object:
    if claim not in payload:
        raise AuthContextValidationError(
            error_code="auth_context_missing_required_claim",
            message="Auth context is missing required claim.",
            reason="auth_context_missing_required_claim",
            details={"claim": claim},
        )
    return payload[claim]


def _require_string_claim(*, payload: dict[str, object], claim: str) -> str:
    value = _require_claim(payload=payload, claim=claim)
    if not isinstance(value, str):
        raise AuthContextValidationError(
            error_code="auth_context_invalid_claim_type",
            message="Auth context claim type is invalid.",
            reason="auth_context_invalid_claim_type",
            details={"claim": claim, "expected_type": "string"},
        )
    return value


def _parse_delegation_context(payload: object) -> DelegationContext:
    if not isinstance(payload, dict):
        raise AuthContextValidationError(
            error_code="auth_context_invalid_delegation_context",
            message="Auth context delegation context is invalid.",
            reason="auth_context_invalid_delegation_context",
            details={"claim": "delegation_context"},
        )
    payload_map = cast(dict[str, object], payload)
    required_claims = (
        "is_delegated",
        "principal_user_id",
        "delegate_user_id",
        "delegation_id",
        "granted_at",
        "revoked_at",
    )
    for claim in required_claims:
        if claim not in payload_map:
            raise AuthContextValidationError(
                error_code="auth_context_invalid_delegation_context",
                message="Auth context delegation context is invalid.",
                reason="auth_context_invalid_delegation_context",
                details={"claim": "delegation_context"},
            )

    is_delegated = payload_map["is_delegated"]
    if not isinstance(is_delegated, bool):
        raise AuthContextValidationError(
            error_code="auth_context_invalid_delegation_context",
            message="Auth context delegation context is invalid.",
            reason="auth_context_invalid_delegation_context",
            details={"claim": "delegation_context"},
        )

    principal_user_id = _parse_nullable_uuid(payload_map["principal_user_id"])
    delegate_user_id = _parse_nullable_uuid(payload_map["delegate_user_id"])
    delegation_id = _parse_nullable_uuid(payload_map["delegation_id"])
    granted_at = _parse_nullable_datetime(payload_map["granted_at"])
    revoked_at = _parse_nullable_datetime(payload_map["revoked_at"])
    return DelegationContext(
        is_delegated=is_delegated,
        principal_user_id=principal_user_id,
        delegate_user_id=delegate_user_id,
        delegation_id=delegation_id,
        granted_at=granted_at,
        revoked_at=revoked_at,
    )


def _parse_nullable_uuid(value: object) -> UUID | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AuthContextValidationError(
            error_code="auth_context_invalid_delegation_context",
            message="Auth context delegation context is invalid.",
            reason="auth_context_invalid_delegation_context",
            details={"claim": "delegation_context"},
        )
    try:
        return UUID(value)
    except ValueError as error:
        raise AuthContextValidationError(
            error_code="auth_context_invalid_delegation_context",
            message="Auth context delegation context is invalid.",
            reason="auth_context_invalid_delegation_context",
            details={"claim": "delegation_context"},
        ) from error


def _parse_nullable_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AuthContextValidationError(
            error_code="auth_context_invalid_delegation_context",
            message="Auth context delegation context is invalid.",
            reason="auth_context_invalid_delegation_context",
            details={"claim": "delegation_context"},
        )
    normalized = value.strip()
    if not normalized:
        raise AuthContextValidationError(
            error_code="auth_context_invalid_delegation_context",
            message="Auth context delegation context is invalid.",
            reason="auth_context_invalid_delegation_context",
            details={"claim": "delegation_context"},
        )
    try:
        return datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise AuthContextValidationError(
            error_code="auth_context_invalid_delegation_context",
            message="Auth context delegation context is invalid.",
            reason="auth_context_invalid_delegation_context",
            details={"claim": "delegation_context"},
        ) from error
