"""Deterministic direct Zoho Mail adapter coverage."""

from __future__ import annotations

from typing import cast

import pytest

from services.auth.app.otp_delivery_adapters import EmailOtpMessage
from services.auth.app.otp_delivery_adapters import OtpProviderPolicy
from services.auth.app.otp_delivery_adapters import ProviderTransportRequest
from services.auth.app.otp_delivery_adapters import ProviderTransportResponse
from services.auth.app.otp_delivery_adapters import ZohoMailEmailOtpDeliveryAdapter
from services.auth.app.otp_delivery_adapters import get_default_email_otp_delivery_adapter


def test_zoho_adapter_success_path_is_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AUTH_OTP_EMAIL_RECIPIENT_OVERRIDE", raising=False)
    observed_requests: list[ProviderTransportRequest] = []

    def transport(request: ProviderTransportRequest) -> ProviderTransportResponse:
        observed_requests.append(request)
        if request.endpoint.endswith("/oauth/v2/token"):
            return ProviderTransportResponse(
                status_code=200,
                body={"access_token": "zoho-access-token", "expires_in": 3600},
            )
        return ProviderTransportResponse(
            status_code=202,
            body={"request_id": "zoho-req-001"},
        )

    adapter = ZohoMailEmailOtpDeliveryAdapter(
        accounts_base_url="https://accounts.zoho.com",
        mail_base_url="https://mail.zoho.com",
        client_id="zoho-client-id",
        client_secret="zoho-client-secret",
        refresh_token="zoho-refresh-token",
        account_id="zoho-account-id",
        from_address="noreply@example.com",
        policy=_policy(),
        transport=transport,
    )
    outcome = adapter.send_otp_challenge(
        message=_message(email_normalized="zoho.success@example.com"),
    )
    assert outcome.status == "delivered"
    assert outcome.reason_code == "email_delivery_provider_delivered"
    assert outcome.provider_ref == "email:provider:zoho-req-001"
    assert len(observed_requests) == 2
    assert observed_requests[0].endpoint == "https://accounts.zoho.com/oauth/v2/token"
    assert observed_requests[0].body_encoding == "form"
    assert observed_requests[0].payload["refresh_token"] == "zoho-refresh-token"
    assert (
        observed_requests[1].endpoint
        == "https://mail.zoho.com/api/accounts/zoho-account-id/messages"
    )
    assert observed_requests[1].payload["fromAddress"] == "noreply@example.com"
    assert observed_requests[1].payload["toAddress"] == "zoho.success@example.com"
    assert observed_requests[1].payload["subject"] == "Test subject"


def test_zoho_adapter_honors_recipient_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH_OTP_EMAIL_RECIPIENT_OVERRIDE", "mkuu@jisortublow.co.ke")
    observed_requests: list[ProviderTransportRequest] = []

    def transport(request: ProviderTransportRequest) -> ProviderTransportResponse:
        observed_requests.append(request)
        if request.endpoint.endswith("/oauth/v2/token"):
            return ProviderTransportResponse(
                status_code=200,
                body={"access_token": "zoho-access-token", "expires_in": 3600},
            )
        return ProviderTransportResponse(
            status_code=202,
            body={"request_id": "zoho-req-override"},
        )

    adapter = ZohoMailEmailOtpDeliveryAdapter(
        accounts_base_url="https://accounts.zoho.com",
        mail_base_url="https://mail.zoho.com",
        client_id="zoho-client-id",
        client_secret="zoho-client-secret",
        refresh_token="zoho-refresh-token",
        account_id="zoho-account-id",
        from_address="noreply@example.com",
        policy=_policy(),
        transport=transport,
    )
    outcome = adapter.send_otp_challenge(
        message=_message(email_normalized="zoho.override@example.com"),
    )
    assert outcome.status == "delivered"
    assert observed_requests[-1].payload["toAddress"] == "mkuu@jisortublow.co.ke"


def test_zoho_adapter_reuses_cached_access_token_until_expiry() -> None:
    observed_requests: list[ProviderTransportRequest] = []

    def transport(request: ProviderTransportRequest) -> ProviderTransportResponse:
        observed_requests.append(request)
        if request.endpoint.endswith("/oauth/v2/token"):
            return ProviderTransportResponse(
                status_code=200,
                body={"access_token": "zoho-access-token", "expires_in": 3600},
            )
        return ProviderTransportResponse(
            status_code=202,
            body={"request_id": "zoho-req-002"},
        )

    adapter = ZohoMailEmailOtpDeliveryAdapter(
        accounts_base_url="https://accounts.zoho.com",
        mail_base_url="https://mail.zoho.com",
        client_id="zoho-client-cache-id",
        client_secret="zoho-client-secret",
        refresh_token="zoho-refresh-token",
        account_id="zoho-account-id",
        from_address="noreply@example.com",
        policy=_policy(),
        transport=transport,
    )
    first = adapter.send_otp_challenge(
        message=_message(email_normalized="zoho.cache.one@example.com"),
    )
    second = adapter.send_otp_challenge(
        message=_message(email_normalized="zoho.cache.two@example.com"),
    )
    assert first.status == "delivered"
    assert second.status == "delivered"
    token_requests = [
        request for request in observed_requests if request.endpoint.endswith("/oauth/v2/token")
    ]
    message_requests = [
        request for request in observed_requests if request.endpoint.endswith("/messages")
    ]
    assert len(token_requests) == 1
    assert len(message_requests) == 2


def test_zoho_adapter_retryable_timeout_exhaustion_is_deterministic() -> None:
    def transport(request: ProviderTransportRequest) -> ProviderTransportResponse:
        if request.endpoint.endswith("/oauth/v2/token"):
            return ProviderTransportResponse(
                status_code=200,
                body={"access_token": "zoho-access-token", "expires_in": 3600},
            )
        return ProviderTransportResponse(status_code=503, body={})

    adapter = ZohoMailEmailOtpDeliveryAdapter(
        accounts_base_url="https://accounts.zoho.com",
        mail_base_url="https://mail.zoho.com",
        client_id="zoho-client-timeout-id",
        client_secret="zoho-client-secret",
        refresh_token="zoho-refresh-token",
        account_id="zoho-account-id",
        from_address="noreply@example.com",
        policy=_policy(max_retries=1),
        transport=transport,
    )

    first = adapter.send_otp_challenge(
        message=_message(email_normalized="zoho.timeout@example.com"),
    )
    second = adapter.send_otp_challenge(
        message=_message(email_normalized="zoho.timeout@example.com"),
    )
    assert first.status == "failed_retryable"
    assert first.reason_code == "email_delivery_provider_unavailable"
    assert cast(str, first.provider_ref).startswith("email:http:503")
    assert first == second


def test_zoho_adapter_malformed_token_response_fails_closed() -> None:
    def transport(request: ProviderTransportRequest) -> ProviderTransportResponse:
        if request.endpoint.endswith("/oauth/v2/token"):
            return ProviderTransportResponse(status_code=200, body={"expires_in": 3600})
        return ProviderTransportResponse(status_code=202, body={})

    adapter = ZohoMailEmailOtpDeliveryAdapter(
        accounts_base_url="https://accounts.zoho.com",
        mail_base_url="https://mail.zoho.com",
        client_id="zoho-client-bad-token-id",
        client_secret="zoho-client-secret",
        refresh_token="zoho-refresh-token-bad-token",
        account_id="zoho-account-id",
        from_address="noreply@example.com",
        policy=_policy(),
        transport=transport,
    )
    outcome = adapter.send_otp_challenge(
        message=_message(email_normalized="zoho.bad.token@example.com"),
    )
    assert outcome.status == "failed_non_retryable"
    assert outcome.reason_code == "email_delivery_provider_rejected"


def test_default_zoho_selection_fails_closed_when_config_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH_OTP_RUNTIME_MODE", "production")
    monkeypatch.setenv("AUTH_OTP_EMAIL_PROVIDER_MODE", "zoho")
    monkeypatch.delenv("AUTH_ZOHO_CLIENT_ID", raising=False)
    monkeypatch.delenv("AUTH_ZOHO_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("AUTH_ZOHO_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("AUTH_ZOHO_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("AUTH_ZOHO_FROM_ADDRESS", raising=False)
    adapter = get_default_email_otp_delivery_adapter()
    outcome = adapter.send_otp_challenge(
        message=_message(email_normalized="zoho.default.misconfigured@example.com"),
    )
    assert outcome.status == "failed_non_retryable"
    assert outcome.reason_code == "otp_delivery_provider_misconfigured"


def _message(*, email_normalized: str) -> EmailOtpMessage:
    return EmailOtpMessage(
        purpose="registration_verify",
        email_normalized=email_normalized,
        subject="Test subject",
        content="<html><body><p>Test content</p></body></html>",
        challenge_id="challenge-001",
    )


def _policy(*, max_retries: int = 0) -> OtpProviderPolicy:
    return OtpProviderPolicy(
        timeout_seconds=5,
        max_retries=max_retries,
        retry_backoff_seconds=1,
        retry_backoff_max_seconds=2,
    )
