"""Focused tests for OTP delivery adapter abstraction and config-driven selection."""

from __future__ import annotations

from services.auth.app.otp_delivery_adapters import OtpDeliveryOutcome
from services.auth.app.otp_delivery_adapters import normalize_sms_delivery_outcome
from services.auth.app.otp_delivery_adapters import normalize_email_delivery_outcome
from services.auth.app.otp_delivery_adapters import get_default_sms_otp_delivery_adapter
from services.auth.app.otp_delivery_adapters import get_default_email_otp_delivery_adapter


def test_sms_adapter_default_stub_delivers_in_non_production_mode(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTH_OTP_RUNTIME_MODE", "development")
    monkeypatch.setenv("AUTH_OTP_SMS_PROVIDER_MODE", "stub")
    adapter = get_default_sms_otp_delivery_adapter()
    outcome = adapter.send_otp_challenge(
        purpose="registration_verify",
        phone_number_normalized="+254700300001",
    )
    assert outcome.status == "delivered"
    assert outcome.reason_code == "sms_delivery_provider_delivered"


def test_email_adapter_default_stub_delivers_in_non_production_mode(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTH_OTP_RUNTIME_MODE", "test")
    monkeypatch.setenv("AUTH_OTP_EMAIL_PROVIDER_MODE", "stub")
    adapter = get_default_email_otp_delivery_adapter()
    outcome = adapter.send_otp_challenge(
        purpose="registration_verify",
        email_normalized="adapter.default@example.com",
    )
    assert outcome.status == "delivered"
    assert outcome.reason_code == "email_delivery_provider_delivered"


def test_sms_delivery_outcome_normalization_maps_retryable_timeout_reason() -> None:
    normalized = normalize_sms_delivery_outcome(
        outcome=OtpDeliveryOutcome(
            status="failed_retryable",
            reason_code="provider_timeout",
            provider_ref="sms:provider:timeout",
        )
    )
    assert normalized.status == "failed_retryable"
    assert normalized.reason_code == "sms_delivery_provider_timeout"
    assert normalized.provider_ref == "sms:provider:timeout"


def test_sms_delivery_outcome_normalization_maps_non_retryable_rejected_reason() -> None:
    normalized = normalize_sms_delivery_outcome(
        outcome=OtpDeliveryOutcome(
            status="failed_non_retryable",
            reason_code="provider_rejected",
            provider_ref="sms:provider:rejected",
        )
    )
    assert normalized.status == "failed_non_retryable"
    assert normalized.reason_code == "sms_delivery_provider_rejected"
    assert normalized.provider_ref == "sms:provider:rejected"


def test_email_delivery_outcome_normalization_maps_retryable_unavailable_reason() -> None:
    normalized = normalize_email_delivery_outcome(
        outcome=OtpDeliveryOutcome(
            status="failed_retryable",
            reason_code="provider_unavailable",
            provider_ref="email:provider:unavailable",
        )
    )
    assert normalized.status == "failed_retryable"
    assert normalized.reason_code == "email_delivery_provider_unavailable"
    assert normalized.provider_ref == "email:provider:unavailable"


def test_provider_mode_misconfiguration_fails_closed_deterministically(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTH_OTP_RUNTIME_MODE", "production")
    monkeypatch.setenv("AUTH_OTP_SMS_PROVIDER_MODE", "stub")
    monkeypatch.setenv("AUTH_OTP_EMAIL_PROVIDER_MODE", "unknown-provider-mode")
    sms_adapter = get_default_sms_otp_delivery_adapter()
    email_adapter = get_default_email_otp_delivery_adapter()
    sms_outcome = sms_adapter.send_otp_challenge(
        purpose="login_step_up",
        phone_number_normalized="+254700300002",
    )
    email_outcome = email_adapter.send_otp_challenge(
        purpose="recovery",
        email_normalized="misconfigured@example.com",
    )
    assert sms_outcome.status == "failed_non_retryable"
    assert sms_outcome.reason_code == "otp_delivery_provider_misconfigured"
    assert email_outcome.status == "failed_non_retryable"
    assert email_outcome.reason_code == "otp_delivery_provider_misconfigured"
