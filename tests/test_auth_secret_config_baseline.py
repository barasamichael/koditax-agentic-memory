"""Focused fail-closed tests for auth secret baseline configuration."""

from __future__ import annotations

from typing import cast

import pytest

from services.auth.app import config as auth_config


def test_missing_required_secret_fails_deterministically(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_valid_production_secret_env(monkeypatch)
    monkeypatch.delenv(auth_config.AUTH_SESSION_SIGNING_KEY_ACTIVE_ENV_VAR, raising=False)

    first = _error_payload(monkeypatch)
    second = _error_payload(monkeypatch)
    assert first == second
    assert first["error_code"] == "auth_secret_missing"
    assert first["reason"] == "auth_secret_missing"
    assert first["details"] == {"env_var": auth_config.AUTH_SESSION_SIGNING_KEY_ACTIVE_ENV_VAR}


def test_malformed_required_secret_fails_deterministically(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_valid_production_secret_env(monkeypatch)
    monkeypatch.setenv(auth_config.AUTH_REFRESH_TOKEN_SECRET_ACTIVE_ENV_VAR, "too-short")

    error = _error_payload(monkeypatch)
    assert error["error_code"] == "auth_secret_invalid_format"
    assert error["reason"] == "auth_secret_invalid_format"
    details = cast(dict[str, object], error["details"])
    assert details["env_var"] == auth_config.AUTH_REFRESH_TOKEN_SECRET_ACTIVE_ENV_VAR


def test_rotation_window_invalid_fails_deterministically(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_valid_production_secret_env(monkeypatch)
    monkeypatch.setenv(
        auth_config.AUTH_SECRET_ROTATION_WINDOW_START_UTC_ENV_VAR,
        "2026-04-01T12:00:00Z",
    )
    monkeypatch.setenv(
        auth_config.AUTH_SECRET_ROTATION_WINDOW_END_UTC_ENV_VAR,
        "2026-04-01T11:59:59Z",
    )

    first = _error_payload(monkeypatch)
    second = _error_payload(monkeypatch)
    assert first == second
    assert first["error_code"] == "auth_secret_rotation_window_invalid"
    assert first["reason"] == "auth_secret_rotation_window_invalid"
    details = cast(dict[str, object], first["details"])
    assert details["requirement"] == "end_must_be_after_start"


def test_valid_production_secret_config_parses_successfully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_production_secret_env(monkeypatch)
    monkeypatch.setenv(auth_config.AUTH_SESSION_SIGNING_KEY_NEXT_ENV_VAR, "z" * 40)
    monkeypatch.setenv(
        auth_config.AUTH_SECRET_ROTATION_WINDOW_START_UTC_ENV_VAR,
        "2026-04-01T10:00:00Z",
    )
    monkeypatch.setenv(
        auth_config.AUTH_SECRET_ROTATION_WINDOW_END_UTC_ENV_VAR,
        "2026-04-01T11:00:00Z",
    )

    loaded = auth_config.load_auth_secret_config_baseline()
    assert loaded.runtime_mode == "production"
    assert loaded.session_signing_key_active is not None
    assert loaded.refresh_token_secret_active is not None
    assert loaded.encryption_key_active is not None
    assert loaded.idempotency_signing_secret is not None
    assert loaded.otp_sms_provider_secret is not None
    assert loaded.otp_email_provider_secret is not None
    assert loaded.rotation_window_start_utc == "2026-04-01T10:00:00Z"
    assert loaded.rotation_window_end_utc == "2026-04-01T11:00:00Z"


def _error_payload(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    del monkeypatch
    with pytest.raises(auth_config.AuthSecretConfigError) as error_info:
        auth_config.load_auth_secret_config_baseline()
    error = error_info.value
    return {
        "error_code": error.error_code,
        "reason": error.reason,
        "details": error.details,
    }


def _set_valid_production_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(auth_config.AUTH_SECRET_RUNTIME_MODE_ENV_VAR, "production")
    monkeypatch.setenv(auth_config.AUTH_SESSION_SIGNING_KEY_ACTIVE_ENV_VAR, "a" * 40)
    monkeypatch.setenv(auth_config.AUTH_REFRESH_TOKEN_SECRET_ACTIVE_ENV_VAR, "b" * 40)
    monkeypatch.setenv(auth_config.AUTH_ENCRYPTION_KEY_ACTIVE_ENV_VAR, "c" * 40)
    monkeypatch.setenv(auth_config.AUTH_IDEMPOTENCY_SIGNING_SECRET_ENV_VAR, "d" * 40)
    monkeypatch.setenv(auth_config.AUTH_OTP_SMS_PROVIDER_SECRET_ENV_VAR, "e" * 40)
    monkeypatch.setenv(auth_config.AUTH_OTP_EMAIL_PROVIDER_SECRET_ENV_VAR, "f" * 40)
    monkeypatch.delenv(auth_config.AUTH_SECRET_ROTATION_WINDOW_START_UTC_ENV_VAR, raising=False)
    monkeypatch.delenv(auth_config.AUTH_SECRET_ROTATION_WINDOW_END_UTC_ENV_VAR, raising=False)
