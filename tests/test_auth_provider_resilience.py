"""Deterministic provider resilience and fallback behavior for auth OTP delivery."""

from __future__ import annotations

from services.auth.app.email_verification import InMemoryEmailVerificationStore
from services.auth.app.phone_verification import PhoneVerificationError
from services.auth.app.phone_verification import InMemoryPhoneVerificationStore
from services.auth.app.phone_verification import PhoneVerificationChallengeRequest
from services.auth.app.phone_verification import issue_phone_verification_challenge
from services.auth.app.otp_delivery_adapters import EmailOtpMessage
from services.auth.app.otp_delivery_adapters import OtpDeliveryOutcome
from services.auth.app.otp_delivery_adapters import SmsOtpDeliveryAdapterProtocol
from services.auth.app.otp_delivery_adapters import EmailOtpDeliveryAdapterProtocol


class _RetryableSmsFailureAdapter(SmsOtpDeliveryAdapterProtocol):
    def send_otp_challenge(
        self,
        *,
        purpose: str,
        phone_number_normalized: str,
    ) -> OtpDeliveryOutcome:
        del purpose, phone_number_normalized
        return OtpDeliveryOutcome(
            status="failed_retryable",
            reason_code="sms_delivery_provider_timeout",
            provider_ref="sms:test:timeout",
        )


class _PermanentSmsFailureAdapter(SmsOtpDeliveryAdapterProtocol):
    def send_otp_challenge(
        self,
        *,
        purpose: str,
        phone_number_normalized: str,
    ) -> OtpDeliveryOutcome:
        del purpose, phone_number_normalized
        return OtpDeliveryOutcome(
            status="failed_non_retryable",
            reason_code="sms_delivery_provider_rejected",
            provider_ref="sms:test:rejected",
        )


class _SuccessEmailAdapter(EmailOtpDeliveryAdapterProtocol):
    def send_otp_challenge(
        self,
        *,
        message: EmailOtpMessage,
    ) -> OtpDeliveryOutcome:
        del message
        return OtpDeliveryOutcome(
            status="delivered",
            reason_code="email_delivery_provider_delivered",
            provider_ref="email:test:delivered",
        )


def test_sms_retryable_failure_maps_to_canonical_error() -> None:
    phone_store = InMemoryPhoneVerificationStore()
    request = PhoneVerificationChallengeRequest(
        purpose="registration_verify",
        channel="sms",
        phone_number="+254700000100",
        fallback_channel=None,
        email=None,
    )
    try:
        issue_phone_verification_challenge(
            request_model=request,
            idempotency_key="provider-resilience-retryable",
            phone_verification_store=phone_store,
            sms_delivery_adapter=_RetryableSmsFailureAdapter(),
        )
    except PhoneVerificationError as error:
        assert error.error_code == "otp_sms_delivery_provider_timeout"
        assert error.reason == "otp_sms_delivery_provider_timeout"
        assert error.details["delivery_failure_class"] == "failed_retryable"
    else:
        raise AssertionError("Expected deterministic retryable failure path.")


def test_sms_permanent_failure_maps_to_canonical_error() -> None:
    phone_store = InMemoryPhoneVerificationStore()
    request = PhoneVerificationChallengeRequest(
        purpose="registration_verify",
        channel="sms",
        phone_number="+254700000101",
        fallback_channel=None,
        email=None,
    )
    try:
        issue_phone_verification_challenge(
            request_model=request,
            idempotency_key="provider-resilience-permanent",
            phone_verification_store=phone_store,
            sms_delivery_adapter=_PermanentSmsFailureAdapter(),
        )
    except PhoneVerificationError as error:
        assert error.error_code == "otp_sms_delivery_provider_rejected"
        assert error.reason == "otp_sms_delivery_provider_rejected"
        assert error.details["delivery_failure_class"] == "failed_non_retryable"
    else:
        raise AssertionError("Expected deterministic permanent failure path.")


def test_sms_failure_with_allowed_email_fallback_succeeds() -> None:
    phone_store = InMemoryPhoneVerificationStore()
    email_store = InMemoryEmailVerificationStore()
    request = PhoneVerificationChallengeRequest(
        purpose="registration_verify",
        channel="sms",
        phone_number="+254700000102",
        fallback_channel="email",
        email="fallback.success@example.com",
    )
    response = issue_phone_verification_challenge(
        request_model=request,
        idempotency_key="provider-resilience-fallback",
        phone_verification_store=phone_store,
        email_verification_store=email_store,
        email_delivery_adapter=_SuccessEmailAdapter(),
        sms_delivery_adapter=_RetryableSmsFailureAdapter(),
    )
    assert response.status == "challenge_issued"


def test_repeated_retryable_failure_has_stable_reason_and_details_shape() -> None:
    request = PhoneVerificationChallengeRequest(
        purpose="registration_verify",
        channel="sms",
        phone_number="+254700000103",
        fallback_channel=None,
        email=None,
    )
    first = _capture_error_details(
        request_model=request,
        idempotency_key="provider-resilience-repeat-first",
    )
    second = _capture_error_details(
        request_model=request,
        idempotency_key="provider-resilience-repeat-second",
    )
    assert first["error_code"] == second["error_code"]
    assert first["reason"] == second["reason"]
    assert set(first["details"].keys()) == set(second["details"].keys())


def _capture_error_details(
    *,
    request_model: PhoneVerificationChallengeRequest,
    idempotency_key: str,
) -> dict[str, object]:
    phone_store = InMemoryPhoneVerificationStore()
    try:
        issue_phone_verification_challenge(
            request_model=request_model,
            idempotency_key=idempotency_key,
            phone_verification_store=phone_store,
            sms_delivery_adapter=_RetryableSmsFailureAdapter(),
        )
    except PhoneVerificationError as error:
        return {
            "error_code": error.error_code,
            "reason": error.reason,
            "details": error.details,
        }
    raise AssertionError("Expected deterministic retryable failure path.")
