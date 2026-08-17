"""Focused tests for deterministic OAuth JIT provisioning guardrails."""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from services.auth.app import config as auth_config
from services.auth.app.main import create_app
from services.auth.app.main import list_auth_audit_events
from services.auth.app.oauth_flow import InMemoryOAuthStateStore
from shared.determinism.input_hash import canonical_json_dumps
from services.auth.app.registration import RegisteredUserRecord
from services.auth.app.registration import InMemoryRegistrationStore
from services.auth.app.oauth_linking import InMemoryOAuthIdentityLinkingStore
from services.auth.app.oauth_provisioning import OAuthJitProvisioningError
from services.auth.app.oauth_provisioning import OAuthJitProvisioningPolicy
from services.auth.app.oauth_provisioning import provision_oauth_identity_if_eligible


class _SuccessfulTokenExchangeClient:
    """Deterministic token-exchange stub for callback JIT tests."""

    def exchange_code_for_token(
        self,
        *,
        provider: object,
        authorization_code: str,
        code_verifier: str,
    ) -> dict[str, object]:
        assert provider is not None
        assert authorization_code.strip()
        assert code_verifier.strip()
        return {"id_token": "signed-id-token"}


class _StaticClaimsIdTokenValidator:
    """Deterministic ID-token validator stub with static trusted claims."""

    def __init__(self, *, claims: dict[str, object]) -> None:
        self._claims = dict(claims)

    def validate_id_token(
        self,
        *,
        provider: object,
        id_token: str,
        expected_nonce: str,
        now_provider: object | None = None,
    ) -> dict[str, object]:
        assert provider is not None
        assert id_token.strip() == "signed-id-token"
        assert expected_nonce.strip()
        assert now_provider is None or callable(now_provider)
        claims = dict(self._claims)
        claims["nonce"] = expected_nonce
        return claims


def test_oauth_jit_provisions_first_login_and_reuses_link_on_subsequent_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH_SECRET_RUNTIME_MODE", "development")
    _configure_oauth_provider_env(monkeypatch)
    app = create_app()
    registration_store = InMemoryRegistrationStore()
    linking_store = InMemoryOAuthIdentityLinkingStore()
    app.state.oauth_state_store = InMemoryOAuthStateStore()
    app.state.oauth_token_exchange_client = _SuccessfulTokenExchangeClient()
    app.state.oauth_id_token_validator = _StaticClaimsIdTokenValidator(
        claims={"sub": "jit-subject-001", "email": "jit.user@example.com"}
    )
    app.state.registration_store = registration_store
    app.state.oauth_identity_linking_store = linking_store
    app.state.oauth_jit_provisioning_policy = _jit_policy(
        enabled=True, eligible_providers={"google"}
    )

    with TestClient(app) as client:
        first_payload = _complete_oauth_callback(
            client=client,
            correlation_id="jit-first-login-corr",
        )
        second_payload = _complete_oauth_callback(
            client=client,
            correlation_id="jit-second-login-corr",
        )

    assert first_payload["status"] == "protocol_validated"
    assert first_payload["link_status"] == "linked_new"
    assert second_payload["status"] == "protocol_validated"
    assert second_payload["link_status"] == "linked_existing"
    assert second_payload["linked_user_id"] == first_payload["linked_user_id"]
    persisted = registration_store.get_user_by_email(email_normalized="jit.user@example.com")
    assert persisted is not None
    assert persisted.account_state == "active"
    assert len(linking_store.list_links()) == 1
    audit_events = list_auth_audit_events(app_instance=app)
    jit_allowed_count = sum(
        1 for event in audit_events if event.event_type == "jit_provisioning_allowed"
    )
    assert jit_allowed_count == 1


def test_oauth_jit_missing_required_claims_rejected_deterministically() -> None:
    registration_store = InMemoryRegistrationStore()
    linking_store = InMemoryOAuthIdentityLinkingStore()
    policy = _jit_policy(enabled=True, eligible_providers={"google"})

    first_error = _capture_jit_error(
        lambda: provision_oauth_identity_if_eligible(
            provider_id="google",
            validated_claims={"sub": "missing-email-subject"},
            tenant_id="default_tenant",
            policy=policy,
            registration_store=registration_store,
            linking_store=linking_store,
        )
    )
    second_error = _capture_jit_error(
        lambda: provision_oauth_identity_if_eligible(
            provider_id="google",
            validated_claims={"sub": "missing-email-subject"},
            tenant_id="default_tenant",
            policy=policy,
            registration_store=registration_store,
            linking_store=linking_store,
        )
    )

    assert first_error["reason"] == "oauth_jit_required_claims_missing"
    assert canonical_json_dumps(first_error) == canonical_json_dumps(second_error)


def test_oauth_jit_tenant_resolution_failure_rejected_deterministically() -> None:
    registration_store = InMemoryRegistrationStore()
    linking_store = InMemoryOAuthIdentityLinkingStore()
    policy = _jit_policy(enabled=True, eligible_providers={"google"})

    error = _capture_jit_error(
        lambda: provision_oauth_identity_if_eligible(
            provider_id="google",
            validated_claims={
                "sub": "tenant-mismatch-subject",
                "email": "jit.tenant@example.com",
                "tenant_id": "other_tenant",
            },
            tenant_id="default_tenant",
            policy=policy,
            registration_store=registration_store,
            linking_store=linking_store,
        )
    )
    assert error["reason"] == "oauth_jit_tenant_resolution_failed"


def test_oauth_jit_identity_conflict_rejected_deterministically() -> None:
    registration_store = InMemoryRegistrationStore()
    _create_active_user(
        registration_store=registration_store,
        email="conflict.user@example.com",
        phone="+254712654321",
    )
    linking_store = InMemoryOAuthIdentityLinkingStore()
    policy = _jit_policy(enabled=True, eligible_providers={"google"})

    first_error = _capture_jit_error(
        lambda: provision_oauth_identity_if_eligible(
            provider_id="google",
            validated_claims={
                "sub": "jit-conflict-subject",
                "email": "conflict.user@example.com",
            },
            tenant_id="default_tenant",
            policy=policy,
            registration_store=registration_store,
            linking_store=linking_store,
        )
    )
    second_error = _capture_jit_error(
        lambda: provision_oauth_identity_if_eligible(
            provider_id="google",
            validated_claims={
                "sub": "jit-conflict-subject",
                "email": "conflict.user@example.com",
            },
            tenant_id="default_tenant",
            policy=policy,
            registration_store=registration_store,
            linking_store=linking_store,
        )
    )
    assert first_error["reason"] == "oauth_jit_identity_conflict"
    assert canonical_json_dumps(first_error) == canonical_json_dumps(second_error)


def test_oauth_jit_provider_not_eligible_rejected_deterministically() -> None:
    registration_store = InMemoryRegistrationStore()
    linking_store = InMemoryOAuthIdentityLinkingStore()
    policy = _jit_policy(enabled=True, eligible_providers={"microsoft"})

    error = _capture_jit_error(
        lambda: provision_oauth_identity_if_eligible(
            provider_id="google",
            validated_claims={"sub": "jit-subject", "email": "jit.user@example.com"},
            tenant_id="default_tenant",
            policy=policy,
            registration_store=registration_store,
            linking_store=linking_store,
        )
    )
    assert error["reason"] == "oauth_jit_provider_not_eligible"


def _configure_oauth_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        auth_config.AUTH_OAUTH_ALLOWED_ISSUERS_ENV_VAR,
        "https://accounts.google.com",
    )
    monkeypatch.setenv(
        auth_config.AUTH_OAUTH_ALLOWED_REDIRECT_URIS_ENV_VAR,
        "https://kodi.example.com/v1/auth/oauth/google/callback",
    )
    monkeypatch.setenv(
        auth_config.AUTH_OAUTH_REQUIRED_SCOPES_ENV_VAR,
        "openid,email",
    )
    monkeypatch.setenv(
        auth_config.AUTH_OAUTH_STATE_TTL_SECONDS_ENV_VAR,
        "300",
    )
    monkeypatch.setenv(
        auth_config.AUTH_OAUTH_PROVIDER_REGISTRY_JSON_ENV_VAR,
        json.dumps(
            [
                {
                    "provider_id": "google",
                    "issuer": "https://accounts.google.com",
                    "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
                    "token_endpoint": "https://oauth2.googleapis.com/token",
                    "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
                    "client_id": "google-client-id",
                    "client_secret_ref": "env:AUTH_OAUTH_SECRET_GOOGLE",
                    "redirect_uri": "https://kodi.example.com/v1/auth/oauth/google/callback",
                    "scopes": ["openid", "email", "profile"],
                    "enabled": True,
                }
            ]
        ),
    )


def _jit_policy(*, enabled: bool, eligible_providers: set[str]) -> OAuthJitProvisioningPolicy:
    return OAuthJitProvisioningPolicy(
        enabled=enabled,
        eligible_providers=frozenset(provider.strip().lower() for provider in eligible_providers),
        required_claims=frozenset({"sub", "email"}),
        default_role="IndividualTaxpayer",
    )


def _complete_oauth_callback(*, client: TestClient, correlation_id: str) -> dict[str, object]:
    start_response = client.post(
        "/v1/auth/oauth/google/start",
        headers={"X-Correlation-ID": f"{correlation_id}-start"},
        json={"redirect_uri": "https://kodi.example.com/v1/auth/oauth/google/callback"},
    )
    assert start_response.status_code == 200
    start_payload = start_response.json()
    assert isinstance(start_payload, dict)
    callback_response = client.get(
        "/v1/auth/oauth/google/callback",
        headers={"X-Correlation-ID": correlation_id},
        params={"state": start_payload["state"], "code": "valid-authz-code"},
    )
    assert callback_response.status_code == 200
    payload = callback_response.json()
    assert isinstance(payload, dict)
    return payload


def _create_active_user(
    *,
    registration_store: InMemoryRegistrationStore,
    email: str,
    phone: str,
) -> RegisteredUserRecord:
    created = registration_store.register_user(
        email_normalized=email,
        phone_number_normalized=phone,
        kra_pin_hash=f"kra:{email}",
        password_hash=f"password:{email}",
        role="IndividualTaxpayer",
        created_at="2026-04-01T08:00:00Z",
    )
    return registration_store.mark_user_email_verified(
        user_id=created.user_id,
        verified_at="2026-04-01T08:01:00Z",
    )


def _capture_jit_error(action: Callable[[], object]) -> dict[str, object]:
    try:
        action()
    except OAuthJitProvisioningError as error:
        return {
            "error_code": error.error_code,
            "message": error.message,
            "reason": error.reason,
        }
    raise AssertionError("Expected OAuthJitProvisioningError")
