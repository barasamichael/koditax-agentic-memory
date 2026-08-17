"""Provider-locked OTP delivery adapters with deterministic resilience semantics."""

from __future__ import annotations

import json
import time
from typing import cast
from typing import Literal
from typing import Protocol
from typing import TypeGuard
from threading import Lock
from dataclasses import dataclass
from urllib.error import URLError
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request
from urllib.request import urlopen
from collections.abc import Callable

from services.auth.app.config import get_auth_zoho_client_id
from services.auth.app.config import get_auth_zoho_account_id
from services.auth.app.config import get_auth_otp_runtime_mode
from services.auth.app.config import get_auth_zoho_from_address
from services.auth.app.config import get_auth_zoho_client_secret
from services.auth.app.config import get_auth_zoho_mail_base_url
from services.auth.app.config import get_auth_zoho_refresh_token
from services.auth.app.config import get_auth_otp_sms_provider_mode
from services.auth.app.config import get_auth_zoho_accounts_base_url
from services.auth.app.config import get_auth_africas_talking_api_key
from services.auth.app.config import get_auth_otp_email_provider_mode
from services.auth.app.config import get_auth_africas_talking_username
from services.auth.app.config import get_auth_africas_talking_sender_id
from services.auth.app.config import get_auth_africas_talking_api_base_url
from services.auth.app.config import get_auth_otp_email_recipient_override
from services.auth.app.config import get_auth_otp_provider_timeout_seconds
from services.auth.app.config import get_auth_otp_provider_retry_max_retries
from services.auth.app.config import get_auth_otp_provider_retry_backoff_seconds
from services.auth.app.config import get_auth_otp_provider_retry_backoff_max_seconds

OtpDeliveryStatus = Literal[
    "delivered", "failed_retryable", "failed_non_retryable"
]


@dataclass(frozen=True)
class OtpDeliveryOutcome:
    """Represent normalized OTP provider delivery outcome."""

    status: OtpDeliveryStatus
    reason_code: str
    provider_ref: str | None = None


@dataclass(frozen=True)
class OtpProviderPolicy:
    """Represent deterministic bounded provider timeout and retry controls."""

    timeout_seconds: int
    max_retries: int
    retry_backoff_seconds: int
    retry_backoff_max_seconds: int


@dataclass(frozen=True)
class ProviderTransportRequest:
    """Represent deterministic provider transport request payload."""

    endpoint: str
    headers: dict[str, str]
    payload: dict[str, object]
    timeout_seconds: int
    body_encoding: Literal["json", "form"] = "json"


@dataclass(frozen=True)
class ProviderTransportResponse:
    """Represent deterministic provider transport response payload."""

    status_code: int
    body: dict[str, object]


@dataclass(frozen=True)
class EmailOtpMessage:
    """Represent deterministic provider-ready email payload content."""

    purpose: str
    email_normalized: str
    subject: str
    content: str
    challenge_id: str | None = None


class SmsOtpDeliveryAdapterProtocol(Protocol):
    """Define SMS OTP delivery adapter boundary."""

    def send_otp_challenge(
        self,
        *,
        purpose: str,
        phone_number_normalized: str,
        otp_code: str,
    ) -> OtpDeliveryOutcome:
        """Deliver one SMS OTP challenge request."""

        ...


class EmailOtpDeliveryAdapterProtocol(Protocol):
    """Define email OTP delivery adapter boundary."""

    def send_otp_challenge(
        self,
        *,
        message: EmailOtpMessage,
    ) -> OtpDeliveryOutcome:
        """Deliver one email OTP challenge request."""

        ...


class ProviderTransportError(ValueError):
    """Represent deterministic provider transport failure classification."""

    def __init__(
        self, *, reason_code: str, retryable: bool, provider_ref: str
    ) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.retryable = retryable
        self.provider_ref = provider_ref


_ZOHO_TOKEN_CACHE_LOCK = Lock()
_ZOHO_TOKEN_CACHE: dict[tuple[str, str, str], tuple[str, int]] = {}


class StubSmsOtpDeliveryAdapter:
    """Provide non-production deterministic SMS OTP delivery behavior."""

    def send_otp_challenge(
        self,
        *,
        purpose: str,
        phone_number_normalized: str,
        otp_code: str,
    ) -> OtpDeliveryOutcome:
        del purpose, phone_number_normalized, otp_code
        return OtpDeliveryOutcome(
            status="delivered",
            reason_code="sms_delivery_provider_delivered",
            provider_ref="sms:stub",
        )


class StubEmailOtpDeliveryAdapter:
    """Provide non-production deterministic email OTP delivery behavior."""

    def send_otp_challenge(
        self,
        *,
        message: EmailOtpMessage,
    ) -> OtpDeliveryOutcome:
        del message
        return OtpDeliveryOutcome(
            status="delivered",
            reason_code="email_delivery_provider_delivered",
            provider_ref="email:stub",
        )


class MisconfiguredSmsOtpDeliveryAdapter:
    """Fail closed when SMS provider mode is unsupported or unsafe for runtime mode."""

    def __init__(self, *, provider_mode: str, runtime_mode: str) -> None:
        self._provider_mode = provider_mode
        self._runtime_mode = runtime_mode

    def send_otp_challenge(
        self,
        *,
        purpose: str,
        phone_number_normalized: str,
        otp_code: str,
    ) -> OtpDeliveryOutcome:
        del purpose, phone_number_normalized, otp_code
        return OtpDeliveryOutcome(
            status="failed_non_retryable",
            reason_code="otp_delivery_provider_misconfigured",
            provider_ref=f"sms:{self._provider_mode}:{self._runtime_mode}",
        )


class MisconfiguredEmailOtpDeliveryAdapter:
    """Fail closed when email provider mode is unsupported or unsafe for runtime mode."""

    def __init__(self, *, provider_mode: str, runtime_mode: str) -> None:
        self._provider_mode = provider_mode
        self._runtime_mode = runtime_mode

    def send_otp_challenge(
        self,
        *,
        message: EmailOtpMessage,
    ) -> OtpDeliveryOutcome:
        del message
        return OtpDeliveryOutcome(
            status="failed_non_retryable",
            reason_code="otp_delivery_provider_misconfigured",
            provider_ref=f"email:{self._provider_mode}:{self._runtime_mode}",
        )


class ZohoMailEmailOtpDeliveryAdapter:
    """Deliver OTP/security email notifications using direct Zoho Mail APIs."""

    def __init__(
        self,
        *,
        accounts_base_url: str,
        mail_base_url: str,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        account_id: str,
        from_address: str,
        policy: OtpProviderPolicy,
        transport: (
            Callable[[ProviderTransportRequest], ProviderTransportResponse]
            | None
        ) = None,
        sleep_fn: Callable[[int], None] | None = None,
    ) -> None:
        self._accounts_base_url = accounts_base_url.rstrip("/")
        self._mail_base_url = mail_base_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._account_id = account_id
        self._from_address = from_address
        self._policy = policy
        self._transport = transport or _default_http_transport
        self._sleep_fn = sleep_fn
        self._recipient_override = get_auth_otp_email_recipient_override()

    def send_otp_challenge(
        self,
        *,
        message: EmailOtpMessage,
    ) -> OtpDeliveryOutcome:
        return _execute_with_retry(
            provider_kind="email",
            delivered_reason="email_delivery_provider_delivered",
            policy=self._policy,
            execute_once=lambda: self._send_message(message=message),
            sleep_fn=self._sleep_fn,
        )

    def _send_message(
        self, *, message: EmailOtpMessage
    ) -> ProviderTransportResponse:
        access_token = self._get_access_token()
        recipient_address = self._recipient_override or message.email_normalized
        request = ProviderTransportRequest(
            endpoint=f"{self._mail_base_url}/api/accounts/{self._account_id}/messages",
            headers={
                "Accept": "application/json",
                "Authorization": f"Zoho-oauthtoken {access_token}",
                "Content-Type": "application/json",
            },
            payload={
                "fromAddress": self._from_address,
                "toAddress": recipient_address,
                "subject": message.subject,
                "content": message.content,
            },
            timeout_seconds=self._policy.timeout_seconds,
        )
        return self._transport(request)

    def _get_access_token(self) -> str:
        cache_key = (
            self._accounts_base_url,
            self._client_id,
            self._refresh_token,
        )
        now = int(time.time())
        with _ZOHO_TOKEN_CACHE_LOCK:
            cached = _ZOHO_TOKEN_CACHE.get(cache_key)
            if cached is not None:
                cached_token, expires_at = cached
                if now < max(0, expires_at - 30):
                    return cached_token

        request = ProviderTransportRequest(
            endpoint=f"{self._accounts_base_url}/oauth/v2/token",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            payload={
                "refresh_token": self._refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "refresh_token",
            },
            timeout_seconds=self._policy.timeout_seconds,
            body_encoding="form",
        )
        response = self._transport(request)
        if not 200 <= response.status_code < 300:
            if response.status_code in {408, 429, 500, 502, 503, 504}:
                raise ProviderTransportError(
                    reason_code="provider_unavailable",
                    retryable=True,
                    provider_ref=f"email:zoho:token_http:{response.status_code}",
                )
            raise ProviderTransportError(
                reason_code="provider_rejected",
                retryable=False,
                provider_ref=f"email:zoho:token_http:{response.status_code}",
            )
        access_token = response.body.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise ProviderTransportError(
                reason_code="provider_rejected",
                retryable=False,
                provider_ref="email:zoho:token_malformed",
            )
        expires_in_raw = response.body.get(
            "expires_in_sec", response.body.get("expires_in", 3600)
        )
        try:
            expires_in = int(str(expires_in_raw))
        except ValueError:
            expires_in = 3600
        with _ZOHO_TOKEN_CACHE_LOCK:
            _ZOHO_TOKEN_CACHE[cache_key] = (
                access_token.strip(),
                now + max(60, expires_in),
            )
        return access_token.strip()


class _AfricasTalkingSmsServiceProtocol(Protocol):
    def send(
        self,
        message: str,
        recipients: list[str],
        sender_id: str | None = None,
    ) -> dict[str, object]: ...


class _AfricasTalkingSdkProtocol(Protocol):
    SMS: _AfricasTalkingSmsServiceProtocol

    def initialize(self, username: str, api_key: str) -> None: ...


class AfricasTalkingSmsOtpDeliveryAdapter:
    """Deliver OTP/security SMS notifications using Africa's Talking SDK."""

    def __init__(
        self,
        *,
        api_base_url: str,
        api_token: str,
        username: str,
        sender_id: str | None,
        policy: OtpProviderPolicy,
        transport: (
            Callable[[ProviderTransportRequest], ProviderTransportResponse]
            | None
        ) = None,
        sleep_fn: Callable[[int], None] | None = None,
    ) -> None:
        self._api_base_url = api_base_url.rstrip("/")
        self._api_token = api_token
        self._username = username
        self._sender_id = sender_id
        self._policy = policy
        self._transport = transport or _default_http_transport
        self._sleep_fn = sleep_fn

    def send_otp_challenge(
        self,
        *,
        purpose: str,
        phone_number_normalized: str,
        otp_code: str,
    ) -> OtpDeliveryOutcome:
        return _execute_with_retry(
            provider_kind="sms",
            delivered_reason="sms_delivery_provider_delivered",
            policy=self._policy,
            execute_once=lambda: self._send_message(
                purpose=purpose,
                phone_number_normalized=phone_number_normalized,
                otp_code=otp_code,
            ),
            sleep_fn=self._sleep_fn,
        )

    def _send_message(
        self,
        *,
        purpose: str,
        phone_number_normalized: str,
        otp_code: str,
    ) -> ProviderTransportResponse:
        import africastalking

        sdk = cast(_AfricasTalkingSdkProtocol, africastalking)
        sdk.initialize(self._username, self._api_token)
        message = _build_africas_talking_sms_message(
            purpose=purpose,
            otp_code=otp_code,
        )
        if self._sender_id is None:
            response = sdk.SMS.send(message, [phone_number_normalized])
        else:
            response = sdk.SMS.send(
                message,
                [phone_number_normalized],
                sender_id=self._sender_id,
            )
        return ProviderTransportResponse(
            status_code=200,
            body=response,
        )


def get_default_sms_otp_delivery_adapter() -> SmsOtpDeliveryAdapterProtocol:
    """Resolve configured SMS OTP delivery adapter with fail-closed behavior."""

    runtime_mode = get_auth_otp_runtime_mode()
    provider_mode = get_auth_otp_sms_provider_mode()
    if provider_mode == "stub":
        if runtime_mode == "production":
            return MisconfiguredSmsOtpDeliveryAdapter(
                provider_mode=provider_mode,
                runtime_mode=runtime_mode,
            )
        return StubSmsOtpDeliveryAdapter()
    if provider_mode != "africas_talking":
        return MisconfiguredSmsOtpDeliveryAdapter(
            provider_mode=provider_mode,
            runtime_mode=runtime_mode,
        )

    api_token = get_auth_africas_talking_api_key()
    username = get_auth_africas_talking_username()
    if api_token is None or username is None:
        return MisconfiguredSmsOtpDeliveryAdapter(
            provider_mode=provider_mode,
            runtime_mode=runtime_mode,
        )
    return AfricasTalkingSmsOtpDeliveryAdapter(
        api_base_url=get_auth_africas_talking_api_base_url(),
        api_token=api_token,
        username=username,
        sender_id=get_auth_africas_talking_sender_id(),
        policy=_resolve_provider_policy(),
    )


def get_default_email_otp_delivery_adapter() -> EmailOtpDeliveryAdapterProtocol:
    """Resolve configured email OTP delivery adapter with fail-closed behavior."""

    runtime_mode = get_auth_otp_runtime_mode()
    provider_mode = get_auth_otp_email_provider_mode()
    if provider_mode == "stub":
        if runtime_mode == "production":
            return MisconfiguredEmailOtpDeliveryAdapter(
                provider_mode=provider_mode,
                runtime_mode=runtime_mode,
            )
        return StubEmailOtpDeliveryAdapter()
    if provider_mode != "zoho":
        return MisconfiguredEmailOtpDeliveryAdapter(
            provider_mode=provider_mode,
            runtime_mode=runtime_mode,
        )

    client_id = get_auth_zoho_client_id()
    client_secret = get_auth_zoho_client_secret()
    refresh_token = get_auth_zoho_refresh_token()
    account_id = get_auth_zoho_account_id()
    from_address = get_auth_zoho_from_address()
    if (
        client_id is None
        or client_secret is None
        or refresh_token is None
        or account_id is None
        or from_address is None
    ):
        return MisconfiguredEmailOtpDeliveryAdapter(
            provider_mode=provider_mode,
            runtime_mode=runtime_mode,
        )

    return ZohoMailEmailOtpDeliveryAdapter(
        accounts_base_url=get_auth_zoho_accounts_base_url(),
        mail_base_url=get_auth_zoho_mail_base_url(),
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        account_id=account_id,
        from_address=from_address,
        policy=_resolve_provider_policy(),
    )


def normalize_sms_delivery_outcome(
    *, outcome: OtpDeliveryOutcome
) -> OtpDeliveryOutcome:
    """Normalize SMS provider-specific delivery output into canonical reason classes."""

    reason_code = outcome.reason_code.strip().lower()
    if outcome.status == "delivered":
        return OtpDeliveryOutcome(
            status="delivered",
            reason_code="sms_delivery_provider_delivered",
            provider_ref=outcome.provider_ref,
        )
    if reason_code == "otp_delivery_provider_misconfigured":
        return OtpDeliveryOutcome(
            status="failed_non_retryable",
            reason_code="otp_delivery_provider_misconfigured",
            provider_ref=outcome.provider_ref,
        )
    if reason_code in {
        "sms_delivery_provider_unavailable",
        "provider_unavailable",
    }:
        return OtpDeliveryOutcome(
            status=outcome.status,
            reason_code="sms_delivery_provider_unavailable",
            provider_ref=outcome.provider_ref,
        )
    if reason_code in {"sms_delivery_provider_timeout", "provider_timeout"}:
        return OtpDeliveryOutcome(
            status=outcome.status,
            reason_code="sms_delivery_provider_timeout",
            provider_ref=outcome.provider_ref,
        )
    if reason_code in {"sms_delivery_provider_rejected", "provider_rejected"}:
        return OtpDeliveryOutcome(
            status=outcome.status,
            reason_code="sms_delivery_provider_rejected",
            provider_ref=outcome.provider_ref,
        )
    if outcome.status == "failed_retryable":
        return OtpDeliveryOutcome(
            status="failed_retryable",
            reason_code="sms_delivery_provider_timeout",
            provider_ref=outcome.provider_ref,
        )
    return OtpDeliveryOutcome(
        status="failed_non_retryable",
        reason_code="sms_delivery_provider_rejected",
        provider_ref=outcome.provider_ref,
    )


def normalize_email_delivery_outcome(
    *, outcome: OtpDeliveryOutcome
) -> OtpDeliveryOutcome:
    """Normalize email provider-specific delivery output into canonical reason classes."""

    reason_code = outcome.reason_code.strip().lower()
    if outcome.status == "delivered":
        return OtpDeliveryOutcome(
            status="delivered",
            reason_code="email_delivery_provider_delivered",
            provider_ref=outcome.provider_ref,
        )
    if reason_code == "otp_delivery_provider_misconfigured":
        return OtpDeliveryOutcome(
            status="failed_non_retryable",
            reason_code="otp_delivery_provider_misconfigured",
            provider_ref=outcome.provider_ref,
        )
    if reason_code in {
        "email_delivery_provider_unavailable",
        "provider_unavailable",
    }:
        return OtpDeliveryOutcome(
            status=outcome.status,
            reason_code="email_delivery_provider_unavailable",
            provider_ref=outcome.provider_ref,
        )
    if reason_code in {"email_delivery_provider_timeout", "provider_timeout"}:
        return OtpDeliveryOutcome(
            status=outcome.status,
            reason_code="email_delivery_provider_timeout",
            provider_ref=outcome.provider_ref,
        )
    if reason_code in {"email_delivery_provider_rejected", "provider_rejected"}:
        return OtpDeliveryOutcome(
            status=outcome.status,
            reason_code="email_delivery_provider_rejected",
            provider_ref=outcome.provider_ref,
        )
    if outcome.status == "failed_retryable":
        return OtpDeliveryOutcome(
            status="failed_retryable",
            reason_code="email_delivery_provider_timeout",
            provider_ref=outcome.provider_ref,
        )
    return OtpDeliveryOutcome(
        status="failed_non_retryable",
        reason_code="email_delivery_provider_rejected",
        provider_ref=outcome.provider_ref,
    )


def _resolve_provider_policy() -> OtpProviderPolicy:
    return OtpProviderPolicy(
        timeout_seconds=get_auth_otp_provider_timeout_seconds(),
        max_retries=get_auth_otp_provider_retry_max_retries(),
        retry_backoff_seconds=get_auth_otp_provider_retry_backoff_seconds(),
        retry_backoff_max_seconds=get_auth_otp_provider_retry_backoff_max_seconds(),
    )


def _execute_with_retry(
    *,
    provider_kind: Literal["sms", "email"],
    delivered_reason: str,
    policy: OtpProviderPolicy,
    execute_once: Callable[[], ProviderTransportResponse],
    sleep_fn: Callable[[int], None] | None,
) -> OtpDeliveryOutcome:
    max_attempts = 1 + max(0, policy.max_retries)
    for attempt_index in range(max_attempts):
        try:
            response = execute_once()
            return _map_transport_response(
                provider_kind=provider_kind,
                delivered_reason=delivered_reason,
                response=response,
            )
        except ProviderTransportError as error:
            if error.retryable and attempt_index < max_attempts - 1:
                if sleep_fn is not None:
                    sleep_fn(
                        _compute_backoff_seconds(
                            retry_backoff_seconds=policy.retry_backoff_seconds,
                            retry_backoff_max_seconds=policy.retry_backoff_max_seconds,
                            retry_index=attempt_index,
                        )
                    )
                continue
            reason = error.reason_code
            if reason == "provider_timeout":
                mapped_reason = f"{provider_kind}_delivery_provider_timeout"
                return OtpDeliveryOutcome(
                    status="failed_retryable",
                    reason_code=mapped_reason,
                    provider_ref=error.provider_ref,
                )
            if reason == "provider_unavailable":
                mapped_reason = f"{provider_kind}_delivery_provider_unavailable"
                return OtpDeliveryOutcome(
                    status="failed_retryable",
                    reason_code=mapped_reason,
                    provider_ref=error.provider_ref,
                )
            mapped_reason = f"{provider_kind}_delivery_provider_rejected"
            return OtpDeliveryOutcome(
                status="failed_non_retryable",
                reason_code=mapped_reason,
                provider_ref=error.provider_ref,
            )
        except Exception:
            if attempt_index < max_attempts - 1:
                continue
            mapped_reason = f"{provider_kind}_delivery_provider_unavailable"
            return OtpDeliveryOutcome(
                status="failed_retryable",
                reason_code=mapped_reason,
                provider_ref=f"{provider_kind}:transport:unknown",
            )
    return OtpDeliveryOutcome(
        status="failed_retryable",
        reason_code=f"{provider_kind}_delivery_provider_unavailable",
        provider_ref=f"{provider_kind}:transport:exhausted",
    )


def _map_transport_response(
    *,
    provider_kind: Literal["sms", "email"],
    delivered_reason: str,
    response: ProviderTransportResponse,
) -> OtpDeliveryOutcome:
    if 200 <= response.status_code < 300:
        provider_ref = _extract_provider_ref(
            provider_kind=provider_kind,
            response_body=response.body,
        )
        return OtpDeliveryOutcome(
            status="delivered",
            reason_code=delivered_reason,
            provider_ref=provider_ref,
        )
    if response.status_code in {408, 429, 500, 502, 503, 504}:
        raise ProviderTransportError(
            reason_code="provider_unavailable",
            retryable=True,
            provider_ref=f"{provider_kind}:http:{response.status_code}",
        )
    raise ProviderTransportError(
        reason_code="provider_rejected",
        retryable=False,
        provider_ref=f"{provider_kind}:http:{response.status_code}",
    )


def _is_string_keyed_object_mapping(
    value: object,
) -> TypeGuard[dict[str, object]]:
    if not isinstance(value, dict):
        return False
    return all(isinstance(key, str) for key in value)  # type: ignore


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _extract_provider_ref(
    *,
    provider_kind: Literal["sms", "email"],
    response_body: dict[str, object],
) -> str:
    if provider_kind == "sms":
        sms_data = response_body.get("SMSMessageData")
        if _is_string_keyed_object_mapping(sms_data):
            recipients = sms_data.get("Recipients")
            if _is_object_list(recipients):
                for recipient in recipients:
                    if not _is_string_keyed_object_mapping(recipient):
                        continue
                    candidate = recipient.get("messageId")
                    if isinstance(candidate, str) and candidate.strip():
                        return f"{provider_kind}:provider:{candidate.strip()}"
                    legacy_candidate = recipient.get("message_id")
                    if (
                        isinstance(legacy_candidate, str)
                        and legacy_candidate.strip()
                    ):
                        return f"{provider_kind}:provider:{legacy_candidate.strip()}"
    for key in ("provider_ref", "request_id", "message_id"):
        value = response_body.get(key)
        if isinstance(value, str) and value.strip():
            return f"{provider_kind}:provider:{value.strip()}"
    return f"{provider_kind}:provider:accepted"


def _build_africas_talking_sms_message(*, purpose: str, otp_code: str) -> str:
    normalized_purpose = purpose.strip().lower()
    action = {
        "registration_verify": "verify your account",
        "login_step_up": "complete your login",
        "recovery": "recover your account",
        "account_deletion_confirm": "confirm account deletion",
        "phone_change_confirm": "confirm your phone change",
    }.get(normalized_purpose, "complete your request")
    return f"Your KODI OTP code is {otp_code}. Use it to {action}."


def _default_http_transport(
    request: ProviderTransportRequest,
) -> ProviderTransportResponse:
    if request.body_encoding == "form":
        payload_bytes = urlencode(
            {
                key: str(value)
                for key, value in request.payload.items()
                if value is not None
            }
        ).encode("utf-8")
    else:
        payload_bytes = json.dumps(request.payload).encode("utf-8")
    http_request = Request(
        request.endpoint,
        data=payload_bytes,
        headers=request.headers,
        method="POST",
    )
    try:
        with urlopen(http_request, timeout=request.timeout_seconds) as response:
            raw_body = response.read().decode("utf-8")
            parsed_body = _parse_response_body(raw_body)
            return ProviderTransportResponse(
                status_code=response.status,
                body=parsed_body,
            )
    except TimeoutError as error:
        raise ProviderTransportError(
            reason_code="provider_timeout",
            retryable=True,
            provider_ref="transport:timeout",
        ) from error
    except HTTPError as error:
        retryable_status = error.code in {408, 429, 500, 502, 503, 504}
        raise ProviderTransportError(
            reason_code=(
                "provider_unavailable"
                if retryable_status
                else "provider_rejected"
            ),
            retryable=retryable_status,
            provider_ref=f"transport:http:{error.code}",
        ) from error
    except URLError as error:
        raise ProviderTransportError(
            reason_code="provider_unavailable",
            retryable=True,
            provider_ref="transport:url_error",
        ) from error


def _parse_response_body(raw_body: str) -> dict[str, object]:
    normalized = raw_body.strip()
    if not normalized:
        return {}
    try:
        parsed = json.loads(normalized)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return cast(dict[str, object], parsed)


def _compute_backoff_seconds(
    *,
    retry_backoff_seconds: int,
    retry_backoff_max_seconds: int,
    retry_index: int,
) -> int:
    base = max(0, retry_backoff_seconds)
    if base <= 0:
        return 0
    computed = base * (2**retry_index)
    return min(computed, max(0, retry_backoff_max_seconds))
