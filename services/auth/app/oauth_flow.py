"""Deterministic OAuth Authorization Code + PKCE start/callback flow primitives."""

from __future__ import annotations

import os
import re
from uuid import UUID
from typing import Any
from typing import cast
from typing import Literal
from typing import Protocol
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from threading import Lock
from dataclasses import dataclass
from collections.abc import Mapping
from collections.abc import Callable

from authlib.common.security import generate_token
from authlib.integrations.httpx_client import OAuth2Client

from services.auth.app.config import get_auth_oauth_provider_timeout_seconds
from services.auth.app.oauth_config import OAuthProviderConfig
from services.auth.app.oauth_config import OAuthProviderConfigError
from services.auth.app.oauth_config import OAuthProviderTrustPolicy
from services.auth.app.oauth_config import get_trusted_enabled_oauth_provider
from services.auth.app.oauth_config import load_oauth_provider_registry_from_env
from services.auth.app.oauth_config import get_default_oauth_provider_trust_policy
from services.auth.app.registration import RegistrationStoreProtocol
from services.auth.app.registration import get_default_registration_store
from services.auth.app.oauth_linking import OAuthIdentityLinkingError
from services.auth.app.oauth_linking import resolve_or_link_oauth_identity
from services.auth.app.oauth_linking import OAuthIdentityLinkingStoreProtocol
from services.auth.app.oauth_linking import get_default_oauth_identity_linking_store
from services.auth.app.oauth_linking import is_oauth_identity_linking_no_match_error
from services.auth.app.oauth_resilience import OAuthProviderResilienceError
from services.auth.app.oauth_resilience import OAuthProviderResiliencePolicy
from services.auth.app.oauth_resilience import OAuthProviderCircuitStoreProtocol
from services.auth.app.oauth_resilience import exchange_code_for_token_with_resilience
from services.auth.app.oauth_resilience import get_default_oauth_provider_circuit_store
from services.auth.app.oauth_resilience import get_default_oauth_provider_resilience_policy
from services.auth.app.oauth_validation import OidcIdTokenValidationError
from services.auth.app.oauth_validation import OidcIdTokenValidatorProtocol
from services.auth.app.oauth_validation import get_default_oidc_id_token_validator
from services.auth.app.oauth_provisioning import OAuthJitProvisioningError
from services.auth.app.oauth_provisioning import OAuthJitProvisioningPolicy
from services.auth.app.oauth_provisioning import provision_oauth_identity_if_eligible
from services.auth.app.oauth_provisioning import get_default_oauth_jit_provisioning_policy
from services.auth.app.persistence_support import auth_runtime_requires_persistence


@dataclass(frozen=True)
class OAuthAuthorizationState:
    """Represent one transient OAuth authorization-state challenge context."""

    provider_id: str
    state: str
    nonce: str
    redirect_uri: str
    code_verifier: str
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None


@dataclass(frozen=True)
class OAuthStartFlowResult:
    """Represent deterministic OAuth start-flow output."""

    status: Literal["redirect_required"]
    provider_id: str
    authorization_url: str
    state: str
    nonce: str
    expires_at: str


@dataclass(frozen=True)
class OAuthCallbackFlowResult:
    """Represent deterministic OAuth callback protocol-validation output."""

    status: Literal["protocol_validated"]
    provider_id: str
    oauth_subject: str | None
    linked_user_id: UUID
    linked_tenant_id: str
    linked_user_role: str
    link_status: Literal["linked_existing", "linked_new"]
    jit_provisioned: bool
    provider_recovered: bool
    nonce: str


class OAuthFlowError(ValueError):
    """Represent deterministic OAuth start/callback failure outcomes."""

    def __init__(
        self,
        *,
        status_code: int,
        error_code: str,
        message: str,
        reason: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.reason = reason
        self.details = details or {}


class OAuthStateStoreProtocol(Protocol):
    """Define transient OAuth state storage boundary for deterministic flow checks."""

    def put_state(self, *, state: OAuthAuthorizationState) -> None:
        """Persist one OAuth authorization-state challenge."""

        ...

    def get_state(self, *, state: str) -> OAuthAuthorizationState | None:
        """Return one OAuth authorization-state challenge."""

        ...

    def consume_state(self, *, state: str, consumed_at: datetime) -> OAuthAuthorizationState | None:
        """Mark OAuth authorization state as consumed and return updated record."""

        ...

    def reset(self) -> None:
        """Reset process-local state records for deterministic tests."""

        ...


class OAuthTokenExchangeClientProtocol(Protocol):
    """Define token-exchange boundary for OAuth callback flow."""

    def exchange_code_for_token(
        self,
        *,
        provider: OAuthProviderConfig,
        authorization_code: str,
        code_verifier: str,
    ) -> Mapping[str, object]:
        """Exchange authorization code for token response using provider settings."""

        ...


class InMemoryOAuthStateStore:
    """Process-local deterministic OAuth state challenge store."""

    def __init__(self) -> None:
        self._records: dict[str, OAuthAuthorizationState] = {}
        self._lock = Lock()

    def put_state(self, *, state: OAuthAuthorizationState) -> None:
        with self._lock:
            self._records[state.state] = state

    def get_state(self, *, state: str) -> OAuthAuthorizationState | None:
        with self._lock:
            return self._records.get(state)

    def consume_state(self, *, state: str, consumed_at: datetime) -> OAuthAuthorizationState | None:
        with self._lock:
            existing = self._records.get(state)
            if existing is None:
                return None
            updated = OAuthAuthorizationState(
                provider_id=existing.provider_id,
                state=existing.state,
                nonce=existing.nonce,
                redirect_uri=existing.redirect_uri,
                code_verifier=existing.code_verifier,
                created_at=existing.created_at,
                expires_at=existing.expires_at,
                consumed_at=consumed_at,
            )
            self._records[state] = updated
            return updated

    def reset(self) -> None:
        with self._lock:
            self._records.clear()


class UnavailableOAuthStateStore:
    """Fail closed when durable OAuth state storage is unavailable."""

    def __init__(
        self,
        *,
        status_code: int,
        error_code: str,
        message: str,
        reason: str,
    ) -> None:
        self._status_code = status_code
        self._error_code = error_code
        self._message = message
        self._reason = reason

    def _raise(self) -> None:
        raise OAuthFlowError(
            status_code=self._status_code,
            error_code=self._error_code,
            message=self._message,
            reason=self._reason,
        )

    def put_state(self, *, state: OAuthAuthorizationState) -> None:
        del state
        self._raise()

    def get_state(self, *, state: str) -> OAuthAuthorizationState | None:
        del state
        self._raise()

    def consume_state(self, *, state: str, consumed_at: datetime) -> OAuthAuthorizationState | None:
        del state, consumed_at
        self._raise()

    def reset(self) -> None:
        return None


class AuthlibOAuthTokenExchangeClient:
    """Use standards-based Authlib client for OAuth authorization-code exchange."""

    def __init__(
        self,
        *,
        client_secret_resolver: Callable[[str], str | None] | None = None,
        request_timeout_seconds: int | None = None,
    ) -> None:
        self._client_secret_resolver = (
            _resolve_client_secret_from_reference
            if client_secret_resolver is None
            else client_secret_resolver
        )
        self._request_timeout_seconds = request_timeout_seconds

    def exchange_code_for_token(
        self,
        *,
        provider: OAuthProviderConfig,
        authorization_code: str,
        code_verifier: str,
    ) -> Mapping[str, object]:
        client_secret = self._client_secret_resolver(provider.client_secret_ref)
        if not client_secret:
            raise OAuthFlowError(
                status_code=401,
                error_code="oauth_token_exchange_failed",
                message="OAuth callback token exchange failed.",
                reason="oauth_token_exchange_failed",
                details={
                    "provider_id": provider.provider_id,
                    "requirement": "client_secret_unavailable",
                },
            )
        oauth_client = OAuth2Client(
            client_id=provider.client_id,
            client_secret=client_secret,
            redirect_uri=provider.redirect_uri,
            scope=list(provider.scopes),
        )
        try:
            oauth_client_any = cast(Any, oauth_client)
            token_response = oauth_client_any.fetch_token(
                url=provider.token_endpoint,
                code=authorization_code,
                code_verifier=code_verifier,
                grant_type="authorization_code",
                timeout=self._request_timeout_seconds,
            )
        except Exception as error:
            raise OAuthFlowError(
                status_code=401,
                error_code="oauth_token_exchange_failed",
                message="OAuth callback token exchange failed.",
                reason="oauth_token_exchange_failed",
                details={
                    "provider_id": provider.provider_id,
                    "requirement": "provider_exchange_error",
                },
            ) from error
        finally:
            close_fn = getattr(oauth_client, "close", None)
            if callable(close_fn):
                close_fn()

        if not isinstance(token_response, Mapping):
            raise OAuthFlowError(
                status_code=401,
                error_code="oauth_token_exchange_failed",
                message="OAuth callback token exchange failed.",
                reason="oauth_token_exchange_failed",
                details={
                    "provider_id": provider.provider_id,
                    "requirement": "mapping_token_response_required",
                },
            )
        return cast(Mapping[str, object], token_response)


def start_oauth_authorization(
    *,
    provider_id: str,
    redirect_uri: str,
    state_store: OAuthStateStoreProtocol,
    state_ttl_seconds: int,
    now_provider: Callable[[], datetime] | None = None,
    registry: Mapping[str, OAuthProviderConfig] | None = None,
    trust_policy: OAuthProviderTrustPolicy | None = None,
) -> OAuthStartFlowResult:
    """Start deterministic OAuth Authorization Code + PKCE flow."""

    provider = _resolve_provider_for_oauth_flow(
        provider_id=provider_id,
        registry=registry,
        trust_policy=trust_policy,
    )
    normalized_redirect_uri = redirect_uri.strip()
    if not normalized_redirect_uri or normalized_redirect_uri != provider.redirect_uri:
        raise OAuthFlowError(
            status_code=400,
            error_code="oauth_provider_redirect_uri_not_allowed",
            message="OAuth provider redirect URI is not allowed.",
            reason="oauth_provider_redirect_uri_not_allowed",
            details={"provider_id": provider.provider_id},
        )

    code_verifier = generate_token(64)
    nonce = generate_token(20)
    state = generate_token(42)
    oauth_client = OAuth2Client(
        client_id=provider.client_id,
        redirect_uri=provider.redirect_uri,
        scope=list(provider.scopes),
        code_challenge_method="S256",
    )
    oauth_client_any = cast(Any, oauth_client)
    authorization_result = oauth_client_any.create_authorization_url(
        provider.authorization_endpoint,
        state=state,
        nonce=nonce,
        code_verifier=code_verifier,
    )
    authorization_url, resolved_state = cast(tuple[str, str], authorization_result)
    close_fn = getattr(oauth_client, "close", None)
    if callable(close_fn):
        close_fn()

    current_time = _now(now_provider=now_provider)
    expires_at = current_time + timedelta(seconds=state_ttl_seconds)
    state_store.put_state(
        state=OAuthAuthorizationState(
            provider_id=provider.provider_id,
            state=resolved_state,
            nonce=nonce,
            redirect_uri=provider.redirect_uri,
            code_verifier=code_verifier,
            created_at=current_time,
            expires_at=expires_at,
            consumed_at=None,
        )
    )
    return OAuthStartFlowResult(
        status="redirect_required",
        provider_id=provider.provider_id,
        authorization_url=authorization_url,
        state=resolved_state,
        nonce=nonce,
        expires_at=_iso_utc(expires_at),
    )


def complete_oauth_callback(
    *,
    provider_id: str,
    state: str,
    code: str,
    state_store: OAuthStateStoreProtocol,
    token_exchange_client: OAuthTokenExchangeClientProtocol,
    id_token_validator: OidcIdTokenValidatorProtocol | None = None,
    registration_store: RegistrationStoreProtocol | None = None,
    identity_linking_store: OAuthIdentityLinkingStoreProtocol | None = None,
    jit_policy: OAuthJitProvisioningPolicy | None = None,
    resilience_policy: OAuthProviderResiliencePolicy | None = None,
    circuit_store: OAuthProviderCircuitStoreProtocol | None = None,
    tenant_id: str = "default_tenant",
    now_provider: Callable[[], datetime] | None = None,
    registry: Mapping[str, OAuthProviderConfig] | None = None,
    trust_policy: OAuthProviderTrustPolicy | None = None,
) -> OAuthCallbackFlowResult:
    """Complete deterministic OAuth callback protocol checks and token exchange."""

    normalized_code = code.strip()
    if not normalized_code:
        raise OAuthFlowError(
            status_code=400,
            error_code="oauth_callback_code_missing",
            message="OAuth callback code is missing.",
            reason="oauth_callback_code_missing",
            details={"provider_id": provider_id.strip()},
        )
    provider = _resolve_provider_for_oauth_flow(
        provider_id=provider_id,
        registry=registry,
        trust_policy=trust_policy,
    )
    normalized_state = state.strip()
    if not normalized_state:
        raise OAuthFlowError(
            status_code=400,
            error_code="oauth_state_invalid",
            message="OAuth callback state is invalid.",
            reason="oauth_state_invalid",
            details={"provider_id": provider.provider_id},
        )
    state_record = state_store.get_state(state=normalized_state)
    if state_record is None:
        raise OAuthFlowError(
            status_code=400,
            error_code="oauth_state_invalid",
            message="OAuth callback state is invalid.",
            reason="oauth_state_invalid",
            details={"provider_id": provider.provider_id},
        )
    if state_record.provider_id != provider.provider_id:
        raise OAuthFlowError(
            status_code=400,
            error_code="oauth_state_invalid",
            message="OAuth callback state is invalid.",
            reason="oauth_state_invalid",
            details={"provider_id": provider.provider_id},
        )
    if state_record.consumed_at is not None:
        raise OAuthFlowError(
            status_code=409,
            error_code="oauth_callback_replay_detected",
            message="OAuth callback replay is detected.",
            reason="oauth_callback_replay_detected",
            details={"provider_id": provider.provider_id},
        )
    current_time = _now(now_provider=now_provider)
    if state_record.expires_at <= current_time:
        raise OAuthFlowError(
            status_code=409,
            error_code="oauth_state_expired",
            message="OAuth callback state is expired.",
            reason="oauth_state_expired",
            details={"provider_id": provider.provider_id},
        )

    state_store.consume_state(state=normalized_state, consumed_at=current_time)
    effective_resilience_policy = (
        get_default_oauth_provider_resilience_policy()
        if resilience_policy is None
        else resilience_policy
    )
    effective_circuit_store = (
        get_default_oauth_provider_circuit_store() if circuit_store is None else circuit_store
    )
    provider_recovered = False
    jit_provisioned = False
    try:
        resilient_exchange = exchange_code_for_token_with_resilience(
            provider=provider,
            authorization_code=normalized_code,
            code_verifier=state_record.code_verifier,
            token_exchange_client=token_exchange_client,
            circuit_store=effective_circuit_store,
            policy=effective_resilience_policy,
            now_provider=now_provider,
        )
    except OAuthProviderResilienceError as error:
        raise OAuthFlowError(
            status_code=error.status_code,
            error_code=error.error_code,
            message=error.message,
            reason=error.reason,
            details=error.details,
        ) from error
    token_response = resilient_exchange.token_response
    provider_recovered = resilient_exchange.provider_recovered
    raw_id_token = token_response.get("id_token")
    normalized_id_token = raw_id_token.strip() if isinstance(raw_id_token, str) else ""
    if not normalized_id_token:
        raise OAuthFlowError(
            status_code=401,
            error_code="oidc_id_token_missing",
            message="OIDC ID token is missing.",
            reason="oidc_id_token_missing",
            details={"provider_id": provider.provider_id},
        )
    effective_id_token_validator = (
        get_default_oidc_id_token_validator() if id_token_validator is None else id_token_validator
    )
    try:
        validated_claims = effective_id_token_validator.validate_id_token(
            provider=provider,
            id_token=normalized_id_token,
            expected_nonce=state_record.nonce,
            now_provider=now_provider,
        )
    except OidcIdTokenValidationError as error:
        raise OAuthFlowError(
            status_code=error.status_code,
            error_code=error.error_code,
            message=error.message,
            reason=error.reason,
            details=error.details,
        ) from error
    effective_registration_store = (
        get_default_registration_store() if registration_store is None else registration_store
    )
    effective_identity_linking_store = (
        get_default_oauth_identity_linking_store()
        if identity_linking_store is None
        else identity_linking_store
    )
    effective_jit_policy = (
        get_default_oauth_jit_provisioning_policy() if jit_policy is None else jit_policy
    )
    jit_provisioned = False
    try:
        linking_result = resolve_or_link_oauth_identity(
            provider_id=provider.provider_id,
            validated_claims=validated_claims,
            tenant_id=tenant_id,
            registration_store=effective_registration_store,
            linking_store=effective_identity_linking_store,
            now_provider=now_provider,
        )
    except OAuthIdentityLinkingError as error:
        if is_oauth_identity_linking_no_match_error(error):
            try:
                jit_result = provision_oauth_identity_if_eligible(
                    provider_id=provider.provider_id,
                    validated_claims=validated_claims,
                    tenant_id=tenant_id,
                    policy=effective_jit_policy,
                    registration_store=effective_registration_store,
                    linking_store=effective_identity_linking_store,
                    now_provider=now_provider,
                )
            except OAuthJitProvisioningError as jit_error:
                raise OAuthFlowError(
                    status_code=jit_error.status_code,
                    error_code=jit_error.error_code,
                    message=jit_error.message,
                    reason=jit_error.reason,
                    details=jit_error.details,
                ) from jit_error
            linking_user_id = jit_result.user_id
            linking_tenant_id = jit_result.tenant_id
            linking_role = jit_result.role
            link_status: Literal["linked_existing", "linked_new"] = "linked_new"
            jit_provisioned = True
        else:
            raise OAuthFlowError(
                status_code=error.status_code,
                error_code=error.error_code,
                message=error.message,
                reason=error.reason,
                details=error.details,
            ) from error
    else:
        linking_user_id = linking_result.user_id
        linking_tenant_id = linking_result.tenant_id
        linking_role = linking_result.role
        link_status = linking_result.link_status

    return OAuthCallbackFlowResult(
        status="protocol_validated",
        provider_id=provider.provider_id,
        oauth_subject=_extract_subject(claims=validated_claims),
        linked_user_id=linking_user_id,
        linked_tenant_id=linking_tenant_id,
        linked_user_role=linking_role,
        link_status=link_status,
        jit_provisioned=jit_provisioned,
        provider_recovered=provider_recovered,
        nonce=state_record.nonce,
    )


def get_default_oauth_state_store() -> OAuthStateStoreProtocol:
    """Return process-local default OAuth state store."""

    return _DEFAULT_OAUTH_STATE_STORE


def build_default_oauth_state_store() -> OAuthStateStoreProtocol:
    """Build the OAuth state store for the current runtime mode."""

    if not auth_runtime_requires_persistence():
        return InMemoryOAuthStateStore()
    return UnavailableOAuthStateStore(
        status_code=503,
        error_code="oauth_state_persistence_unavailable",
        message="OAuth state persistence is unavailable.",
        reason="oauth_state_persistence_unavailable",
    )


def reset_default_oauth_state_store() -> None:
    """Reset the process-local OAuth state store for isolated tests."""

    global _DEFAULT_OAUTH_STATE_STORE
    _DEFAULT_OAUTH_STATE_STORE = build_default_oauth_state_store()


def get_default_oauth_token_exchange_client() -> AuthlibOAuthTokenExchangeClient:
    """Return default Authlib-backed OAuth token-exchange client."""

    return _DEFAULT_OAUTH_TOKEN_EXCHANGE_CLIENT


def _resolve_provider_for_oauth_flow(
    *,
    provider_id: str,
    registry: Mapping[str, OAuthProviderConfig] | None,
    trust_policy: OAuthProviderTrustPolicy | None,
) -> OAuthProviderConfig:
    normalized_provider_id = provider_id.strip().lower()
    if not normalized_provider_id:
        raise OAuthFlowError(
            status_code=404,
            error_code="oauth_provider_not_supported",
            message="OAuth provider is not supported.",
            reason="oauth_provider_not_supported",
        )
    effective_registry = (
        load_oauth_provider_registry_from_env() if registry is None else dict(registry)
    )
    if normalized_provider_id not in effective_registry:
        raise OAuthFlowError(
            status_code=404,
            error_code="oauth_provider_not_supported",
            message="OAuth provider is not supported.",
            reason="oauth_provider_not_supported",
            details={"provider_id": normalized_provider_id},
        )
    provider = effective_registry[normalized_provider_id]
    if not provider.enabled:
        raise OAuthFlowError(
            status_code=403,
            error_code="oauth_provider_disabled",
            message="OAuth provider is disabled by trust policy.",
            reason="oauth_provider_disabled",
            details={"provider_id": normalized_provider_id},
        )
    effective_trust_policy = (
        get_default_oauth_provider_trust_policy() if trust_policy is None else trust_policy
    )
    try:
        return get_trusted_enabled_oauth_provider(
            provider_id=normalized_provider_id,
            registry=effective_registry,
            trust_policy=effective_trust_policy,
        )
    except OAuthProviderConfigError as error:
        raise OAuthFlowError(
            status_code=_status_code_for_provider_config_reason(error.reason),
            error_code=error.reason,
            message=error.message,
            reason=error.reason,
            details=error.details,
        ) from error


def _status_code_for_provider_config_reason(reason: str) -> int:
    if reason in {
        "oauth_provider_issuer_not_allowed",
        "oauth_provider_redirect_uri_not_allowed",
        "oauth_provider_disabled",
    }:
        return 403
    if reason == "oauth_provider_secret_reference_missing":
        return 400
    return 400


def _extract_subject(*, claims: Mapping[str, object]) -> str | None:
    direct_subject = claims.get("sub")
    if isinstance(direct_subject, str) and direct_subject.strip():
        return direct_subject.strip()
    return None


def _resolve_client_secret_from_reference(secret_ref: str) -> str | None:
    normalized_secret_ref = secret_ref.strip()
    if not normalized_secret_ref:
        return None
    if normalized_secret_ref.startswith("env:"):
        env_var_name = normalized_secret_ref.removeprefix("env:").strip()
        if not env_var_name:
            return None
    else:
        sanitized_reference = _NON_ALNUM.sub("_", normalized_secret_ref.upper()).strip("_")
        env_var_name = f"AUTH_OAUTH_CLIENT_SECRET_REF_{sanitized_reference}"
    raw_value = os.getenv(env_var_name)
    if raw_value is None:
        return None
    normalized_value = raw_value.strip()
    if not normalized_value:
        return None
    return normalized_value


def _now(*, now_provider: Callable[[], datetime] | None) -> datetime:
    if now_provider is None:
        return datetime.now(UTC)
    return now_provider()


def _iso_utc(timestamp: datetime) -> str:
    return timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")


_NON_ALNUM = re.compile(r"[^A-Z0-9]+")
_DEFAULT_OAUTH_STATE_STORE: OAuthStateStoreProtocol = build_default_oauth_state_store()
_DEFAULT_OAUTH_TOKEN_EXCHANGE_CLIENT = AuthlibOAuthTokenExchangeClient(
    request_timeout_seconds=get_auth_oauth_provider_timeout_seconds(),
)
