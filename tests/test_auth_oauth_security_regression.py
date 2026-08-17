"""Deterministic OAuth/OIDC security regression coverage for Phase 8.5.7."""

from __future__ import annotations

import json
from typing import Any
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from dataclasses import dataclass
from collections.abc import Mapping
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from authlib.jose import jwt
from authlib.jose import JsonWebKey
from fastapi.testclient import TestClient
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from services.auth.app import config as auth_config
from services.auth.app.main import create_app
from services.auth.app.oauth_flow import InMemoryOAuthStateStore
from services.auth.app.oauth_flow import OAuthAuthorizationState
from shared.determinism.input_hash import canonical_json_dumps
from services.auth.app.registration import InMemoryRegistrationStore
from services.auth.app.oauth_linking import InMemoryOAuthIdentityLinkingStore
from services.auth.app.oauth_resilience import OAuthProviderCircuitState
from services.auth.app.oauth_resilience import InMemoryOAuthProviderCircuitStore
from services.auth.app.oauth_validation import AuthlibOidcIdTokenValidator

_OAUTH_REDIRECT_URI = "https://kodi.example.com/v1/auth/oauth/google/callback"
_OAUTH_ISSUER = "https://accounts.google.com"
_OAUTH_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_OAUTH_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_OAUTH_JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"
_OAUTH_PROVIDER_ID = "google"
_OAUTH_CLIENT_ID = "google-client-id"


class _StaticJwksResolver:
    """Provide deterministic JWKS payload for callback validation flow."""

    def __init__(self, *, jwks_payload: Mapping[str, object]) -> None:
        self._jwks_payload = dict(jwks_payload)

    def resolve_jwks(self, *, provider: object) -> Mapping[str, object]:
        assert provider is not None
        return dict(self._jwks_payload)


class _StaticIdTokenExchangeClient:
    """Provide deterministic ID-token response for callback flow tests."""

    def __init__(self, *, id_token: str) -> None:
        self._id_token = id_token
        self.call_count = 0

    def exchange_code_for_token(
        self,
        *,
        provider: object,
        authorization_code: str,
        code_verifier: str,
    ) -> dict[str, object]:
        self.call_count += 1
        assert provider is not None
        assert authorization_code.strip()
        assert code_verifier.strip()
        return {"id_token": self._id_token}


class _CountingIdTokenValidator:
    """Count validator calls while delegating to real deterministic validator."""

    def __init__(self, *, delegate: AuthlibOidcIdTokenValidator) -> None:
        self._delegate = delegate
        self.call_count = 0

    def validate_id_token(
        self,
        *,
        provider: object,
        id_token: str,
        expected_nonce: str,
        now_provider: object | None = None,
    ) -> Mapping[str, object]:
        self.call_count += 1
        assert provider is not None
        return self._delegate.validate_id_token(
            provider=provider,
            id_token=id_token,
            expected_nonce=expected_nonce,
            now_provider=now_provider,
        )


@dataclass(frozen=True)
class _SecurityRuntimeContext:
    client: TestClient
    app: FastAPI
    oauth_state_store: InMemoryOAuthStateStore
    trusted_signing_jwk: object
    untrusted_signing_jwk: object


@pytest.fixture()
def oauth_security_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[_SecurityRuntimeContext]:
    """Create isolated OAuth runtime with deterministic provider registry + validator."""

    monkeypatch.setenv("AUTH_SECRET_RUNTIME_MODE", "development")
    _configure_oauth_env(monkeypatch=monkeypatch, client_id=_OAUTH_CLIENT_ID)

    trusted_signing_jwk, trusted_jwks_payload = _generate_signing_jwk_and_jwks()
    untrusted_signing_jwk, _unused_untrusted_jwks = _generate_signing_jwk_and_jwks()

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
    app.state.registration_store = registration_store
    app.state.oauth_identity_linking_store = InMemoryOAuthIdentityLinkingStore()
    app.state.oauth_id_token_validator = AuthlibOidcIdTokenValidator(
        jwks_resolver=_StaticJwksResolver(jwks_payload=trusted_jwks_payload),
        clock_skew_seconds=0,
    )

    with TestClient(app) as client:
        yield _SecurityRuntimeContext(
            client=client,
            app=app,
            oauth_state_store=oauth_state_store,
            trusted_signing_jwk=trusted_signing_jwk,
            untrusted_signing_jwk=untrusted_signing_jwk,
        )


def test_oauth_valid_path_remains_green_under_security_regression_suite(
    oauth_security_runtime: _SecurityRuntimeContext,
) -> None:
    start_payload = _start_oauth_flow(client=oauth_security_runtime.client)
    oauth_security_runtime.app.state.oauth_token_exchange_client = _StaticIdTokenExchangeClient(
        id_token=_encode_id_token(
            signing_jwk=oauth_security_runtime.trusted_signing_jwk,
            issuer=_OAUTH_ISSUER,
            audience=_OAUTH_CLIENT_ID,
            nonce=start_payload["nonce"],
        )
    )

    response = oauth_security_runtime.client.get(
        "/v1/auth/oauth/google/callback",
        headers={"X-Correlation-ID": "oauth-security-valid-corr"},
        params={"state": start_payload["state"], "code": "valid-authz-code"},
    )

    payload = _response_json(response)
    assert response.status_code == 200
    assert payload["status"] == "protocol_validated"
    assert payload["provider"] == _OAUTH_PROVIDER_ID
    assert payload["link_status"] in {"linked_existing", "linked_new"}


def test_state_mismatch_is_blocked_with_deterministic_canonical_error_envelope(
    oauth_security_runtime: _SecurityRuntimeContext,
) -> None:
    now = datetime.now(UTC)
    mismatched_state = "oauth-state-mismatch-001"
    oauth_security_runtime.oauth_state_store.put_state(
        state=OAuthAuthorizationState(
            provider_id="different_provider",
            state=mismatched_state,
            nonce="nonce-not-used",
            redirect_uri=_OAUTH_REDIRECT_URI,
            code_verifier="code-verifier-not-used",
            created_at=now,
            expires_at=now + timedelta(minutes=5),
            consumed_at=None,
        )
    )

    first = oauth_security_runtime.client.get(
        "/v1/auth/oauth/google/callback",
        headers={"X-Correlation-ID": "oauth-security-state-mismatch-corr"},
        params={"state": mismatched_state, "code": "valid-authz-code"},
    )
    second = oauth_security_runtime.client.get(
        "/v1/auth/oauth/google/callback",
        headers={"X-Correlation-ID": "oauth-security-state-mismatch-corr"},
        params={"state": mismatched_state, "code": "valid-authz-code"},
    )

    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)
    assert first.status_code == 400
    assert second.status_code == 400
    assert first_error["reason"] == "oauth_state_invalid"
    assert canonical_json_dumps(first_error) == canonical_json_dumps(second_error)


def test_state_replay_is_rejected_as_single_use_callback_boundary(
    oauth_security_runtime: _SecurityRuntimeContext,
) -> None:
    start_payload = _start_oauth_flow(client=oauth_security_runtime.client)
    oauth_security_runtime.app.state.oauth_token_exchange_client = _StaticIdTokenExchangeClient(
        id_token=_encode_id_token(
            signing_jwk=oauth_security_runtime.trusted_signing_jwk,
            issuer=_OAUTH_ISSUER,
            audience=_OAUTH_CLIENT_ID,
            nonce=start_payload["nonce"],
        )
    )
    callback_params = {"state": start_payload["state"], "code": "valid-authz-code"}

    first = oauth_security_runtime.client.get(
        "/v1/auth/oauth/google/callback",
        headers={"X-Correlation-ID": "oauth-security-state-replay-first-corr"},
        params=callback_params,
    )
    second = oauth_security_runtime.client.get(
        "/v1/auth/oauth/google/callback",
        headers={"X-Correlation-ID": "oauth-security-state-replay-second-corr"},
        params=callback_params,
    )

    assert first.status_code == 200
    second_error = _extract_error_detail(second)
    assert second.status_code == 409
    assert second_error["reason"] == "oauth_callback_replay_detected"


def test_nonce_mismatch_replay_inputs_are_rejected_deterministically(
    oauth_security_runtime: _SecurityRuntimeContext,
) -> None:
    first_start = _start_oauth_flow(client=oauth_security_runtime.client)
    mismatched_nonce_token = _encode_id_token(
        signing_jwk=oauth_security_runtime.trusted_signing_jwk,
        issuer=_OAUTH_ISSUER,
        audience=_OAUTH_CLIENT_ID,
        nonce=first_start["nonce"],
    )

    second_start = _start_oauth_flow(client=oauth_security_runtime.client)
    oauth_security_runtime.app.state.oauth_token_exchange_client = _StaticIdTokenExchangeClient(
        id_token=mismatched_nonce_token
    )
    first_attempt = oauth_security_runtime.client.get(
        "/v1/auth/oauth/google/callback",
        headers={"X-Correlation-ID": "oauth-security-nonce-mismatch-corr"},
        params={"state": second_start["state"], "code": "valid-authz-code"},
    )

    third_start = _start_oauth_flow(client=oauth_security_runtime.client)
    oauth_security_runtime.app.state.oauth_token_exchange_client = _StaticIdTokenExchangeClient(
        id_token=mismatched_nonce_token
    )
    second_attempt = oauth_security_runtime.client.get(
        "/v1/auth/oauth/google/callback",
        headers={"X-Correlation-ID": "oauth-security-nonce-mismatch-corr"},
        params={"state": third_start["state"], "code": "valid-authz-code"},
    )

    first_error = _extract_error_detail(first_attempt)
    second_error = _extract_error_detail(second_attempt)
    assert first_attempt.status_code == 401
    assert second_attempt.status_code == 401
    assert first_error["reason"] == "oidc_id_token_nonce_invalid"
    assert canonical_json_dumps(first_error) == canonical_json_dumps(second_error)


@pytest.mark.parametrize(
    ("token_kind", "expected_reason"),
    (
        ("invalid_signature", "oidc_id_token_signature_invalid"),
        ("issuer_mismatch", "oidc_id_token_issuer_mismatch"),
        ("audience_mismatch", "oidc_id_token_audience_mismatch"),
    ),
)
def test_signature_issuer_and_audience_failures_remain_blocked_in_callback_flow(
    oauth_security_runtime: _SecurityRuntimeContext,
    token_kind: str,
    expected_reason: str,
) -> None:
    start_payload = _start_oauth_flow(client=oauth_security_runtime.client)
    if token_kind == "invalid_signature":
        token = _encode_id_token(
            signing_jwk=oauth_security_runtime.untrusted_signing_jwk,
            issuer=_OAUTH_ISSUER,
            audience=_OAUTH_CLIENT_ID,
            nonce=start_payload["nonce"],
        )
    elif token_kind == "issuer_mismatch":
        token = _encode_id_token(
            signing_jwk=oauth_security_runtime.trusted_signing_jwk,
            issuer="https://issuer-not-allowed.example.com",
            audience=_OAUTH_CLIENT_ID,
            nonce=start_payload["nonce"],
        )
    else:
        token = _encode_id_token(
            signing_jwk=oauth_security_runtime.trusted_signing_jwk,
            issuer=_OAUTH_ISSUER,
            audience="some-other-client",
            nonce=start_payload["nonce"],
        )
    oauth_security_runtime.app.state.oauth_token_exchange_client = _StaticIdTokenExchangeClient(
        id_token=token
    )

    response = oauth_security_runtime.client.get(
        "/v1/auth/oauth/google/callback",
        headers={"X-Correlation-ID": f"oauth-security-token-{token_kind}-corr"},
        params={"state": start_payload["state"], "code": "valid-authz-code"},
    )

    error = _extract_error_detail(response)
    assert response.status_code == 401
    assert error["reason"] == expected_reason


def test_claim_drift_after_provider_config_change_is_rejected_deterministically(
    oauth_security_runtime: _SecurityRuntimeContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_payload = _start_oauth_flow(client=oauth_security_runtime.client)
    oauth_security_runtime.app.state.oauth_token_exchange_client = _StaticIdTokenExchangeClient(
        id_token=_encode_id_token(
            signing_jwk=oauth_security_runtime.trusted_signing_jwk,
            issuer=_OAUTH_ISSUER,
            audience=_OAUTH_CLIENT_ID,
            nonce=start_payload["nonce"],
        )
    )
    _configure_oauth_env(monkeypatch=monkeypatch, client_id="google-client-id-v2")

    response = oauth_security_runtime.client.get(
        "/v1/auth/oauth/google/callback",
        headers={"X-Correlation-ID": "oauth-security-claim-drift-corr"},
        params={"state": start_payload["state"], "code": "valid-authz-code"},
    )

    error = _extract_error_detail(response)
    assert response.status_code == 401
    assert error["reason"] == "oidc_id_token_audience_mismatch"


def test_degraded_mode_blocks_provider_calls_without_security_bypass(
    oauth_security_runtime: _SecurityRuntimeContext,
) -> None:
    start_payload = _start_oauth_flow(client=oauth_security_runtime.client)
    exchange_client = _StaticIdTokenExchangeClient(
        id_token=_encode_id_token(
            signing_jwk=oauth_security_runtime.trusted_signing_jwk,
            issuer=_OAUTH_ISSUER,
            audience=_OAUTH_CLIENT_ID,
            nonce=start_payload["nonce"],
        )
    )
    oauth_security_runtime.app.state.oauth_token_exchange_client = exchange_client

    counting_validator = _CountingIdTokenValidator(
        delegate=oauth_security_runtime.app.state.oauth_id_token_validator
    )
    oauth_security_runtime.app.state.oauth_id_token_validator = counting_validator

    now = datetime.now(UTC)
    circuit_store = InMemoryOAuthProviderCircuitStore()
    circuit_store.put_state(
        state=OAuthProviderCircuitState(
            provider_id=_OAUTH_PROVIDER_ID,
            status="open",
            consecutive_failures=3,
            opened_at=now,
            open_until=now + timedelta(minutes=5),
            recovery_probe_started_at=None,
            last_failure_reason="oauth_provider_unavailable",
            last_failure_at=now,
        )
    )
    oauth_security_runtime.app.state.oauth_provider_circuit_store = circuit_store

    response = oauth_security_runtime.client.get(
        "/v1/auth/oauth/google/callback",
        headers={"X-Correlation-ID": "oauth-security-circuit-open-corr"},
        params={"state": start_payload["state"], "code": "valid-authz-code"},
    )

    error = _extract_error_detail(response)
    assert response.status_code == 503
    assert error["reason"] == "oauth_provider_circuit_open"
    assert exchange_client.call_count == 0
    assert counting_validator.call_count == 0


def _configure_oauth_env(*, monkeypatch: pytest.MonkeyPatch, client_id: str) -> None:
    monkeypatch.setenv(
        auth_config.AUTH_OAUTH_ALLOWED_ISSUERS_ENV_VAR,
        _OAUTH_ISSUER,
    )
    monkeypatch.setenv(
        auth_config.AUTH_OAUTH_ALLOWED_REDIRECT_URIS_ENV_VAR,
        _OAUTH_REDIRECT_URI,
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
        _provider_registry_json(client_id=client_id),
    )


def _provider_registry_json(*, client_id: str) -> str:
    return json.dumps(
        [
            {
                "provider_id": _OAUTH_PROVIDER_ID,
                "issuer": _OAUTH_ISSUER,
                "authorization_endpoint": _OAUTH_AUTHORIZATION_ENDPOINT,
                "token_endpoint": _OAUTH_TOKEN_ENDPOINT,
                "jwks_uri": _OAUTH_JWKS_URI,
                "client_id": client_id,
                "client_secret_ref": "env:AUTH_OAUTH_SECRET_GOOGLE",
                "redirect_uri": _OAUTH_REDIRECT_URI,
                "scopes": ["openid", "email", "profile"],
                "enabled": True,
            }
        ]
    )


def _start_oauth_flow(*, client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/v1/auth/oauth/google/start",
        headers={"X-Correlation-ID": "oauth-security-start-helper-corr"},
        json={"redirect_uri": _OAUTH_REDIRECT_URI},
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
    assert "access_token" not in detail
    assert "refresh_token" not in detail
    assert "id_token" not in detail
    assert "code_verifier" not in detail
    return detail


def _response_json(response: Any) -> dict[str, Any]:
    payload = response.json()
    assert isinstance(payload, dict)
    return payload


def _generate_signing_jwk_and_jwks() -> tuple[object, dict[str, object]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    signing_jwk = JsonWebKey.import_key(private_pem, {"kty": "RSA", "kid": "k1"})
    verification_jwk = JsonWebKey.import_key(public_pem, {"kty": "RSA", "kid": "k1"})
    jwks_payload = {"keys": [verification_jwk.as_dict(is_private=False)]}
    return signing_jwk, jwks_payload


def _encode_id_token(
    *,
    signing_jwk: object,
    issuer: str,
    audience: str,
    nonce: str,
) -> str:
    now = datetime.now(UTC)
    issued_at = int(now.timestamp())
    payload = {
        "iss": issuer,
        "aud": audience,
        "sub": "oidc-subject-001",
        "nonce": nonce,
        "email": "linked.user@example.com",
        "iat": issued_at,
        "exp": issued_at + 300,
    }
    encoded = jwt.encode({"alg": "RS256", "kid": "k1"}, payload, signing_jwk)
    return encoded.decode("utf-8")
