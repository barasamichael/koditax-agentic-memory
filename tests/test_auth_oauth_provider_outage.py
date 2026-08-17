"""Focused tests for deterministic OAuth provider outage resilience behavior."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from collections.abc import Callable

import pytest

from services.auth.app.oauth_flow import OAuthFlowError
from shared.determinism.input_hash import canonical_json_dumps
from services.auth.app.oauth_config import OAuthProviderConfig
from services.auth.app.oauth_resilience import OAuthProviderResilienceError
from services.auth.app.oauth_resilience import OAuthProviderResiliencePolicy
from services.auth.app.oauth_resilience import InMemoryOAuthProviderCircuitStore
from services.auth.app.oauth_resilience import exchange_code_for_token_with_resilience


class _HealthyClient:
    """Deterministic healthy OAuth token-exchange stub."""

    def exchange_code_for_token(
        self,
        *,
        provider: OAuthProviderConfig,
        authorization_code: str,
        code_verifier: str,
    ) -> dict[str, object]:
        assert provider.provider_id
        assert authorization_code.strip()
        assert code_verifier.strip()
        return {"id_token": "healthy-id-token"}


class _TimeoutClient:
    """Deterministic timeout token-exchange stub."""

    def exchange_code_for_token(
        self,
        *,
        provider: OAuthProviderConfig,
        authorization_code: str,
        code_verifier: str,
    ) -> dict[str, object]:
        assert provider.provider_id
        assert authorization_code.strip()
        assert code_verifier.strip()
        raise TimeoutError("provider timeout")


class _UnavailableClient:
    """Deterministic unavailable token-exchange stub."""

    def __init__(self) -> None:
        self.call_count = 0

    def exchange_code_for_token(
        self,
        *,
        provider: OAuthProviderConfig,
        authorization_code: str,
        code_verifier: str,
    ) -> dict[str, object]:
        self.call_count += 1
        assert provider.provider_id
        assert authorization_code.strip()
        assert code_verifier.strip()
        raise OAuthFlowError(
            status_code=401,
            error_code="oauth_token_exchange_failed",
            message="OAuth callback token exchange failed.",
            reason="oauth_token_exchange_failed",
            details={
                "provider_id": provider.provider_id,
                "requirement": "provider_exchange_error",
            },
        )


def test_healthy_provider_path_remains_functional() -> None:
    provider = _provider()
    store = InMemoryOAuthProviderCircuitStore()
    policy = _policy()

    result = exchange_code_for_token_with_resilience(
        provider=provider,
        authorization_code="valid-code",
        code_verifier="valid-verifier",
        token_exchange_client=_HealthyClient(),
        circuit_store=store,
        policy=policy,
    )
    assert result.token_response["id_token"] == "healthy-id-token"
    assert result.provider_recovered is False


def test_timeout_triggers_deterministic_outage_error() -> None:
    provider = _provider()
    store = InMemoryOAuthProviderCircuitStore()
    policy = _policy(max_retries=0)

    error = _capture_resilience_error(
        lambda: exchange_code_for_token_with_resilience(
            provider=provider,
            authorization_code="valid-code",
            code_verifier="valid-verifier",
            token_exchange_client=_TimeoutClient(),
            circuit_store=store,
            policy=policy,
        )
    )
    assert error["reason"] == "oauth_provider_timeout"
    assert error["error_code"] == "oauth_provider_timeout"


def test_repeated_failures_open_circuit_and_reject_deterministically() -> None:
    provider = _provider()
    store = InMemoryOAuthProviderCircuitStore()
    policy = _policy(max_retries=0, circuit_failure_threshold=2, circuit_open_seconds=90)
    unavailable_client = _UnavailableClient()
    now = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)

    first_error = _capture_resilience_error(
        lambda: exchange_code_for_token_with_resilience(
            provider=provider,
            authorization_code="valid-code",
            code_verifier="valid-verifier",
            token_exchange_client=unavailable_client,
            circuit_store=store,
            policy=policy,
            now_provider=lambda: now,
        )
    )
    second_error = _capture_resilience_error(
        lambda: exchange_code_for_token_with_resilience(
            provider=provider,
            authorization_code="valid-code",
            code_verifier="valid-verifier",
            token_exchange_client=unavailable_client,
            circuit_store=store,
            policy=policy,
            now_provider=lambda: now + timedelta(seconds=1),
        )
    )
    third_error = _capture_resilience_error(
        lambda: exchange_code_for_token_with_resilience(
            provider=provider,
            authorization_code="valid-code",
            code_verifier="valid-verifier",
            token_exchange_client=unavailable_client,
            circuit_store=store,
            policy=policy,
            now_provider=lambda: now + timedelta(seconds=2),
        )
    )

    assert first_error["reason"] == "oauth_provider_unavailable"
    assert second_error["reason"] == "oauth_provider_degraded_mode_active"
    assert third_error["reason"] == "oauth_provider_circuit_open"


def test_circuit_open_blocks_provider_calls_until_recovery_window() -> None:
    provider = _provider()
    store = InMemoryOAuthProviderCircuitStore()
    policy = _policy(max_retries=0, circuit_failure_threshold=2, circuit_open_seconds=120)
    unavailable_client = _UnavailableClient()
    now = datetime(2026, 4, 1, 12, 30, 0, tzinfo=UTC)

    _capture_resilience_error(
        lambda: exchange_code_for_token_with_resilience(
            provider=provider,
            authorization_code="valid-code",
            code_verifier="valid-verifier",
            token_exchange_client=unavailable_client,
            circuit_store=store,
            policy=policy,
            now_provider=lambda: now,
        )
    )
    _capture_resilience_error(
        lambda: exchange_code_for_token_with_resilience(
            provider=provider,
            authorization_code="valid-code",
            code_verifier="valid-verifier",
            token_exchange_client=unavailable_client,
            circuit_store=store,
            policy=policy,
            now_provider=lambda: now + timedelta(seconds=1),
        )
    )
    call_count_before_open_reject = unavailable_client.call_count
    _capture_resilience_error(
        lambda: exchange_code_for_token_with_resilience(
            provider=provider,
            authorization_code="valid-code",
            code_verifier="valid-verifier",
            token_exchange_client=unavailable_client,
            circuit_store=store,
            policy=policy,
            now_provider=lambda: now + timedelta(seconds=2),
        )
    )
    assert unavailable_client.call_count == call_count_before_open_reject


def test_recovery_path_closes_circuit_after_successful_probe() -> None:
    provider = _provider()
    store = InMemoryOAuthProviderCircuitStore()
    policy = _policy(max_retries=0, circuit_failure_threshold=2, circuit_open_seconds=30)
    unavailable_client = _UnavailableClient()
    now = datetime(2026, 4, 1, 13, 0, 0, tzinfo=UTC)

    _capture_resilience_error(
        lambda: exchange_code_for_token_with_resilience(
            provider=provider,
            authorization_code="valid-code",
            code_verifier="valid-verifier",
            token_exchange_client=unavailable_client,
            circuit_store=store,
            policy=policy,
            now_provider=lambda: now,
        )
    )
    _capture_resilience_error(
        lambda: exchange_code_for_token_with_resilience(
            provider=provider,
            authorization_code="valid-code",
            code_verifier="valid-verifier",
            token_exchange_client=unavailable_client,
            circuit_store=store,
            policy=policy,
            now_provider=lambda: now + timedelta(seconds=1),
        )
    )

    recovered_result = exchange_code_for_token_with_resilience(
        provider=provider,
        authorization_code="valid-code",
        code_verifier="valid-verifier",
        token_exchange_client=_HealthyClient(),
        circuit_store=store,
        policy=policy,
        now_provider=lambda: now + timedelta(seconds=31),
    )
    assert recovered_result.provider_recovered is True
    assert recovered_result.token_response["id_token"] == "healthy-id-token"


def test_same_outage_condition_yields_stable_error_payload() -> None:
    provider = _provider()
    store = InMemoryOAuthProviderCircuitStore()
    policy = _policy(max_retries=0, circuit_failure_threshold=2, circuit_open_seconds=60)
    unavailable_client = _UnavailableClient()
    now = datetime(2026, 4, 1, 14, 0, 0, tzinfo=UTC)

    _capture_resilience_error(
        lambda: exchange_code_for_token_with_resilience(
            provider=provider,
            authorization_code="valid-code",
            code_verifier="valid-verifier",
            token_exchange_client=unavailable_client,
            circuit_store=store,
            policy=policy,
            now_provider=lambda: now,
        )
    )
    _capture_resilience_error(
        lambda: exchange_code_for_token_with_resilience(
            provider=provider,
            authorization_code="valid-code",
            code_verifier="valid-verifier",
            token_exchange_client=unavailable_client,
            circuit_store=store,
            policy=policy,
            now_provider=lambda: now + timedelta(seconds=1),
        )
    )
    first_error = _capture_resilience_error(
        lambda: exchange_code_for_token_with_resilience(
            provider=provider,
            authorization_code="valid-code",
            code_verifier="valid-verifier",
            token_exchange_client=unavailable_client,
            circuit_store=store,
            policy=policy,
            now_provider=lambda: now + timedelta(seconds=2),
        )
    )
    second_error = _capture_resilience_error(
        lambda: exchange_code_for_token_with_resilience(
            provider=provider,
            authorization_code="valid-code",
            code_verifier="valid-verifier",
            token_exchange_client=unavailable_client,
            circuit_store=store,
            policy=policy,
            now_provider=lambda: now + timedelta(seconds=2),
        )
    )
    assert canonical_json_dumps(first_error) == canonical_json_dumps(second_error)


def _provider() -> OAuthProviderConfig:
    return OAuthProviderConfig(
        provider_id="google",
        issuer="https://accounts.google.com",
        authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        token_endpoint="https://oauth2.googleapis.com/token",
        jwks_uri="https://www.googleapis.com/oauth2/v3/certs",
        client_id="google-client-id",
        client_secret_ref="env:AUTH_OAUTH_SECRET_GOOGLE",
        redirect_uri="https://kodi.example.com/v1/auth/oauth/google/callback",
        scopes=("openid", "email", "profile"),
        enabled=True,
    )


def _policy(
    *,
    max_retries: int = 1,
    circuit_failure_threshold: int = 3,
    circuit_open_seconds: int = 60,
) -> OAuthProviderResiliencePolicy:
    return OAuthProviderResiliencePolicy(
        request_timeout_seconds=5,
        max_retries=max_retries,
        retry_backoff_seconds=1,
        retry_backoff_max_seconds=2,
        circuit_failure_threshold=circuit_failure_threshold,
        circuit_open_seconds=circuit_open_seconds,
        recovery_probe_interval_seconds=30,
    )


def _capture_resilience_error(action: Callable[[], object]) -> dict[str, object]:
    with pytest.raises(OAuthProviderResilienceError) as error_info:
        action()
    return {
        "error_code": error_info.value.error_code,
        "message": error_info.value.message,
        "reason": error_info.value.reason,
        "details": error_info.value.details,
    }
