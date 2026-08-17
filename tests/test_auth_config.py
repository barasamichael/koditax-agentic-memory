"""General auth configuration tests for secret baseline guard behavior."""

from __future__ import annotations

import pytest

from services.auth.app import config as auth_config


def test_non_production_mode_allows_missing_required_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(auth_config.AUTH_SECRET_RUNTIME_MODE_ENV_VAR, "development")
    monkeypatch.setenv(auth_config.AUTH_OTP_SMS_PROVIDER_MODE_ENV_VAR, "stub")
    monkeypatch.setenv(auth_config.AUTH_OTP_EMAIL_PROVIDER_MODE_ENV_VAR, "stub")
    monkeypatch.delenv(auth_config.AUTH_SESSION_SIGNING_KEY_ACTIVE_ENV_VAR, raising=False)
    monkeypatch.delenv(auth_config.AUTH_REFRESH_TOKEN_SECRET_ACTIVE_ENV_VAR, raising=False)
    monkeypatch.delenv(auth_config.AUTH_ENCRYPTION_KEY_ACTIVE_ENV_VAR, raising=False)
    monkeypatch.delenv(auth_config.AUTH_IDEMPOTENCY_SIGNING_SECRET_ENV_VAR, raising=False)
    monkeypatch.delenv(auth_config.AUTH_OTP_SMS_PROVIDER_SECRET_ENV_VAR, raising=False)
    monkeypatch.delenv(auth_config.AUTH_OTP_EMAIL_PROVIDER_SECRET_ENV_VAR, raising=False)
    monkeypatch.delenv(auth_config.AT_API_KEY_ENV_VAR, raising=False)
    monkeypatch.delenv(auth_config.AUTH_ZOHO_CLIENT_SECRET_ENV_VAR, raising=False)
    monkeypatch.delenv(auth_config.AUTH_ZOHO_REFRESH_TOKEN_ENV_VAR, raising=False)

    loaded = auth_config.load_auth_secret_config_baseline()
    assert loaded.runtime_mode == "development"
    assert loaded.session_signing_key_active is None
    assert loaded.refresh_token_secret_active is None
    assert loaded.encryption_key_active is None
    assert loaded.idempotency_signing_secret is None
    assert loaded.otp_sms_provider_secret is None
    assert loaded.otp_email_provider_secret is None


def test_invalid_runtime_mode_defaults_to_development(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(auth_config.AUTH_SECRET_RUNTIME_MODE_ENV_VAR, "invalid-mode")
    assert auth_config.get_auth_secret_runtime_mode() == "development"


def test_hackathon_runtime_mode_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(auth_config.AUTH_SECRET_RUNTIME_MODE_ENV_VAR, "hackathon")
    assert auth_config.get_auth_secret_runtime_mode() == "hackathon"


def test_optional_next_signing_key_invalid_format_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(auth_config.AUTH_SECRET_RUNTIME_MODE_ENV_VAR, "production")
    monkeypatch.setenv(auth_config.AUTH_SESSION_SIGNING_KEY_ACTIVE_ENV_VAR, "a" * 40)
    monkeypatch.setenv(auth_config.AUTH_REFRESH_TOKEN_SECRET_ACTIVE_ENV_VAR, "b" * 40)
    monkeypatch.setenv(auth_config.AUTH_ENCRYPTION_KEY_ACTIVE_ENV_VAR, "c" * 40)
    monkeypatch.setenv(auth_config.AUTH_IDEMPOTENCY_SIGNING_SECRET_ENV_VAR, "d" * 40)
    monkeypatch.setenv(auth_config.AUTH_OTP_SMS_PROVIDER_SECRET_ENV_VAR, "e" * 40)
    monkeypatch.setenv(auth_config.AUTH_OTP_EMAIL_PROVIDER_SECRET_ENV_VAR, "f" * 40)
    monkeypatch.setenv(auth_config.AUTH_SESSION_SIGNING_KEY_NEXT_ENV_VAR, "short")

    with pytest.raises(auth_config.AuthSecretConfigError) as error_info:
        auth_config.load_auth_secret_config_baseline()
    assert error_info.value.error_code == "auth_secret_invalid_format"
    assert error_info.value.reason == "auth_secret_invalid_format"
    assert error_info.value.details["env_var"] == auth_config.AUTH_SESSION_SIGNING_KEY_NEXT_ENV_VAR
