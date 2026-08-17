"""Expose auth registration endpoint with deterministic validation behavior."""

from uuid import UUID
from uuid import uuid5
from uuid import NAMESPACE_URL
from pathlib import Path as PathlibPath
from typing import cast
from typing import Literal
from typing import Protocol
from typing import Annotated
from hashlib import sha256
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from threading import Lock
from contextlib import AbstractContextManager
from dataclasses import replace
from dataclasses import dataclass
from collections.abc import Mapping

from fastapi import Body
from fastapi import Path
from fastapi import Query
from fastapi import Depends
from fastapi import FastAPI
from fastapi import Request
from fastapi import APIRouter
from fastapi import HTTPException
from pydantic import Field
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from shared.authz.rbac import Principal
from shared.authz.rbac import build_authorized_principal_dependency
from services.auth.app.login import LoginError
from services.auth.app.login import AUTH_LOG_EVENT_LOGIN
from services.auth.app.login import AUTH_LOG_EVENT_EMAIL_OTP_LOGIN
from services.auth.app.login import LoginSuccessEnvelope
from services.auth.app.login import LoginResponseEnvelope
from services.auth.app.login import login_with_credentials
from services.auth.app.login import login_with_email_otp
from services.auth.app.login import EmailOtpLoginResponseEnvelope
from services.auth.app.login import SessionContextEnvelope
from services.auth.app.login import is_login_lockout_reason
from services.auth.app.login import LoginStepUpStoreProtocol
from services.auth.app.login import DelegationContextEnvelope
from services.auth.app.login import LoginLockoutStoreProtocol
from services.auth.app.login import get_default_login_lockout_store
from services.auth.app.login import get_default_login_step_up_store
from services.auth.app.login import build_default_login_lockout_store
from services.auth.app.login import build_default_login_step_up_store
from services.auth.app.config import get_auth_otp_runtime_mode
from services.auth.app.config import get_auth_otp_sms_provider_mode
from services.auth.app.config import get_auth_oauth_state_ttl_seconds
from services.auth.app.config import get_auth_otp_email_provider_mode
from services.auth.app.config import load_auth_secret_config_baseline
from services.auth.app.logging import StructuredAuthLogEvent
from services.auth.app.logging import emit_auth_structured_log
from services.auth.app.logging import InMemoryAuthStructuredLogStore
from services.auth.app.logging import get_default_auth_structured_log_store
from services.auth.app.metrics import MetricEvent
from services.auth.app.metrics import AuthMetricsEmitter
from services.auth.app.metrics import AUTH_LOGIN_FAILURE_TOTAL
from services.auth.app.metrics import AUTH_LOGIN_SUCCESS_TOTAL
from services.auth.app.metrics import AUTH_OAUTH_FAILURE_TOTAL
from services.auth.app.metrics import AUTH_SESSION_ISSUED_TOTAL
from services.auth.app.metrics import AUTH_LOCKOUT_APPLIED_TOTAL
from services.auth.app.metrics import AUTH_OTP_VERIFY_FAILURE_TOTAL
from services.auth.app.metrics import AUTH_OTP_VERIFY_SUCCESS_TOTAL
from services.auth.app.metrics import AUTH_OTP_CHALLENGE_ISSUED_TOTAL
from services.auth.app.metrics import AUTH_REGISTRATION_FAILURE_TOTAL
from services.auth.app.metrics import AUTH_REGISTRATION_SUCCESS_TOTAL
from services.auth.app.metrics import get_default_auth_metrics_emitter
from services.auth.app.metrics import AUTH_SESSION_REFRESH_FAILURE_TOTAL
from services.auth.app.metrics import AUTH_SESSION_REFRESH_SUCCESS_TOTAL
from services.auth.app.metrics import AUTH_PASSWORD_RESET_CONFIRM_FAILURE_TOTAL
from services.auth.app.metrics import AUTH_PASSWORD_RESET_CONFIRM_SUCCESS_TOTAL
from shared.tracing.correlation import get_trace_id
from shared.tracing.correlation import get_correlation_id
from shared.tracing.correlation import CorrelationIdMiddleware
from services.auth.app.oauth_flow import OAuthFlowError
from services.auth.app.oauth_flow import complete_oauth_callback
from services.auth.app.oauth_flow import OAuthStateStoreProtocol
from services.auth.app.oauth_flow import start_oauth_authorization
from services.auth.app.oauth_flow import get_default_oauth_state_store
from services.auth.app.oauth_flow import OAuthTokenExchangeClientProtocol
from services.auth.app.oauth_flow import get_default_oauth_token_exchange_client
from shared.determinism.input_hash import canonical_json_dumps
from services.auth.app.phone_change import PhoneChangeError
from services.auth.app.phone_change import PhoneChangeStoreProtocol
from services.auth.app.phone_change import PhoneChangeConfirmResponse
from services.auth.app.phone_change import PhoneChangeRequestResponse
from services.auth.app.phone_change import AUTH_LOG_EVENT_PHONE_CHANGE
from services.auth.app.phone_change import create_phone_change_request
from services.auth.app.phone_change import confirm_phone_change_request
from services.auth.app.phone_change import get_default_phone_change_store
from services.auth.app.phone_change import build_default_phone_change_store
from services.auth.app.persistence_support import connect_auth_database
from services.auth.app.persistence_support import load_auth_database_url
from services.auth.app.registration import register_user
from services.auth.app.registration import ALLOWED_AUTH_ROLES
from services.auth.app.registration import RegisteredUserRecord
from services.auth.app.registration import RegistrationConflictError
from services.auth.app.registration import DelegationStoreProtocol
from services.auth.app.registration import RegistrationStoreProtocol
from services.auth.app.registration import parse_registration_request
from services.auth.app.registration import RegistrationSuccessEnvelope
from services.auth.app.registration import RegistrationValidationError
from services.auth.app.registration import RegistrationPersistenceError
from services.auth.app.registration import get_default_delegation_store
from services.auth.app.registration import get_default_registration_store
from services.auth.app.registration import normalize_phone_number as _normalize_reg_phone
from shared.idempotency.idempotency import require_idempotency_key
from services.auth.app.oauth_linking import OAuthIdentityLinkingStoreProtocol
from services.auth.app.oauth_linking import (
    get_default_oauth_identity_linking_store,
)
from services.auth.app.observability import AuthSloAlert
from services.auth.app.observability import AuthSloMetricSnapshot
from services.auth.app.observability import AuthSloThresholdPolicy
from services.auth.app.observability import evaluate_auth_slo_thresholds
from services.auth.app.observability import (
    get_default_auth_slo_threshold_policy,
)
from services.auth.app.password_reset import PasswordResetError
from services.auth.app.password_reset import PasswordResetStoreProtocol
from services.auth.app.password_reset import PasswordResetConfirmEnvelope
from services.auth.app.password_reset import AUTH_LOG_EVENT_PASSWORD_RESET
from services.auth.app.password_reset import PasswordResetChallengeEnvelope
from services.auth.app.password_reset import confirm_password_reset_challenge
from services.auth.app.password_reset import get_default_password_reset_store
from services.auth.app.password_reset import initiate_password_reset_challenge
from services.auth.app.password_reset import build_default_password_reset_store
from services.auth.app.password_reset import (
    parse_password_reset_confirm_request,
)
from services.auth.app.password_reset import (
    parse_password_reset_initiate_request,
)
from services.auth.app.password_reset import (
    resolve_password_reset_metrics_purpose,
)
from services.auth.app.account_deletion import AccountDeletionRequestError
from services.auth.app.account_deletion import AccountDeletionCancelResponse
from services.auth.app.account_deletion import AccountDeletionConfirmResponse
from services.auth.app.account_deletion import AccountDeletionExecuteResponse
from services.auth.app.account_deletion import AccountDeletionRequestResponse
from services.auth.app.account_deletion import AUTH_LOG_EVENT_ACCOUNT_DELETION
from services.auth.app.account_deletion import cancel_account_deletion_request
from services.auth.app.account_deletion import create_account_deletion_request
from services.auth.app.account_deletion import confirm_account_deletion_request
from services.auth.app.account_deletion import execute_account_deletion_request
from services.auth.app.account_deletion import (
    AccountDeletionRequestStoreProtocol,
)
from services.auth.app.account_deletion import (
    get_default_account_deletion_request_store,
)
from services.auth.app.account_deletion import (
    build_default_account_deletion_request_store,
)
from services.auth.app.oauth_resilience import OAuthProviderResiliencePolicy
from services.auth.app.oauth_resilience import OAuthProviderCircuitStoreProtocol
from services.auth.app.oauth_resilience import (
    get_default_oauth_provider_circuit_store,
)
from services.auth.app.oauth_resilience import (
    get_default_oauth_provider_resilience_policy,
)
from services.auth.app.oauth_validation import OidcIdTokenValidatorProtocol
from services.auth.app.oauth_validation import (
    get_default_oidc_id_token_validator,
)
from services.auth.app.session_issuance import SessionIssuanceError
from services.auth.app.session_issuance import SessionIssuanceStoreProtocol
from services.auth.app.session_issuance import (
    get_default_session_issuance_store,
)
from services.auth.app.session_issuance import (
    build_default_session_issuance_store,
)
from services.event_store.app.repository import EventStoreRepository
from services.event_store.app.repository import (
    PERSISTENCE_UNAVAILABLE as EVENT_STORE_PERSISTENCE_UNAVAILABLE,
)
from services.event_store.app.repository import (
    RETENTION_POLICY_INVALID as EVENT_STORE_RETENTION_POLICY_INVALID,
)
from services.event_store.app.repository import EventStoreRepositoryError
from services.event_store.app.repository import (
    PERSISTENCE_NOT_CONFIGURED as EVENT_STORE_PERSISTENCE_NOT_CONFIGURED,
)
from services.auth.app.email_verification import EmailVerificationError
from services.auth.app.email_verification import EMAIL_VERIFICATION_CHANNEL
from services.auth.app.email_verification import EmailVerificationStoreProtocol
from services.auth.app.email_verification import EmailVerificationVerifyEnvelope
from services.auth.app.email_verification import (
    AUTH_LOG_EVENT_EMAIL_VERIFICATION,
)
from services.auth.app.email_verification import (
    EmailVerificationChallengeEnvelope,
)
from services.auth.app.email_verification import (
    issue_email_verification_challenge,
)
from services.auth.app.email_verification import (
    verify_email_verification_challenge,
)
from services.auth.app.email_verification import (
    get_default_email_verification_store,
)
from services.auth.app.email_verification import (
    build_default_email_verification_store,
)
from services.auth.app.email_verification import (
    parse_email_verification_verify_request,
)
from services.auth.app.email_verification import (
    parse_email_verification_challenge_request,
)
from services.auth.app.oauth_provisioning import OAuthJitProvisioningPolicy
from services.auth.app.oauth_provisioning import (
    get_default_oauth_jit_provisioning_policy,
)
from services.auth.app.phone_verification import PhoneVerificationError
from services.auth.app.phone_verification import PHONE_VERIFICATION_CHANNEL
from services.auth.app.phone_verification import PhoneVerificationStoreProtocol
from services.auth.app.phone_verification import PhoneVerificationVerifyEnvelope
from services.auth.app.phone_verification import (
    AUTH_LOG_EVENT_PHONE_VERIFICATION,
)
from services.auth.app.phone_verification import (
    issue_phone_verification_challenge,
)
from services.auth.app.phone_verification import (
    PhoneVerificationChallengeEnvelope,
)
from services.auth.app.phone_verification import (
    verify_phone_verification_challenge,
)
from services.auth.app.phone_verification import (
    get_default_phone_verification_store,
)
from services.auth.app.phone_verification import (
    build_default_phone_verification_store,
)
from services.auth.app.phone_verification import (
    parse_phone_verification_verify_request,
)
from services.auth.app.phone_verification import (
    parse_phone_verification_challenge_request,
)
from services.auth.app.otp_delivery_adapters import (
    SmsOtpDeliveryAdapterProtocol,
)
from services.auth.app.otp_delivery_adapters import (
    EmailOtpDeliveryAdapterProtocol,
)
from services.auth.app.otp_delivery_adapters import (
    get_default_sms_otp_delivery_adapter,
)
from services.auth.app.otp_delivery_adapters import (
    get_default_email_otp_delivery_adapter,
)

load_dotenv(dotenv_path=PathlibPath(
    __file__).parent.parent.parent.parent / ".env")

dev_router = APIRouter(prefix="/dev", tags=["dev"])

ROUTER = APIRouter()
DEFAULT_TENANT_ID = "default_tenant"
AUTH_AUDIT_SCHEMA_VERSION = "1.0.0"
AUTH_AUDIT_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "auth_registration_requested",
        "auth_registration_verified",
        "auth_login_succeeded",
        "auth_login_failed",
        "auth_lockout_applied",
        "auth_session_refreshed",
        "auth_session_revoked",
        "auth_password_reset_requested",
        "auth_password_reset_completed",
        "auth_phone_change_requested",
        "auth_phone_change_confirmed",
        "auth_account_deletion_requested",
        "auth_account_deletion_confirmed",
        "auth_account_deletion_executed",
        "auth_otp_challenge_issued",
        "auth_otp_challenge_verified",
        "auth_oauth_identity_link_succeeded",
        "auth_oauth_identity_link_denied",
        "auth_oauth_identity_link_suspicious",
        "jit_provisioning_allowed",
        "jit_provisioning_denied",
        "jit_provisioning_conflict_detected",
        "auth_oauth_provider_degraded_mode_active",
        "auth_oauth_provider_circuit_open",
        "auth_oauth_provider_recovery_in_progress",
        "auth_oauth_provider_recovered",
        "auth_role_change_succeeded",
        "auth_role_change_rejected",
    }
)
# ── Registration phone-update rate-limit store ────────────────────────────────
# Tracks per-user attempt counts and last-change timestamps in process memory.
# Intentionally not persisted — limits reset on restart, which is acceptable
# for a low-traffic pre-verification flow.
@dataclass
class _RegistrationPhoneUpdateRecord:
    attempts: int
    last_changed_at: datetime


_REG_PHONE_UPDATE_STORE: dict[str, _RegistrationPhoneUpdateRecord] = {}
_REG_PHONE_UPDATE_LOCK = Lock()
_REG_PHONE_UPDATE_MAX_ATTEMPTS = 3
_REG_PHONE_UPDATE_COOLDOWN_SECONDS = 60


class RegistrationPhoneUpdateEnvelope(BaseModel):
    """Response envelope for registration phone-number update."""

    status: str
    challenge_id: str
    expires_at: str
    updated_phone_number: str
    attempts_remaining: int


_SENSITIVE_AUDIT_DETAIL_TOKENS: tuple[str, ...] = (
    "password",
    "otp",
    "token",
    "secret",
    "proof",
    "hash",
)
_AUTH_LOG_EVENT_REGISTRATION = "auth.registration"
_AUTH_LOG_EVENT_SESSION = "auth.session"
_AUTH_LOG_EVENT_OTP = "auth.otp"
_AUTH_LOG_EVENT_OAUTH = "auth.oauth"
_AUTH_LOG_EVENT_ROLE_CHANGE = "auth.role_change"


class AuthAuditEventEnvelope(BaseModel):
    """Represent canonical deterministic auth audit evidence payload."""

    schema_version: Literal["1.0.0"]
    event_type: str
    event_time: str
    user_id: UUID | None
    tenant_id: str
    session_id: UUID | None
    correlation_id: str
    trace_id: str
    action_status: str
    reason_code: str | None
    evidence_hash: str
    details: dict[str, object] = Field(default_factory=dict)


class AuthAuditStoreProtocol(Protocol):
    """Define storage boundary for canonical auth audit evidence."""

    def append_event(self, *, event: AuthAuditEventEnvelope) -> None:
        """Persist one immutable auth audit event."""

        ...

    def list_events(self) -> tuple[AuthAuditEventEnvelope, ...]:
        """Return immutable snapshot of auth audit events."""

        ...

    def reset(self) -> None:
        """Clear in-memory auth audit state for deterministic isolated tests."""

        ...


@dataclass
class InMemoryAuthAuditStore:
    """Store canonical auth audit evidence in process-local memory."""

    _events: list[AuthAuditEventEnvelope]
    _lock: Lock

    def __init__(self) -> None:
        self._events = []
        self._lock = Lock()

    def append_event(self, *, event: AuthAuditEventEnvelope) -> None:
        with self._lock:
            self._events.append(event)

    def list_events(self) -> tuple[AuthAuditEventEnvelope, ...]:
        with self._lock:
            return tuple(self._events)

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


class AuthAuditStoreError(RuntimeError):
    """Represent deterministic auth audit persistence failure."""

    def __init__(self, *, status_code: int, error_code: str, message: str, reason: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.reason = reason


@dataclass
class EventStoreBackedAuthAuditStore:
    """Persist canonical auth audit envelopes through the event-store repository."""

    _repository: EventStoreRepository
    _visible_events_floor: datetime
    _lock: Lock

    def __init__(self, *, repository: EventStoreRepository) -> None:
        self._repository = repository
        self._visible_events_floor = datetime.fromtimestamp(0, tz=UTC)
        self._lock = Lock()

    def append_event(self, *, event: AuthAuditEventEnvelope) -> None:
        try:
            self._repository.append_event(
                event_type=event.event_type,
                user_id=event.user_id,
                role_at_time=_resolve_auth_audit_role_at_time(event),
                trace_id=event.trace_id,
                correlation_id=event.correlation_id,
                idempotency_key=_build_auth_audit_idempotency_key(event),
                is_delegated=False,
                principal_user_id=None,
                delegate_user_id=None,
                delegation_id=None,
                event_timestamp=_parse_auth_audit_event_time(event.event_time),
                details={"auth_audit_envelope": event.model_dump(mode="json")},
                resource_id=_resolve_auth_audit_resource_id(event),
            )
        except EventStoreRepositoryError as error:
            raise _build_auth_audit_store_error(error=error) from error

    def list_events(self) -> tuple[AuthAuditEventEnvelope, ...]:
        with self._lock:
            visible_events_floor = self._visible_events_floor
        try:
            persisted_events = self._repository.list_events_since(
                created_at_floor=visible_events_floor
            )
        except EventStoreRepositoryError as error:
            raise _build_auth_audit_store_error(error=error) from error

        envelopes: list[AuthAuditEventEnvelope] = []
        for persisted_event in persisted_events:
            envelope = _extract_auth_audit_envelope_from_persisted_event(
                persisted_event.details)
            if envelope is not None:
                envelopes.append(envelope)
        return tuple(envelopes)

    def reset(self) -> None:
        try:
            latest_created_at = self._repository.latest_created_at()
        except EventStoreRepositoryError as error:
            raise _build_auth_audit_store_error(error=error) from error
        with self._lock:
            if latest_created_at is None:
                self._visible_events_floor = datetime.now(UTC)
                return
            self._visible_events_floor = latest_created_at + \
                timedelta(microseconds=1)


class RefreshRequestEnvelope(BaseModel):
    """Represent deterministic refresh request payload."""

    refresh_token: str


class RefreshResponseEnvelope(BaseModel):
    """Represent deterministic refresh success payload."""

    status: Literal["refreshed"]
    access_token: str
    refresh_token: str
    expires_at: str
    session: SessionContextEnvelope


class LogoutRequestEnvelope(BaseModel):
    """Represent deterministic logout request payload."""

    revoke_scope: Literal["single_session", "all_sessions"]
    target_session_id: UUID | None = None


class TraceabilityEnvelope(BaseModel):
    """Represent deterministic traceability envelope for auth responses."""

    trace_id: str
    correlation_id: str


class LogoutResponseEnvelope(BaseModel):
    """Represent deterministic logout revocation response payload."""

    status: Literal["revoked"]
    revoke_scope: Literal["single_session", "all_sessions"]
    revoked_session_count: int
    traceability: TraceabilityEnvelope


class SessionIntrospectionResponseEnvelope(BaseModel):
    """Represent deterministic session-introspection response payload."""

    status: Literal["active", "warning", "invalidated", "expired"]
    session: SessionContextEnvelope
    issued_at: str
    expires_at: str
    inactivity_expires_at: str
    absolute_expires_at: str
    last_activity_at: str
    warning_window_started_at: str
    extension_allowed: bool
    is_invalidated: bool
    traceability: TraceabilityEnvelope


class OAuthStartRequestEnvelope(BaseModel):
    """Represent deterministic OAuth start request payload."""

    redirect_uri: str
    state_hint: str | None = None


class OAuthStartResponseEnvelope(BaseModel):
    """Represent deterministic OAuth Authorization Code start response payload."""

    status: Literal["redirect_required"]
    provider: str
    authorization_url: str
    state: str
    nonce: str
    expires_at: str
    traceability: TraceabilityEnvelope


class OAuthCallbackResponseEnvelope(BaseModel):
    """Represent deterministic OAuth callback protocol-validation response payload."""

    status: Literal["protocol_validated"]
    provider: str
    callback_status: Literal["protocol_validated"]
    oauth_subject: str | None = None
    linked_user_id: UUID
    linked_tenant_id: str
    link_status: Literal["linked_existing", "linked_new"]
    traceability: TraceabilityEnvelope


class RoleChangeRequestEnvelope(BaseModel):
    """Represent deterministic role-change request payload."""

    target_user_id: UUID
    new_role: str
    reason: str | None = None


class RoleChangeResponseEnvelope(BaseModel):
    """Represent deterministic role-change response payload."""

    status: Literal["role_updated"]
    target_user_id: UUID
    previous_role: str
    new_role: str
    changed_by_user_id: UUID
    changed_at: str
    traceability: TraceabilityEnvelope


@dataclass(frozen=True)
class RoleChangeGovernanceError(ValueError):
    """Represent deterministic role-governance failure."""

    status_code: int
    error_code: str
    message: str
    reason: str
    details: dict[str, object]


require_role_change_principal = build_authorized_principal_dependency(
    allowed_roles=frozenset({"Administrator"}),
    allow_delegation=False,
)


def get_registration_store(request: Request) -> RegistrationStoreProtocol:
    """Resolve optional test override or default registration store."""

    configured_store = getattr(request.app.state, "registration_store", None)
    if configured_store is not None:
        return cast(RegistrationStoreProtocol, configured_store)
    return get_default_registration_store()


def get_delegation_store(request: Request) -> DelegationStoreProtocol:
    """Resolve optional test override or default delegation store."""

    configured_store = getattr(request.app.state, "delegation_store", None)
    if configured_store is not None:
        return cast(DelegationStoreProtocol, configured_store)
    return get_default_delegation_store()


def get_email_verification_store(
    request: Request,
) -> EmailVerificationStoreProtocol:
    """Resolve optional test override or default email-verification store."""

    configured_store = getattr(
        request.app.state, "email_verification_store", None)
    if configured_store is not None:
        return cast(EmailVerificationStoreProtocol, configured_store)
    return get_default_email_verification_store()


def get_phone_verification_store(
    request: Request,
) -> PhoneVerificationStoreProtocol:
    """Resolve optional test override or default phone-verification store."""

    configured_store = getattr(
        request.app.state, "phone_verification_store", None)
    if configured_store is not None:
        return cast(PhoneVerificationStoreProtocol, configured_store)
    return get_default_phone_verification_store()


def get_sms_delivery_adapter(request: Request) -> SmsOtpDeliveryAdapterProtocol:
    """Resolve optional test override or default SMS delivery adapter."""

    configured_adapter = getattr(
        request.app.state, "sms_delivery_adapter", None)
    if configured_adapter is not None:
        return cast(SmsOtpDeliveryAdapterProtocol, configured_adapter)
    return get_default_sms_otp_delivery_adapter()


def get_email_delivery_adapter(
    request: Request,
) -> EmailOtpDeliveryAdapterProtocol:
    """Resolve optional test override or default email delivery adapter."""

    configured_adapter = getattr(
        request.app.state, "email_delivery_adapter", None)
    if configured_adapter is not None:
        return cast(EmailOtpDeliveryAdapterProtocol, configured_adapter)
    return get_default_email_otp_delivery_adapter()


def get_password_reset_store(request: Request) -> PasswordResetStoreProtocol:
    """Resolve optional test override or default password-reset store."""

    configured_store = getattr(request.app.state, "password_reset_store", None)
    if configured_store is not None:
        return cast(PasswordResetStoreProtocol, configured_store)
    return get_default_password_reset_store()


def get_account_deletion_request_store(
    request: Request,
) -> AccountDeletionRequestStoreProtocol:
    """Resolve optional test override or default account-deletion request store."""

    configured_store = getattr(
        request.app.state, "account_deletion_request_store", None)
    if configured_store is not None:
        return cast(AccountDeletionRequestStoreProtocol, configured_store)
    return get_default_account_deletion_request_store()


def get_phone_change_store(request: Request) -> PhoneChangeStoreProtocol:
    """Resolve optional test override or default phone-change request store."""

    configured_store = getattr(request.app.state, "phone_change_store", None)
    if configured_store is not None:
        return cast(PhoneChangeStoreProtocol, configured_store)
    return get_default_phone_change_store()


def get_session_issuance_store(
    request: Request,
) -> SessionIssuanceStoreProtocol:
    """Resolve optional test override or default auth session issuance store."""

    configured_store = getattr(
        request.app.state, "session_issuance_store", None)
    if configured_store is not None:
        return cast(SessionIssuanceStoreProtocol, configured_store)
    return get_default_session_issuance_store()


def get_login_lockout_store(request: Request) -> LoginLockoutStoreProtocol:
    """Resolve optional test override or default login-lockout store."""

    configured_store = getattr(request.app.state, "login_lockout_store", None)
    if configured_store is not None:
        return cast(LoginLockoutStoreProtocol, configured_store)
    return get_default_login_lockout_store()


def get_login_step_up_store(request: Request) -> LoginStepUpStoreProtocol:
    """Resolve optional test override or default login step-up store."""

    configured_store = getattr(request.app.state, "login_step_up_store", None)
    if configured_store is not None:
        return cast(LoginStepUpStoreProtocol, configured_store)
    return get_default_login_step_up_store()


def get_auth_audit_store(request: Request) -> AuthAuditStoreProtocol:
    """Resolve optional test override or default auth audit store."""

    configured_store = getattr(request.app.state, "auth_audit_store", None)
    if configured_store is not None:
        return cast(AuthAuditStoreProtocol, configured_store)
    return cast(AuthAuditStoreProtocol, request.app.state.auth_audit_store)


def get_auth_metrics_emitter(request: Request) -> AuthMetricsEmitter:
    """Resolve optional test override or default auth metrics emitter."""

    configured_emitter = getattr(
        request.app.state, "auth_metrics_emitter", None)
    if configured_emitter is not None:
        return cast(AuthMetricsEmitter, configured_emitter)
    return get_default_auth_metrics_emitter()


def get_oauth_state_store(request: Request) -> OAuthStateStoreProtocol:
    """Resolve optional test override or default OAuth state store."""

    configured_store = getattr(request.app.state, "oauth_state_store", None)
    if configured_store is not None:
        return cast(OAuthStateStoreProtocol, configured_store)
    return get_default_oauth_state_store()


def get_oauth_token_exchange_client(
    request: Request,
) -> OAuthTokenExchangeClientProtocol:
    """Resolve optional test override or default OAuth token-exchange client."""

    configured_client = getattr(
        request.app.state, "oauth_token_exchange_client", None)
    if configured_client is not None:
        return cast(OAuthTokenExchangeClientProtocol, configured_client)
    return get_default_oauth_token_exchange_client()


def get_oauth_identity_linking_store(
    request: Request,
) -> OAuthIdentityLinkingStoreProtocol:
    """Resolve optional test override or default OAuth identity-linking store."""

    configured_store = getattr(
        request.app.state, "oauth_identity_linking_store", None)
    if configured_store is not None:
        return cast(OAuthIdentityLinkingStoreProtocol, configured_store)
    return get_default_oauth_identity_linking_store()


def get_oauth_jit_provisioning_policy(
    request: Request,
) -> OAuthJitProvisioningPolicy:
    """Resolve optional test override or default OAuth JIT provisioning policy."""

    configured_policy = getattr(
        request.app.state, "oauth_jit_provisioning_policy", None)
    if configured_policy is not None:
        return cast(OAuthJitProvisioningPolicy, configured_policy)
    return get_default_oauth_jit_provisioning_policy()


def get_oauth_provider_resilience_policy(
    request: Request,
) -> OAuthProviderResiliencePolicy:
    """Resolve optional test override or default OAuth provider resilience policy."""

    configured_policy = getattr(
        request.app.state, "oauth_provider_resilience_policy", None)
    if configured_policy is not None:
        return cast(OAuthProviderResiliencePolicy, configured_policy)
    return get_default_oauth_provider_resilience_policy()


def get_oauth_provider_circuit_store(
    request: Request,
) -> OAuthProviderCircuitStoreProtocol:
    """Resolve optional test override or default OAuth provider circuit-state store."""

    configured_store = getattr(
        request.app.state, "oauth_provider_circuit_store", None)
    if configured_store is not None:
        return cast(OAuthProviderCircuitStoreProtocol, configured_store)
    return get_default_oauth_provider_circuit_store()


def get_oidc_id_token_validator(
    request: Request,
) -> OidcIdTokenValidatorProtocol:
    """Resolve optional test override or default OIDC ID-token validator."""

    configured_validator = getattr(
        request.app.state, "oauth_id_token_validator", None)
    if configured_validator is not None:
        return cast(OidcIdTokenValidatorProtocol, configured_validator)
    return get_default_oidc_id_token_validator()


@dev_router.get("/otp/{challenge_id}")
def dev_get_otp(request: Request, challenge_id: UUID):  # type: ignore
    phone_store = get_phone_verification_store(request)
    phone_challenge = phone_store.get_challenge(challenge_id=challenge_id)
    if phone_challenge is not None:
        return {
            "otp_code": phone_challenge.otp_code,
            "channel": "sms",
            "purpose": phone_challenge.purpose,
            "expires_at": phone_challenge.expires_at.isoformat().replace("+00:00", "Z"),
            "consumed_at": (
                phone_challenge.consumed_at.isoformat().replace("+00:00", "Z")
                if phone_challenge.consumed_at is not None
                else None
            ),
        }
    email_store = get_email_verification_store(request)
    email_challenge = email_store.get_challenge(challenge_id=challenge_id)
    if email_challenge is not None:
        return {
            "otp_code": email_challenge.otp_code,
            "channel": "email",
            "purpose": email_challenge.purpose,
            "expires_at": email_challenge.expires_at.isoformat().replace("+00:00", "Z"),
            "consumed_at": (
                email_challenge.consumed_at.isoformat().replace("+00:00", "Z")
                if email_challenge.consumed_at is not None
                else None
            ),
        }
    raise HTTPException(status_code=404, detail="Challenge not found")


@ROUTER.post(
    "/v1/auth/register",
    response_model=RegistrationSuccessEnvelope,
    status_code=201,
)
def register_auth_user(
    request: Request,
    payload: Annotated[object, Body(...)],
    registration_store: Annotated[RegistrationStoreProtocol, Depends(get_registration_store)],
    auth_audit_store: Annotated[AuthAuditStoreProtocol, Depends(get_auth_audit_store)],
    auth_metrics_emitter: Annotated[AuthMetricsEmitter, Depends(get_auth_metrics_emitter)],
) -> RegistrationSuccessEnvelope:
    """Register user account with deterministic validation and conflict handling."""

    try:
        request_record = parse_registration_request(payload)
        response = register_user(
            request_record=request_record,
            registration_store=registration_store,
        )
        _emit_auth_audit_event(
            request=request,
            auth_audit_store=auth_audit_store,
            event_type="auth_registration_requested",
            user_id=response.user_id,
            tenant_id=DEFAULT_TENANT_ID,
            session_id=None,
            action_status="succeeded",
            reason_code=None,
            details={
                "registration_status": response.registration_status,
                "requested_role": request_record.role,
            },
        )
        _emit_auth_structured_log_event(
            request=request,
            event_type=_AUTH_LOG_EVENT_REGISTRATION,
            event_status="succeeded",
            reason_code=None,
            user_id=response.user_id,
            tenant_id=DEFAULT_TENANT_ID,
            details={"registration_status": response.registration_status},
        )
        auth_metrics_emitter.increment_counter_non_blocking(
            AUTH_REGISTRATION_SUCCESS_TOTAL)
        return response
    except RegistrationValidationError as error:
        auth_metrics_emitter.increment_counter_non_blocking(
            AUTH_REGISTRATION_FAILURE_TOTAL,
            dimensions={"reason_code": error.reason},
        )
        _emit_auth_audit_event(
            request=request,
            auth_audit_store=auth_audit_store,
            event_type="auth_registration_requested",
            user_id=None,
            tenant_id=DEFAULT_TENANT_ID,
            session_id=None,
            action_status="rejected",
            reason_code=error.reason,
            details=error.details,
        )
        raise _create_auth_http_error(
            request=request,
            status_code=400,
            error_code=error.error_code,
            message=error.message,
            reason=error.reason,
            details=error.details,
        ) from error
    except RegistrationConflictError as error:
        auth_metrics_emitter.increment_counter_non_blocking(
            AUTH_REGISTRATION_FAILURE_TOTAL,
            dimensions={"reason_code": error.reason},
        )
        _emit_auth_audit_event(
            request=request,
            auth_audit_store=auth_audit_store,
            event_type="auth_registration_requested",
            user_id=None,
            tenant_id=DEFAULT_TENANT_ID,
            session_id=None,
            action_status="rejected",
            reason_code=error.reason,
            details={},
        )
        raise _create_auth_http_error(
            request=request,
            status_code=409,
            error_code=error.error_code,
            message=error.message,
            reason=error.reason,
            details={},
        ) from error
    except RegistrationPersistenceError as error:
        auth_metrics_emitter.increment_counter_non_blocking(
            AUTH_REGISTRATION_FAILURE_TOTAL,
            dimensions={"reason_code": error.reason},
        )
        raise _create_auth_http_error(
            request=request,
            status_code=error.status_code,
            error_code=error.error_code,
            message=error.message,
            reason=error.reason,
            details=error.details,
        ) from error


@ROUTER.patch(
    "/v1/auth/phone-verification/update-phone",
    response_model=RegistrationPhoneUpdateEnvelope,
    status_code=200,
)
def update_registration_phone_endpoint(
    request: Request,
    payload: Annotated[object, Body(...)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    registration_store: Annotated[RegistrationStoreProtocol, Depends(get_registration_store)],
    phone_verification_store: Annotated[
        PhoneVerificationStoreProtocol, Depends(get_phone_verification_store)
    ],
    sms_delivery_adapter: Annotated[
        SmsOtpDeliveryAdapterProtocol, Depends(get_sms_delivery_adapter)
    ],
    auth_audit_store: Annotated[AuthAuditStoreProtocol, Depends(get_auth_audit_store)],
    auth_metrics_emitter: Annotated[AuthMetricsEmitter, Depends(get_auth_metrics_emitter)],
) -> RegistrationPhoneUpdateEnvelope:
    """Update phone number for a pending-verification registration and re-issue OTP."""

    if not isinstance(payload, dict):
        raise _create_auth_http_error(
            request=request,
            status_code=400,
            error_code="invalid_phone_update_request",
            message="Invalid phone update request payload.",
            reason="invalid_phone_update_request",
            details={},
        )

    body = cast(dict[str, object], payload)

    # ── Parse user_id ──────────────────────────────────────────────────────
    raw_user_id = body.get("user_id")
    if not isinstance(raw_user_id, str) or not raw_user_id.strip():
        raise _create_auth_http_error(
            request=request,
            status_code=400,
            error_code="invalid_phone_update_request",
            message="user_id is required.",
            reason="invalid_phone_update_request",
            details={},
        )
    try:
        user_id = UUID(raw_user_id.strip())
    except ValueError:
        raise _create_auth_http_error(
            request=request,
            status_code=400,
            error_code="invalid_phone_update_request",
            message="user_id is not a valid UUID.",
            reason="invalid_phone_update_request",
            details={},
        )

    # ── Parse + normalize new phone ────────────────────────────────────────
    raw_phone = body.get("new_phone_number")
    if not isinstance(raw_phone, str) or not raw_phone.strip():
        raise _create_auth_http_error(
            request=request,
            status_code=400,
            error_code="invalid_phone_update_request",
            message="new_phone_number is required.",
            reason="invalid_phone_update_request",
            details={},
        )
    phone_normalized = _normalize_reg_phone(raw_phone.strip())
    if not phone_normalized:
        raise _create_auth_http_error(
            request=request,
            status_code=400,
            error_code="registration_invalid_phone",
            message="Phone number is not a valid Kenyan mobile number.",
            reason="registration_invalid_phone",
            details={},
        )

    # ── Verify user exists and is still pending verification ──────────────
    user_record = registration_store.get_user_by_id(user_id=user_id)
    if user_record is None or user_record.account_state != "pending_verification":
        raise _create_auth_http_error(
            request=request,
            status_code=404,
            error_code="registration_not_found",
            message="Pending registration not found.",
            reason="registration_not_found",
            details={},
        )

    # ── Rate limiting ──────────────────────────────────────────────────────
    user_key = str(user_id)
    now = datetime.now(UTC)
    with _REG_PHONE_UPDATE_LOCK:
        record = _REG_PHONE_UPDATE_STORE.get(user_key)
        if record is not None:
            elapsed = (now - record.last_changed_at).total_seconds()
            if elapsed < _REG_PHONE_UPDATE_COOLDOWN_SECONDS:
                wait = int(_REG_PHONE_UPDATE_COOLDOWN_SECONDS - elapsed)
                raise _create_auth_http_error(
                    request=request,
                    status_code=429,
                    error_code="registration_phone_update_cooldown",
                    message=f"Please wait {wait} seconds before changing your phone number again.",
                    reason="registration_phone_update_cooldown",
                    details={"retry_after_seconds": wait},
                )
            if record.attempts >= _REG_PHONE_UPDATE_MAX_ATTEMPTS:
                raise _create_auth_http_error(
                    request=request,
                    status_code=429,
                    error_code="registration_phone_update_limit_exceeded",
                    message="Maximum phone number changes reached. Contact support.",
                    reason="registration_phone_update_limit_exceeded",
                    details={"max_attempts": _REG_PHONE_UPDATE_MAX_ATTEMPTS},
                )

    # ── Duplicate check ────────────────────────────────────────────────────
    existing_owner = registration_store.get_user_by_phone(
        phone_number_normalized=phone_normalized
    )
    if existing_owner is not None and existing_owner.user_id != user_id:
        raise _create_auth_http_error(
            request=request,
            status_code=409,
            error_code="registration_duplicate_phone",
            message="This phone number is already registered to another account.",
            reason="registration_duplicate_phone",
            details={},
        )

    # ── Update phone in DB ─────────────────────────────────────────────────
    try:
        registration_store.update_user_phone_number(
            user_id=user_id,
            phone_number_normalized=phone_normalized,
        )
    except RegistrationConflictError as error:
        raise _create_auth_http_error(
            request=request,
            status_code=409,
            error_code=error.error_code,
            message=error.message,
            reason=error.reason,
            details={},
        ) from error
    except RegistrationPersistenceError as error:
        raise _create_auth_http_error(
            request=request,
            status_code=error.status_code,
            error_code=error.error_code,
            message=error.message,
            reason=error.reason,
            details=error.details,
        ) from error

    # ── Update rate-limit record ───────────────────────────────────────────
    with _REG_PHONE_UPDATE_LOCK:
        prev = _REG_PHONE_UPDATE_STORE.get(user_key)
        new_attempts = (prev.attempts + 1) if prev is not None else 1
        _REG_PHONE_UPDATE_STORE[user_key] = _RegistrationPhoneUpdateRecord(
            attempts=new_attempts,
            last_changed_at=now,
        )

    # ── Issue + send new OTP to new number ────────────────────────────────
    challenge_request = parse_phone_verification_challenge_request({
        "purpose": "registration_verify",
        "channel": "sms",
        "phone_number": phone_normalized,
    })
    try:
        challenge_response = issue_phone_verification_challenge(
            request_model=challenge_request,
            idempotency_key=idempotency_key,
            phone_verification_store=phone_verification_store,
            sms_delivery_adapter=sms_delivery_adapter,
        )
    except PhoneVerificationError as error:
        raise _create_auth_http_error(
            request=request,
            status_code=error.status_code,
            error_code=error.error_code,
            message=error.message,
            reason=error.reason,
            details=error.details,
        ) from error

    attempts_remaining = _REG_PHONE_UPDATE_MAX_ATTEMPTS - new_attempts

    _emit_auth_audit_event(
        request=request,
        auth_audit_store=auth_audit_store,
        event_type="auth_registration_requested",
        user_id=user_id,
        tenant_id=DEFAULT_TENANT_ID,
        session_id=None,
        action_status="phone_updated",
        reason_code=None,
        details={"updated_phone": phone_normalized},
    )
    _emit_auth_structured_log_event(
        request=request,
        event_type=_AUTH_LOG_EVENT_REGISTRATION,
        event_status="phone_updated",
        reason_code=None,
        user_id=user_id,
        tenant_id=DEFAULT_TENANT_ID,
        details={"attempts_remaining": attempts_remaining},
    )
    auth_metrics_emitter.increment_counter_non_blocking(AUTH_OTP_CHALLENGE_ISSUED_TOTAL)

    return RegistrationPhoneUpdateEnvelope(
        status="phone_updated",
        challenge_id=str(challenge_response.challenge_id),
        expires_at=challenge_response.expires_at,
        updated_phone_number=phone_normalized,
        attempts_remaining=attempts_remaining,
    )


@ROUTER.post("/v1/auth/login", response_model=LoginResponseEnvelope, status_code=200)
def login_auth_user(
    request: Request,
    payload: Annotated[object, Body(...)],
    registration_store: Annotated[RegistrationStoreProtocol, Depends(get_registration_store)],
    session_issuance_store: Annotated[
        SessionIssuanceStoreProtocol, Depends(get_session_issuance_store)
    ],
    login_lockout_store: Annotated[LoginLockoutStoreProtocol, Depends(get_login_lockout_store)],
    login_step_up_store: Annotated[LoginStepUpStoreProtocol, Depends(get_login_step_up_store)],
    email_verification_store: Annotated[
        EmailVerificationStoreProtocol, Depends(get_email_verification_store)
    ],
    phone_verification_store: Annotated[
        PhoneVerificationStoreProtocol, Depends(get_phone_verification_store)
    ],
    sms_delivery_adapter: Annotated[
        SmsOtpDeliveryAdapterProtocol, Depends(get_sms_delivery_adapter)
    ],
    auth_audit_store: Annotated[AuthAuditStoreProtocol, Depends(get_auth_audit_store)],
    auth_metrics_emitter: Annotated[AuthMetricsEmitter, Depends(get_auth_metrics_emitter)],
) -> LoginResponseEnvelope:
    """Authenticate user credential request with deterministic validation semantics."""

    try:
        response = login_with_credentials(
            payload=payload,
            source_ip=_extract_source_ip(request),
            registration_store=registration_store,
            session_issuance_store=session_issuance_store,
            login_lockout_store=login_lockout_store,
            login_step_up_store=login_step_up_store,
            email_verification_store=email_verification_store,
            phone_verification_store=phone_verification_store,
            sms_delivery_adapter=sms_delivery_adapter,
        )
        if isinstance(response, LoginSuccessEnvelope):
            _emit_auth_audit_event(
                request=request,
                auth_audit_store=auth_audit_store,
                event_type="auth_login_succeeded",
                user_id=response.session.user_id,
                tenant_id=response.session.tenant_id,
                session_id=response.session.session_id,
                action_status="succeeded",
                reason_code=None,
                details={
                    "status": response.status,
                    "role": response.session.role,
                },
            )
            auth_metrics_emitter.increment_counter_non_blocking(
                AUTH_LOGIN_SUCCESS_TOTAL)
            auth_metrics_emitter.increment_counter_non_blocking(
                AUTH_SESSION_ISSUED_TOTAL)
            _emit_auth_structured_log_event(
                request=request,
                event_type=AUTH_LOG_EVENT_LOGIN,
                event_status="succeeded",
                reason_code=None,
                user_id=response.session.user_id,
                tenant_id=response.session.tenant_id,
                details={"status": response.status},
            )
        else:
            auth_metrics_emitter.increment_counter_non_blocking(
                AUTH_OTP_CHALLENGE_ISSUED_TOTAL,
                dimensions={
                    "channel": response.step_up_channel,
                    "purpose": response.step_up_purpose,
                    "provider": _resolve_otp_provider_for_channel(response.step_up_channel),
                },
            )
            _emit_auth_structured_log_event(
                request=request,
                event_type=AUTH_LOG_EVENT_LOGIN,
                event_status="pending_step_up",
                reason_code=None,
                user_id=None,
                tenant_id=DEFAULT_TENANT_ID,
                details={
                    "step_up_channel": response.step_up_channel,
                    "step_up_purpose": response.step_up_purpose,
                },
            )
        return response
    except LoginError as error:
        auth_metrics_emitter.increment_counter_non_blocking(
            AUTH_LOGIN_FAILURE_TOTAL,
            dimensions={"reason_code": error.reason},
        )
        _emit_auth_audit_event(
            request=request,
            auth_audit_store=auth_audit_store,
            event_type="auth_login_failed",
            user_id=None,
            tenant_id=DEFAULT_TENANT_ID,
            session_id=None,
            action_status="failed",
            reason_code=error.reason,
            details=error.details,
        )
        if is_login_lockout_reason(reason=error.reason):
            auth_metrics_emitter.increment_counter_non_blocking(
                AUTH_LOCKOUT_APPLIED_TOTAL,
                dimensions={"reason_code": error.reason},
            )
            _emit_auth_audit_event(
                request=request,
                auth_audit_store=auth_audit_store,
                event_type="auth_lockout_applied",
                user_id=None,
                tenant_id=DEFAULT_TENANT_ID,
                session_id=None,
                action_status="applied",
                reason_code=error.reason,
                details=error.details,
            )
        raise _create_auth_http_error(
            request=request,
            status_code=error.status_code,
            error_code=error.error_code,
            message=error.message,
            reason=error.reason,
            details=error.details,
        ) from error


@ROUTER.post("/v1/auth/login/email-otp", response_model=EmailOtpLoginResponseEnvelope, status_code=200)
def login_auth_user_email_otp(
    request: Request,
    payload: Annotated[object, Body(...)],
    registration_store: Annotated[RegistrationStoreProtocol, Depends(get_registration_store)],
    session_issuance_store: Annotated[
        SessionIssuanceStoreProtocol, Depends(get_session_issuance_store)
    ],
    login_lockout_store: Annotated[LoginLockoutStoreProtocol, Depends(get_login_lockout_store)],
    login_step_up_store: Annotated[LoginStepUpStoreProtocol, Depends(get_login_step_up_store)],
    email_verification_store: Annotated[
        EmailVerificationStoreProtocol, Depends(get_email_verification_store)
    ],
    email_delivery_adapter: Annotated[
        EmailOtpDeliveryAdapterProtocol, Depends(get_email_delivery_adapter)
    ],
    auth_audit_store: Annotated[AuthAuditStoreProtocol, Depends(get_auth_audit_store)],
    auth_metrics_emitter: Annotated[AuthMetricsEmitter, Depends(get_auth_metrics_emitter)],
) -> EmailOtpLoginResponseEnvelope:
    """Authenticate user via email address and OTP (passwordless)."""

    try:
        response = login_with_email_otp(
            payload=payload,
            source_ip=_extract_source_ip(request),
            registration_store=registration_store,
            session_issuance_store=session_issuance_store,
            login_lockout_store=login_lockout_store,
            login_step_up_store=login_step_up_store,
            email_verification_store=email_verification_store,
            email_delivery_adapter=email_delivery_adapter,
        )
        if isinstance(response, LoginSuccessEnvelope):
            _emit_auth_audit_event(
                request=request,
                auth_audit_store=auth_audit_store,
                event_type="auth_login_succeeded",
                user_id=response.session.user_id,
                tenant_id=response.session.tenant_id,
                session_id=response.session.session_id,
                action_status="succeeded",
                reason_code=None,
                details={
                    "status": response.status,
                    "role": response.session.role,
                    "method": "email_otp",
                },
            )
            auth_metrics_emitter.increment_counter_non_blocking(AUTH_LOGIN_SUCCESS_TOTAL)
            auth_metrics_emitter.increment_counter_non_blocking(AUTH_SESSION_ISSUED_TOTAL)
            _emit_auth_structured_log_event(
                request=request,
                event_type=AUTH_LOG_EVENT_EMAIL_OTP_LOGIN,
                event_status="succeeded",
                reason_code=None,
                user_id=response.session.user_id,
                tenant_id=response.session.tenant_id,
                details={"status": response.status, "method": "email_otp"},
            )
        else:
            auth_metrics_emitter.increment_counter_non_blocking(
                AUTH_OTP_CHALLENGE_ISSUED_TOTAL,
                dimensions={
                    "channel": "email",
                    "purpose": "login_step_up",
                    "provider": _resolve_otp_provider_for_channel("email"),
                },
            )
            _emit_auth_structured_log_event(
                request=request,
                event_type=AUTH_LOG_EVENT_EMAIL_OTP_LOGIN,
                event_status="pending_step_up",
                reason_code=None,
                user_id=None,
                tenant_id=DEFAULT_TENANT_ID,
                details={"step_up_channel": "email", "step_up_purpose": "login_step_up"},
            )
        return response
    except LoginError as error:
        auth_metrics_emitter.increment_counter_non_blocking(
            AUTH_LOGIN_FAILURE_TOTAL,
            dimensions={"reason_code": error.reason},
        )
        _emit_auth_audit_event(
            request=request,
            auth_audit_store=auth_audit_store,
            event_type="auth_login_failed",
            user_id=None,
            tenant_id=DEFAULT_TENANT_ID,
            session_id=None,
            action_status="failed",
            reason_code=error.reason,
            details=error.details,
        )
        if is_login_lockout_reason(reason=error.reason):
            auth_metrics_emitter.increment_counter_non_blocking(
                AUTH_LOCKOUT_APPLIED_TOTAL,
                dimensions={"reason_code": error.reason},
            )
            _emit_auth_audit_event(
                request=request,
                auth_audit_store=auth_audit_store,
                event_type="auth_lockout_applied",
                user_id=None,
                tenant_id=DEFAULT_TENANT_ID,
                session_id=None,
                action_status="applied",
                reason_code=error.reason,
                details=error.details,
            )
        raise _create_auth_http_error(
            request=request,
            status_code=error.status_code,
            error_code=error.error_code,
            message=error.message,
            reason=error.reason,
            details=error.details,
        ) from error


@ROUTER.post("/v1/auth/refresh", response_model=RefreshResponseEnvelope, status_code=200)
def refresh_auth_session(
    request: Request,
    payload: Annotated[object, Body(...)],
    session_issuance_store: Annotated[
        SessionIssuanceStoreProtocol, Depends(get_session_issuance_store)
    ],
    auth_audit_store: Annotated[AuthAuditStoreProtocol, Depends(get_auth_audit_store)],
    auth_metrics_emitter: Annotated[AuthMetricsEmitter, Depends(get_auth_metrics_emitter)],
) -> RefreshResponseEnvelope:
    """Rotate deterministic refresh token and issue updated auth token pair."""

    try:
        refresh_request = _parse_refresh_request(payload=payload)
        refresh_result = session_issuance_store.refresh_session(
            refresh_token=refresh_request.refresh_token
        )
        session_record = session_issuance_store.get_session(
            session_id=refresh_result.session_id)
        if session_record is None:
            raise SessionIssuanceError(
                status_code=401,
                error_code="refresh_token_invalid",
                message="Refresh token is invalid.",
                reason="refresh_token_invalid",
            )
        response = RefreshResponseEnvelope(
            status="refreshed",
            access_token=refresh_result.access_token,
            refresh_token=refresh_result.refresh_token,
            expires_at=refresh_result.expires_at,
            session=SessionContextEnvelope(
                user_id=session_record.user_id,
                tenant_id=session_record.tenant_id,
                role=session_record.role,
                session_id=session_record.session_id,
                delegation_context=DelegationContextEnvelope(
                    is_delegated=False,
                ),
            ),
        )
        _emit_auth_audit_event(
            request=request,
            auth_audit_store=auth_audit_store,
            event_type="auth_session_refreshed",
            user_id=response.session.user_id,
            tenant_id=response.session.tenant_id,
            session_id=response.session.session_id,
            action_status="succeeded",
            reason_code=None,
            details={"status": response.status, "role": response.session.role},
        )
        auth_metrics_emitter.increment_counter_non_blocking(
            AUTH_SESSION_REFRESH_SUCCESS_TOTAL)
        _emit_auth_structured_log_event(
            request=request,
            event_type=_AUTH_LOG_EVENT_SESSION,
            event_status="refreshed",
            reason_code=None,
            user_id=response.session.user_id,
            tenant_id=response.session.tenant_id,
            details={"status": response.status},
        )
        return response
    except SessionIssuanceError as error:
        auth_metrics_emitter.increment_counter_non_blocking(
            AUTH_SESSION_REFRESH_FAILURE_TOTAL,
            dimensions={"reason_code": error.reason},
        )
        raise _create_auth_http_error(
            request=request,
            status_code=error.status_code,
            error_code=error.error_code,
            message=error.message,
            reason=error.reason,
            details=error.details,
        ) from error


@ROUTER.post("/v1/auth/logout", response_model=LogoutResponseEnvelope, status_code=200)
def logout_auth_session(
    request: Request,
    payload: Annotated[object, Body(...)],
    session_issuance_store: Annotated[
        SessionIssuanceStoreProtocol, Depends(get_session_issuance_store)
    ],
    auth_audit_store: Annotated[AuthAuditStoreProtocol, Depends(get_auth_audit_store)],
) -> LogoutResponseEnvelope:
    """Revoke one or all owned sessions with deterministic logout semantics."""

    try:
        auth_user_id = _parse_authenticated_user_id(
            authorization_header=request.headers.get("Authorization")
        )
        logout_request = _parse_logout_request(payload=payload)
        if logout_request.revoke_scope == "single_session":
            target_session_id = logout_request.target_session_id
            if target_session_id is None:
                raise SessionIssuanceError(
                    status_code=400,
                    error_code="logout_invalid_request",
                    message="Invalid logout request payload.",
                    reason="logout_invalid_request",
                )
            revoked_session_count = session_issuance_store.revoke_session(
                user_id=auth_user_id,
                session_id=target_session_id,
            )
        else:
            revoked_session_count = session_issuance_store.revoke_all_sessions_for_user(
                user_id=auth_user_id
            )

        correlation_id = get_correlation_id(request)
        trace_id = sha256(correlation_id.encode("utf-8")).hexdigest()
        response = LogoutResponseEnvelope(
            status="revoked",
            revoke_scope=logout_request.revoke_scope,
            revoked_session_count=revoked_session_count,
            traceability=TraceabilityEnvelope(
                trace_id=trace_id,
                correlation_id=correlation_id,
            ),
        )
        _emit_auth_audit_event(
            request=request,
            auth_audit_store=auth_audit_store,
            event_type="auth_session_revoked",
            user_id=auth_user_id,
            tenant_id=DEFAULT_TENANT_ID,
            session_id=logout_request.target_session_id,
            action_status="succeeded",
            reason_code=None,
            details={
                "revoke_scope": logout_request.revoke_scope,
                "revoked_session_count": revoked_session_count,
            },
        )
        _emit_auth_structured_log_event(
            request=request,
            event_type=_AUTH_LOG_EVENT_SESSION,
            event_status="revoked",
            reason_code=None,
            user_id=auth_user_id,
            tenant_id=DEFAULT_TENANT_ID,
            details={
                "revoke_scope": logout_request.revoke_scope,
                "revoked_session_count": revoked_session_count,
            },
        )
        return response
    except SessionIssuanceError as error:
        raise _create_auth_http_error(
            request=request,
            status_code=error.status_code,
            error_code=error.error_code,
            message=error.message,
            reason=error.reason,
            details=error.details,
        ) from error


@ROUTER.get(
    "/v1/auth/sessions/{session_id}",
    response_model=SessionIntrospectionResponseEnvelope,
    status_code=200,
)
def get_auth_session(
    request: Request,
    session_id: Annotated[UUID, Path(...)],
    session_issuance_store: Annotated[
        SessionIssuanceStoreProtocol, Depends(get_session_issuance_store)
    ],
) -> SessionIntrospectionResponseEnvelope:
    """Return deterministic session-introspection status for owned session context."""

    try:
        auth_user_id = _parse_authenticated_user_id_for_session_introspection(
            authorization_header=request.headers.get("Authorization")
        )
        session_record = session_issuance_store.get_session(
            session_id=session_id)
        if session_record is None or session_record.user_id != auth_user_id:
            raise SessionIssuanceError(
                status_code=404,
                error_code="session_not_found_or_not_owned",
                message="Session is not found or not owned by principal.",
                reason="session_not_found_or_not_owned",
            )
        evaluation = session_issuance_store.evaluate_session(
            session_id=session_id)
        if evaluation is None:
            raise SessionIssuanceError(
                status_code=404,
                error_code="session_not_found_or_not_owned",
                message="Session is not found or not owned by principal.",
                reason="session_not_found_or_not_owned",
            )
        correlation_id = get_correlation_id(request)
        trace_id = sha256(correlation_id.encode("utf-8")).hexdigest()
        return SessionIntrospectionResponseEnvelope(
            status=evaluation.status,
            session=SessionContextEnvelope(
                user_id=session_record.user_id,
                tenant_id=session_record.tenant_id,
                role=session_record.role,
                session_id=session_record.session_id,
                delegation_context=DelegationContextEnvelope(
                    is_delegated=False,
                ),
            ),
            issued_at=evaluation.issued_at,
            expires_at=evaluation.expires_at,
            inactivity_expires_at=evaluation.inactivity_expires_at,
            absolute_expires_at=evaluation.absolute_expires_at,
            last_activity_at=evaluation.last_activity_at,
            warning_window_started_at=evaluation.warning_window_started_at,
            extension_allowed=evaluation.extension_allowed,
            is_invalidated=evaluation.is_invalidated,
            traceability=TraceabilityEnvelope(
                trace_id=trace_id,
                correlation_id=correlation_id,
            ),
        )
    except SessionIssuanceError as error:
        raise _create_auth_http_error(
            request=request,
            status_code=error.status_code,
            error_code=error.error_code,
            message=error.message,
            reason=error.reason,
            details=error.details,
        ) from error


@ROUTER.post(
    "/v1/auth/roles/change",
    response_model=RoleChangeResponseEnvelope,
    status_code=200,
)
def change_auth_user_role(
    request: Request,
    payload: Annotated[object, Body(...)],
    principal: Annotated[Principal, Depends(require_role_change_principal)],
    registration_store: Annotated[RegistrationStoreProtocol, Depends(get_registration_store)],
    auth_audit_store: Annotated[AuthAuditStoreProtocol, Depends(get_auth_audit_store)],
) -> RoleChangeResponseEnvelope:
    """Apply deterministic governed role-change transition for one user."""

    target_user_id = None
    requested_new_role = ""
    try:
        request_model = _parse_role_change_request(payload=payload)
        target_user_id = request_model.target_user_id
        requested_new_role = request_model.new_role
        if target_user_id == principal.user_id:
            raise RoleChangeGovernanceError(
                status_code=403,
                error_code="role_change_self_escalation_forbidden",
                message="Self role-change is forbidden.",
                reason="role_change_self_escalation_forbidden",
                details={
                    "actor_user_id": str(principal.user_id),
                    "target_user_id": str(target_user_id),
                },
            )

        previous_role, updated_record = _apply_role_change(
            registration_store=registration_store,
            target_user_id=target_user_id,
            new_role=requested_new_role,
        )
        changed_at = _utc_now_iso()
        response = RoleChangeResponseEnvelope(
            status="role_updated",
            target_user_id=updated_record.user_id,
            previous_role=previous_role,
            new_role=updated_record.role,
            changed_by_user_id=principal.user_id,
            changed_at=changed_at,
            traceability=TraceabilityEnvelope(
                trace_id=get_trace_id(request),
                correlation_id=get_correlation_id(request),
            ),
        )
        _emit_auth_audit_event(
            request=request,
            auth_audit_store=auth_audit_store,
            event_type="auth_role_change_succeeded",
            user_id=principal.user_id,
            tenant_id=principal.tenant_id,
            session_id=principal.session_id,
            action_status="succeeded",
            reason_code=None,
            details={
                "actor_user_id": str(principal.user_id),
                "target_user_id": str(response.target_user_id),
                "previous_role": response.previous_role,
                "new_role": response.new_role,
                "actor_role": principal.role,
                "decision": "allowed",
                "changed_at": changed_at,
            },
        )
        _emit_auth_structured_log_event(
            request=request,
            event_type=_AUTH_LOG_EVENT_ROLE_CHANGE,
            event_status="succeeded",
            reason_code=None,
            user_id=principal.user_id,
            tenant_id=principal.tenant_id,
            details={
                "target_user_id": str(response.target_user_id),
                "previous_role": response.previous_role,
                "new_role": response.new_role,
                "decision": "allowed",
            },
        )
        return response
    except RoleChangeGovernanceError as error:
        _emit_auth_audit_event(
            request=request,
            auth_audit_store=auth_audit_store,
            event_type="auth_role_change_rejected",
            user_id=principal.user_id,
            tenant_id=principal.tenant_id,
            session_id=principal.session_id,
            action_status="rejected",
            reason_code=error.reason,
            details={
                "actor_user_id": str(principal.user_id),
                "target_user_id": (
                    str(target_user_id) if isinstance(
                        target_user_id, UUID) else "[unknown]"
                ),
                "requested_new_role": requested_new_role or "[unknown]",
                "actor_role": principal.role,
                "decision": "rejected",
            },
        )
        raise _create_auth_http_error(
            request=request,
            status_code=error.status_code,
            error_code=error.error_code,
            message=error.message,
            reason=error.reason,
            details=error.details,
        ) from error


@ROUTER.post(
    "/v1/auth/oauth/{provider}/start",
    response_model=OAuthStartResponseEnvelope,
    status_code=200,
)
def start_oauth_flow(
    request: Request,
    provider: Annotated[str, Path(...)],
    payload: Annotated[object, Body(...)],
    oauth_state_store: Annotated[OAuthStateStoreProtocol, Depends(get_oauth_state_store)],
    auth_metrics_emitter: Annotated[AuthMetricsEmitter, Depends(get_auth_metrics_emitter)],
) -> OAuthStartResponseEnvelope:
    """Start deterministic OAuth Authorization Code + PKCE flow."""

    try:
        try:
            request_model = OAuthStartRequestEnvelope.model_validate(payload)
        except Exception as error:
            raise OAuthFlowError(
                status_code=400,
                error_code="oauth_provider_config_invalid",
                message="OAuth provider configuration is invalid.",
                reason="oauth_provider_config_invalid",
                details={
                    "field": "redirect_uri",
                    "requirement": "valid_request_payload",
                },
            ) from error
        result = start_oauth_authorization(
            provider_id=provider,
            redirect_uri=request_model.redirect_uri,
            state_store=oauth_state_store,
            state_ttl_seconds=get_auth_oauth_state_ttl_seconds(),
        )
        _emit_auth_structured_log_event(
            request=request,
            event_type=_AUTH_LOG_EVENT_OAUTH,
            event_status="start_issued",
            reason_code=None,
            user_id=None,
            tenant_id=DEFAULT_TENANT_ID,
            details={"provider": result.provider_id},
        )
        return OAuthStartResponseEnvelope(
            status=result.status,
            provider=result.provider_id,
            authorization_url=result.authorization_url,
            state=result.state,
            nonce=result.nonce,
            expires_at=result.expires_at,
            traceability=TraceabilityEnvelope(
                trace_id=get_trace_id(request),
                correlation_id=get_correlation_id(request),
            ),
        )
    except OAuthFlowError as error:
        auth_metrics_emitter.increment_counter_non_blocking(
            AUTH_OAUTH_FAILURE_TOTAL,
            dimensions={
                "reason_code": error.reason,
                "provider": provider.strip().lower() or "unknown",
            },
        )
        raise _create_auth_http_error(
            request=request,
            status_code=error.status_code,
            error_code=error.error_code,
            message=error.message,
            reason=error.reason,
            details=error.details,
        ) from error


@ROUTER.get(
    "/v1/auth/oauth/{provider}/callback",
    response_model=OAuthCallbackResponseEnvelope,
    status_code=200,
)
def complete_oauth_callback_endpoint(
    request: Request,
    provider: Annotated[str, Path(...)],
    oauth_state_store: Annotated[OAuthStateStoreProtocol, Depends(get_oauth_state_store)],
    oauth_token_exchange_client: Annotated[
        OAuthTokenExchangeClientProtocol,
        Depends(get_oauth_token_exchange_client),
    ],
    registration_store: Annotated[RegistrationStoreProtocol, Depends(get_registration_store)],
    oauth_identity_linking_store: Annotated[
        OAuthIdentityLinkingStoreProtocol,
        Depends(get_oauth_identity_linking_store),
    ],
    oauth_jit_provisioning_policy: Annotated[
        OAuthJitProvisioningPolicy, Depends(get_oauth_jit_provisioning_policy)
    ],
    oauth_provider_resilience_policy: Annotated[
        OAuthProviderResiliencePolicy,
        Depends(get_oauth_provider_resilience_policy),
    ],
    oauth_provider_circuit_store: Annotated[
        OAuthProviderCircuitStoreProtocol,
        Depends(get_oauth_provider_circuit_store),
    ],
    oidc_id_token_validator: Annotated[
        OidcIdTokenValidatorProtocol, Depends(get_oidc_id_token_validator)
    ],
    auth_audit_store: Annotated[AuthAuditStoreProtocol, Depends(get_auth_audit_store)],
    auth_metrics_emitter: Annotated[AuthMetricsEmitter, Depends(get_auth_metrics_emitter)],
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
) -> OAuthCallbackResponseEnvelope:
    """Validate deterministic OAuth callback state and perform token exchange."""

    try:
        result = complete_oauth_callback(
            provider_id=provider,
            state="" if state is None else state,
            code="" if code is None else code,
            state_store=oauth_state_store,
            token_exchange_client=oauth_token_exchange_client,
            id_token_validator=oidc_id_token_validator,
            registration_store=registration_store,
            identity_linking_store=oauth_identity_linking_store,
            jit_policy=oauth_jit_provisioning_policy,
            resilience_policy=oauth_provider_resilience_policy,
            circuit_store=oauth_provider_circuit_store,
            tenant_id=DEFAULT_TENANT_ID,
        )
        if result.provider_recovered:
            _emit_auth_audit_event(
                request=request,
                auth_audit_store=auth_audit_store,
                event_type="auth_oauth_provider_recovered",
                user_id=result.linked_user_id,
                tenant_id=result.linked_tenant_id,
                session_id=None,
                action_status="succeeded",
                reason_code=None,
                details={"provider_id": result.provider_id},
            )
        if result.jit_provisioned:
            _emit_auth_audit_event(
                request=request,
                auth_audit_store=auth_audit_store,
                event_type="jit_provisioning_allowed",
                user_id=result.linked_user_id,
                tenant_id=result.linked_tenant_id,
                session_id=None,
                action_status="succeeded",
                reason_code=None,
                details={
                    "provider_id": result.provider_id,
                    "jit_status": "jit_provisioning_allowed",
                },
            )
        _emit_auth_audit_event(
            request=request,
            auth_audit_store=auth_audit_store,
            event_type="auth_oauth_identity_link_succeeded",
            user_id=result.linked_user_id,
            tenant_id=result.linked_tenant_id,
            session_id=None,
            action_status="succeeded",
            reason_code=None,
            details={
                "provider_id": result.provider_id,
                "link_status": result.link_status,
            },
        )
        _emit_auth_structured_log_event(
            request=request,
            event_type=_AUTH_LOG_EVENT_OAUTH,
            event_status="callback_validated",
            reason_code=None,
            user_id=result.linked_user_id,
            tenant_id=result.linked_tenant_id,
            details={
                "provider": result.provider_id,
                "link_status": result.link_status,
            },
        )
        return OAuthCallbackResponseEnvelope(
            status=result.status,
            provider=result.provider_id,
            callback_status=result.status,
            oauth_subject=result.oauth_subject,
            linked_user_id=result.linked_user_id,
            linked_tenant_id=result.linked_tenant_id,
            link_status=result.link_status,
            traceability=TraceabilityEnvelope(
                trace_id=get_trace_id(request),
                correlation_id=get_correlation_id(request),
            ),
        )
    except OAuthFlowError as error:
        if error.reason.startswith("oauth_provider_"):
            provider_event_type = {
                "oauth_provider_degraded_mode_active": "auth_oauth_provider_degraded_mode_active",
                "oauth_provider_circuit_open": "auth_oauth_provider_circuit_open",
                "oauth_provider_recovery_in_progress": "auth_oauth_provider_recovery_in_progress",
            }.get(error.reason, "auth_oauth_provider_degraded_mode_active")
            _emit_auth_audit_event(
                request=request,
                auth_audit_store=auth_audit_store,
                event_type=provider_event_type,
                user_id=None,
                tenant_id=DEFAULT_TENANT_ID,
                session_id=None,
                action_status="rejected",
                reason_code=error.reason,
                details={
                    "provider_id": provider.strip().lower(),
                    "retry_after_seconds": error.details.get("retry_after_seconds"),
                },
            )
        if error.reason.startswith("oauth_jit_"):
            jit_event_type = (
                "jit_provisioning_conflict_detected"
                if error.reason == "oauth_jit_identity_conflict"
                else "jit_provisioning_denied"
            )
            _emit_auth_audit_event(
                request=request,
                auth_audit_store=auth_audit_store,
                event_type=jit_event_type,
                user_id=None,
                tenant_id=DEFAULT_TENANT_ID,
                session_id=None,
                action_status="rejected",
                reason_code=error.reason,
                details={"provider_id": provider.strip().lower()},
            )
        if error.reason.startswith("oauth_identity_"):
            if error.reason in {
                "oauth_identity_already_linked_to_different_user",
                "oauth_identity_claim_conflict",
                "oauth_identity_tenant_mismatch",
            }:
                audit_event_type = "auth_oauth_identity_link_suspicious"
            else:
                audit_event_type = "auth_oauth_identity_link_denied"
            _emit_auth_audit_event(
                request=request,
                auth_audit_store=auth_audit_store,
                event_type=audit_event_type,
                user_id=None,
                tenant_id=DEFAULT_TENANT_ID,
                session_id=None,
                action_status="rejected",
                reason_code=error.reason,
                details={"provider_id": provider.strip().lower()},
            )
        auth_metrics_emitter.increment_counter_non_blocking(
            AUTH_OAUTH_FAILURE_TOTAL,
            dimensions={
                "reason_code": error.reason,
                "provider": provider.strip().lower() or "unknown",
            },
        )
        raise _create_auth_http_error(
            request=request,
            status_code=error.status_code,
            error_code=error.error_code,
            message=error.message,
            reason=error.reason,
            details=error.details,
        ) from error


@ROUTER.post(
    "/v1/auth/password-reset/initiate",
    response_model=PasswordResetChallengeEnvelope,
    status_code=201,
)
def initiate_password_reset_endpoint(
    request: Request,
    payload: Annotated[object, Body(...)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    registration_store: Annotated[RegistrationStoreProtocol, Depends(get_registration_store)],
    password_reset_store: Annotated[PasswordResetStoreProtocol, Depends(get_password_reset_store)],
    auth_audit_store: Annotated[AuthAuditStoreProtocol, Depends(get_auth_audit_store)],
    auth_metrics_emitter: Annotated[AuthMetricsEmitter, Depends(get_auth_metrics_emitter)],
    email_delivery_adapter: Annotated[
        EmailOtpDeliveryAdapterProtocol, Depends(get_email_delivery_adapter)
    ],
) -> PasswordResetChallengeEnvelope:
    """Issue deterministic non-enumerating password-reset challenge."""

    try:
        request_model = parse_password_reset_initiate_request(payload)
        response = initiate_password_reset_challenge(
            request_model=request_model,
            idempotency_key=idempotency_key,
            registration_store=registration_store,
            password_reset_store=password_reset_store,
            email_delivery_adapter=email_delivery_adapter,
        )
        challenge_record = password_reset_store.get_challenge(
            challenge_id=response.challenge_id)
        _emit_auth_audit_event(
            request=request,
            auth_audit_store=auth_audit_store,
            event_type="auth_password_reset_requested",
            user_id=(
                None if challenge_record is None else challenge_record.user_id),
            tenant_id=DEFAULT_TENANT_ID,
            session_id=None,
            action_status="requested",
            reason_code=None,
            details={"status": response.status},
        )
        _emit_auth_structured_log_event(
            request=request,
            event_type=AUTH_LOG_EVENT_PASSWORD_RESET,
            event_status="challenge_issued",
            reason_code=None,
            user_id=(
                None if challenge_record is None else challenge_record.user_id),
            tenant_id=DEFAULT_TENANT_ID,
            details={
                "channel": request_model.channel,
                "purpose": request_model.purpose,
            },
        )
        auth_metrics_emitter.increment_counter_non_blocking(
            AUTH_OTP_CHALLENGE_ISSUED_TOTAL,
            dimensions={
                "channel": request_model.channel,
                "purpose": resolve_password_reset_metrics_purpose(
                    reset_purpose=request_model.purpose
                ),
                "provider": _resolve_otp_provider_for_channel(request_model.channel),
            },
        )
        return response
    except PasswordResetError as error:
        raise _create_auth_http_error(
            request=request,
            status_code=error.status_code,
            error_code=error.error_code,
            message=error.message,
            reason=error.reason,
            details=error.details,
        ) from error


@ROUTER.post(
    "/v1/auth/password-reset/confirm",
    response_model=PasswordResetConfirmEnvelope,
    status_code=200,
)
def confirm_password_reset_endpoint(
    request: Request,
    payload: Annotated[object, Body(...)],
    registration_store: Annotated[RegistrationStoreProtocol, Depends(get_registration_store)],
    password_reset_store: Annotated[PasswordResetStoreProtocol, Depends(get_password_reset_store)],
    session_issuance_store: Annotated[
        SessionIssuanceStoreProtocol, Depends(get_session_issuance_store)
    ],
    auth_audit_store: Annotated[AuthAuditStoreProtocol, Depends(get_auth_audit_store)],
    auth_metrics_emitter: Annotated[AuthMetricsEmitter, Depends(get_auth_metrics_emitter)],
) -> PasswordResetConfirmEnvelope:
    """Confirm deterministic password-reset challenge and update password securely."""

    try:
        request_model = parse_password_reset_confirm_request(payload)
        challenge_record = password_reset_store.get_challenge(
            challenge_id=request_model.challenge_id
        )
        response = confirm_password_reset_challenge(
            request_model=request_model,
            registration_store=registration_store,
            password_reset_store=password_reset_store,
        )
        revoked_session_count = 0
        if challenge_record is not None and challenge_record.user_id is not None:
            revoked_session_count = session_issuance_store.revoke_all_sessions_for_user(
                user_id=challenge_record.user_id
            )
        _emit_auth_audit_event(
            request=request,
            auth_audit_store=auth_audit_store,
            event_type="auth_password_reset_completed",
            user_id=(
                None if challenge_record is None else challenge_record.user_id),
            tenant_id=DEFAULT_TENANT_ID,
            session_id=None,
            action_status="completed",
            reason_code=None,
            details={
                "status": response.status,
                "revoked_session_count": revoked_session_count,
            },
        )
        _emit_auth_structured_log_event(
            request=request,
            event_type=AUTH_LOG_EVENT_PASSWORD_RESET,
            event_status="completed",
            reason_code=None,
            user_id=(
                None if challenge_record is None else challenge_record.user_id),
            tenant_id=DEFAULT_TENANT_ID,
            details={
                "status": response.status,
                "revoked_session_count": revoked_session_count,
            },
        )
        auth_metrics_emitter.increment_counter_non_blocking(
            AUTH_PASSWORD_RESET_CONFIRM_SUCCESS_TOTAL
        )
        return response
    except PasswordResetError as error:
        auth_metrics_emitter.increment_counter_non_blocking(
            AUTH_PASSWORD_RESET_CONFIRM_FAILURE_TOTAL,
            dimensions={"reason_code": error.reason},
        )
        raise _create_auth_http_error(
            request=request,
            status_code=error.status_code,
            error_code=error.error_code,
            message=error.message,
            reason=error.reason,
            details=error.details,
        ) from error


@ROUTER.post(
    "/v1/auth/account-deletion/requests",
    response_model=AccountDeletionRequestResponse,
    status_code=201,
)
def create_account_deletion_request_endpoint(
    request: Request,
    payload: Annotated[object, Body(...)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    registration_store: Annotated[RegistrationStoreProtocol, Depends(get_registration_store)],
    account_deletion_request_store: Annotated[
        AccountDeletionRequestStoreProtocol,
        Depends(get_account_deletion_request_store),
    ],
    auth_audit_store: Annotated[AuthAuditStoreProtocol, Depends(get_auth_audit_store)],
) -> AccountDeletionRequestResponse:
    """Create deterministic self-service account deletion request intent."""

    try:
        authorization_header = request.headers.get("Authorization")
        response = create_account_deletion_request(
            payload=payload,
            authorization_header=authorization_header,
            idempotency_key=idempotency_key,
            correlation_id=get_correlation_id(request),
            registration_store=registration_store,
            account_deletion_store=account_deletion_request_store,
        )
        request_record = account_deletion_request_store.get_request_by_id(
            request_id=response.request_id
        )
        blocker_reason = None
        if request_record is not None and request_record.blocker_reasons:
            blocker_reason = request_record.blocker_reasons[0]
        _emit_auth_audit_event(
            request=request,
            auth_audit_store=auth_audit_store,
            event_type="auth_account_deletion_requested",
            user_id=None if request_record is None else request_record.user_id,
            tenant_id=(
                DEFAULT_TENANT_ID if request_record is None else request_record.tenant_id),
            session_id=None,
            action_status=("requested" if response.deletion_state ==
                           "requested" else "rejected"),
            reason_code=blocker_reason,
            details={
                "deletion_state": response.deletion_state,
                "blockers": response.blockers,
            },
        )
        _emit_auth_structured_log_event(
            request=request,
            event_type=AUTH_LOG_EVENT_ACCOUNT_DELETION,
            event_status=response.deletion_state,
            reason_code=blocker_reason,
            user_id=None if request_record is None else request_record.user_id,
            tenant_id=(
                DEFAULT_TENANT_ID if request_record is None else request_record.tenant_id),
            details={
                "deletion_state": response.deletion_state,
                "request_id": str(response.request_id),
            },
        )
        return response
    except AccountDeletionRequestError as error:
        raise _create_auth_http_error(
            request=request,
            status_code=error.status_code,
            error_code=error.error_code,
            message=error.message,
            reason=error.reason,
            details=error.details,
        ) from error


@ROUTER.post(
    "/v1/auth/account-deletion/confirm",
    response_model=AccountDeletionConfirmResponse,
    status_code=200,
)
def confirm_account_deletion_request_endpoint(
    request: Request,
    payload: Annotated[object, Body(...)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    registration_store: Annotated[RegistrationStoreProtocol, Depends(get_registration_store)],
    account_deletion_request_store: Annotated[
        AccountDeletionRequestStoreProtocol,
        Depends(get_account_deletion_request_store),
    ],
    auth_audit_store: Annotated[AuthAuditStoreProtocol, Depends(get_auth_audit_store)],
) -> AccountDeletionConfirmResponse:
    """Confirm deterministic self-service account deletion request intent."""

    try:
        authorization_header = request.headers.get("Authorization")
        response = confirm_account_deletion_request(
            payload=payload,
            authorization_header=authorization_header,
            idempotency_key=idempotency_key,
            correlation_id=get_correlation_id(request),
            registration_store=registration_store,
            account_deletion_store=account_deletion_request_store,
        )
        request_record = account_deletion_request_store.get_request_by_id(
            request_id=response.request_id
        )
        _emit_auth_audit_event(
            request=request,
            auth_audit_store=auth_audit_store,
            event_type="auth_account_deletion_confirmed",
            user_id=None if request_record is None else request_record.user_id,
            tenant_id=(
                DEFAULT_TENANT_ID if request_record is None else request_record.tenant_id),
            session_id=None,
            action_status="confirmed",
            reason_code=None,
            details={"deletion_state": response.deletion_state},
        )
        _emit_auth_structured_log_event(
            request=request,
            event_type=AUTH_LOG_EVENT_ACCOUNT_DELETION,
            event_status="confirmed",
            reason_code=None,
            user_id=None if request_record is None else request_record.user_id,
            tenant_id=(
                DEFAULT_TENANT_ID if request_record is None else request_record.tenant_id),
            details={
                "deletion_state": response.deletion_state,
                "request_id": str(response.request_id),
            },
        )
        return response
    except AccountDeletionRequestError as error:
        raise _create_auth_http_error(
            request=request,
            status_code=error.status_code,
            error_code=error.error_code,
            message=error.message,
            reason=error.reason,
            details=error.details,
        ) from error


@ROUTER.post(
    "/v1/auth/account-deletion/cancel",
    response_model=AccountDeletionCancelResponse,
    status_code=200,
)
def cancel_account_deletion_request_endpoint(
    request: Request,
    payload: Annotated[object, Body(...)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    registration_store: Annotated[RegistrationStoreProtocol, Depends(get_registration_store)],
    account_deletion_request_store: Annotated[
        AccountDeletionRequestStoreProtocol,
        Depends(get_account_deletion_request_store),
    ],
) -> AccountDeletionCancelResponse:
    """Cancel deterministic self-service account deletion request during cooldown."""

    try:
        authorization_header = request.headers.get("Authorization")
        response = cancel_account_deletion_request(
            payload=payload,
            authorization_header=authorization_header,
            idempotency_key=idempotency_key,
            correlation_id=get_correlation_id(request),
            registration_store=registration_store,
            account_deletion_store=account_deletion_request_store,
        )
        request_record = account_deletion_request_store.get_request_by_id(
            request_id=response.request_id
        )
        _emit_auth_structured_log_event(
            request=request,
            event_type=AUTH_LOG_EVENT_ACCOUNT_DELETION,
            event_status="cancelled",
            reason_code=None,
            user_id=None if request_record is None else request_record.user_id,
            tenant_id=(
                DEFAULT_TENANT_ID if request_record is None else request_record.tenant_id),
            details={
                "deletion_state": response.deletion_state,
                "request_id": str(response.request_id),
            },
        )
        return response
    except AccountDeletionRequestError as error:
        raise _create_auth_http_error(
            request=request,
            status_code=error.status_code,
            error_code=error.error_code,
            message=error.message,
            reason=error.reason,
            details=error.details,
        ) from error


@ROUTER.post(
    "/v1/auth/account-deletion/execute",
    response_model=AccountDeletionExecuteResponse,
    status_code=200,
)
def execute_account_deletion_request_endpoint(
    request: Request,
    payload: Annotated[object, Body(...)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    registration_store: Annotated[RegistrationStoreProtocol, Depends(get_registration_store)],
    account_deletion_request_store: Annotated[
        AccountDeletionRequestStoreProtocol,
        Depends(get_account_deletion_request_store),
    ],
    auth_audit_store: Annotated[AuthAuditStoreProtocol, Depends(get_auth_audit_store)],
) -> AccountDeletionExecuteResponse:
    """Execute deterministic self-service account deletion request after cooldown."""

    try:
        authorization_header = request.headers.get("Authorization")
        response = execute_account_deletion_request(
            payload=payload,
            authorization_header=authorization_header,
            idempotency_key=idempotency_key,
            correlation_id=get_correlation_id(request),
            registration_store=registration_store,
            account_deletion_store=account_deletion_request_store,
        )
        request_record = account_deletion_request_store.get_request_by_id(
            request_id=response.request_id
        )
        _emit_auth_audit_event(
            request=request,
            auth_audit_store=auth_audit_store,
            event_type="auth_account_deletion_executed",
            user_id=None if request_record is None else request_record.user_id,
            tenant_id=(
                DEFAULT_TENANT_ID if request_record is None else request_record.tenant_id),
            session_id=None,
            action_status="executed",
            reason_code=None,
            details={
                "deletion_state": response.deletion_state,
                "execution_outcome": response.execution_outcome,
            },
        )
        _emit_auth_structured_log_event(
            request=request,
            event_type=AUTH_LOG_EVENT_ACCOUNT_DELETION,
            event_status="executed",
            reason_code=None,
            user_id=None if request_record is None else request_record.user_id,
            tenant_id=(
                DEFAULT_TENANT_ID if request_record is None else request_record.tenant_id),
            details={
                "deletion_state": response.deletion_state,
                "request_id": str(response.request_id),
                "execution_outcome": response.execution_outcome,
            },
        )
        return response
    except AccountDeletionRequestError as error:
        raise _create_auth_http_error(
            request=request,
            status_code=error.status_code,
            error_code=error.error_code,
            message=error.message,
            reason=error.reason,
            details=error.details,
        ) from error


@ROUTER.post(
    "/v1/auth/phone-change/requests",
    response_model=PhoneChangeRequestResponse,
    status_code=201,
)
def create_phone_change_request_endpoint(
    request: Request,
    payload: Annotated[object, Body(...)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    registration_store: Annotated[RegistrationStoreProtocol, Depends(get_registration_store)],
    phone_verification_store: Annotated[
        PhoneVerificationStoreProtocol, Depends(get_phone_verification_store)
    ],
    phone_change_store: Annotated[PhoneChangeStoreProtocol, Depends(get_phone_change_store)],
    auth_audit_store: Annotated[AuthAuditStoreProtocol, Depends(get_auth_audit_store)],
    sms_delivery_adapter: Annotated[
        SmsOtpDeliveryAdapterProtocol, Depends(get_sms_delivery_adapter)
    ],
) -> PhoneChangeRequestResponse:
    """Create deterministic phone-number change request with step-up challenge context."""

    try:
        authorization_header = request.headers.get("Authorization")
        response = create_phone_change_request(
            payload=payload,
            authorization_header=authorization_header,
            idempotency_key=idempotency_key,
            correlation_id=get_correlation_id(request),
            registration_store=registration_store,
            phone_verification_store=phone_verification_store,
            phone_change_store=phone_change_store,
            sms_delivery_adapter=sms_delivery_adapter,
        )
        request_record = phone_change_store.get_request_by_id(
            request_id=response.request_id)
        _emit_auth_audit_event(
            request=request,
            auth_audit_store=auth_audit_store,
            event_type="auth_phone_change_requested",
            user_id=None if request_record is None else request_record.user_id,
            tenant_id=(
                DEFAULT_TENANT_ID if request_record is None else request_record.tenant_id),
            session_id=None,
            action_status="requested",
            reason_code=None,
            details={"phone_change_state": response.phone_change_state},
        )
        _emit_auth_structured_log_event(
            request=request,
            event_type=AUTH_LOG_EVENT_PHONE_CHANGE,
            event_status="requested",
            reason_code=None,
            user_id=None if request_record is None else request_record.user_id,
            tenant_id=(
                DEFAULT_TENANT_ID if request_record is None else request_record.tenant_id),
            details={
                "phone_change_state": response.phone_change_state,
                "request_id": str(response.request_id),
            },
        )
        return response
    except PhoneChangeError as error:
        raise _create_auth_http_error(
            request=request,
            status_code=error.status_code,
            error_code=error.error_code,
            message=error.message,
            reason=error.reason,
            details=error.details,
        ) from error


@ROUTER.post(
    "/v1/auth/phone-change/confirm",
    response_model=PhoneChangeConfirmResponse,
    status_code=200,
)
def confirm_phone_change_request_endpoint(
    request: Request,
    payload: Annotated[object, Body(...)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    registration_store: Annotated[RegistrationStoreProtocol, Depends(get_registration_store)],
    phone_verification_store: Annotated[
        PhoneVerificationStoreProtocol, Depends(get_phone_verification_store)
    ],
    phone_change_store: Annotated[PhoneChangeStoreProtocol, Depends(get_phone_change_store)],
    auth_audit_store: Annotated[AuthAuditStoreProtocol, Depends(get_auth_audit_store)],
) -> PhoneChangeConfirmResponse:
    """Confirm deterministic phone-number change request with bound OTP proof."""

    try:
        authorization_header = request.headers.get("Authorization")
        response = confirm_phone_change_request(
            payload=payload,
            authorization_header=authorization_header,
            idempotency_key=idempotency_key,
            correlation_id=get_correlation_id(request),
            registration_store=registration_store,
            phone_verification_store=phone_verification_store,
            phone_change_store=phone_change_store,
        )
        request_record = phone_change_store.get_request_by_id(
            request_id=response.request_id)
        _emit_auth_audit_event(
            request=request,
            auth_audit_store=auth_audit_store,
            event_type="auth_phone_change_confirmed",
            user_id=None if request_record is None else request_record.user_id,
            tenant_id=(
                DEFAULT_TENANT_ID if request_record is None else request_record.tenant_id),
            session_id=None,
            action_status="confirmed",
            reason_code=None,
            details={"phone_change_state": response.phone_change_state},
        )
        _emit_auth_structured_log_event(
            request=request,
            event_type=AUTH_LOG_EVENT_PHONE_CHANGE,
            event_status="confirmed",
            reason_code=None,
            user_id=None if request_record is None else request_record.user_id,
            tenant_id=(
                DEFAULT_TENANT_ID if request_record is None else request_record.tenant_id),
            details={
                "phone_change_state": response.phone_change_state,
                "request_id": str(response.request_id),
            },
        )
        return response
    except PhoneChangeError as error:
        raise _create_auth_http_error(
            request=request,
            status_code=error.status_code,
            error_code=error.error_code,
            message=error.message,
            reason=error.reason,
            details=error.details,
        ) from error


@ROUTER.post(
    "/v1/auth/otp/challenges",
    response_model=EmailVerificationChallengeEnvelope,
    status_code=201,
)
def issue_email_verification_challenge_endpoint(
    request: Request,
    payload: Annotated[object, Body(...)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    email_verification_store: Annotated[
        EmailVerificationStoreProtocol, Depends(get_email_verification_store)
    ],
    phone_verification_store: Annotated[
        PhoneVerificationStoreProtocol, Depends(get_phone_verification_store)
    ],
    sms_delivery_adapter: Annotated[
        SmsOtpDeliveryAdapterProtocol, Depends(get_sms_delivery_adapter)
    ],
    email_delivery_adapter: Annotated[
        EmailOtpDeliveryAdapterProtocol, Depends(get_email_delivery_adapter)
    ],
    auth_audit_store: Annotated[AuthAuditStoreProtocol, Depends(get_auth_audit_store)],
    auth_metrics_emitter: Annotated[AuthMetricsEmitter, Depends(get_auth_metrics_emitter)],
) -> EmailVerificationChallengeEnvelope | PhoneVerificationChallengeEnvelope:
    """Issue deterministic OTP challenge using canonical purpose-scoped contract."""

    try:
        challenge_channel = _resolve_challenge_channel(payload)
        if challenge_channel not in {"sms", "email"}:
            raise PhoneVerificationError(
                status_code=400,
                error_code="invalid_otp_challenge_request",
                message="Invalid OTP challenge request payload.",
                reason="unsupported_otp_challenge_context",
            )

        if challenge_channel == "sms":
            challenge_request = parse_phone_verification_challenge_request(
                payload)
            response = issue_phone_verification_challenge(
                request_model=challenge_request,
                idempotency_key=idempotency_key,
                phone_verification_store=phone_verification_store,
                email_verification_store=email_verification_store,
                email_delivery_adapter=email_delivery_adapter,
                sms_delivery_adapter=sms_delivery_adapter,
            )
            _emit_auth_audit_event(
                request=request,
                auth_audit_store=auth_audit_store,
                event_type="auth_otp_challenge_issued",
                user_id=None,
                tenant_id=DEFAULT_TENANT_ID,
                session_id=None,
                action_status="requested",
                reason_code=None,
                details={
                    "purpose": challenge_request.purpose,
                    "channel": challenge_request.channel,
                    "status": response.status,
                },
            )
            auth_metrics_emitter.increment_counter_non_blocking(
                AUTH_OTP_CHALLENGE_ISSUED_TOTAL,
                dimensions={
                    "channel": challenge_request.channel,
                    "purpose": challenge_request.purpose,
                    "provider": _resolve_otp_provider_for_channel(challenge_request.channel),
                },
            )
            _emit_auth_structured_log_event(
                request=request,
                event_type=AUTH_LOG_EVENT_PHONE_VERIFICATION,
                event_status="challenge_issued",
                reason_code=None,
                user_id=None,
                tenant_id=DEFAULT_TENANT_ID,
                details={
                    "channel": challenge_request.channel,
                    "purpose": challenge_request.purpose,
                },
            )
            return response

        challenge_request = parse_email_verification_challenge_request(payload)
        response = issue_email_verification_challenge(
            request_model=challenge_request,
            idempotency_key=idempotency_key,
            email_verification_store=email_verification_store,
            email_delivery_adapter=email_delivery_adapter,
        )
        _emit_auth_audit_event(
            request=request,
            auth_audit_store=auth_audit_store,
            event_type="auth_otp_challenge_issued",
            user_id=None,
            tenant_id=DEFAULT_TENANT_ID,
            session_id=None,
            action_status="requested",
            reason_code=None,
            details={
                "purpose": challenge_request.purpose,
                "channel": challenge_request.channel,
                "status": response.status,
            },
        )
        auth_metrics_emitter.increment_counter_non_blocking(
            AUTH_OTP_CHALLENGE_ISSUED_TOTAL,
            dimensions={
                "channel": challenge_request.channel,
                "purpose": challenge_request.purpose,
                "provider": _resolve_otp_provider_for_channel(challenge_request.channel),
            },
        )
        _emit_auth_structured_log_event(
            request=request,
            event_type=AUTH_LOG_EVENT_EMAIL_VERIFICATION,
            event_status="challenge_issued",
            reason_code=None,
            user_id=None,
            tenant_id=DEFAULT_TENANT_ID,
            details={
                "channel": challenge_request.channel,
                "purpose": challenge_request.purpose,
            },
        )
        return response
    except EmailVerificationError as error:
        raise _create_auth_http_error(
            request=request,
            status_code=error.status_code,
            error_code=error.error_code,
            message=error.message,
            reason=error.reason,
            details=error.details,
        ) from error
    except PhoneVerificationError as error:
        normalized_error = _normalize_phone_otp_challenge_http_error(
            error=error)
        raise _create_auth_http_error(
            request=request,
            status_code=normalized_error.status_code,
            error_code=normalized_error.error_code,
            message=normalized_error.message,
            reason=normalized_error.reason,
            details=normalized_error.details,
        ) from error


@ROUTER.post(
    "/v1/auth/otp/verify",
    response_model=EmailVerificationVerifyEnvelope,
    status_code=200,
)
def verify_email_verification_challenge_endpoint(
    request: Request,
    payload: Annotated[object, Body(...)],
    email_verification_store: Annotated[
        EmailVerificationStoreProtocol, Depends(get_email_verification_store)
    ],
    phone_verification_store: Annotated[
        PhoneVerificationStoreProtocol, Depends(get_phone_verification_store)
    ],
    registration_store: Annotated[RegistrationStoreProtocol, Depends(get_registration_store)],
    auth_audit_store: Annotated[AuthAuditStoreProtocol, Depends(get_auth_audit_store)],
    auth_metrics_emitter: Annotated[AuthMetricsEmitter, Depends(get_auth_metrics_emitter)],
) -> EmailVerificationVerifyEnvelope | PhoneVerificationVerifyEnvelope:
    """Verify deterministic email-verification token and transition user state."""

    try:
        phone_verify_request = parse_phone_verification_verify_request(payload)
        phone_challenge_record = phone_verification_store.get_challenge(
            challenge_id=phone_verify_request.challenge_id
        )
        if phone_challenge_record is not None:
            response = verify_phone_verification_challenge(
                verify_request=phone_verify_request,
                phone_verification_store=phone_verification_store,
                registration_store=registration_store,
            )
            _emit_auth_audit_event(
                request=request,
                auth_audit_store=auth_audit_store,
                event_type="auth_otp_challenge_verified",
                user_id=None,
                tenant_id=DEFAULT_TENANT_ID,
                session_id=None,
                action_status="verified",
                reason_code=None,
                details={"purpose": phone_challenge_record.purpose},
            )
            auth_metrics_emitter.increment_counter_non_blocking(
                AUTH_OTP_VERIFY_SUCCESS_TOTAL,
                dimensions={
                    "channel": PHONE_VERIFICATION_CHANNEL,
                    "purpose": phone_challenge_record.purpose,
                },
            )
            _emit_auth_structured_log_event(
                request=request,
                event_type=AUTH_LOG_EVENT_PHONE_VERIFICATION,
                event_status="verified",
                reason_code=None,
                user_id=None,
                tenant_id=DEFAULT_TENANT_ID,
                details={
                    "channel": PHONE_VERIFICATION_CHANNEL,
                    "purpose": phone_challenge_record.purpose,
                },
            )
            if phone_challenge_record.purpose == "registration_verify":
                registered_user = registration_store.get_user_by_phone(
                    phone_number_normalized=phone_challenge_record.phone_number_normalized
                )
                _emit_auth_audit_event(
                    request=request,
                    auth_audit_store=auth_audit_store,
                    event_type="auth_registration_verified",
                    user_id=(
                        None if registered_user is None else registered_user.user_id),
                    tenant_id=DEFAULT_TENANT_ID,
                    session_id=None,
                    action_status="verified",
                    reason_code=None,
                    details={"channel": PHONE_VERIFICATION_CHANNEL},
                )
            return response

        email_verify_request = parse_email_verification_verify_request(payload)
        email_challenge_record = email_verification_store.get_challenge(
            challenge_id=email_verify_request.challenge_id
        )
        response = verify_email_verification_challenge(
            verify_request=email_verify_request,
            email_verification_store=email_verification_store,
            registration_store=registration_store,
        )
        challenge_purpose = (
            "registration_verify"
            if email_challenge_record is None
            else email_challenge_record.purpose
        )
        _emit_auth_audit_event(
            request=request,
            auth_audit_store=auth_audit_store,
            event_type="auth_otp_challenge_verified",
            user_id=None,
            tenant_id=DEFAULT_TENANT_ID,
            session_id=None,
            action_status="verified",
            reason_code=None,
            details={"purpose": challenge_purpose},
        )
        auth_metrics_emitter.increment_counter_non_blocking(
            AUTH_OTP_VERIFY_SUCCESS_TOTAL,
            dimensions={
                "channel": EMAIL_VERIFICATION_CHANNEL,
                "purpose": challenge_purpose,
            },
        )
        _emit_auth_structured_log_event(
            request=request,
            event_type=AUTH_LOG_EVENT_EMAIL_VERIFICATION,
            event_status="verified",
            reason_code=None,
            user_id=None,
            tenant_id=DEFAULT_TENANT_ID,
            details={
                "channel": EMAIL_VERIFICATION_CHANNEL,
                "purpose": challenge_purpose,
            },
        )
        if challenge_purpose == "registration_verify" and email_challenge_record is not None:
            registered_user = registration_store.get_user_by_email(
                email_normalized=email_challenge_record.email_normalized
            )
            _emit_auth_audit_event(
                request=request,
                auth_audit_store=auth_audit_store,
                event_type="auth_registration_verified",
                user_id=(
                    None if registered_user is None else registered_user.user_id),
                tenant_id=DEFAULT_TENANT_ID,
                session_id=None,
                action_status="verified",
                reason_code=None,
                details={"channel": EMAIL_VERIFICATION_CHANNEL},
            )
        return response
    except EmailVerificationError as error:
        auth_metrics_emitter.increment_counter_non_blocking(
            AUTH_OTP_VERIFY_FAILURE_TOTAL,
            dimensions=_build_otp_verify_failure_dimensions(
                payload=payload,
                reason_code=error.reason,
                email_verification_store=email_verification_store,
                phone_verification_store=phone_verification_store,
            ),
        )
        raise _create_auth_http_error(
            request=request,
            status_code=error.status_code,
            error_code=error.error_code,
            message=error.message,
            reason=error.reason,
            details=error.details,
        ) from error
    except PhoneVerificationError as error:
        auth_metrics_emitter.increment_counter_non_blocking(
            AUTH_OTP_VERIFY_FAILURE_TOTAL,
            dimensions=_build_otp_verify_failure_dimensions(
                payload=payload,
                reason_code=error.reason,
                email_verification_store=email_verification_store,
                phone_verification_store=phone_verification_store,
            ),
        )
        raise _create_auth_http_error(
            request=request,
            status_code=error.status_code,
            error_code=error.error_code,
            message=error.message,
            reason=error.reason,
            details=error.details,
        ) from error


class UserProfileResponseEnvelope(BaseModel):
    """Represent deterministic user profile response payload."""

    user_id: str
    role: str
    phone_number: str
    email: str
    account_state: str
    subscription_tier: str
    member_since: str
    gravatar_url: str


def _parse_authenticated_user_id_for_profile(
    *,
    authorization_header: str | None,
) -> UUID:
    if authorization_header is None:
        raise RegistrationPersistenceError(
            status_code=401,
            error_code="profile_unauthorized",
            message="Authentication is required to retrieve profile.",
            reason="profile_unauthorized",
        )
    normalized_header = authorization_header.strip()
    if not normalized_header.startswith("Bearer "):
        raise RegistrationPersistenceError(
            status_code=401,
            error_code="profile_unauthorized",
            message="Authentication is required to retrieve profile.",
            reason="profile_unauthorized",
        )
    encoded_context = normalized_header.removeprefix("Bearer ").strip()
    if not encoded_context:
        raise RegistrationPersistenceError(
            status_code=401,
            error_code="profile_unauthorized",
            message="Authentication is required to retrieve profile.",
            reason="profile_unauthorized",
        )
    segments = [segment.strip() for segment in encoded_context.split(";") if segment.strip()]
    parsed: dict[str, str] = {}
    for segment in segments:
        key, separator, value = segment.partition("=")
        if separator != "=":
            continue
        parsed[key.strip().lower()] = value.strip()
    user_id_raw = parsed.get("user_id", "")
    if not user_id_raw:
        raise RegistrationPersistenceError(
            status_code=401,
            error_code="profile_unauthorized",
            message="Authentication is required to retrieve profile.",
            reason="profile_unauthorized",
        )
    try:
        return UUID(user_id_raw)
    except ValueError as error:
        raise RegistrationPersistenceError(
            status_code=401,
            error_code="profile_unauthorized",
            message="Authentication is required to retrieve profile.",
            reason="profile_unauthorized",
        ) from error


def _build_gravatar_url(email: str, size: int = 200) -> str:
    """Return a Gravatar URL for the given email address."""
    email_hash = sha256(email.strip().lower().encode("utf-8")).hexdigest()
    return f"https://www.gravatar.com/avatar/{email_hash}?s={size}&d=identicon"


def _format_member_since(iso_timestamp: str) -> str:
    """Return a human-readable month+year string from an ISO timestamp."""
    try:
        dt = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
        return dt.strftime("%B %Y")
    except (ValueError, AttributeError):
        return "Unknown"


def _fetch_subscription_tier(*, user_id: UUID, database_url: str) -> str:
    """Return the subscription_tier for one user from the database."""
    try:
        with connect_auth_database(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT subscription_tier FROM users WHERE id = %s",
                    (user_id,),
                )
                row: tuple[object, ...] | None = cursor.fetchone()
        if row is not None:
            return str(row[0])
    except Exception:  # noqa: BLE001
        pass
    return "standard"


@ROUTER.get(
    "/v1/auth/profile",
    response_model=UserProfileResponseEnvelope,
    status_code=200,
)
def get_auth_user_profile(
    request: Request,
    registration_store: Annotated[RegistrationStoreProtocol, Depends(get_registration_store)],
) -> UserProfileResponseEnvelope:
    """Return deterministic user-friendly profile summary for the authenticated user."""

    try:
        user_id = _parse_authenticated_user_id_for_profile(
            authorization_header=request.headers.get("Authorization"),
        )
        user_record = registration_store.get_user_by_id(user_id=user_id)
        if user_record is None:
            raise RegistrationPersistenceError(
                status_code=404,
                error_code="profile_not_found",
                message="User profile is not found.",
                reason="profile_not_found",
            )
        database_url = load_auth_database_url()
        subscription_tier = (
            _fetch_subscription_tier(user_id=user_id, database_url=database_url)
            if database_url
            else "standard"
        )
        return UserProfileResponseEnvelope(
            user_id=str(user_record.user_id),
            role=user_record.role,
            phone_number=user_record.phone_number_normalized,
            email=user_record.email_normalized,
            account_state=user_record.account_state,
            subscription_tier=subscription_tier,
            member_since=_format_member_since(user_record.created_at),
            gravatar_url=_build_gravatar_url(user_record.email_normalized),
        )
    except RegistrationPersistenceError as error:
        raise _create_auth_http_error(
            request=request,
            status_code=error.status_code,
            error_code=error.error_code,
            message=error.message,
            reason=error.reason,
            details=error.details,
        ) from error


def create_app() -> FastAPI:
    """Build the auth FastAPI application."""

    auth_secret_config = load_auth_secret_config_baseline()
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
    app.state.registration_store = get_default_registration_store()
    app.state.delegation_store = get_default_delegation_store()
    app.state.email_verification_store = build_default_email_verification_store()
    app.state.phone_verification_store = build_default_phone_verification_store()
    app.state.password_reset_store = build_default_password_reset_store()
    app.state.phone_change_store = build_default_phone_change_store()
    app.state.account_deletion_request_store = build_default_account_deletion_request_store()
    app.state.session_issuance_store = build_default_session_issuance_store()
    app.state.login_lockout_store = build_default_login_lockout_store()
    app.state.login_step_up_store = build_default_login_step_up_store()
    app.state.oauth_state_store = get_default_oauth_state_store()
    app.state.oauth_token_exchange_client = get_default_oauth_token_exchange_client()
    app.state.oauth_identity_linking_store = get_default_oauth_identity_linking_store()
    app.state.oauth_jit_provisioning_policy = get_default_oauth_jit_provisioning_policy()
    app.state.oauth_provider_resilience_policy = get_default_oauth_provider_resilience_policy()
    app.state.oauth_provider_circuit_store = get_default_oauth_provider_circuit_store()
    app.state.oauth_id_token_validator = get_default_oidc_id_token_validator()
    if auth_secret_config.runtime_mode == "production":
        app.state.auth_audit_store = EventStoreBackedAuthAuditStore(
            repository=EventStoreRepository()
        )
    else:
        app.state.auth_audit_store = InMemoryAuthAuditStore()
    app.state.auth_metrics_emitter = get_default_auth_metrics_emitter()
    app.state.auth_slo_threshold_policy = get_default_auth_slo_threshold_policy()
    app.state.auth_structured_log_store = get_default_auth_structured_log_store()
    app.state.auth_secret_config = auth_secret_config
    app.add_middleware(CorrelationIdMiddleware)
    app.include_router(ROUTER)

    if get_auth_otp_runtime_mode() != "production":
        app.include_router(dev_router)

    return app


def _normalize_phone_otp_challenge_http_error(
    *,
    error: PhoneVerificationError,
) -> PhoneVerificationError:
    if error.reason not in {
        "otp_sms_delivery_provider_timeout",
        "otp_sms_delivery_provider_unavailable",
        "otp_sms_delivery_provider_rejected",
    }:
        return error
    if error.details.get("primary_channel") != "sms":
        return error
    delivery_failure_class = error.details.get("delivery_failure_class")
    if delivery_failure_class == "failed_retryable":
        return PhoneVerificationError(
            status_code=error.status_code,
            error_code="otp_primary_delivery_failed_retryable",
            message="Primary OTP challenge delivery failed and is retryable.",
            reason="otp_primary_delivery_failed_retryable",
            details=error.details,
        )

    if delivery_failure_class == "failed_non_retryable":
        return PhoneVerificationError(
            status_code=error.status_code,
            error_code="otp_primary_delivery_failed_non_retryable",
            message="Primary OTP challenge delivery failed and is non-retryable.",
            reason="otp_primary_delivery_failed_non_retryable",
            details=error.details,
        )
    return error


def list_auth_audit_events(*, app_instance: FastAPI) -> tuple[AuthAuditEventEnvelope, ...]:
    """Return immutable snapshot of auth audit events for diagnostics/tests."""

    configured_store = getattr(app_instance.state, "auth_audit_store", None)
    if configured_store is None:
        return tuple()
    return cast(AuthAuditStoreProtocol, configured_store).list_events()


def reset_auth_audit_events(*, app_instance: FastAPI) -> None:
    """Reset auth audit events for deterministic isolated tests."""

    configured_store = getattr(app_instance.state, "auth_audit_store", None)
    if configured_store is None:
        return
    cast(AuthAuditStoreProtocol, configured_store).reset()


def _build_auth_audit_store_error(*, error: EventStoreRepositoryError) -> AuthAuditStoreError:
    if error.reason_code == EVENT_STORE_PERSISTENCE_UNAVAILABLE:
        return AuthAuditStoreError(
            status_code=503,
            error_code="auth_audit_persistence_unavailable",
            message="Auth audit persistence is unavailable.",
            reason="auth_audit_persistence_unavailable",
        )
    if error.reason_code == EVENT_STORE_PERSISTENCE_NOT_CONFIGURED:
        return AuthAuditStoreError(
            status_code=500,
            error_code="auth_audit_persistence_not_configured",
            message="Auth audit persistence is not configured.",
            reason="auth_audit_persistence_not_configured",
        )
    if error.reason_code == EVENT_STORE_RETENTION_POLICY_INVALID:
        return AuthAuditStoreError(
            status_code=500,
            error_code="auth_audit_retention_policy_invalid",
            message="Auth audit retention policy is invalid.",
            reason="auth_audit_retention_policy_invalid",
        )
    return AuthAuditStoreError(
        status_code=503,
        error_code="auth_audit_persistence_unavailable",
        message="Auth audit persistence is unavailable.",
        reason="auth_audit_persistence_unavailable",
    )


def _build_auth_audit_idempotency_key(event: AuthAuditEventEnvelope) -> str:
    canonical_source = {
        "event_type": event.event_type,
        "correlation_id": event.correlation_id,
        "trace_id": event.trace_id,
        "evidence_hash": event.evidence_hash,
    }
    digest = sha256(canonical_json_dumps(
        canonical_source).encode("utf-8")).hexdigest()
    return f"auth-audit-{digest}"


def _resolve_auth_audit_resource_id(event: AuthAuditEventEnvelope) -> UUID:
    if event.user_id is not None:
        return event.user_id
    return uuid5(NAMESPACE_URL, f"auth-audit:{event.correlation_id}")


def _resolve_auth_audit_role_at_time(
    event: AuthAuditEventEnvelope,
) -> str | None:
    explicit_actor_role = event.details.get("actor_role")
    if isinstance(explicit_actor_role, str) and explicit_actor_role.strip():
        return explicit_actor_role.strip()
    explicit_role = event.details.get("role")
    if isinstance(explicit_role, str) and explicit_role.strip():
        return explicit_role.strip()
    requested_role = event.details.get("requested_role")
    if isinstance(requested_role, str) and requested_role.strip():
        return requested_role.strip()
    return None


def _parse_auth_audit_event_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _extract_auth_audit_envelope_from_persisted_event(
    details: Mapping[str, object],
) -> AuthAuditEventEnvelope | None:
    payload = details.get("auth_audit_envelope")
    if not isinstance(payload, Mapping):
        return None
    try:
        payload_map = cast(Mapping[str, object], payload)
        return AuthAuditEventEnvelope.model_validate(dict(payload_map))
    except Exception:
        return None


def list_auth_metric_events(*, app_instance: FastAPI) -> tuple[MetricEvent, ...]:
    """Return immutable snapshot of emitted auth metric events for tests/diagnostics."""

    configured_emitter = getattr(
        app_instance.state, "auth_metrics_emitter", None)
    if configured_emitter is None:
        return tuple()
    return cast(AuthMetricsEmitter, configured_emitter).snapshot()


def reset_auth_metric_events(*, app_instance: FastAPI) -> None:
    """Reset emitted auth metric events for deterministic isolated tests."""

    configured_emitter = getattr(
        app_instance.state, "auth_metrics_emitter", None)
    if configured_emitter is None:
        return
    cast(AuthMetricsEmitter, configured_emitter).reset()


def evaluate_auth_slo_alerts(
    *,
    app_instance: FastAPI,
    metrics_snapshot: AuthSloMetricSnapshot,
    correlation_id: str | None = None,
) -> tuple[AuthSloAlert, ...]:
    """Evaluate deterministic auth SLO threshold alerts using app policy state."""

    configured_policy = getattr(
        app_instance.state, "auth_slo_threshold_policy", None)
    effective_policy = (
        cast(AuthSloThresholdPolicy, configured_policy)
        if configured_policy is not None
        else get_default_auth_slo_threshold_policy()
    )
    return evaluate_auth_slo_thresholds(
        metrics_snapshot=metrics_snapshot,
        policy=effective_policy,
        correlation_id=correlation_id,
    )


def list_auth_structured_logs(*, app_instance: FastAPI) -> tuple[StructuredAuthLogEvent, ...]:
    """Return immutable snapshot of structured auth logs for tests/diagnostics."""

    configured_store = getattr(
        app_instance.state, "auth_structured_log_store", None)
    if configured_store is None:
        return tuple()
    return cast(InMemoryAuthStructuredLogStore, configured_store).snapshot()


def reset_auth_structured_logs(*, app_instance: FastAPI) -> None:
    """Reset structured auth logs for deterministic isolated tests."""

    configured_store = getattr(
        app_instance.state, "auth_structured_log_store", None)
    if configured_store is None:
        return
    cast(InMemoryAuthStructuredLogStore, configured_store).clear()


def _resolve_challenge_channel(payload: object) -> str:
    if not isinstance(payload, Mapping):
        return "unknown"
    payload_map = cast(Mapping[str, object], payload)
    channel_value = payload_map.get("channel")
    if not isinstance(channel_value, str):
        return "unknown"
    return channel_value.strip().lower()


def _resolve_otp_provider_for_channel(channel: str) -> str:
    normalized_channel = channel.strip().lower()
    if normalized_channel == PHONE_VERIFICATION_CHANNEL:
        return get_auth_otp_sms_provider_mode()
    if normalized_channel == EMAIL_VERIFICATION_CHANNEL:
        return get_auth_otp_email_provider_mode()
    return "unknown"


def _build_otp_verify_failure_dimensions(
    *,
    payload: object,
    reason_code: str,
    email_verification_store: EmailVerificationStoreProtocol,
    phone_verification_store: PhoneVerificationStoreProtocol,
) -> dict[str, str]:
    dimensions: dict[str, str] = {"reason_code": reason_code}
    challenge_id = _parse_challenge_id_from_payload(payload=payload)
    if challenge_id is None:
        return dimensions
    phone_challenge = phone_verification_store.get_challenge(
        challenge_id=challenge_id)
    if phone_challenge is not None:
        dimensions["channel"] = PHONE_VERIFICATION_CHANNEL
        dimensions["purpose"] = phone_challenge.purpose
        return dimensions
    email_challenge = email_verification_store.get_challenge(
        challenge_id=challenge_id)
    if email_challenge is not None:
        dimensions["channel"] = EMAIL_VERIFICATION_CHANNEL
        dimensions["purpose"] = email_challenge.purpose
    return dimensions


def _parse_challenge_id_from_payload(*, payload: object) -> UUID | None:
    if not isinstance(payload, Mapping):
        return None
    payload_map = cast(Mapping[str, object], payload)
    raw_challenge_id = payload_map.get("challenge_id")
    if isinstance(raw_challenge_id, UUID):
        return raw_challenge_id
    if not isinstance(raw_challenge_id, str):
        return None
    try:
        return UUID(raw_challenge_id)
    except ValueError:
        return None


def _parse_role_change_request(*, payload: object) -> RoleChangeRequestEnvelope:
    if not isinstance(payload, dict):
        raise RoleChangeGovernanceError(
            status_code=400,
            error_code="role_change_invalid_request",
            message="Invalid role-change request payload.",
            reason="role_change_invalid_request",
            details={"field": "payload"},
        )
    try:
        request_model = RoleChangeRequestEnvelope.model_validate(payload)
    except Exception as error:
        raise RoleChangeGovernanceError(
            status_code=400,
            error_code="role_change_invalid_request",
            message="Invalid role-change request payload.",
            reason="role_change_invalid_request",
            details={},
        ) from error

    normalized_role = request_model.new_role.strip()
    if normalized_role not in ALLOWED_AUTH_ROLES:
        raise RoleChangeGovernanceError(
            status_code=400,
            error_code="role_change_invalid_transition",
            message="Requested role transition is invalid.",
            reason="role_change_invalid_transition",
            details={"requested_role": normalized_role},
        )

    normalized_reason = None
    if isinstance(request_model.reason, str):
        stripped_reason = request_model.reason.strip()
        if stripped_reason:
            normalized_reason = stripped_reason

    return RoleChangeRequestEnvelope(
        target_user_id=request_model.target_user_id,
        new_role=normalized_role,
        reason=normalized_reason,
    )


def _apply_role_change(
    *,
    registration_store: RegistrationStoreProtocol,
    target_user_id: UUID,
    new_role: str,
) -> tuple[str, RegisteredUserRecord]:
    existing_record = registration_store.get_user_by_id(user_id=target_user_id)
    if existing_record is None:
        raise RoleChangeGovernanceError(
            status_code=404,
            error_code="role_change_target_not_found",
            message="Role-change target account was not found.",
            reason="role_change_target_not_found",
            details={"target_user_id": str(target_user_id)},
        )
    previous_role = existing_record.role
    if previous_role == new_role:
        raise RoleChangeGovernanceError(
            status_code=409,
            error_code="role_change_invalid_transition",
            message="Requested role transition is invalid.",
            reason="role_change_invalid_transition",
            details={
                "target_user_id": str(target_user_id),
                "previous_role": previous_role,
                "new_role": new_role,
            },
        )

    raw_records_by_user_id = getattr(
        registration_store, "_records_by_user_id", None)
    raw_records_by_email = getattr(
        registration_store, "_records_by_email", None)
    raw_records_by_phone = getattr(
        registration_store, "_records_by_phone", None)
    if not isinstance(raw_records_by_user_id, dict):
        raise RoleChangeGovernanceError(
            status_code=500,
            error_code="role_change_store_unsupported",
            message="Role-change persistence is unavailable.",
            reason="role_change_store_unsupported",
            details={},
        )
    if not isinstance(raw_records_by_email, dict):
        raise RoleChangeGovernanceError(
            status_code=500,
            error_code="role_change_store_unsupported",
            message="Role-change persistence is unavailable.",
            reason="role_change_store_unsupported",
            details={},
        )
    if not isinstance(raw_records_by_phone, dict):
        raise RoleChangeGovernanceError(
            status_code=500,
            error_code="role_change_store_unsupported",
            message="Role-change persistence is unavailable.",
            reason="role_change_store_unsupported",
            details={},
        )

    store_lock = getattr(registration_store, "_lock", None)
    records_by_user_id = cast(
        dict[UUID, RegisteredUserRecord], raw_records_by_user_id)
    records_by_email = cast(
        dict[str, RegisteredUserRecord], raw_records_by_email)
    records_by_phone = cast(
        dict[str, RegisteredUserRecord], raw_records_by_phone)
    if (
        store_lock is not None
        and hasattr(store_lock, "__enter__")
        and hasattr(store_lock, "__exit__")
    ):
        with cast(AbstractContextManager[object], store_lock):
            refreshed_record = records_by_user_id.get(target_user_id)
            if refreshed_record is None:
                raise RoleChangeGovernanceError(
                    status_code=404,
                    error_code="role_change_target_not_found",
                    message="Role-change target account was not found.",
                    reason="role_change_target_not_found",
                    details={"target_user_id": str(target_user_id)},
                )
            updated_record = replace(refreshed_record, role=new_role)
            records_by_user_id[target_user_id] = updated_record
            records_by_email[updated_record.email_normalized] = updated_record
            records_by_phone[updated_record.phone_number_normalized] = updated_record
    else:
        updated_record = replace(existing_record, role=new_role)
        records_by_user_id[target_user_id] = updated_record
        records_by_email[updated_record.email_normalized] = updated_record
        records_by_phone[updated_record.phone_number_normalized] = updated_record
    return previous_role, updated_record


def _parse_refresh_request(*, payload: object) -> RefreshRequestEnvelope:
    if not isinstance(payload, dict):
        raise SessionIssuanceError(
            status_code=401,
            error_code="refresh_token_malformed",
            message="Refresh token is malformed.",
            reason="refresh_token_malformed",
        )
    try:
        request_model = RefreshRequestEnvelope.model_validate(payload)
    except Exception as error:
        raise SessionIssuanceError(
            status_code=401,
            error_code="refresh_token_malformed",
            message="Refresh token is malformed.",
            reason="refresh_token_malformed",
        ) from error
    normalized_refresh_token = request_model.refresh_token.strip()
    if not normalized_refresh_token:
        raise SessionIssuanceError(
            status_code=401,
            error_code="refresh_token_malformed",
            message="Refresh token is malformed.",
            reason="refresh_token_malformed",
        )
    return RefreshRequestEnvelope(refresh_token=normalized_refresh_token)


def _parse_logout_request(*, payload: object) -> LogoutRequestEnvelope:
    if not isinstance(payload, dict):
        raise SessionIssuanceError(
            status_code=400,
            error_code="logout_invalid_request",
            message="Invalid logout request payload.",
            reason="logout_invalid_request",
        )
    try:
        request_model = LogoutRequestEnvelope.model_validate(payload)
    except Exception as error:
        raise SessionIssuanceError(
            status_code=400,
            error_code="logout_invalid_request",
            message="Invalid logout request payload.",
            reason="logout_invalid_request",
        ) from error
    if request_model.revoke_scope == "single_session" and request_model.target_session_id is None:
        raise SessionIssuanceError(
            status_code=400,
            error_code="logout_invalid_request",
            message="Invalid logout request payload.",
            reason="logout_invalid_request",
        )
    if request_model.revoke_scope == "all_sessions" and request_model.target_session_id is not None:
        raise SessionIssuanceError(
            status_code=400,
            error_code="logout_invalid_request",
            message="Invalid logout request payload.",
            reason="logout_invalid_request",
        )
    return request_model


def _parse_authenticated_user_id(*, authorization_header: str | None) -> UUID:
    if authorization_header is None:
        raise SessionIssuanceError(
            status_code=401,
            error_code="logout_unauthorized",
            message="Authentication is required for logout.",
            reason="logout_unauthorized",
        )
    normalized_header = authorization_header.strip()
    if not normalized_header.startswith("Bearer "):
        raise SessionIssuanceError(
            status_code=401,
            error_code="logout_unauthorized",
            message="Authentication is required for logout.",
            reason="logout_unauthorized",
        )
    encoded_context = normalized_header.removeprefix("Bearer ").strip()
    if not encoded_context:
        raise SessionIssuanceError(
            status_code=401,
            error_code="logout_unauthorized",
            message="Authentication is required for logout.",
            reason="logout_unauthorized",
        )
    segments = [segment.strip()
                for segment in encoded_context.split(";") if segment.strip()]
    parsed: dict[str, str] = {}
    for segment in segments:
        key, separator, value = segment.partition("=")
        if separator != "=":
            continue
        parsed[key.strip().lower()] = value.strip()
    user_id_raw = parsed.get("user_id", "")
    if not user_id_raw:
        raise SessionIssuanceError(
            status_code=401,
            error_code="logout_unauthorized",
            message="Authentication is required for logout.",
            reason="logout_unauthorized",
        )
    try:
        return UUID(user_id_raw)
    except ValueError as error:
        raise SessionIssuanceError(
            status_code=401,
            error_code="logout_unauthorized",
            message="Authentication is required for logout.",
            reason="logout_unauthorized",
        ) from error


def _parse_authenticated_user_id_for_session_introspection(
    *,
    authorization_header: str | None,
) -> UUID:
    if authorization_header is None:
        raise SessionIssuanceError(
            status_code=401,
            error_code="session_introspection_unauthorized",
            message="Authentication is required for session introspection.",
            reason="session_introspection_unauthorized",
        )
    normalized_header = authorization_header.strip()
    if not normalized_header.startswith("Bearer "):
        raise SessionIssuanceError(
            status_code=401,
            error_code="session_introspection_unauthorized",
            message="Authentication is required for session introspection.",
            reason="session_introspection_unauthorized",
        )
    encoded_context = normalized_header.removeprefix("Bearer ").strip()
    if not encoded_context:
        raise SessionIssuanceError(
            status_code=401,
            error_code="session_introspection_unauthorized",
            message="Authentication is required for session introspection.",
            reason="session_introspection_unauthorized",
        )
    segments = [segment.strip()
                for segment in encoded_context.split(";") if segment.strip()]
    parsed: dict[str, str] = {}
    for segment in segments:
        key, separator, value = segment.partition("=")
        if separator != "=":
            continue
        parsed[key.strip().lower()] = value.strip()
    user_id_raw = parsed.get("user_id", "")
    if not user_id_raw:
        raise SessionIssuanceError(
            status_code=401,
            error_code="session_introspection_unauthorized",
            message="Authentication is required for session introspection.",
            reason="session_introspection_unauthorized",
        )
    try:
        return UUID(user_id_raw)
    except ValueError as error:
        raise SessionIssuanceError(
            status_code=401,
            error_code="session_introspection_unauthorized",
            message="Authentication is required for session introspection.",
            reason="session_introspection_unauthorized",
        ) from error


def _extract_source_ip(request: Request) -> str:
    x_forwarded_for = request.headers.get("X-Forwarded-For")
    if isinstance(x_forwarded_for, str):
        candidate = x_forwarded_for.split(",")[0].strip()
        if candidate:
            return candidate

    x_real_ip = request.headers.get("X-Real-IP")
    if isinstance(x_real_ip, str):
        candidate = x_real_ip.strip()
        if candidate:
            return candidate

    request_client = request.client
    if request_client is not None:
        candidate = request_client.host.strip()
        if candidate:
            return candidate
    return "unknown"


def _resolve_auth_structured_log_store(
    request: Request,
) -> InMemoryAuthStructuredLogStore:
    configured_store = getattr(
        request.app.state, "auth_structured_log_store", None)
    if configured_store is not None:
        return cast(InMemoryAuthStructuredLogStore, configured_store)
    return get_default_auth_structured_log_store()


def _emit_auth_structured_log_event(
    *,
    request: Request,
    event_type: str,
    event_status: str,
    reason_code: str | None,
    user_id: UUID | None,
    tenant_id: str,
    details: dict[str, object] | None = None,
) -> None:
    emit_auth_structured_log(
        event_type=event_type,
        event_status=event_status,
        reason_code=reason_code,
        trace_id=get_trace_id(request),
        correlation_id=get_correlation_id(request),
        user_id=user_id,
        tenant_id=tenant_id,
        details=details,
        structured_log_store=_resolve_auth_structured_log_store(request),
    )


def _resolve_auth_log_event_type_for_path(path: str) -> str:
    if path.startswith("/v1/auth/register"):
        return _AUTH_LOG_EVENT_REGISTRATION
    if path.startswith("/v1/auth/login"):
        return AUTH_LOG_EVENT_LOGIN
    if path.startswith("/v1/auth/oauth/"):
        return _AUTH_LOG_EVENT_OAUTH
    if path.startswith("/v1/auth/refresh"):
        return _AUTH_LOG_EVENT_SESSION
    if path.startswith("/v1/auth/logout") or path.startswith("/v1/auth/sessions/"):
        return _AUTH_LOG_EVENT_SESSION
    if path.startswith("/v1/auth/password-reset/"):
        return AUTH_LOG_EVENT_PASSWORD_RESET
    if path.startswith("/v1/auth/otp/"):
        return _AUTH_LOG_EVENT_OTP
    if path.startswith("/v1/auth/phone-change/"):
        return AUTH_LOG_EVENT_PHONE_CHANGE
    if path.startswith("/v1/auth/roles/change"):
        return _AUTH_LOG_EVENT_ROLE_CHANGE
    if path.startswith("/v1/auth/account-deletion/"):
        return AUTH_LOG_EVENT_ACCOUNT_DELETION
    return "auth.unknown"


def _try_extract_authenticated_user_id(request: Request) -> UUID | None:
    authorization_header = request.headers.get("Authorization")
    if not isinstance(authorization_header, str):
        return None
    prefix, _, token_part = authorization_header.partition(" ")
    if prefix.lower() != "bearer" or not token_part:
        return None
    user_id_raw, _, _role = token_part.partition(":")
    if not user_id_raw:
        return None
    try:
        return UUID(user_id_raw)
    except ValueError:
        return None


def _create_auth_http_error(
    *,
    request: Request,
    status_code: int,
    error_code: str,
    message: str,
    reason: str,
    details: dict[str, object],
) -> HTTPException:
    _emit_auth_structured_log_event(
        request=request,
        event_type=_resolve_auth_log_event_type_for_path(request.url.path),
        event_status="failed",
        reason_code=reason,
        user_id=_try_extract_authenticated_user_id(request),
        tenant_id=DEFAULT_TENANT_ID,
        details=details,
    )
    correlation_id = get_correlation_id(request)
    trace_id = get_trace_id(request)
    detail: dict[str, object] = {
        "error_code": error_code,
        "message": message,
        "reason": reason,
        "correlation_id": correlation_id,
        "trace_id": trace_id,
        "details": details,
    }
    current_state = details.get("current_state")
    requested_state = details.get("requested_state")
    if isinstance(current_state, str):
        detail["current_state"] = current_state
    if isinstance(requested_state, str):
        detail["requested_state"] = requested_state
    lockout_expires_at = details.get("lockout_expires_at")
    lockout_remaining_seconds = details.get("lockout_remaining_seconds")
    if isinstance(lockout_expires_at, str):
        detail["lockout_expires_at"] = lockout_expires_at
    if isinstance(lockout_remaining_seconds, int):
        detail["lockout_remaining_seconds"] = lockout_remaining_seconds
    account_deletion_state = details.get("account_deletion_state")
    audit_reference_id = details.get("audit_reference_id")
    incident_code = details.get("incident_code")
    if isinstance(account_deletion_state, str):
        detail["account_deletion_state"] = account_deletion_state
    if isinstance(audit_reference_id, str):
        detail["audit_reference_id"] = audit_reference_id
    if isinstance(incident_code, str):
        detail["incident_code"] = incident_code
    return HTTPException(
        status_code=status_code,
        detail=detail,
    )


def _emit_auth_audit_event(
    *,
    request: Request,
    auth_audit_store: AuthAuditStoreProtocol,
    event_type: str,
    user_id: UUID | None,
    tenant_id: str,
    session_id: UUID | None,
    action_status: str,
    reason_code: str | None,
    details: dict[str, object],
) -> None:
    correlation_id = get_correlation_id(request)
    trace_id = get_trace_id(request)
    event = build_auth_audit_event_envelope(
        event_type=event_type,
        event_time=_utc_now_iso(),
        user_id=user_id,
        tenant_id=tenant_id,
        session_id=session_id,
        correlation_id=correlation_id,
        trace_id=trace_id,
        action_status=action_status,
        reason_code=reason_code,
        details=details,
    )
    try:
        auth_audit_store.append_event(event=event)
    except AuthAuditStoreError as error:
        raise _create_auth_http_error(
            request=request,
            status_code=error.status_code,
            error_code=error.error_code,
            message=error.message,
            reason=error.reason,
            details={},
        ) from error


def build_auth_audit_event_envelope(
    *,
    event_type: str,
    event_time: str,
    user_id: UUID | None,
    tenant_id: str,
    session_id: UUID | None,
    correlation_id: str,
    trace_id: str,
    action_status: str,
    reason_code: str | None,
    details: dict[str, object],
) -> AuthAuditEventEnvelope:
    """Build one canonical deterministic auth audit event envelope."""

    if event_type not in AUTH_AUDIT_EVENT_TYPES:
        raise ValueError(f"auth_audit_unsupported_event_type:{event_type}")
    if not tenant_id.strip():
        raise ValueError("auth_audit_missing_required_field:tenant_id")
    if not action_status.strip():
        raise ValueError("auth_audit_missing_required_field:action_status")
    if not correlation_id.strip():
        raise ValueError("auth_audit_missing_required_field:correlation_id")
    if not trace_id.strip():
        raise ValueError("auth_audit_missing_required_field:trace_id")
    sanitized_details = _sanitize_audit_details(details)
    normalized_tenant_id = tenant_id.strip() or DEFAULT_TENANT_ID
    evidence_source = {
        "schema_version": AUTH_AUDIT_SCHEMA_VERSION,
        "event_type": event_type,
        "event_time": event_time,
        "user_id": None if user_id is None else str(user_id),
        "tenant_id": normalized_tenant_id,
        "session_id": None if session_id is None else str(session_id),
        "correlation_id": correlation_id,
        "trace_id": trace_id,
        "action_status": action_status,
        "reason_code": reason_code,
        "details": sanitized_details,
    }
    evidence_hash = sha256(canonical_json_dumps(
        evidence_source).encode("utf-8")).hexdigest()
    return AuthAuditEventEnvelope(
        schema_version=AUTH_AUDIT_SCHEMA_VERSION,
        event_type=event_type,
        event_time=event_time,
        user_id=user_id,
        tenant_id=normalized_tenant_id,
        session_id=session_id,
        correlation_id=correlation_id,
        trace_id=trace_id,
        action_status=action_status,
        reason_code=reason_code,
        evidence_hash=evidence_hash,
        details=sanitized_details,
    )


def _sanitize_audit_details(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    sanitized: dict[str, object] = {}
    value_map = cast(Mapping[str, object], value)
    for key, raw_value in value_map.items():
        normalized_key = key.strip()
        if not normalized_key:
            continue
        if _contains_sensitive_token(normalized_key):
            sanitized[normalized_key] = "[REDACTED]"
            continue
        sanitized[normalized_key] = _sanitize_audit_value(raw_value)
    return sanitized


def _sanitize_audit_value(value: object) -> object:
    if isinstance(value, dict):
        return _sanitize_audit_details(cast(Mapping[str, object], value))
    if isinstance(value, list):
        return [_sanitize_audit_value(item) for item in cast(list[object], value)]
    if isinstance(value, tuple):
        return [_sanitize_audit_value(item) for item in cast(tuple[object, ...], value)]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _contains_sensitive_token(key: str) -> bool:
    normalized = key.strip().lower()
    return any(token in normalized for token in _SENSITIVE_AUDIT_DETAIL_TOKENS)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


app = create_app()
