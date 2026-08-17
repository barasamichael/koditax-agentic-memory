"""Expose deterministic tax-core computation execution endpoint."""

from pathlib import Path as PathlibPath
from typing import cast
from typing import Protocol
from typing import Annotated

from fastapi import Body
from fastapi import Header
from fastapi import Depends
from fastapi import FastAPI
from fastapi import Request
from fastapi import APIRouter
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError as PydanticValidationError

from shared.authz.rbac import Principal
from shared.authz.rbac import AUTH_CONTEXT_HEADER_NAME
from shared.authz.rbac import AUTHORIZATION_HEADER_NAME
from shared.authz.rbac import require_authenticated_principal
from shared.authz.rbac import build_authorized_principal_dependency
from shared.errors.envelope import create_request_http_error
from shared.tracing.correlation import get_correlation_id
from shared.tracing.correlation import CorrelationIdMiddleware
from shared.determinism.input_hash import InputHashError
from shared.idempotency.idempotency import require_idempotency_key
from services.tax_core.app.engine.replay import ReplayVerificationError
from services.tax_core.app.engine.replay import verify_persisted_computation_replay
from services.tax_core.app.engine.executor import execute_computation
from services.tax_core.app.engine.validation import ValidationError as ComputationValidationError
from services.tax_core.app.engine.validation import validate_persisted_computation
from services.tax_core.app.engine.finalization import FinalizationError
from services.tax_core.app.engine.finalization import finalize_computation
from services.tax_core.app.engine.rule_binding import RuleBindingError
from services.tax_core.app.engine.execution_contract import MaterializationContext
from services.tax_core.app.engine.execution_contract import ReplayVerificationResult
from services.tax_core.app.engine.execution_contract import ReplayVerificationContext
from services.tax_core.app.engine.execution_contract import ReplayVerificationRequest
from services.tax_core.app.engine.execution_contract import ComputationExecutionResult
from services.tax_core.app.engine.execution_contract import ComputationExecutionRequest
from services.tax_core.app.engine.execution_contract import ComputationValidationResult
from services.tax_core.app.engine.execution_contract import ComputationValidationContext
from services.tax_core.app.engine.execution_contract import ComputationValidationRequest
from services.tax_core.app.engine.execution_contract import ComputationFinalizationResult
from services.tax_core.app.engine.execution_contract import ComputationFinalizationContext
from services.tax_core.app.engine.execution_contract import ComputationFinalizationRequest
from services.tax_core.app.engine.execution_contract import MaterializedComputationExecutionResult
from services.tax_core.app.persistence.materialization import MaterializationError
from services.tax_core.app.persistence.materialization import IdempotencyConflictError
from services.tax_core.app.persistence.materialization import materialize_execution_result

load_dotenv(dotenv_path=PathlibPath(__file__).parent.parent.parent.parent / ".env")

INVALID_COMPUTATION_REQUEST = "invalid_computation_request"
INVALID_REPLAY_REQUEST = "invalid_replay_request"
INVALID_FINALIZATION_REQUEST = "invalid_finalization_request"
INVALID_VALIDATION_REQUEST = "invalid_validation_request"
INVALID_RULE_BINDING = "invalid_rule_binding"
IDEMPOTENCY_KEY_CONFLICT = "idempotency_key_conflict"
COMPUTATION_MATERIALIZATION_FAILED = "computation_materialization_failed"
ROUTER = APIRouter()
_TAX_CORE_ALLOWED_ROLES = frozenset({"IndividualTaxpayer", "TaxAgent", "Accountant"})
require_tax_core_auth_context_principal = build_authorized_principal_dependency(
    allowed_roles=_TAX_CORE_ALLOWED_ROLES,
    allow_delegation=False,
)


def require_tax_core_principal(
    request: Request,
    auth_context_header: str | None = Header(default=None, alias=AUTH_CONTEXT_HEADER_NAME),
    authorization: str | None = Header(default=None, alias=AUTHORIZATION_HEADER_NAME),
) -> Principal:
    """Resolve tax-core principal using canonical auth context with bearer fallback.

    Canonical `X-Auth-Context` remains the primary boundary contract. When absent,
    a legacy `Authorization` bearer token is accepted for compatibility with
    existing deterministic tax-core request flows.
    """

    if auth_context_header is not None and auth_context_header.strip():
        return require_tax_core_auth_context_principal(request, auth_context_header)
    if authorization is not None:
        principal = require_authenticated_principal(request, authorization)
        if principal.role not in _TAX_CORE_ALLOWED_ROLES:
            raise create_request_http_error(
                request=request,
                status_code=403,
                error_code="authorization_role_forbidden",
                message="Role is forbidden for this endpoint.",
                details={"role": principal.role},
            )
        return principal
    return require_tax_core_auth_context_principal(request, auth_context_header)


class MaterializerProtocol(Protocol):
    """Define deterministic materialization contract for endpoint execution."""

    def __call__(
        self,
        execution_request: ComputationExecutionRequest,
        execution_result: ComputationExecutionResult,
        context: MaterializationContext,
    ) -> MaterializedComputationExecutionResult:
        """Persist deterministic execution records and return materialized envelope."""

        ...


class ReplayVerifierProtocol(Protocol):
    """Define deterministic replay verification contract for endpoint execution."""

    def __call__(
        self,
        replay_request: ReplayVerificationRequest,
        replay_context: ReplayVerificationContext,
    ) -> ReplayVerificationResult:
        """Verify replay determinism against persisted computation artifacts."""

        ...


class FinalizerProtocol(Protocol):
    """Define deterministic computation finalization contract for endpoint execution."""

    def __call__(
        self,
        finalization_request: ComputationFinalizationRequest,
        finalization_context: ComputationFinalizationContext,
    ) -> ComputationFinalizationResult:
        """Finalize persisted computation and return stable finalized state."""

        ...


class ValidatorProtocol(Protocol):
    """Define deterministic computation validation contract for endpoint execution."""

    def __call__(
        self,
        validation_request: ComputationValidationRequest,
        validation_context: ComputationValidationContext,
    ) -> ComputationValidationResult:
        """Persist computation-bound validation findings and return canonical response."""

        ...


def get_materializer(request: Request) -> MaterializerProtocol:
    """Resolve optional test override or default DB materializer."""

    configured_materializer = getattr(request.app.state, "materializer", None)
    if configured_materializer is not None:
        return cast(MaterializerProtocol, configured_materializer)

    return materialize_execution_result


def get_replay_verifier(request: Request) -> ReplayVerifierProtocol:
    """Resolve optional test override or default replay verifier."""

    configured_replay_verifier = getattr(request.app.state, "replay_verifier", None)
    if configured_replay_verifier is not None:
        return cast(ReplayVerifierProtocol, configured_replay_verifier)

    return verify_persisted_computation_replay


def get_finalizer(request: Request) -> FinalizerProtocol:
    """Resolve optional test override or default computation finalizer."""

    configured_finalizer = getattr(request.app.state, "finalizer", None)
    if configured_finalizer is not None:
        return cast(FinalizerProtocol, configured_finalizer)

    return finalize_computation


def get_validator(request: Request) -> ValidatorProtocol:
    """Resolve optional test override or default computation validator."""

    configured_validator = getattr(request.app.state, "validator", None)
    if configured_validator is not None:
        return cast(ValidatorProtocol, configured_validator)

    return validate_persisted_computation


@ROUTER.post("/computations/execute", response_model=MaterializedComputationExecutionResult)
def execute_computation_endpoint(
    request: Request,
    payload: Annotated[object, Body(...)],
    principal: Annotated[Principal, Depends(require_tax_core_principal)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    materializer: Annotated[MaterializerProtocol, Depends(get_materializer)],
) -> MaterializedComputationExecutionResult:
    """Execute one deterministic computation request.

    :param request: Active HTTP request.
    :param payload: Raw request payload to validate at boundary.
    :param principal: Parsed authenticated principal.
    :param idempotency_key: Validated idempotency key from request header.
    :param materializer: Injected execution materializer dependency.
    :return: Canonical persisted execution result envelope.
    """

    execution_request = _parse_execution_request(request=request, payload=payload)
    try:
        execution_result = execute_computation(execution_request)
    except InputHashError as error:
        raise create_request_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_COMPUTATION_REQUEST,
            message="Invalid computation request payload.",
            details=error.details(),
        ) from error
    except RuleBindingError as error:
        raise create_request_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_RULE_BINDING,
            message="Invalid rule binding for computation request.",
            details=error.details(),
        ) from error

    materialization_context = MaterializationContext(
        user_id=principal.user_id,
        role_at_time=principal.role,
        correlation_id=get_correlation_id(request),
        idempotency_key=idempotency_key,
    )
    try:
        return materializer(
            execution_request,
            execution_result,
            materialization_context,
        )
    except IdempotencyConflictError as error:
        raise create_request_http_error(
            request=request,
            status_code=409,
            error_code=IDEMPOTENCY_KEY_CONFLICT,
            message="Idempotency key conflicts with an existing computation.",
            details=error.details(),
        ) from error
    except MaterializationError as error:
        raise create_request_http_error(
            request=request,
            status_code=500,
            error_code=COMPUTATION_MATERIALIZATION_FAILED,
            message="Failed to materialize deterministic computation records.",
            details=error.details(),
        ) from error


@ROUTER.post("/computations/replay", response_model=ReplayVerificationResult)
def replay_computation_endpoint(
    request: Request,
    payload: Annotated[object, Body(...)],
    principal: Annotated[Principal, Depends(require_tax_core_principal)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    replay_verifier: Annotated[ReplayVerifierProtocol, Depends(get_replay_verifier)],
) -> ReplayVerificationResult:
    """Replay one persisted computation deterministically and verify stored output consistency."""

    replay_request = _parse_replay_request(request=request, payload=payload)
    replay_context = ReplayVerificationContext(
        user_id=principal.user_id,
        role_at_time=principal.role,
        correlation_id=get_correlation_id(request),
        idempotency_key=idempotency_key,
    )
    try:
        return replay_verifier(replay_request, replay_context)
    except ReplayVerificationError as error:
        raise create_request_http_error(
            request=request,
            status_code=error.status_code,
            error_code=error.reason,
            message=error.message,
            details=error.details(),
        ) from error


@ROUTER.post("/computations/finalize", response_model=ComputationFinalizationResult)
def finalize_computation_endpoint(
    request: Request,
    payload: Annotated[object, Body(...)],
    principal: Annotated[Principal, Depends(require_tax_core_principal)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    finalizer: Annotated[FinalizerProtocol, Depends(get_finalizer)],
) -> ComputationFinalizationResult:
    """Finalize one persisted computation with deterministic idempotent behavior."""

    finalization_request = _parse_finalization_request(request=request, payload=payload)
    finalization_context = ComputationFinalizationContext(
        user_id=principal.user_id,
        role_at_time=principal.role,
        correlation_id=get_correlation_id(request),
        idempotency_key=idempotency_key,
    )
    try:
        return finalizer(finalization_request, finalization_context)
    except FinalizationError as error:
        raise create_request_http_error(
            request=request,
            status_code=error.status_code,
            error_code=error.reason,
            message=error.message,
            details=error.details(),
        ) from error


@ROUTER.post("/computations/validate", response_model=ComputationValidationResult)
def validate_computation_endpoint(
    request: Request,
    payload: Annotated[object, Body(...)],
    principal: Annotated[Principal, Depends(require_tax_core_principal)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    validator: Annotated[ValidatorProtocol, Depends(get_validator)],
) -> ComputationValidationResult:
    """Persist deterministic validation findings for one persisted computation."""

    validation_request = _parse_validation_request(request=request, payload=payload)
    validation_context = ComputationValidationContext(
        user_id=principal.user_id,
        role_at_time=principal.role,
        correlation_id=get_correlation_id(request),
        idempotency_key=idempotency_key,
    )
    try:
        return validator(validation_request, validation_context)
    except ComputationValidationError as error:
        raise create_request_http_error(
            request=request,
            status_code=error.status_code,
            error_code=error.reason,
            message=error.message,
            details=error.details(),
        ) from error


def _parse_execution_request(
    request: Request,
    payload: object,
) -> ComputationExecutionRequest:
    if not isinstance(payload, dict):
        raise create_request_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_COMPUTATION_REQUEST,
            message="Invalid computation request payload.",
            details={"reason": "request_body_must_be_object"},
        )

    try:
        return ComputationExecutionRequest.model_validate(cast(dict[str, object], payload))
    except PydanticValidationError as error:
        raise create_request_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_COMPUTATION_REQUEST,
            message="Invalid computation request payload.",
            details={"validation_errors": error.errors(include_url=False)},
        ) from error


def _parse_replay_request(
    request: Request,
    payload: object,
) -> ReplayVerificationRequest:
    if not isinstance(payload, dict):
        raise create_request_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_REPLAY_REQUEST,
            message="Invalid replay request payload.",
            details={"reason": "request_body_must_be_object"},
        )

    try:
        return ReplayVerificationRequest.model_validate(cast(dict[str, object], payload))
    except PydanticValidationError as error:
        raise create_request_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_REPLAY_REQUEST,
            message="Invalid replay request payload.",
            details={"validation_errors": error.errors(include_url=False)},
        ) from error


def _parse_finalization_request(
    request: Request,
    payload: object,
) -> ComputationFinalizationRequest:
    if not isinstance(payload, dict):
        raise create_request_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_FINALIZATION_REQUEST,
            message="Invalid finalization request payload.",
            details={"reason": "request_body_must_be_object"},
        )

    try:
        return ComputationFinalizationRequest.model_validate(cast(dict[str, object], payload))
    except PydanticValidationError as error:
        raise create_request_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_FINALIZATION_REQUEST,
            message="Invalid finalization request payload.",
            details={"validation_errors": error.errors(include_url=False)},
        ) from error


def _parse_validation_request(
    request: Request,
    payload: object,
) -> ComputationValidationRequest:
    if not isinstance(payload, dict):
        raise create_request_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_VALIDATION_REQUEST,
            message="Invalid validation request payload.",
            details={"reason": "request_body_must_be_object"},
        )

    try:
        return ComputationValidationRequest.model_validate(cast(dict[str, object], payload))
    except PydanticValidationError as error:
        raise create_request_http_error(
            request=request,
            status_code=400,
            error_code=INVALID_VALIDATION_REQUEST,
            message="Invalid validation request payload.",
            details={"validation_errors": error.errors(include_url=False)},
        ) from error


def create_app() -> FastAPI:
    """Build the tax-core FastAPI application.

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
