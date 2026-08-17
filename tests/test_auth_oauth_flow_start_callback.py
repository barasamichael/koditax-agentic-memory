"""Runtime tests for OAuth Authorization Code + PKCE start/callback flow."""

from __future__ import annotations

import json
from typing import Any
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from urllib.parse import parse_qs
from urllib.parse import urlparse
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.auth.app import config as auth_config
from services.auth.app.main import create_app
from services.auth.app.main import list_auth_audit_events
from services.auth.app.oauth_flow import OAuthFlowError
from services.auth.app.oauth_flow import InMemoryOAuthStateStore
from services.auth.app.oauth_flow import OAuthAuthorizationState
from shared.determinism.input_hash import canonical_json_dumps
from services.auth.app.registration import InMemoryRegistrationStore
from services.auth.app.oauth_linking import InMemoryOAuthIdentityLinkingStore
from services.auth.app.oauth_validation import OidcIdTokenValidationError


class _SuccessfulTokenExchangeClient:
    """Deterministic token-exchange stub for callback happy-path coverage."""

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


class _MissingIdTokenExchangeClient:
    """Deterministic token-exchange stub for missing ID-token callback coverage."""

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
        return {"token_type": "Bearer"}


class _FailingTokenExchangeClient:
    """Deterministic token-exchange stub for canonical callback failure coverage."""

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
        raise OAuthFlowError(
            status_code=401,
            error_code="oauth_token_exchange_failed",
            message="OAuth callback token exchange failed.",
            reason="oauth_token_exchange_failed",
            details={"provider_id": "google"},
        )


class _SuccessfulIdTokenValidator:
    """Deterministic ID-token validator stub for callback protocol tests."""

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
        return {
            "sub": "oidc-subject-001",
            "nonce": expected_nonce,
            "email": "linked.user@example.com",
        }


class _FailingIdTokenValidator:
    """Deterministic ID-token validator stub for canonical callback error coverage."""

    def validate_id_token(
        self,
        *,
        provider: object,
        id_token: str,
        expected_nonce: str,
        now_provider: object | None = None,
    ) -> dict[str, object]:
        assert provider is not None
        assert id_token.strip()
        assert expected_nonce.strip()
        assert now_provider is None or callable(now_provider)
        raise OidcIdTokenValidationError(
            status_code=401,
            error_code="oidc_id_token_nonce_invalid",
            message="OIDC ID token nonce is invalid.",
            reason="oidc_id_token_nonce_invalid",
            details={"provider_id": "google"},
        )


class _TenantMismatchIdTokenValidator:
    """Deterministic validator stub that returns tenant-mismatched claims."""

    def validate_id_token(
        self,
        *,
        provider: object,
        id_token: str,
        expected_nonce: str,
        now_provider: object | None = None,
    ) -> dict[str, object]:
        assert provider is not None
        assert id_token.strip()
        assert expected_nonce.strip()
        assert now_provider is None or callable(now_provider)
        return {
            "sub": "oidc-subject-tenant-mismatch",
            "nonce": expected_nonce,
            "email": "linked.user@example.com",
            "tenant_id": "different_tenant",
        }


@pytest.fixture()
def oauth_client_and_store(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, FastAPI, InMemoryOAuthStateStore]]:
    """Create isolated OAuth runtime client with deterministic provider registry."""

    monkeypatch.setenv("AUTH_SECRET_RUNTIME_MODE", "development")
    monkeypatch.setenv(
        auth_config.AUTH_OAUTH_ALLOWED_ISSUERS_ENV_VAR,
        "https://accounts.google.com",
    )
    monkeypatch.setenv(
        auth_config.AUTH_OAUTH_ALLOWED_REDIRECT_URIS_ENV_VAR,
        ",".join(
            (
                "https://kodi.example.com/v1/auth/oauth/google/callback",
                "https://kodi.example.com/v1/auth/oauth/disabled_google/callback",
            )
        ),
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
                },
                {
                    "provider_id": "disabled_google",
                    "issuer": "https://accounts.google.com",
                    "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
                    "token_endpoint": "https://oauth2.googleapis.com/token",
                    "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
                    "client_id": "google-disabled-client-id",
                    "client_secret_ref": "env:AUTH_OAUTH_SECRET_DISABLED",
                    "redirect_uri": "https://kodi.example.com/v1/auth/oauth/disabled_google/callback",
                    "scopes": ["openid", "email", "profile"],
                    "enabled": False,
                },
            ]
        ),
    )

    app = create_app()
    oauth_state_store = InMemoryOAuthStateStore()
    registration_store = InMemoryRegistrationStore()
    seeded_user = registration_store.register_user(
        email_normalized="linked.user@example.com",
        phone_number_normalized="+254712345678",
        kra_pin_hash="kra-hash-001",
        password_hash="password-hash-001",
        role="IndividualTaxpayer",
        created_at="2026-04-01T10:00:00Z",
    )
    registration_store.mark_user_email_verified(
        user_id=seeded_user.user_id,
        verified_at="2026-04-01T10:05:00Z",
    )
    app.state.oauth_state_store = oauth_state_store
    app.state.oauth_token_exchange_client = _SuccessfulTokenExchangeClient()
    app.state.oauth_id_token_validator = _SuccessfulIdTokenValidator()
    app.state.registration_store = registration_store
    app.state.oauth_identity_linking_store = InMemoryOAuthIdentityLinkingStore()

    with TestClient(app) as client:
        yield client, app, oauth_state_store


def test_oauth_start_returns_pkce_state_nonce_and_provider_redirect_metadata(
    oauth_client_and_store: tuple[TestClient, FastAPI, InMemoryOAuthStateStore],
) -> None:
    client, _, oauth_state_store = oauth_client_and_store

    response = client.post(
        "/v1/auth/oauth/google/start",
        headers={"X-Correlation-ID": "oauth-start-success-corr"},
        json={"redirect_uri": "https://kodi.example.com/v1/auth/oauth/google/callback"},
    )

    payload = _response_json(response)
    assert response.status_code == 200
    assert payload["status"] == "redirect_required"
    assert payload["provider"] == "google"
    assert isinstance(payload["state"], str)
    assert isinstance(payload["nonce"], str)
    assert isinstance(payload["expires_at"], str)
    assert isinstance(payload["authorization_url"], str)
    assert isinstance(payload["traceability"]["trace_id"], str)
    assert isinstance(payload["traceability"]["correlation_id"], str)
    assert payload["traceability"]["correlation_id"] == "oauth-start-success-corr"

    parsed_redirect = urlparse(payload["authorization_url"])
    query = parse_qs(parsed_redirect.query)
    assert query["state"] == [payload["state"]]
    assert query["nonce"] == [payload["nonce"]]
    assert query["client_id"] == ["google-client-id"]
    assert query["code_challenge_method"] == ["S256"]
    assert "code_challenge" in query
    assert query["redirect_uri"] == ["https://kodi.example.com/v1/auth/oauth/google/callback"]

    stored_state = oauth_state_store.get_state(state=payload["state"])
    assert stored_state is not None
    assert stored_state.provider_id == "google"
    assert stored_state.consumed_at is None


def test_oauth_callback_valid_path_returns_protocol_validated_without_session_tokens(
    oauth_client_and_store: tuple[TestClient, FastAPI, InMemoryOAuthStateStore],
) -> None:
    client, app, oauth_state_store = oauth_client_and_store
    start_payload = _start_oauth_flow(client=client)

    response = client.get(
        "/v1/auth/oauth/google/callback",
        headers={"X-Correlation-ID": "oauth-callback-success-corr"},
        params={"state": start_payload["state"], "code": "valid-authz-code"},
    )

    payload = _response_json(response)
    assert response.status_code == 200
    assert payload["status"] == "protocol_validated"
    assert payload["callback_status"] == "protocol_validated"
    assert payload["provider"] == "google"
    assert payload["oauth_subject"] == "oidc-subject-001"
    assert isinstance(payload["linked_user_id"], str)
    assert payload["linked_tenant_id"] == "default_tenant"
    assert payload["link_status"] == "linked_new"
    assert "access_token" not in payload
    assert "refresh_token" not in payload
    assert "session" not in payload

    stored_state = oauth_state_store.get_state(state=start_payload["state"])
    assert stored_state is not None
    assert stored_state.consumed_at is not None
    audit_events = list_auth_audit_events(app_instance=app)
    assert audit_events[-1].event_type == "auth_oauth_identity_link_succeeded"
    assert audit_events[-1].reason_code is None


def test_oauth_callback_resolves_existing_link_deterministically(
    oauth_client_and_store: tuple[TestClient, FastAPI, InMemoryOAuthStateStore],
) -> None:
    client, _, _ = oauth_client_and_store
    first_start_payload = _start_oauth_flow(client=client)
    first_response = client.get(
        "/v1/auth/oauth/google/callback",
        headers={"X-Correlation-ID": "oauth-callback-linked-new-corr"},
        params={"state": first_start_payload["state"], "code": "valid-authz-code"},
    )
    first_payload = _response_json(first_response)
    assert first_response.status_code == 200
    assert first_payload["link_status"] == "linked_new"

    second_start_payload = _start_oauth_flow(client=client)
    second_response = client.get(
        "/v1/auth/oauth/google/callback",
        headers={"X-Correlation-ID": "oauth-callback-linked-existing-corr"},
        params={"state": second_start_payload["state"], "code": "valid-authz-code"},
    )
    second_payload = _response_json(second_response)
    assert second_response.status_code == 200
    assert second_payload["link_status"] == "linked_existing"
    assert second_payload["linked_user_id"] == first_payload["linked_user_id"]


def test_oauth_callback_invalid_state_is_rejected_deterministically(
    oauth_client_and_store: tuple[TestClient, FastAPI, InMemoryOAuthStateStore],
) -> None:
    client, _, _ = oauth_client_and_store
    params = {"state": "missing-oauth-state", "code": "valid-authz-code"}
    headers = {"X-Correlation-ID": "oauth-callback-invalid-state-corr"}

    first = client.get("/v1/auth/oauth/google/callback", headers=headers, params=params)
    second = client.get("/v1/auth/oauth/google/callback", headers=headers, params=params)

    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)
    assert first.status_code == 400
    assert second.status_code == 400
    assert first_error["error_code"] == "oauth_state_invalid"
    assert first_error["reason"] == "oauth_state_invalid"
    assert canonical_json_dumps(first_error) == canonical_json_dumps(second_error)


def test_oauth_callback_expired_state_is_rejected_deterministically(
    oauth_client_and_store: tuple[TestClient, FastAPI, InMemoryOAuthStateStore],
) -> None:
    client, _, oauth_state_store = oauth_client_and_store
    state = "expired-oauth-state-001"
    now = datetime(2026, 4, 1, 10, 0, 0, tzinfo=UTC)
    oauth_state_store.put_state(
        state=OAuthAuthorizationState(
            provider_id="google",
            state=state,
            nonce="expired-nonce",
            redirect_uri="https://kodi.example.com/v1/auth/oauth/google/callback",
            code_verifier="expired-code-verifier",
            created_at=now - timedelta(minutes=10),
            expires_at=now - timedelta(seconds=1),
            consumed_at=None,
        )
    )

    response = client.get(
        "/v1/auth/oauth/google/callback",
        headers={"X-Correlation-ID": "oauth-callback-expired-corr"},
        params={"state": state, "code": "valid-authz-code"},
    )

    error = _extract_error_detail(response)
    assert response.status_code == 409
    assert error["error_code"] == "oauth_state_expired"
    assert error["reason"] == "oauth_state_expired"


def test_oauth_callback_replay_is_rejected_deterministically(
    oauth_client_and_store: tuple[TestClient, FastAPI, InMemoryOAuthStateStore],
) -> None:
    client, _, _ = oauth_client_and_store
    start_payload = _start_oauth_flow(client=client)
    callback_params = {"state": start_payload["state"], "code": "valid-authz-code"}

    first = client.get(
        "/v1/auth/oauth/google/callback",
        headers={"X-Correlation-ID": "oauth-callback-replay-first-corr"},
        params=callback_params,
    )
    second = client.get(
        "/v1/auth/oauth/google/callback",
        headers={"X-Correlation-ID": "oauth-callback-replay-second-corr"},
        params=callback_params,
    )

    assert first.status_code == 200
    error = _extract_error_detail(second)
    assert second.status_code == 409
    assert error["error_code"] == "oauth_callback_replay_detected"
    assert error["reason"] == "oauth_callback_replay_detected"


def test_oauth_callback_missing_code_is_rejected_deterministically(
    oauth_client_and_store: tuple[TestClient, FastAPI, InMemoryOAuthStateStore],
) -> None:
    client, _, _ = oauth_client_and_store
    start_payload = _start_oauth_flow(client=client)

    response = client.get(
        "/v1/auth/oauth/google/callback",
        headers={"X-Correlation-ID": "oauth-callback-code-missing-corr"},
        params={"state": start_payload["state"]},
    )

    error = _extract_error_detail(response)
    assert response.status_code == 400
    assert error["error_code"] == "oauth_callback_code_missing"
    assert error["reason"] == "oauth_callback_code_missing"


def test_oauth_start_rejects_unknown_and_disabled_provider_deterministically(
    oauth_client_and_store: tuple[TestClient, FastAPI, InMemoryOAuthStateStore],
) -> None:
    client, _, _ = oauth_client_and_store

    unknown = client.post(
        "/v1/auth/oauth/unknown_provider/start",
        headers={"X-Correlation-ID": "oauth-start-unknown-provider-corr"},
        json={"redirect_uri": "https://kodi.example.com/v1/auth/oauth/google/callback"},
    )
    unknown_error = _extract_error_detail(unknown)
    assert unknown.status_code == 404
    assert unknown_error["error_code"] == "oauth_provider_not_supported"
    assert unknown_error["reason"] == "oauth_provider_not_supported"

    disabled = client.post(
        "/v1/auth/oauth/disabled_google/start",
        headers={"X-Correlation-ID": "oauth-start-disabled-provider-corr"},
        json={"redirect_uri": "https://kodi.example.com/v1/auth/oauth/disabled_google/callback"},
    )
    disabled_error = _extract_error_detail(disabled)
    assert disabled.status_code == 403
    assert disabled_error["error_code"] == "oauth_provider_disabled"
    assert disabled_error["reason"] == "oauth_provider_disabled"


def test_oauth_callback_token_exchange_failure_returns_canonical_error(
    oauth_client_and_store: tuple[TestClient, FastAPI, InMemoryOAuthStateStore],
) -> None:
    client, app, _ = oauth_client_and_store
    app.state.oauth_token_exchange_client = _FailingTokenExchangeClient()
    start_payload = _start_oauth_flow(client=client)

    response = client.get(
        "/v1/auth/oauth/google/callback",
        headers={"X-Correlation-ID": "oauth-callback-exchange-failure-corr"},
        params={"state": start_payload["state"], "code": "valid-authz-code"},
    )

    error = _extract_error_detail(response)
    assert response.status_code == 503
    assert error["error_code"] == "oauth_provider_unavailable"
    assert error["reason"] == "oauth_provider_unavailable"


def test_oauth_callback_missing_id_token_returns_canonical_error(
    oauth_client_and_store: tuple[TestClient, FastAPI, InMemoryOAuthStateStore],
) -> None:
    client, app, _ = oauth_client_and_store
    app.state.oauth_token_exchange_client = _MissingIdTokenExchangeClient()
    start_payload = _start_oauth_flow(client=client)

    response = client.get(
        "/v1/auth/oauth/google/callback",
        headers={"X-Correlation-ID": "oauth-callback-id-token-missing-corr"},
        params={"state": start_payload["state"], "code": "valid-authz-code"},
    )

    error = _extract_error_detail(response)
    assert response.status_code == 401
    assert error["error_code"] == "oidc_id_token_missing"
    assert error["reason"] == "oidc_id_token_missing"


def test_oauth_callback_id_token_validation_failure_returns_canonical_error(
    oauth_client_and_store: tuple[TestClient, FastAPI, InMemoryOAuthStateStore],
) -> None:
    client, app, _ = oauth_client_and_store
    app.state.oauth_id_token_validator = _FailingIdTokenValidator()
    start_payload = _start_oauth_flow(client=client)

    response = client.get(
        "/v1/auth/oauth/google/callback",
        headers={"X-Correlation-ID": "oauth-callback-id-token-invalid-corr"},
        params={"state": start_payload["state"], "code": "valid-authz-code"},
    )

    error = _extract_error_detail(response)
    assert response.status_code == 401
    assert error["error_code"] == "oidc_id_token_nonce_invalid"
    assert error["reason"] == "oidc_id_token_nonce_invalid"


def test_oauth_callback_tenant_mismatch_is_rejected_and_audited_as_suspicious(
    oauth_client_and_store: tuple[TestClient, FastAPI, InMemoryOAuthStateStore],
) -> None:
    client, app, _ = oauth_client_and_store
    app.state.oauth_id_token_validator = _TenantMismatchIdTokenValidator()
    start_payload = _start_oauth_flow(client=client)

    response = client.get(
        "/v1/auth/oauth/google/callback",
        headers={"X-Correlation-ID": "oauth-callback-tenant-mismatch-corr"},
        params={"state": start_payload["state"], "code": "valid-authz-code"},
    )

    error = _extract_error_detail(response)
    assert response.status_code == 403
    assert error["error_code"] == "oauth_identity_tenant_mismatch"
    assert error["reason"] == "oauth_identity_tenant_mismatch"

    audit_events = list_auth_audit_events(app_instance=app)
    assert audit_events[-1].event_type == "auth_oauth_identity_link_suspicious"
    assert audit_events[-1].reason_code == "oauth_identity_tenant_mismatch"


def _start_oauth_flow(*, client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/v1/auth/oauth/google/start",
        headers={"X-Correlation-ID": "oauth-start-helper-corr"},
        json={"redirect_uri": "https://kodi.example.com/v1/auth/oauth/google/callback"},
    )
    assert response.status_code == 200
    return _response_json(response)


def _extract_error_detail(response: Any) -> dict[str, object]:
    payload = _response_json(response)
    detail = payload.get("detail")
    assert isinstance(detail, dict)
    assert "error_code" in detail
    assert "message" in detail
    assert "reason" in detail
    assert "password" not in detail
    assert "otp_code" not in detail
    assert "access_token" not in detail
    assert "refresh_token" not in detail
    return detail


def _response_json(response: Any) -> dict[str, Any]:
    payload = response.json()
    assert isinstance(payload, dict)
    return payload
