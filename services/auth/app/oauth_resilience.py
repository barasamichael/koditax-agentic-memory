"""Deterministic OAuth provider outage and degraded-mode resilience controls."""

from __future__ import annotations

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

from services.auth.app.config import get_auth_oauth_provider_timeout_seconds
from services.auth.app.config import get_auth_oauth_provider_retry_max_retries
from services.auth.app.config import get_auth_oauth_provider_circuit_open_seconds
from services.auth.app.config import get_auth_oauth_provider_retry_backoff_seconds
from services.auth.app.config import get_auth_oauth_provider_circuit_failure_threshold
from services.auth.app.config import get_auth_oauth_provider_retry_backoff_max_seconds
from services.auth.app.config import get_auth_oauth_provider_recovery_probe_interval_seconds
from services.auth.app.oauth_config import OAuthProviderConfig


@dataclass(frozen=True)
class OAuthProviderResiliencePolicy:
    """Represent deterministic OAuth provider resilience controls."""

    request_timeout_seconds: int
    max_retries: int
    retry_backoff_seconds: int
    retry_backoff_max_seconds: int
    circuit_failure_threshold: int
    circuit_open_seconds: int
    recovery_probe_interval_seconds: int


@dataclass(frozen=True)
class OAuthProviderCircuitState:
    """Represent deterministic process-local provider circuit state."""

    provider_id: str
    status: Literal["closed", "open", "half_open"]
    consecutive_failures: int
    opened_at: datetime | None
    open_until: datetime | None
    recovery_probe_started_at: datetime | None
    last_failure_reason: str | None
    last_failure_at: datetime | None


@dataclass(frozen=True)
class OAuthResilientTokenExchangeResult:
    """Represent deterministic resilient token exchange outcome."""

    token_response: Mapping[str, object]
    provider_recovered: bool


class OAuthProviderResilienceError(ValueError):
    """Represent deterministic provider-outage rejection outcomes."""

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


class OAuthProviderCircuitStoreProtocol(Protocol):
    """Define storage boundary for deterministic provider circuit states."""

    def get_state(self, *, provider_id: str) -> OAuthProviderCircuitState:
        """Return provider circuit state for deterministic resilience checks."""

        ...

    def put_state(self, *, state: OAuthProviderCircuitState) -> None:
        """Persist deterministic provider circuit state."""

        ...

    def reset(self) -> None:
        """Reset deterministic process-local circuit state for tests."""

        ...


class OAuthTokenExchangeClientProtocol(Protocol):
    """Define token-exchange boundary used by resilience wrapper."""

    def exchange_code_for_token(
        self,
        *,
        provider: OAuthProviderConfig,
        authorization_code: str,
        code_verifier: str,
    ) -> Mapping[str, object]:
        """Exchange authorization code for token response."""

        ...


class InMemoryOAuthProviderCircuitStore:
    """Process-local deterministic provider circuit state store."""

    def __init__(self) -> None:
        self._states: dict[str, OAuthProviderCircuitState] = {}
        self._lock = Lock()

    def get_state(self, *, provider_id: str) -> OAuthProviderCircuitState:
        normalized_provider = provider_id.strip().lower()
        with self._lock:
            existing = self._states.get(normalized_provider)
            if existing is not None:
                return existing
            state = _default_circuit_state(provider_id=normalized_provider)
            self._states[normalized_provider] = state
            return state

    def put_state(self, *, state: OAuthProviderCircuitState) -> None:
        normalized_provider = state.provider_id.strip().lower()
        with self._lock:
            self._states[normalized_provider] = OAuthProviderCircuitState(
                provider_id=normalized_provider,
                status=state.status,
                consecutive_failures=state.consecutive_failures,
                opened_at=state.opened_at,
                open_until=state.open_until,
                recovery_probe_started_at=state.recovery_probe_started_at,
                last_failure_reason=state.last_failure_reason,
                last_failure_at=state.last_failure_at,
            )

    def reset(self) -> None:
        with self._lock:
            self._states.clear()


def get_default_oauth_provider_resilience_policy() -> OAuthProviderResiliencePolicy:
    """Return deterministic default resilience policy from auth config."""

    return OAuthProviderResiliencePolicy(
        request_timeout_seconds=get_auth_oauth_provider_timeout_seconds(),
        max_retries=get_auth_oauth_provider_retry_max_retries(),
        retry_backoff_seconds=get_auth_oauth_provider_retry_backoff_seconds(),
        retry_backoff_max_seconds=get_auth_oauth_provider_retry_backoff_max_seconds(),
        circuit_failure_threshold=get_auth_oauth_provider_circuit_failure_threshold(),
        circuit_open_seconds=get_auth_oauth_provider_circuit_open_seconds(),
        recovery_probe_interval_seconds=get_auth_oauth_provider_recovery_probe_interval_seconds(),
    )


def get_default_oauth_provider_circuit_store() -> OAuthProviderCircuitStoreProtocol:
    """Return deterministic process-local provider circuit store."""

    return _DEFAULT_OAUTH_PROVIDER_CIRCUIT_STORE


def exchange_code_for_token_with_resilience(
    *,
    provider: OAuthProviderConfig,
    authorization_code: str,
    code_verifier: str,
    token_exchange_client: OAuthTokenExchangeClientProtocol,
    circuit_store: OAuthProviderCircuitStoreProtocol,
    policy: OAuthProviderResiliencePolicy,
    now_provider: Callable[[], datetime] | None = None,
    sleep_fn: Callable[[int], None] | None = None,
) -> OAuthResilientTokenExchangeResult:
    """Exchange OAuth code with deterministic retry/circuit behavior."""

    provider_id = provider.provider_id.strip().lower()
    now = _now(now_provider=now_provider)
    state = circuit_store.get_state(provider_id=provider_id)
    if state.status == "open":
        if state.open_until is not None and now < state.open_until:
            retry_after = max(1, int((state.open_until - now).total_seconds()))
            raise OAuthProviderResilienceError(
                status_code=503,
                error_code="oauth_provider_circuit_open",
                message="OAuth provider is temporarily unavailable.",
                reason="oauth_provider_circuit_open",
                details={
                    "provider_id": provider_id,
                    "retry_after_seconds": retry_after,
                    "safe_next_action": "retry_later_or_use_alternate_login_path",
                },
            )
        if (
            state.recovery_probe_started_at is not None
            and now
            < state.recovery_probe_started_at
            + timedelta(seconds=policy.recovery_probe_interval_seconds)
        ):
            retry_after = max(
                1,
                int(
                    (
                        state.recovery_probe_started_at
                        + timedelta(seconds=policy.recovery_probe_interval_seconds)
                        - now
                    ).total_seconds()
                ),
            )
            raise OAuthProviderResilienceError(
                status_code=503,
                error_code="oauth_provider_recovery_in_progress",
                message="OAuth provider recovery is in progress.",
                reason="oauth_provider_recovery_in_progress",
                details={
                    "provider_id": provider_id,
                    "retry_after_seconds": retry_after,
                    "safe_next_action": "retry_later_or_use_alternate_login_path",
                },
            )
        state = OAuthProviderCircuitState(
            provider_id=provider_id,
            status="half_open",
            consecutive_failures=state.consecutive_failures,
            opened_at=state.opened_at,
            open_until=state.open_until,
            recovery_probe_started_at=now,
            last_failure_reason=state.last_failure_reason,
            last_failure_at=state.last_failure_at,
        )
        circuit_store.put_state(state=state)
    elif state.status == "half_open":
        raise OAuthProviderResilienceError(
            status_code=503,
            error_code="oauth_provider_recovery_in_progress",
            message="OAuth provider recovery is in progress.",
            reason="oauth_provider_recovery_in_progress",
            details={
                "provider_id": provider_id,
                "safe_next_action": "retry_later_or_use_alternate_login_path",
            },
        )

    max_attempts = 1 + max(0, policy.max_retries)
    failure_reason: str | None = None
    for attempt_index in range(max_attempts):
        try:
            response = token_exchange_client.exchange_code_for_token(
                provider=provider,
                authorization_code=authorization_code,
                code_verifier=code_verifier,
            )
        except Exception as error:
            failure_reason = _classify_provider_failure(error=error)
            if attempt_index < max_attempts - 1:
                backoff = _compute_backoff_seconds(
                    policy=policy,
                    retry_index=attempt_index,
                )
                if sleep_fn is not None and backoff > 0:
                    sleep_fn(backoff)
                continue
            return _handle_final_provider_failure(
                provider_id=provider_id,
                prior_state=state,
                failure_reason=failure_reason,
                circuit_store=circuit_store,
                policy=policy,
                now_provider=now_provider,
            )
        provider_recovered = state.status == "half_open"
        closed_state = OAuthProviderCircuitState(
            provider_id=provider_id,
            status="closed",
            consecutive_failures=0,
            opened_at=None,
            open_until=None,
            recovery_probe_started_at=None,
            last_failure_reason=None,
            last_failure_at=None,
        )
        circuit_store.put_state(state=closed_state)
        return OAuthResilientTokenExchangeResult(
            token_response=response,
            provider_recovered=provider_recovered,
        )

    # Unreachable by construction; retain fail-closed behavior.
    raise OAuthProviderResilienceError(
        status_code=503,
        error_code="oauth_provider_unavailable",
        message="OAuth provider is temporarily unavailable.",
        reason="oauth_provider_unavailable",
        details={
            "provider_id": provider_id,
            "safe_next_action": "retry_later_or_use_alternate_login_path",
        },
    )


def _handle_final_provider_failure(
    *,
    provider_id: str,
    prior_state: OAuthProviderCircuitState,
    failure_reason: str,
    circuit_store: OAuthProviderCircuitStoreProtocol,
    policy: OAuthProviderResiliencePolicy,
    now_provider: Callable[[], datetime] | None,
) -> OAuthResilientTokenExchangeResult:
    now = _now(now_provider=now_provider)
    next_failures = prior_state.consecutive_failures + 1
    should_open_circuit = (
        prior_state.status == "half_open" or next_failures >= policy.circuit_failure_threshold
    )
    if should_open_circuit:
        opened_state = OAuthProviderCircuitState(
            provider_id=provider_id,
            status="open",
            consecutive_failures=next_failures,
            opened_at=now,
            open_until=now + timedelta(seconds=policy.circuit_open_seconds),
            recovery_probe_started_at=(
                now if prior_state.status == "half_open" else prior_state.recovery_probe_started_at
            ),
            last_failure_reason=failure_reason,
            last_failure_at=now,
        )
        circuit_store.put_state(state=opened_state)
        raise OAuthProviderResilienceError(
            status_code=503,
            error_code="oauth_provider_degraded_mode_active",
            message="OAuth provider is operating in degraded mode.",
            reason="oauth_provider_degraded_mode_active",
            details={
                "provider_id": provider_id,
                "delivery_failure_class": failure_reason,
                "retry_after_seconds": policy.circuit_open_seconds,
                "safe_next_action": "retry_later_or_use_alternate_login_path",
                "state_transition": "circuit_opened",
            },
        )
    updated_state = OAuthProviderCircuitState(
        provider_id=provider_id,
        status="closed",
        consecutive_failures=next_failures,
        opened_at=None,
        open_until=None,
        recovery_probe_started_at=prior_state.recovery_probe_started_at,
        last_failure_reason=failure_reason,
        last_failure_at=now,
    )
    circuit_store.put_state(state=updated_state)
    status_code = 504 if failure_reason == "oauth_provider_timeout" else 503
    raise OAuthProviderResilienceError(
        status_code=status_code,
        error_code=failure_reason,
        message="OAuth provider is temporarily unavailable.",
        reason=failure_reason,
        details={
            "provider_id": provider_id,
            "safe_next_action": "retry_later_or_use_alternate_login_path",
        },
    )


def _classify_provider_failure(*, error: Exception) -> str:
    reason = getattr(error, "reason", None)
    details_value = getattr(error, "details", {})
    details: Mapping[str, object]
    if isinstance(details_value, Mapping):
        details = cast(Mapping[str, object], details_value)
    else:
        details = {}
    if isinstance(reason, str):
        if reason in {
            "oauth_provider_timeout",
            "oauth_provider_unavailable",
        }:
            return reason
        if reason == "oauth_token_exchange_failed":
            requirement = details.get("requirement")
            if requirement == "provider_exchange_error":
                return "oauth_provider_unavailable"
            return "oauth_provider_unavailable"
    if isinstance(error, TimeoutError):
        return "oauth_provider_timeout"
    error_message = str(error).lower()
    if "timeout" in error_message or "timed out" in error_message:
        return "oauth_provider_timeout"
    return "oauth_provider_unavailable"


def _compute_backoff_seconds(
    *,
    policy: OAuthProviderResiliencePolicy,
    retry_index: int,
) -> int:
    base = max(0, policy.retry_backoff_seconds)
    if base <= 0:
        return 0
    computed = base * (2**retry_index)
    return min(computed, max(0, policy.retry_backoff_max_seconds))


def _default_circuit_state(*, provider_id: str) -> OAuthProviderCircuitState:
    return OAuthProviderCircuitState(
        provider_id=provider_id,
        status="closed",
        consecutive_failures=0,
        opened_at=None,
        open_until=None,
        recovery_probe_started_at=None,
        last_failure_reason=None,
        last_failure_at=None,
    )


def _now(*, now_provider: Callable[[], datetime] | None) -> datetime:
    if now_provider is None:
        return datetime.now(UTC)
    return now_provider()


_DEFAULT_OAUTH_PROVIDER_CIRCUIT_STORE = InMemoryOAuthProviderCircuitStore()
