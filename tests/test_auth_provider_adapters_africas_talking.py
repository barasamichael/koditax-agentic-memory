"""Deterministic Africa's Talking SMS OTP adapter coverage."""

from __future__ import annotations

from typing import cast

from services.auth.app.otp_delivery_adapters import OtpProviderPolicy
from services.auth.app.otp_delivery_adapters import ProviderTransportRequest
from services.auth.app.otp_delivery_adapters import ProviderTransportResponse
from services.auth.app.otp_delivery_adapters import AfricasTalkingSmsOtpDeliveryAdapter
from services.auth.app.otp_delivery_adapters import get_default_sms_otp_delivery_adapter


def test_africas_talking_adapter_success_path_is_deterministic() -> None:
    observed_requests: list[ProviderTransportRequest] = []

    def transport(request: ProviderTransportRequest) -> ProviderTransportResponse:
        observed_requests.append(request)
        return ProviderTransportResponse(
            status_code=201,
            body={"message_id": "at-msg-001"},
        )

    adapter = AfricasTalkingSmsOtpDeliveryAdapter(
        api_base_url="https://api.africastalking.com/version1/messaging",
        api_token="sms-token",
        username="sandbox",
        sender_id="Kodi Solutions",
        policy=_policy(),
        transport=transport,
    )
    outcome = adapter.send_otp_challenge(
        purpose="registration_verify",
        phone_number_normalized="+254700123456",
    )
    assert outcome.status == "delivered"
    assert outcome.reason_code == "sms_delivery_provider_delivered"
    assert outcome.provider_ref == "sms:provider:at-msg-001"
    assert len(observed_requests) == 1
    assert observed_requests[0].endpoint == "https://api.africastalking.com/version1/messaging"
    assert observed_requests[0].body_encoding == "form"
    assert observed_requests[0].payload["username"] == "sandbox"
    assert observed_requests[0].payload["to"] == "+254700123456"
    assert "message" in observed_requests[0].payload
    assert observed_requests[0].payload["from"] == "Kodi Solutions"


def test_africas_talking_adapter_timeout_exhaustion_is_deterministic() -> None:
    def transport(_request: ProviderTransportRequest) -> ProviderTransportResponse:
        return ProviderTransportResponse(status_code=504, body={})

    adapter = AfricasTalkingSmsOtpDeliveryAdapter(
        api_base_url="https://api.africastalking.com/version1/messaging",
        api_token="sms-token",
        username="sandbox",
        sender_id=None,
        policy=_policy(max_retries=1),
        transport=transport,
    )

    first = adapter.send_otp_challenge(
        purpose="login_step_up",
        phone_number_normalized="+254700123456",
    )
    second = adapter.send_otp_challenge(
        purpose="login_step_up",
        phone_number_normalized="+254700123456",
    )
    assert first.status == "failed_retryable"
    assert first.reason_code == "sms_delivery_provider_unavailable"
    assert cast(str, first.provider_ref).startswith("sms:http:504")
    assert first == second


def test_africas_talking_adapter_permanent_reject_is_non_retryable() -> None:
    adapter = AfricasTalkingSmsOtpDeliveryAdapter(
        api_base_url="https://api.africastalking.com/version1/messaging",
        api_token="sms-token",
        username="sandbox",
        sender_id=None,
        policy=_policy(),
        transport=lambda _request: ProviderTransportResponse(status_code=400, body={}),
    )
    outcome = adapter.send_otp_challenge(
        purpose="recovery",
        phone_number_normalized="+254700123456",
    )
    assert outcome.status == "failed_non_retryable"
    assert outcome.reason_code == "sms_delivery_provider_rejected"


def test_default_africas_talking_selection_fails_closed_when_config_missing(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_OTP_RUNTIME_MODE", "production")
    monkeypatch.setenv("AUTH_OTP_SMS_PROVIDER_MODE", "africas_talking")
    monkeypatch.delenv("AT_API_KEY", raising=False)
    monkeypatch.delenv("AUTH_OTP_SMS_PROVIDER_SECRET", raising=False)
    monkeypatch.delenv("AT_USERNAME", raising=False)
    monkeypatch.delenv("AUTH_AFRICAS_TALKING_USERNAME", raising=False)
    adapter = get_default_sms_otp_delivery_adapter()
    outcome = adapter.send_otp_challenge(
        purpose="registration_verify",
        phone_number_normalized="+254700123456",
    )
    assert outcome.status == "failed_non_retryable"
    assert outcome.reason_code == "otp_delivery_provider_misconfigured"


def _policy(*, max_retries: int = 0) -> OtpProviderPolicy:
    return OtpProviderPolicy(
        timeout_seconds=5,
        max_retries=max_retries,
        retry_backoff_seconds=1,
        retry_backoff_max_seconds=2,
    )
