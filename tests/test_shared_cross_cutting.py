"""Test shared cross-cutting middleware and dependency helpers."""

from __future__ import annotations

from uuid import UUID
from uuid import uuid4
from typing import Any
from typing import cast
from typing import Annotated
from typing import TypedDict

import pytest
from fastapi import Depends
from fastapi import FastAPI
from fastapi import Request
from fastapi import APIRouter
from fastapi.testclient import TestClient

from shared.errors import codes
from shared.authz.rbac import Principal
from shared.authz.rbac import require_authenticated_principal
from shared.errors.envelope import ErrorEnvelope
from shared.tracing.correlation import get_correlation_id
from shared.tracing.correlation import CorrelationIdMiddleware
from shared.tracing.correlation import CORRELATION_ID_HEADER_NAME
from shared.idempotency.idempotency import require_idempotency_key
from shared.idempotency.idempotency import validate_idempotency_key
from shared.idempotency.idempotency import InvalidIdempotencyKeyError

ROUTER = APIRouter()


class CorrelationResponse(TypedDict):
    """Represent correlation endpoint payload.

    :param correlation_id: Correlation ID from request context.
    """

    correlation_id: str


class PrincipalResponse(TypedDict):
    """Represent principal endpoint payload.

    :param user_id: Principal user ID string.
    :param role: Principal role string.
    """

    user_id: str
    role: str


class IdempotencyResponse(TypedDict):
    """Represent idempotency endpoint payload.

    :param idempotency_key: Normalized idempotency key value.
    """

    idempotency_key: str


@ROUTER.get("/correlation")
def correlation_endpoint(request: Request) -> CorrelationResponse:
    """Return resolved correlation ID for assertions.

    :param request: Active HTTP request.
    :return: Payload containing correlation ID.
    """

    return {"correlation_id": get_correlation_id(request)}


@ROUTER.get("/auth")
def auth_endpoint(
    principal: Annotated[Principal, Depends(require_authenticated_principal)],
) -> PrincipalResponse:
    """Return resolved principal payload for assertions.

    :param principal: Parsed authenticated principal dependency.
    :return: Payload containing principal fields.
    """

    return {"user_id": str(principal.user_id), "role": principal.role}


@ROUTER.post("/idempotency")
def idempotency_endpoint(
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
) -> IdempotencyResponse:
    """Return normalized idempotency key for assertions.

    :param idempotency_key: Validated idempotency key dependency.
    :return: Payload containing normalized idempotency key.
    """

    return {"idempotency_key": idempotency_key}


def test_correlation_header_is_preserved_when_provided() -> None:
    """Verify provided correlation ID is echoed unchanged.

    :return: None.
    """

    app = _create_app()
    client = TestClient(app)
    response = client.get("/correlation", headers={CORRELATION_ID_HEADER_NAME: "abc-123"})

    payload = cast(CorrelationResponse, _response_json(response))
    assert response.status_code == 200
    assert response.headers[CORRELATION_ID_HEADER_NAME] == "abc-123"
    assert payload["correlation_id"] == "abc-123"


def test_correlation_header_is_generated_when_missing() -> None:
    """Verify missing correlation ID is generated and propagated consistently.

    :return: None.
    """

    app = _create_app()
    client = TestClient(app)
    response = client.get("/correlation")

    payload = cast(CorrelationResponse, _response_json(response))
    generated_correlation_id = response.headers[CORRELATION_ID_HEADER_NAME]

    assert response.status_code == 200
    assert payload["correlation_id"] == generated_correlation_id
    UUID(generated_correlation_id)


def test_validate_idempotency_key_accepts_valid_key() -> None:
    """Verify a valid idempotency key is normalized and accepted.

    :return: None.
    """

    assert validate_idempotency_key("  key-123  ") == "key-123"


def test_validate_idempotency_key_rejects_empty_and_whitespace() -> None:
    """Verify empty and whitespace idempotency keys are rejected.

    :return: None.
    """

    with pytest.raises(InvalidIdempotencyKeyError):
        validate_idempotency_key("")

    with pytest.raises(InvalidIdempotencyKeyError):
        validate_idempotency_key("   ")


def test_validate_idempotency_key_rejects_too_long_value() -> None:
    """Verify overlength idempotency keys are rejected.

    :return: None.
    """

    with pytest.raises(InvalidIdempotencyKeyError):
        validate_idempotency_key("a" * 129)


def test_missing_idempotency_header_returns_standard_envelope() -> None:
    """Verify missing Idempotency-Key returns 400 with standard envelope.

    :return: None.
    """

    app = _create_app()
    client = TestClient(app)
    response = client.post("/idempotency")

    envelope = _extract_error_envelope(response)

    assert response.status_code == 400
    assert envelope["error_code"] == codes.MISSING_IDEMPOTENCY_KEY


def test_valid_idempotency_dependency_returns_normalized_value() -> None:
    """Verify idempotency dependency returns normalized header value.

    :return: None.
    """

    app = _create_app()
    client = TestClient(app)
    response = client.post("/idempotency", headers={"Idempotency-Key": " key-456 "})

    payload = cast(IdempotencyResponse, _response_json(response))

    assert response.status_code == 200
    assert payload["idempotency_key"] == "key-456"


def test_missing_authorization_header_returns_standard_envelope() -> None:
    """Verify missing Authorization produces 401 with standard envelope.

    :return: None.
    """

    app = _create_app()
    client = TestClient(app)
    response = client.get("/auth")

    envelope = _extract_error_envelope(response)

    assert response.status_code == 401
    assert envelope["error_code"] == codes.MISSING_AUTHORIZATION_HEADER


def test_wrong_authorization_scheme_returns_standard_envelope() -> None:
    """Verify wrong Authorization scheme produces 401 with standard envelope.

    :return: None.
    """

    app = _create_app()
    client = TestClient(app)
    response = client.get("/auth", headers={"Authorization": "Basic deadbeef"})

    envelope = _extract_error_envelope(response)

    assert response.status_code == 401
    assert envelope["error_code"] == codes.INVALID_AUTHORIZATION_SCHEME


def test_valid_stub_bearer_token_returns_principal_payload() -> None:
    """Verify valid stub bearer token returns parsed principal fields.

    :return: None.
    """

    user_id = uuid4()
    app = _create_app()
    client = TestClient(app)
    response = client.get(
        "/auth",
        headers={"Authorization": f"Bearer {user_id}:IndividualTaxpayer"},
    )

    payload = cast(PrincipalResponse, _response_json(response))

    assert response.status_code == 200
    assert payload["user_id"] == str(user_id)
    assert payload["role"] == "IndividualTaxpayer"


def test_invalid_uuid_in_stub_bearer_token_returns_standard_envelope() -> None:
    """Verify invalid UUID token segment returns 401 with standard envelope.

    :return: None.
    """

    app = _create_app()
    client = TestClient(app)
    response = client.get("/auth", headers={"Authorization": "Bearer not-a-uuid:Admin"})

    envelope = _extract_error_envelope(response)

    assert response.status_code == 401
    assert envelope["error_code"] == codes.INVALID_BEARER_TOKEN


def _create_app() -> FastAPI:
    """Build a minimal app wiring shared middleware and dependencies.

    :return: FastAPI app instance.
    """

    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)
    app.include_router(ROUTER)
    return app


def _response_json(response: object) -> dict[str, object]:
    """Parse a TestClient response payload as a JSON object.

    :param response: TestClient response object.
    :return: JSON object payload.
    """

    # TestClient responses are an untyped boundary; isolate Any usage here.
    payload = cast(Any, response).json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


def _extract_error_envelope(response: object) -> ErrorEnvelope:
    """Extract and validate standard error envelope from response payload.

    :param response: TestClient response object.
    :return: Parsed standard error envelope.
    """

    payload = _response_json(response)

    # Option A contract: FastAPI HTTPException responses are nested under "detail".
    assert "detail" in payload
    assert "error_code" not in payload

    detail_payload = payload["detail"]
    assert isinstance(detail_payload, dict)

    envelope = cast(ErrorEnvelope, detail_payload)
    assert "message" in envelope
    assert "error_code" in envelope
    assert "correlation_id" in envelope

    response_headers = cast(Any, response).headers
    assert response_headers[CORRELATION_ID_HEADER_NAME] == envelope["correlation_id"]

    return envelope
