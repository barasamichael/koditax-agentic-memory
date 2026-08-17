"""Focused tests for deterministic OAuth provider config and trust-policy safety."""

from __future__ import annotations

import json

import pytest

from services.auth.app import config as auth_config
from services.auth.app.oauth_config import OAuthProviderConfigError
from services.auth.app.oauth_config import get_trusted_enabled_oauth_provider
from services.auth.app.oauth_config import load_oauth_provider_registry_from_env
from services.auth.app.oauth_config import get_default_oauth_provider_trust_policy


def test_valid_oauth_provider_config_loads_successfully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_base_oauth_policy_env(monkeypatch)
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
                    "client_secret_ref": "secret://auth/oauth/google",
                    "redirect_uri": "https://kodi.example.com/v1/auth/oauth/google/callback",
                    "scopes": ["openid", "email", "profile"],
                    "enabled": True,
                }
            ]
        ),
    )

    registry = load_oauth_provider_registry_from_env()
    provider = get_trusted_enabled_oauth_provider(provider_id="google", registry=registry)

    assert provider.provider_id == "google"
    assert provider.enabled is True
    assert provider.client_secret_ref == "secret://auth/oauth/google"
    assert set(provider.scopes) == {"openid", "email", "profile"}


def test_missing_required_field_rejected_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_base_oauth_policy_env(monkeypatch)
    monkeypatch.setenv(
        auth_config.AUTH_OAUTH_PROVIDER_REGISTRY_JSON_ENV_VAR,
        json.dumps(
            [
                {
                    "provider_id": "google",
                    "issuer": "https://accounts.google.com",
                    "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
                    "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
                    "client_id": "google-client-id",
                    "client_secret_ref": "secret://auth/oauth/google",
                    "redirect_uri": "https://kodi.example.com/v1/auth/oauth/google/callback",
                    "scopes": ["openid", "email"],
                    "enabled": True,
                }
            ]
        ),
    )

    first = _error_envelope_for_registry_load()
    second = _error_envelope_for_registry_load()
    assert first == second
    assert first["reason"] == "oauth_provider_config_invalid"


def test_non_https_endpoints_are_rejected_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_base_oauth_policy_env(monkeypatch)
    monkeypatch.setenv(
        auth_config.AUTH_OAUTH_PROVIDER_REGISTRY_JSON_ENV_VAR,
        json.dumps(
            [
                {
                    "provider_id": "google",
                    "issuer": "https://accounts.google.com",
                    "authorization_endpoint": "http://accounts.google.com/o/oauth2/v2/auth",
                    "token_endpoint": "https://oauth2.googleapis.com/token",
                    "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
                    "client_id": "google-client-id",
                    "client_secret_ref": "secret://auth/oauth/google",
                    "redirect_uri": "https://kodi.example.com/v1/auth/oauth/google/callback",
                    "scopes": ["openid", "email"],
                    "enabled": True,
                }
            ]
        ),
    )

    error = _error_envelope_for_registry_load()
    assert error["reason"] == "oauth_provider_config_invalid"
    assert error["error_code"] == "oauth_provider_configuration_error"


def test_disallowed_issuer_is_rejected_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_base_oauth_policy_env(monkeypatch)
    monkeypatch.setenv(
        auth_config.AUTH_OAUTH_PROVIDER_REGISTRY_JSON_ENV_VAR,
        json.dumps(
            [
                {
                    "provider_id": "google",
                    "issuer": "https://issuer.not-allowlisted.example.com",
                    "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
                    "token_endpoint": "https://oauth2.googleapis.com/token",
                    "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
                    "client_id": "google-client-id",
                    "client_secret_ref": "secret://auth/oauth/google",
                    "redirect_uri": "https://kodi.example.com/v1/auth/oauth/google/callback",
                    "scopes": ["openid", "email", "profile"],
                    "enabled": True,
                }
            ]
        ),
    )

    registry = load_oauth_provider_registry_from_env()
    first = _error_envelope_for_provider_resolution(provider_id="google", registry=registry)
    second = _error_envelope_for_provider_resolution(provider_id="google", registry=registry)
    assert first == second
    assert first["reason"] == "oauth_provider_issuer_not_allowed"


def test_disallowed_redirect_uri_is_rejected_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_base_oauth_policy_env(monkeypatch)
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
                    "client_secret_ref": "secret://auth/oauth/google",
                    "redirect_uri": "https://malicious.example.com/callback",
                    "scopes": ["openid", "email", "profile"],
                    "enabled": True,
                }
            ]
        ),
    )

    registry = load_oauth_provider_registry_from_env()
    error = _error_envelope_for_provider_resolution(provider_id="google", registry=registry)
    assert error["reason"] == "oauth_provider_redirect_uri_not_allowed"


def test_disabled_provider_cannot_be_implicitly_activated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_base_oauth_policy_env(monkeypatch)
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
                    "client_secret_ref": "secret://auth/oauth/google",
                    "redirect_uri": "https://kodi.example.com/v1/auth/oauth/google/callback",
                    "scopes": ["openid", "email", "profile"],
                    "enabled": False,
                }
            ]
        ),
    )

    registry = load_oauth_provider_registry_from_env()
    error = _error_envelope_for_provider_resolution(provider_id="google", registry=registry)
    assert error["reason"] == "oauth_provider_disabled"


def test_missing_secret_reference_rejected_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_base_oauth_policy_env(monkeypatch)
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
                    "client_secret_ref": "",
                    "redirect_uri": "https://kodi.example.com/v1/auth/oauth/google/callback",
                    "scopes": ["openid", "email", "profile"],
                    "enabled": True,
                }
            ]
        ),
    )

    error = _error_envelope_for_registry_load()
    assert error["reason"] == "oauth_provider_secret_reference_missing"


def test_default_trust_policy_uses_configured_allowlists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_base_oauth_policy_env(monkeypatch)
    trust_policy = get_default_oauth_provider_trust_policy()
    assert trust_policy.allowed_issuers == {"https://accounts.google.com"}
    assert trust_policy.allowed_redirect_uris == {
        "https://kodi.example.com/v1/auth/oauth/google/callback"
    }
    assert trust_policy.required_scopes == {"openid", "email"}


def _error_envelope_for_registry_load() -> dict[str, object]:
    with pytest.raises(OAuthProviderConfigError) as error_info:
        load_oauth_provider_registry_from_env()
    return error_info.value.to_error_envelope()


def _error_envelope_for_provider_resolution(
    *,
    provider_id: str,
    registry: dict[str, object],
) -> dict[str, object]:
    with pytest.raises(OAuthProviderConfigError) as error_info:
        get_trusted_enabled_oauth_provider(
            provider_id=provider_id,
            registry=registry,
        )
    return error_info.value.to_error_envelope()


def _set_base_oauth_policy_env(monkeypatch: pytest.MonkeyPatch) -> None:
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
