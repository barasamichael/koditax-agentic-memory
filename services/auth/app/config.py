"""Deterministic auth configuration settings for Phase 8 baseline."""

from __future__ import annotations

import os
from datetime import datetime
from dataclasses import dataclass

EMAIL_VERIFICATION_TTL_SECONDS_ENV_VAR = "AUTH_EMAIL_VERIFICATION_TTL_SECONDS"
DEFAULT_EMAIL_VERIFICATION_TTL_SECONDS = 600
EMAIL_VERIFICATION_MAX_ATTEMPTS_ENV_VAR = "AUTH_EMAIL_VERIFICATION_MAX_ATTEMPTS"
DEFAULT_EMAIL_VERIFICATION_MAX_ATTEMPTS = 3
PHONE_VERIFICATION_TTL_SECONDS_ENV_VAR = "AUTH_PHONE_VERIFICATION_TTL_SECONDS"
DEFAULT_PHONE_VERIFICATION_TTL_SECONDS = 600
PHONE_VERIFICATION_MAX_ATTEMPTS_ENV_VAR = "AUTH_PHONE_VERIFICATION_MAX_ATTEMPTS"
DEFAULT_PHONE_VERIFICATION_MAX_ATTEMPTS = 3
PHONE_VERIFICATION_RESEND_MIN_INTERVAL_SECONDS_ENV_VAR = (
    "AUTH_PHONE_VERIFICATION_RESEND_MIN_INTERVAL_SECONDS"
)
DEFAULT_PHONE_VERIFICATION_RESEND_MIN_INTERVAL_SECONDS = 120
PASSWORD_RESET_TTL_SECONDS_ENV_VAR = "AUTH_PASSWORD_RESET_TTL_SECONDS"
DEFAULT_PASSWORD_RESET_TTL_SECONDS = 900
PASSWORD_RESET_MAX_ATTEMPTS_ENV_VAR = "AUTH_PASSWORD_RESET_MAX_ATTEMPTS"
DEFAULT_PASSWORD_RESET_MAX_ATTEMPTS = 5
ACCOUNT_DELETION_COOLDOWN_SECONDS_ENV_VAR = "AUTH_ACCOUNT_DELETION_COOLDOWN_SECONDS"
DEFAULT_ACCOUNT_DELETION_COOLDOWN_SECONDS = 259200
AUTH_SESSION_TTL_SECONDS_ENV_VAR = "AUTH_SESSION_TTL_SECONDS"
DEFAULT_AUTH_SESSION_TTL_SECONDS = 3600
AUTH_SESSION_INACTIVITY_TIMEOUT_SECONDS_ENV_VAR = "AUTH_SESSION_INACTIVITY_TIMEOUT_SECONDS"
DEFAULT_AUTH_SESSION_INACTIVITY_TIMEOUT_SECONDS = 1800
AUTH_SESSION_ABSOLUTE_LIFETIME_SECONDS_ENV_VAR = "AUTH_SESSION_ABSOLUTE_LIFETIME_SECONDS"
MAX_AUTH_SESSION_ABSOLUTE_LIFETIME_SECONDS = 604800
DEFAULT_AUTH_SESSION_ABSOLUTE_LIFETIME_SECONDS = MAX_AUTH_SESSION_ABSOLUTE_LIFETIME_SECONDS
AUTH_SESSION_WARNING_WINDOW_SECONDS_ENV_VAR = "AUTH_SESSION_WARNING_WINDOW_SECONDS"
DEFAULT_AUTH_SESSION_WARNING_WINDOW_SECONDS = 120
AUTH_SESSION_MAX_CONCURRENT_SESSIONS_ENV_VAR = "AUTH_SESSION_MAX_CONCURRENT_SESSIONS"
DEFAULT_AUTH_SESSION_MAX_CONCURRENT_SESSIONS = 3
AUTH_LOGIN_LOCKOUT_MAX_FAILED_ATTEMPTS_ENV_VAR = "AUTH_LOGIN_LOCKOUT_MAX_FAILED_ATTEMPTS"
DEFAULT_AUTH_LOGIN_LOCKOUT_MAX_FAILED_ATTEMPTS = 5
AUTH_LOGIN_LOCKOUT_ATTEMPT_WINDOW_SECONDS_ENV_VAR = "AUTH_LOGIN_LOCKOUT_ATTEMPT_WINDOW_SECONDS"
DEFAULT_AUTH_LOGIN_LOCKOUT_ATTEMPT_WINDOW_SECONDS = 900
AUTH_LOGIN_LOCKOUT_DURATION_SECONDS_ENV_VAR = "AUTH_LOGIN_LOCKOUT_DURATION_SECONDS"
DEFAULT_AUTH_LOGIN_LOCKOUT_DURATION_SECONDS = 1800
# Backward-compatible alias; old env var maps to lockout duration semantics.
AUTH_LOGIN_LOCKOUT_WINDOW_SECONDS_ENV_VAR = AUTH_LOGIN_LOCKOUT_DURATION_SECONDS_ENV_VAR
DEFAULT_AUTH_LOGIN_LOCKOUT_WINDOW_SECONDS = DEFAULT_AUTH_LOGIN_LOCKOUT_DURATION_SECONDS
AUTH_SLO_EVALUATION_WINDOW_ENV_VAR = "AUTH_SLO_EVALUATION_WINDOW"
DEFAULT_AUTH_SLO_EVALUATION_WINDOW = "5m"
AUTH_SLO_LOGIN_SUCCESS_RATE_MIN_ENV_VAR = "AUTH_SLO_LOGIN_SUCCESS_RATE_MIN"
DEFAULT_AUTH_SLO_LOGIN_SUCCESS_RATE_MIN = 0.95
AUTH_SLO_OTP_VERIFY_SUCCESS_RATE_MIN_ENV_VAR = "AUTH_SLO_OTP_VERIFY_SUCCESS_RATE_MIN"
DEFAULT_AUTH_SLO_OTP_VERIFY_SUCCESS_RATE_MIN = 0.90
AUTH_SLO_PASSWORD_RESET_SUCCESS_RATE_MIN_ENV_VAR = "AUTH_SLO_PASSWORD_RESET_SUCCESS_RATE_MIN"
DEFAULT_AUTH_SLO_PASSWORD_RESET_SUCCESS_RATE_MIN = 0.90
AUTH_SLO_LATENCY_P95_MS_MAX_ENV_VAR = "AUTH_SLO_LATENCY_P95_MS_MAX"
DEFAULT_AUTH_SLO_LATENCY_P95_MS_MAX = 750
AUTH_SLO_LATENCY_P99_MS_MAX_ENV_VAR = "AUTH_SLO_LATENCY_P99_MS_MAX"
DEFAULT_AUTH_SLO_LATENCY_P99_MS_MAX = 1500
AUTH_SLO_ABUSE_LOCKOUT_SPIKE_THRESHOLD_ENV_VAR = "AUTH_SLO_ABUSE_LOCKOUT_SPIKE_THRESHOLD"
DEFAULT_AUTH_SLO_ABUSE_LOCKOUT_SPIKE_THRESHOLD = 25
AUTH_SLO_ABUSE_OTP_ATTEMPT_SPIKE_THRESHOLD_ENV_VAR = "AUTH_SLO_ABUSE_OTP_ATTEMPT_SPIKE_THRESHOLD"
DEFAULT_AUTH_SLO_ABUSE_OTP_ATTEMPT_SPIKE_THRESHOLD = 25
AUTH_PASSWORD_BCRYPT_COST_ENV_VAR = "AUTH_PASSWORD_BCRYPT_COST"
DEFAULT_AUTH_PASSWORD_BCRYPT_COST = 12
AUTH_PASSWORD_HISTORY_DEPTH_ENV_VAR = "AUTH_PASSWORD_HISTORY_DEPTH"
DEFAULT_AUTH_PASSWORD_HISTORY_DEPTH = 5
AUTH_REGISTRATION_PHONE_OTP_ENABLED_ENV_VAR = "AUTH_REGISTRATION_PHONE_OTP_ENABLED"
DEFAULT_AUTH_REGISTRATION_PHONE_OTP_ENABLED = False
AUTH_LOGIN_PHONE_OTP_ENABLED_ENV_VAR = "AUTH_LOGIN_PHONE_OTP_ENABLED"
DEFAULT_AUTH_LOGIN_PHONE_OTP_ENABLED = False
AUTH_OTP_SMS_EMAIL_FALLBACK_ENABLED_ENV_VAR = "AUTH_OTP_SMS_EMAIL_FALLBACK_ENABLED"
DEFAULT_AUTH_OTP_SMS_EMAIL_FALLBACK_ENABLED = True
AUTH_OTP_FALLBACK_ALLOWED_PURPOSES_ENV_VAR = "AUTH_OTP_FALLBACK_ALLOWED_PURPOSES"
DEFAULT_AUTH_OTP_FALLBACK_ALLOWED_PURPOSES: tuple[str, ...] = (
    "registration_verify",
    "recovery",
)
AUTH_OTP_RUNTIME_MODE_ENV_VAR = "AUTH_OTP_RUNTIME_MODE"
DEFAULT_AUTH_OTP_RUNTIME_MODE = "development"
AUTH_DEFAULT_TENANT_ID_ENV_VAR = "AUTH_DEFAULT_TENANT_ID"
DEFAULT_AUTH_TENANT_ID = "pilot_tenant_alpha"
AUTH_OTP_SMS_PROVIDER_MODE_ENV_VAR = "AUTH_OTP_SMS_PROVIDER_MODE"
DEFAULT_AUTH_OTP_SMS_PROVIDER_MODE = "stub"
AUTH_OTP_EMAIL_PROVIDER_MODE_ENV_VAR = "AUTH_OTP_EMAIL_PROVIDER_MODE"
DEFAULT_AUTH_OTP_EMAIL_PROVIDER_MODE = "zoho"
AUTH_OTP_PROVIDER_TIMEOUT_SECONDS_ENV_VAR = "AUTH_OTP_PROVIDER_TIMEOUT_SECONDS"
DEFAULT_AUTH_OTP_PROVIDER_TIMEOUT_SECONDS = 5
AUTH_OTP_PROVIDER_RETRY_MAX_RETRIES_ENV_VAR = "AUTH_OTP_PROVIDER_RETRY_MAX_RETRIES"
DEFAULT_AUTH_OTP_PROVIDER_RETRY_MAX_RETRIES = 1
AUTH_OTP_PROVIDER_RETRY_BACKOFF_SECONDS_ENV_VAR = "AUTH_OTP_PROVIDER_RETRY_BACKOFF_SECONDS"
DEFAULT_AUTH_OTP_PROVIDER_RETRY_BACKOFF_SECONDS = 1
AUTH_OTP_PROVIDER_RETRY_BACKOFF_MAX_SECONDS_ENV_VAR = "AUTH_OTP_PROVIDER_RETRY_BACKOFF_MAX_SECONDS"
DEFAULT_AUTH_OTP_PROVIDER_RETRY_BACKOFF_MAX_SECONDS = 4
AUTH_ZOHO_ACCOUNTS_BASE_URL_ENV_VAR = "AUTH_ZOHO_ACCOUNTS_BASE_URL"
DEFAULT_AUTH_ZOHO_ACCOUNTS_BASE_URL = "https://accounts.zoho.com"
AUTH_ZOHO_MAIL_BASE_URL_ENV_VAR = "AUTH_ZOHO_MAIL_BASE_URL"
DEFAULT_AUTH_ZOHO_MAIL_BASE_URL = "https://mail.zoho.com"
AUTH_ZOHO_CLIENT_ID_ENV_VAR = "AUTH_ZOHO_CLIENT_ID"
AUTH_ZOHO_CLIENT_SECRET_ENV_VAR = "AUTH_ZOHO_CLIENT_SECRET"
AUTH_ZOHO_REFRESH_TOKEN_ENV_VAR = "AUTH_ZOHO_REFRESH_TOKEN"
AUTH_ZOHO_ACCOUNT_ID_ENV_VAR = "AUTH_ZOHO_ACCOUNT_ID"
AUTH_ZOHO_FROM_ADDRESS_ENV_VAR = "AUTH_ZOHO_FROM_ADDRESS"
AUTH_OTP_EMAIL_RECIPIENT_OVERRIDE_ENV_VAR = "AUTH_OTP_EMAIL_RECIPIENT_OVERRIDE"
AUTH_ZOHO_MAIL_API_BASE_URL_ENV_VAR = "AUTH_ZOHO_MAIL_API_BASE_URL"
DEFAULT_AUTH_ZOHO_MAIL_API_BASE_URL = "https://mail.zoho.com/api"
AUTH_ZOHO_MAIL_TEMPLATE_REGISTRATION_VERIFY_ENV_VAR = "AUTH_ZOHO_MAIL_TEMPLATE_REGISTRATION_VERIFY"
AUTH_ZOHO_MAIL_TEMPLATE_LOGIN_STEP_UP_ENV_VAR = "AUTH_ZOHO_MAIL_TEMPLATE_LOGIN_STEP_UP"
AUTH_ZOHO_MAIL_TEMPLATE_RECOVERY_ENV_VAR = "AUTH_ZOHO_MAIL_TEMPLATE_RECOVERY"
AUTH_ZOHO_MAIL_TEMPLATE_ACCOUNT_DELETION_CONFIRM_ENV_VAR = (
    "AUTH_ZOHO_MAIL_TEMPLATE_ACCOUNT_DELETION_CONFIRM"
)
AUTH_ZOHO_MAIL_TEMPLATE_PHONE_CHANGE_CONFIRM_ENV_VAR = (
    "AUTH_ZOHO_MAIL_TEMPLATE_PHONE_CHANGE_CONFIRM"
)
AUTH_AFRICAS_TALKING_API_BASE_URL_ENV_VAR = "AUTH_AFRICAS_TALKING_API_BASE_URL"
DEFAULT_AUTH_AFRICAS_TALKING_API_BASE_URL = "https://api.africastalking.com/version1/messaging"
AUTH_AFRICAS_TALKING_USERNAME_ENV_VAR = "AUTH_AFRICAS_TALKING_USERNAME"
AT_USERNAME_ENV_VAR = "AT_USERNAME"
AT_API_KEY_ENV_VAR = "AT_API_KEY"
AUTH_AFRICAS_TALKING_SENDER_ID_ENV_VAR = "AUTH_AFRICAS_TALKING_SENDER_ID"
SENDER_ID_ENV_VAR = "SENDER_ID"
LEGACY_SENDER_ID_ENV_VAR = "sender_id"
AUTH_OTP_RESEND_WINDOW_SECONDS_ENV_VAR = "AUTH_OTP_RESEND_WINDOW_SECONDS"
DEFAULT_AUTH_OTP_RESEND_WINDOW_SECONDS = 86400
AUTH_OTP_RESEND_LIMIT_REGISTRATION_VERIFY_ENV_VAR = "AUTH_OTP_RESEND_LIMIT_REGISTRATION_VERIFY"
DEFAULT_AUTH_OTP_RESEND_LIMIT_REGISTRATION_VERIFY = 5
AUTH_OTP_RESEND_LIMIT_LOGIN_STEP_UP_ENV_VAR = "AUTH_OTP_RESEND_LIMIT_LOGIN_STEP_UP"
DEFAULT_AUTH_OTP_RESEND_LIMIT_LOGIN_STEP_UP = 4
AUTH_OTP_RESEND_LIMIT_RECOVERY_ENV_VAR = "AUTH_OTP_RESEND_LIMIT_RECOVERY"
DEFAULT_AUTH_OTP_RESEND_LIMIT_RECOVERY = 4
AUTH_OTP_RESEND_LIMIT_ACCOUNT_DELETION_CONFIRM_ENV_VAR = (
    "AUTH_OTP_RESEND_LIMIT_ACCOUNT_DELETION_CONFIRM"
)
DEFAULT_AUTH_OTP_RESEND_LIMIT_ACCOUNT_DELETION_CONFIRM = 3
AUTH_OTP_RESEND_LIMIT_PHONE_CHANGE_CONFIRM_ENV_VAR = "AUTH_OTP_RESEND_LIMIT_PHONE_CHANGE_CONFIRM"
DEFAULT_AUTH_OTP_RESEND_LIMIT_PHONE_CHANGE_CONFIRM = 4
AUTH_OTP_COOLDOWN_REGISTRATION_VERIFY_SECONDS_ENV_VAR = (
    "AUTH_OTP_COOLDOWN_REGISTRATION_VERIFY_SECONDS"
)
DEFAULT_AUTH_OTP_COOLDOWN_REGISTRATION_VERIFY_SECONDS = 120
AUTH_OTP_COOLDOWN_LOGIN_STEP_UP_SECONDS_ENV_VAR = "AUTH_OTP_COOLDOWN_LOGIN_STEP_UP_SECONDS"
DEFAULT_AUTH_OTP_COOLDOWN_LOGIN_STEP_UP_SECONDS = 1800
AUTH_OTP_COOLDOWN_RECOVERY_SECONDS_ENV_VAR = "AUTH_OTP_COOLDOWN_RECOVERY_SECONDS"
DEFAULT_AUTH_OTP_COOLDOWN_RECOVERY_SECONDS = 3600
AUTH_OTP_COOLDOWN_ACCOUNT_DELETION_CONFIRM_SECONDS_ENV_VAR = (
    "AUTH_OTP_COOLDOWN_ACCOUNT_DELETION_CONFIRM_SECONDS"
)
DEFAULT_AUTH_OTP_COOLDOWN_ACCOUNT_DELETION_CONFIRM_SECONDS = 3600
AUTH_OTP_COOLDOWN_PHONE_CHANGE_CONFIRM_SECONDS_ENV_VAR = (
    "AUTH_OTP_COOLDOWN_PHONE_CHANGE_CONFIRM_SECONDS"
)
DEFAULT_AUTH_OTP_COOLDOWN_PHONE_CHANGE_CONFIRM_SECONDS = 1800
AUTH_OAUTH_PROVIDER_REGISTRY_JSON_ENV_VAR = "AUTH_OAUTH_PROVIDER_REGISTRY_JSON"
DEFAULT_AUTH_OAUTH_PROVIDER_REGISTRY_JSON = "[]"
AUTH_OAUTH_ALLOWED_ISSUERS_ENV_VAR = "AUTH_OAUTH_ALLOWED_ISSUERS"
DEFAULT_AUTH_OAUTH_ALLOWED_ISSUERS: tuple[str, ...] = ()
AUTH_OAUTH_ALLOWED_REDIRECT_URIS_ENV_VAR = "AUTH_OAUTH_ALLOWED_REDIRECT_URIS"
DEFAULT_AUTH_OAUTH_ALLOWED_REDIRECT_URIS: tuple[str, ...] = ()
AUTH_OAUTH_REQUIRED_SCOPES_ENV_VAR = "AUTH_OAUTH_REQUIRED_SCOPES"
DEFAULT_AUTH_OAUTH_REQUIRED_SCOPES: tuple[str, ...] = ("openid",)
AUTH_OAUTH_STATE_TTL_SECONDS_ENV_VAR = "AUTH_OAUTH_STATE_TTL_SECONDS"
DEFAULT_AUTH_OAUTH_STATE_TTL_SECONDS = 300
AUTH_OAUTH_PROVIDER_TIMEOUT_SECONDS_ENV_VAR = "AUTH_OAUTH_PROVIDER_TIMEOUT_SECONDS"
DEFAULT_AUTH_OAUTH_PROVIDER_TIMEOUT_SECONDS = 5
AUTH_OAUTH_PROVIDER_RETRY_MAX_RETRIES_ENV_VAR = "AUTH_OAUTH_PROVIDER_RETRY_MAX_RETRIES"
DEFAULT_AUTH_OAUTH_PROVIDER_RETRY_MAX_RETRIES = 1
AUTH_OAUTH_PROVIDER_RETRY_BACKOFF_SECONDS_ENV_VAR = "AUTH_OAUTH_PROVIDER_RETRY_BACKOFF_SECONDS"
DEFAULT_AUTH_OAUTH_PROVIDER_RETRY_BACKOFF_SECONDS = 1
AUTH_OAUTH_PROVIDER_RETRY_BACKOFF_MAX_SECONDS_ENV_VAR = (
    "AUTH_OAUTH_PROVIDER_RETRY_BACKOFF_MAX_SECONDS"
)
DEFAULT_AUTH_OAUTH_PROVIDER_RETRY_BACKOFF_MAX_SECONDS = 4
AUTH_OAUTH_PROVIDER_CIRCUIT_FAILURE_THRESHOLD_ENV_VAR = (
    "AUTH_OAUTH_PROVIDER_CIRCUIT_FAILURE_THRESHOLD"
)
DEFAULT_AUTH_OAUTH_PROVIDER_CIRCUIT_FAILURE_THRESHOLD = 3
AUTH_OAUTH_PROVIDER_CIRCUIT_OPEN_SECONDS_ENV_VAR = "AUTH_OAUTH_PROVIDER_CIRCUIT_OPEN_SECONDS"
DEFAULT_AUTH_OAUTH_PROVIDER_CIRCUIT_OPEN_SECONDS = 60
AUTH_OAUTH_PROVIDER_RECOVERY_PROBE_INTERVAL_SECONDS_ENV_VAR = (
    "AUTH_OAUTH_PROVIDER_RECOVERY_PROBE_INTERVAL_SECONDS"
)
DEFAULT_AUTH_OAUTH_PROVIDER_RECOVERY_PROBE_INTERVAL_SECONDS = 30
AUTH_SECRET_RUNTIME_MODE_ENV_VAR = "AUTH_SECRET_RUNTIME_MODE"
DEFAULT_AUTH_SECRET_RUNTIME_MODE = "development"
AUTH_SESSION_SIGNING_KEY_ACTIVE_ENV_VAR = "AUTH_SESSION_SIGNING_KEY_ACTIVE"
AUTH_SESSION_SIGNING_KEY_NEXT_ENV_VAR = "AUTH_SESSION_SIGNING_KEY_NEXT"
AUTH_REFRESH_TOKEN_SECRET_ACTIVE_ENV_VAR = "AUTH_REFRESH_TOKEN_SECRET_ACTIVE"
AUTH_ENCRYPTION_KEY_ACTIVE_ENV_VAR = "AUTH_ENCRYPTION_KEY_ACTIVE"
AUTH_IDEMPOTENCY_SIGNING_SECRET_ENV_VAR = "AUTH_IDEMPOTENCY_SIGNING_SECRET"
AUTH_OTP_SMS_PROVIDER_SECRET_ENV_VAR = "AUTH_OTP_SMS_PROVIDER_SECRET"
AUTH_OTP_EMAIL_PROVIDER_SECRET_ENV_VAR = "AUTH_OTP_EMAIL_PROVIDER_SECRET"
AUTH_SECRET_ROTATION_WINDOW_START_UTC_ENV_VAR = "AUTH_SECRET_ROTATION_WINDOW_START_UTC"
AUTH_SECRET_ROTATION_WINDOW_END_UTC_ENV_VAR = "AUTH_SECRET_ROTATION_WINDOW_END_UTC"
_AUTH_SECRET_MIN_LENGTH = 32


@dataclass(frozen=True)
class AuthSecretConfig:
    """Represent deterministic auth secret baseline configuration."""

    runtime_mode: str
    session_signing_key_active: str | None
    session_signing_key_next: str | None
    refresh_token_secret_active: str | None
    encryption_key_active: str | None
    idempotency_signing_secret: str | None
    otp_sms_provider_secret: str | None
    otp_email_provider_secret: str | None
    zoho_client_secret: str | None
    zoho_refresh_token: str | None
    rotation_window_start_utc: str | None
    rotation_window_end_utc: str | None


class AuthSecretConfigError(ValueError):
    """Represent deterministic secret baseline configuration failure."""

    def __init__(
        self,
        *,
        error_code: str,
        message: str,
        reason: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.reason = reason
        self.details = details or {}


@dataclass(frozen=True)
class OtpAbusePolicy:
    """Represent deterministic purpose-scoped OTP abuse policy controls."""

    ttl_seconds: int
    max_attempts: int
    resend_min_interval_seconds: int
    resend_max_per_window: int
    resend_window_seconds: int
    cooldown_seconds: int


@dataclass(frozen=True)
class OtpChannelPolicy:
    """Represent deterministic purpose-scoped OTP channel availability."""

    enabled: bool
    channel: str


def get_email_verification_ttl_seconds() -> int:
    """Return email-verification challenge TTL in seconds."""

    return _read_positive_int(
        env_var=EMAIL_VERIFICATION_TTL_SECONDS_ENV_VAR,
        default=DEFAULT_EMAIL_VERIFICATION_TTL_SECONDS,
    )


def get_email_verification_max_attempts() -> int:
    """
    Return max allowed verification attempts per email-verification challenge.
    """

    return _read_positive_int(
        env_var=EMAIL_VERIFICATION_MAX_ATTEMPTS_ENV_VAR,
        default=DEFAULT_EMAIL_VERIFICATION_MAX_ATTEMPTS,
    )


def get_phone_verification_ttl_seconds() -> int:
    """Return phone-verification challenge TTL in seconds."""

    return _read_positive_int(
        env_var=PHONE_VERIFICATION_TTL_SECONDS_ENV_VAR,
        default=DEFAULT_PHONE_VERIFICATION_TTL_SECONDS,
    )


def get_phone_verification_max_attempts() -> int:
    """
    Return max allowed verification attempts per phone-verification challenge.
    """

    return _read_positive_int(
        env_var=PHONE_VERIFICATION_MAX_ATTEMPTS_ENV_VAR,
        default=DEFAULT_PHONE_VERIFICATION_MAX_ATTEMPTS,
    )


def get_phone_verification_resend_min_interval_seconds() -> int:
    """Return minimum seconds between phone-verification resend challenges."""

    return _read_positive_int(
        env_var=PHONE_VERIFICATION_RESEND_MIN_INTERVAL_SECONDS_ENV_VAR,
        default=DEFAULT_PHONE_VERIFICATION_RESEND_MIN_INTERVAL_SECONDS,
    )


def get_password_reset_ttl_seconds() -> int:
    """Return password-reset challenge TTL in seconds."""

    return _read_positive_int(
        env_var=PASSWORD_RESET_TTL_SECONDS_ENV_VAR,
        default=DEFAULT_PASSWORD_RESET_TTL_SECONDS,
    )


def get_password_reset_max_attempts() -> int:
    """Return max allowed verification attempts per password-reset challenge."""

    return _read_positive_int(
        env_var=PASSWORD_RESET_MAX_ATTEMPTS_ENV_VAR,
        default=DEFAULT_PASSWORD_RESET_MAX_ATTEMPTS,
    )


def get_account_deletion_cooldown_seconds() -> int:
    """Return account-deletion cooldown duration in seconds."""

    return _read_positive_int(
        env_var=ACCOUNT_DELETION_COOLDOWN_SECONDS_ENV_VAR,
        default=DEFAULT_ACCOUNT_DELETION_COOLDOWN_SECONDS,
    )


def get_auth_session_ttl_seconds() -> int:
    """Return auth session/token TTL in seconds."""

    return _read_positive_int(
        env_var=AUTH_SESSION_TTL_SECONDS_ENV_VAR,
        default=DEFAULT_AUTH_SESSION_TTL_SECONDS,
    )


def get_login_access_token_ttl_seconds() -> int:
    """Return login access-token TTL in seconds (backward-compatible alias)."""

    return get_auth_session_ttl_seconds()


def get_auth_session_inactivity_timeout_seconds() -> int:
    """Return auth session inactivity timeout in seconds."""

    return _read_positive_int(
        env_var=AUTH_SESSION_INACTIVITY_TIMEOUT_SECONDS_ENV_VAR,
        default=DEFAULT_AUTH_SESSION_INACTIVITY_TIMEOUT_SECONDS,
    )


def get_auth_session_absolute_lifetime_seconds() -> int:
    """Return auth session absolute lifetime cap in seconds."""

    return min(
        _read_positive_int(
        env_var=AUTH_SESSION_ABSOLUTE_LIFETIME_SECONDS_ENV_VAR,
        default=DEFAULT_AUTH_SESSION_ABSOLUTE_LIFETIME_SECONDS,
        ),
        MAX_AUTH_SESSION_ABSOLUTE_LIFETIME_SECONDS,
    )


def get_auth_session_warning_window_seconds() -> int:
    """Return auth session warning window threshold in seconds."""

    return _read_positive_int(
        env_var=AUTH_SESSION_WARNING_WINDOW_SECONDS_ENV_VAR,
        default=DEFAULT_AUTH_SESSION_WARNING_WINDOW_SECONDS,
    )


def get_auth_session_max_concurrent_sessions() -> int:
    """Return max active concurrent sessions allowed per user."""

    return _read_positive_int(
        env_var=AUTH_SESSION_MAX_CONCURRENT_SESSIONS_ENV_VAR,
        default=DEFAULT_AUTH_SESSION_MAX_CONCURRENT_SESSIONS,
    )


def get_auth_login_lockout_max_failed_attempts() -> int:
    """Return max failed-login attempts before lockout is activated."""

    return _read_positive_int(
        env_var=AUTH_LOGIN_LOCKOUT_MAX_FAILED_ATTEMPTS_ENV_VAR,
        default=DEFAULT_AUTH_LOGIN_LOCKOUT_MAX_FAILED_ATTEMPTS,
    )


def get_auth_login_lockout_window_seconds() -> int:
    """Return login lockout duration in seconds."""

    return _read_positive_int(
        env_var=AUTH_LOGIN_LOCKOUT_WINDOW_SECONDS_ENV_VAR,
        default=DEFAULT_AUTH_LOGIN_LOCKOUT_WINDOW_SECONDS,
    )


def get_auth_login_lockout_attempt_window_seconds() -> int:
    """Return failed-attempt rolling window duration in seconds."""

    return _read_positive_int(
        env_var=AUTH_LOGIN_LOCKOUT_ATTEMPT_WINDOW_SECONDS_ENV_VAR,
        default=DEFAULT_AUTH_LOGIN_LOCKOUT_ATTEMPT_WINDOW_SECONDS,
    )


def get_auth_slo_evaluation_window() -> str:
    """Return deterministic SLO evaluation window identifier."""

    raw_value = os.getenv(AUTH_SLO_EVALUATION_WINDOW_ENV_VAR)
    if raw_value is None:
        return DEFAULT_AUTH_SLO_EVALUATION_WINDOW
    normalized_value = raw_value.strip().lower()
    if not normalized_value:
        return DEFAULT_AUTH_SLO_EVALUATION_WINDOW
    return normalized_value


def get_auth_slo_login_success_rate_min() -> float:
    """Return login success-rate SLO minimum threshold."""

    return _read_ratio_threshold(
        env_var=AUTH_SLO_LOGIN_SUCCESS_RATE_MIN_ENV_VAR,
        default=DEFAULT_AUTH_SLO_LOGIN_SUCCESS_RATE_MIN,
    )


def get_auth_slo_otp_verify_success_rate_min() -> float:
    """Return OTP verification success-rate SLO minimum threshold."""

    return _read_ratio_threshold(
        env_var=AUTH_SLO_OTP_VERIFY_SUCCESS_RATE_MIN_ENV_VAR,
        default=DEFAULT_AUTH_SLO_OTP_VERIFY_SUCCESS_RATE_MIN,
    )


def get_auth_slo_password_reset_success_rate_min() -> float:
    """Return password-reset confirmation success-rate SLO minimum threshold."""

    return _read_ratio_threshold(
        env_var=AUTH_SLO_PASSWORD_RESET_SUCCESS_RATE_MIN_ENV_VAR,
        default=DEFAULT_AUTH_SLO_PASSWORD_RESET_SUCCESS_RATE_MIN,
    )


def get_auth_slo_latency_p95_ms_max() -> int:
    """
    Return max p95 latency threshold in milliseconds for auth endpoint SLO
        checks.
    """

    return _read_positive_int(
        env_var=AUTH_SLO_LATENCY_P95_MS_MAX_ENV_VAR,
        default=DEFAULT_AUTH_SLO_LATENCY_P95_MS_MAX,
    )


def get_auth_slo_latency_p99_ms_max() -> int:
    """
    Return max p99 latency threshold in milliseconds for auth endpoint
        SLO checks.
    """

    return _read_positive_int(
        env_var=AUTH_SLO_LATENCY_P99_MS_MAX_ENV_VAR,
        default=DEFAULT_AUTH_SLO_LATENCY_P99_MS_MAX,
    )


def get_auth_slo_abuse_lockout_spike_threshold() -> int:
    """Return lockout-spike threshold for deterministic auth abuse alerts."""

    return _read_positive_int(
        env_var=AUTH_SLO_ABUSE_LOCKOUT_SPIKE_THRESHOLD_ENV_VAR,
        default=DEFAULT_AUTH_SLO_ABUSE_LOCKOUT_SPIKE_THRESHOLD,
    )


def get_auth_slo_abuse_otp_attempt_spike_threshold() -> int:
    """
    Return OTP attempt-limit spike threshold for deterministic auth abuse
        alerts.
    """

    return _read_positive_int(
        env_var=AUTH_SLO_ABUSE_OTP_ATTEMPT_SPIKE_THRESHOLD_ENV_VAR,
        default=DEFAULT_AUTH_SLO_ABUSE_OTP_ATTEMPT_SPIKE_THRESHOLD,
    )


def get_auth_password_bcrypt_cost() -> int:
    """Return bcrypt cost factor for password hashing (minimum 12)."""

    configured_cost = _read_positive_int(
        env_var=AUTH_PASSWORD_BCRYPT_COST_ENV_VAR,
        default=DEFAULT_AUTH_PASSWORD_BCRYPT_COST,
    )
    if configured_cost < DEFAULT_AUTH_PASSWORD_BCRYPT_COST:
        return DEFAULT_AUTH_PASSWORD_BCRYPT_COST
    return configured_cost


def get_auth_password_history_depth() -> int:
    """Return password history depth for reuse-prevention checks."""

    return _read_positive_int(
        env_var=AUTH_PASSWORD_HISTORY_DEPTH_ENV_VAR,
        default=DEFAULT_AUTH_PASSWORD_HISTORY_DEPTH,
    )


def get_auth_registration_phone_otp_enabled() -> bool:
    """Return whether registration phone OTP activation is policy-enabled."""

    return _read_bool(
        env_var=AUTH_REGISTRATION_PHONE_OTP_ENABLED_ENV_VAR,
        default=DEFAULT_AUTH_REGISTRATION_PHONE_OTP_ENABLED,
    )


def get_auth_login_phone_otp_enabled() -> bool:
    """Return whether login phone OTP activation is policy-enabled."""

    return _read_bool(
        env_var=AUTH_LOGIN_PHONE_OTP_ENABLED_ENV_VAR,
        default=DEFAULT_AUTH_LOGIN_PHONE_OTP_ENABLED,
    )


def get_auth_otp_sms_email_fallback_enabled() -> bool:
    """
    Return whether SMS OTP delivery failure may fallback to email challenge
        delivery.
    """

    return _read_bool(
        env_var=AUTH_OTP_SMS_EMAIL_FALLBACK_ENABLED_ENV_VAR,
        default=DEFAULT_AUTH_OTP_SMS_EMAIL_FALLBACK_ENABLED,
    )


def get_auth_otp_fallback_allowed_purposes() -> frozenset[str]:
    """
    Return purpose set where channel fallback is explicitly permitted by
        policy.
    """

    raw_value = os.getenv(AUTH_OTP_FALLBACK_ALLOWED_PURPOSES_ENV_VAR)
    if raw_value is None:
        return frozenset(DEFAULT_AUTH_OTP_FALLBACK_ALLOWED_PURPOSES)
    normalized_value = raw_value.strip().lower()
    if not normalized_value:
        return frozenset(DEFAULT_AUTH_OTP_FALLBACK_ALLOWED_PURPOSES)
    normalized_purposes = {
        candidate.strip() for candidate in normalized_value.split(",") if candidate.strip()
    }
    if not normalized_purposes:
        return frozenset(DEFAULT_AUTH_OTP_FALLBACK_ALLOWED_PURPOSES)
    return frozenset(normalized_purposes)


def get_auth_otp_runtime_mode() -> str:
    """Return OTP runtime mode used by provider selection policy."""

    raw_value = os.getenv(AUTH_OTP_RUNTIME_MODE_ENV_VAR)
    if raw_value is None:
        return DEFAULT_AUTH_OTP_RUNTIME_MODE
    normalized_value = raw_value.strip().lower()
    if normalized_value in {"development", "test", "production"}:
        return normalized_value
    return DEFAULT_AUTH_OTP_RUNTIME_MODE


def get_auth_default_tenant_id() -> str:
    """Return the tenant assigned to newly authenticated browser sessions."""

    configured_tenant_id = os.getenv(AUTH_DEFAULT_TENANT_ID_ENV_VAR, "").strip()
    return configured_tenant_id or DEFAULT_AUTH_TENANT_ID


def get_auth_otp_sms_provider_mode() -> str:
    """Return configured SMS OTP provider mode."""

    raw_value = os.getenv(AUTH_OTP_SMS_PROVIDER_MODE_ENV_VAR)
    if raw_value is None:
        return DEFAULT_AUTH_OTP_SMS_PROVIDER_MODE
    normalized_value = raw_value.strip().lower()
    if not normalized_value:
        return DEFAULT_AUTH_OTP_SMS_PROVIDER_MODE
    return normalized_value


def get_auth_otp_email_provider_mode() -> str:
    """Return configured email OTP provider mode."""

    raw_value = os.getenv(AUTH_OTP_EMAIL_PROVIDER_MODE_ENV_VAR)
    if raw_value is None:
        return DEFAULT_AUTH_OTP_EMAIL_PROVIDER_MODE
    normalized_value = raw_value.strip().lower()
    if not normalized_value:
        return DEFAULT_AUTH_OTP_EMAIL_PROVIDER_MODE
    return normalized_value


def get_auth_otp_provider_timeout_seconds() -> int:
    """Return bounded OTP provider request timeout in seconds."""

    return _read_positive_int(
        env_var=AUTH_OTP_PROVIDER_TIMEOUT_SECONDS_ENV_VAR,
        default=DEFAULT_AUTH_OTP_PROVIDER_TIMEOUT_SECONDS,
    )


def get_auth_otp_provider_retry_max_retries() -> int:
    """Return bounded OTP provider retry count for transient failures."""

    raw_value = os.getenv(AUTH_OTP_PROVIDER_RETRY_MAX_RETRIES_ENV_VAR)
    if raw_value is None:
        return DEFAULT_AUTH_OTP_PROVIDER_RETRY_MAX_RETRIES
    normalized_value = raw_value.strip()
    if not normalized_value:
        return DEFAULT_AUTH_OTP_PROVIDER_RETRY_MAX_RETRIES
    try:
        parsed = int(normalized_value)
    except ValueError:
        return DEFAULT_AUTH_OTP_PROVIDER_RETRY_MAX_RETRIES
    if parsed < 0:
        return DEFAULT_AUTH_OTP_PROVIDER_RETRY_MAX_RETRIES
    return parsed


def get_auth_otp_provider_retry_backoff_seconds() -> int:
    """Return OTP provider retry backoff base in seconds."""

    return _read_positive_int(
        env_var=AUTH_OTP_PROVIDER_RETRY_BACKOFF_SECONDS_ENV_VAR,
        default=DEFAULT_AUTH_OTP_PROVIDER_RETRY_BACKOFF_SECONDS,
    )


def get_auth_otp_provider_retry_backoff_max_seconds() -> int:
    """Return OTP provider retry backoff max cap in seconds."""

    return _read_positive_int(
        env_var=AUTH_OTP_PROVIDER_RETRY_BACKOFF_MAX_SECONDS_ENV_VAR,
        default=DEFAULT_AUTH_OTP_PROVIDER_RETRY_BACKOFF_MAX_SECONDS,
    )


def get_auth_zoho_accounts_base_url() -> str:
    """Return configured Zoho Accounts OAuth base URL."""

    raw_value = os.getenv(AUTH_ZOHO_ACCOUNTS_BASE_URL_ENV_VAR)
    if raw_value is None:
        return DEFAULT_AUTH_ZOHO_ACCOUNTS_BASE_URL
    normalized_value = raw_value.strip()
    if not normalized_value:
        return DEFAULT_AUTH_ZOHO_ACCOUNTS_BASE_URL
    return normalized_value.rstrip("/")


def get_auth_zoho_mail_base_url() -> str:
    """Return configured Zoho Mail base URL for direct Accounts API sends."""

    raw_value = os.getenv(AUTH_ZOHO_MAIL_BASE_URL_ENV_VAR)
    if raw_value is not None:
        normalized_value = raw_value.strip()
        if normalized_value:
            return normalized_value.rstrip("/")

    legacy_api_base_url = get_auth_zoho_mail_api_base_url().rstrip("/")
    if legacy_api_base_url.endswith("/api"):
        return legacy_api_base_url[: -len("/api")]
    return legacy_api_base_url


def get_auth_zoho_mail_api_base_url() -> str:
    """Return configured Zoho Mail API base URL."""

    raw_value = os.getenv(AUTH_ZOHO_MAIL_API_BASE_URL_ENV_VAR)
    if raw_value is None:
        return DEFAULT_AUTH_ZOHO_MAIL_API_BASE_URL
    normalized_value = raw_value.strip()
    if not normalized_value:
        return DEFAULT_AUTH_ZOHO_MAIL_API_BASE_URL
    return normalized_value


def get_auth_zoho_client_id() -> str | None:
    """Return configured Zoho OAuth client identifier when present."""

    raw_value = os.getenv(AUTH_ZOHO_CLIENT_ID_ENV_VAR)
    if raw_value is None:
        return None
    normalized_value = raw_value.strip()
    if not normalized_value:
        return None
    return normalized_value


def get_auth_zoho_client_secret() -> str | None:
    """Return configured Zoho OAuth client secret when present."""

    raw_value = os.getenv(AUTH_ZOHO_CLIENT_SECRET_ENV_VAR)
    if raw_value is None:
        return None
    normalized_value = raw_value.strip()
    if not normalized_value:
        return None
    return normalized_value


def get_auth_zoho_refresh_token() -> str | None:
    """Return configured Zoho OAuth refresh token when present."""

    raw_value = os.getenv(AUTH_ZOHO_REFRESH_TOKEN_ENV_VAR)
    if raw_value is None:
        return None
    normalized_value = raw_value.strip()
    if not normalized_value:
        return None
    return normalized_value


def get_auth_zoho_account_id() -> str | None:
    """Return configured Zoho Mail account identifier when present."""

    raw_value = os.getenv(AUTH_ZOHO_ACCOUNT_ID_ENV_VAR)
    if raw_value is None:
        return None
    normalized_value = raw_value.strip()
    if not normalized_value:
        return None
    return normalized_value


def get_auth_zoho_from_address() -> str | None:
    """Return configured Zoho Mail sender address when present."""

    raw_value = os.getenv(AUTH_ZOHO_FROM_ADDRESS_ENV_VAR)
    if raw_value is None:
        return None
    normalized_value = raw_value.strip()
    if not normalized_value:
        return None
    return normalized_value


def get_auth_otp_email_recipient_override() -> str | None:
    """Return optional override recipient for OTP email deliveries."""

    raw_value = os.getenv(AUTH_OTP_EMAIL_RECIPIENT_OVERRIDE_ENV_VAR)
    if raw_value is None:
        return None
    normalized_value = raw_value.strip()
    if not normalized_value:
        return None
    return normalized_value


def get_auth_zoho_template_id_for_purpose(*, purpose: str) -> str | None:
    """Return Zoho template identifier for deterministic OTP purpose mapping."""

    env_var = {
        "registration_verify": AUTH_ZOHO_MAIL_TEMPLATE_REGISTRATION_VERIFY_ENV_VAR,
        "login_step_up": AUTH_ZOHO_MAIL_TEMPLATE_LOGIN_STEP_UP_ENV_VAR,
        "recovery": AUTH_ZOHO_MAIL_TEMPLATE_RECOVERY_ENV_VAR,
        "account_deletion_confirm": AUTH_ZOHO_MAIL_TEMPLATE_ACCOUNT_DELETION_CONFIRM_ENV_VAR,
        "phone_change_confirm": AUTH_ZOHO_MAIL_TEMPLATE_PHONE_CHANGE_CONFIRM_ENV_VAR,
    }.get(purpose.strip().lower())
    if env_var is None:
        return None
    raw_value = os.getenv(env_var)
    if raw_value is None:
        return None
    normalized_value = raw_value.strip()
    if not normalized_value:
        return None
    return normalized_value


def get_auth_africas_talking_api_base_url() -> str:
    """Return configured Africa's Talking API base URL."""

    raw_value = os.getenv(AUTH_AFRICAS_TALKING_API_BASE_URL_ENV_VAR)
    if raw_value is None:
        return DEFAULT_AUTH_AFRICAS_TALKING_API_BASE_URL
    normalized_value = raw_value.strip()
    if not normalized_value:
        return DEFAULT_AUTH_AFRICAS_TALKING_API_BASE_URL
    return normalized_value


def get_auth_africas_talking_username() -> str | None:
    """Return configured Africa's Talking username when present."""

    for env_var in (AT_USERNAME_ENV_VAR, AUTH_AFRICAS_TALKING_USERNAME_ENV_VAR):
        raw_value = os.getenv(env_var)
        if raw_value is None:
            continue
        normalized_value = raw_value.strip()
        if normalized_value:
            return normalized_value
    return None


def get_auth_africas_talking_api_key() -> str | None:
    """Return configured Africa's Talking API key when present."""

    for env_var in (AT_API_KEY_ENV_VAR, AUTH_OTP_SMS_PROVIDER_SECRET_ENV_VAR):
        raw_value = os.getenv(env_var)
        if raw_value is None:
            continue
        normalized_value = raw_value.strip()
        if normalized_value:
            return normalized_value
    return None


def get_auth_africas_talking_sender_id() -> str | None:
    """Return configured Africa's Talking sender ID when present."""

    for env_var in (
        LEGACY_SENDER_ID_ENV_VAR,
        SENDER_ID_ENV_VAR,
        AUTH_AFRICAS_TALKING_SENDER_ID_ENV_VAR,
    ):
        raw_value = os.getenv(env_var)
        if raw_value is None:
            continue
        normalized_value = raw_value.strip()
        if normalized_value:
            return normalized_value
    return None


def get_auth_otp_sms_provider_secret() -> str | None:
    """Return configured SMS provider secret when present."""

    raw_value = os.getenv(AUTH_OTP_SMS_PROVIDER_SECRET_ENV_VAR)
    if raw_value is None:
        return None
    normalized_value = raw_value.strip()
    if not normalized_value:
        return None
    return normalized_value


def get_auth_otp_email_provider_secret() -> str | None:
    """Return configured email provider secret when present."""

    raw_value = os.getenv(AUTH_OTP_EMAIL_PROVIDER_SECRET_ENV_VAR)
    if raw_value is None:
        return None
    normalized_value = raw_value.strip()
    if not normalized_value:
        return None
    return normalized_value


def get_auth_otp_resend_window_seconds() -> int:
    """Return rolling resend-window duration in seconds."""

    return _read_positive_int(
        env_var=AUTH_OTP_RESEND_WINDOW_SECONDS_ENV_VAR,
        default=DEFAULT_AUTH_OTP_RESEND_WINDOW_SECONDS,
    )


def get_auth_oauth_provider_registry_json() -> str:
    """Return configured OAuth provider-registry JSON payload."""

    raw_value = os.getenv(AUTH_OAUTH_PROVIDER_REGISTRY_JSON_ENV_VAR)
    if raw_value is None:
        return DEFAULT_AUTH_OAUTH_PROVIDER_REGISTRY_JSON
    normalized_value = raw_value.strip()
    if not normalized_value:
        return DEFAULT_AUTH_OAUTH_PROVIDER_REGISTRY_JSON
    return normalized_value


def get_auth_oauth_allowed_issuers() -> frozenset[str]:
    """Return configured OAuth issuer allowlist."""

    return _read_csv_values(
        env_var=AUTH_OAUTH_ALLOWED_ISSUERS_ENV_VAR,
        default=DEFAULT_AUTH_OAUTH_ALLOWED_ISSUERS,
    )


def get_auth_oauth_allowed_redirect_uris() -> frozenset[str]:
    """Return configured OAuth redirect-URI allowlist."""

    return _read_csv_values(
        env_var=AUTH_OAUTH_ALLOWED_REDIRECT_URIS_ENV_VAR,
        default=DEFAULT_AUTH_OAUTH_ALLOWED_REDIRECT_URIS,
    )


def get_auth_oauth_required_scopes() -> frozenset[str]:
    """Return required OAuth scopes enforced by trust policy."""

    return _read_csv_values(
        env_var=AUTH_OAUTH_REQUIRED_SCOPES_ENV_VAR,
        default=DEFAULT_AUTH_OAUTH_REQUIRED_SCOPES,
    )


def get_auth_oauth_state_ttl_seconds() -> int:
    """Return OAuth authorization-state TTL in seconds."""

    return _read_positive_int(
        env_var=AUTH_OAUTH_STATE_TTL_SECONDS_ENV_VAR,
        default=DEFAULT_AUTH_OAUTH_STATE_TTL_SECONDS,
    )


def get_auth_oauth_provider_timeout_seconds() -> int:
    """Return bounded OAuth provider request timeout in seconds."""

    return _read_positive_int(
        env_var=AUTH_OAUTH_PROVIDER_TIMEOUT_SECONDS_ENV_VAR,
        default=DEFAULT_AUTH_OAUTH_PROVIDER_TIMEOUT_SECONDS,
    )


def get_auth_oauth_provider_retry_max_retries() -> int:
    """Return bounded OAuth provider retry count for transient failures."""

    raw_value = os.getenv(AUTH_OAUTH_PROVIDER_RETRY_MAX_RETRIES_ENV_VAR)
    if raw_value is None:
        return DEFAULT_AUTH_OAUTH_PROVIDER_RETRY_MAX_RETRIES
    normalized_value = raw_value.strip()
    if not normalized_value:
        return DEFAULT_AUTH_OAUTH_PROVIDER_RETRY_MAX_RETRIES
    try:
        parsed = int(normalized_value)
    except ValueError:
        return DEFAULT_AUTH_OAUTH_PROVIDER_RETRY_MAX_RETRIES
    if parsed < 0:
        return DEFAULT_AUTH_OAUTH_PROVIDER_RETRY_MAX_RETRIES
    return parsed


def get_auth_oauth_provider_retry_backoff_seconds() -> int:
    """Return OAuth provider retry backoff base in seconds."""

    return _read_positive_int(
        env_var=AUTH_OAUTH_PROVIDER_RETRY_BACKOFF_SECONDS_ENV_VAR,
        default=DEFAULT_AUTH_OAUTH_PROVIDER_RETRY_BACKOFF_SECONDS,
    )


def get_auth_oauth_provider_retry_backoff_max_seconds() -> int:
    """Return OAuth provider retry backoff max cap in seconds."""

    return _read_positive_int(
        env_var=AUTH_OAUTH_PROVIDER_RETRY_BACKOFF_MAX_SECONDS_ENV_VAR,
        default=DEFAULT_AUTH_OAUTH_PROVIDER_RETRY_BACKOFF_MAX_SECONDS,
    )


def get_auth_oauth_provider_circuit_failure_threshold() -> int:
    """Return consecutive failure threshold for opening provider circuit."""

    return _read_positive_int(
        env_var=AUTH_OAUTH_PROVIDER_CIRCUIT_FAILURE_THRESHOLD_ENV_VAR,
        default=DEFAULT_AUTH_OAUTH_PROVIDER_CIRCUIT_FAILURE_THRESHOLD,
    )


def get_auth_oauth_provider_circuit_open_seconds() -> int:
    """Return provider circuit-open cool-down duration in seconds."""

    return _read_positive_int(
        env_var=AUTH_OAUTH_PROVIDER_CIRCUIT_OPEN_SECONDS_ENV_VAR,
        default=DEFAULT_AUTH_OAUTH_PROVIDER_CIRCUIT_OPEN_SECONDS,
    )


def get_auth_oauth_provider_recovery_probe_interval_seconds() -> int:
    """Return minimum interval between provider recovery probes in seconds."""

    return _read_positive_int(
        env_var=AUTH_OAUTH_PROVIDER_RECOVERY_PROBE_INTERVAL_SECONDS_ENV_VAR,
        default=DEFAULT_AUTH_OAUTH_PROVIDER_RECOVERY_PROBE_INTERVAL_SECONDS,
    )


def get_auth_otp_policy_for_purpose(purpose: str) -> OtpAbusePolicy:
    """Return deterministic purpose-scoped OTP abuse policy."""

    normalized_purpose = purpose.strip().lower()
    resend_window_seconds = get_auth_otp_resend_window_seconds()
    if normalized_purpose == "login_step_up":
        return OtpAbusePolicy(
            ttl_seconds=180,
            max_attempts=3,
            resend_min_interval_seconds=60,
            resend_max_per_window=_read_positive_int(
                env_var=AUTH_OTP_RESEND_LIMIT_LOGIN_STEP_UP_ENV_VAR,
                default=DEFAULT_AUTH_OTP_RESEND_LIMIT_LOGIN_STEP_UP,
            ),
            resend_window_seconds=resend_window_seconds,
            cooldown_seconds=_read_positive_int(
                env_var=AUTH_OTP_COOLDOWN_LOGIN_STEP_UP_SECONDS_ENV_VAR,
                default=DEFAULT_AUTH_OTP_COOLDOWN_LOGIN_STEP_UP_SECONDS,
            ),
        )
    if normalized_purpose == "recovery":
        return OtpAbusePolicy(
            ttl_seconds=300,
            max_attempts=4,
            resend_min_interval_seconds=90,
            resend_max_per_window=_read_positive_int(
                env_var=AUTH_OTP_RESEND_LIMIT_RECOVERY_ENV_VAR,
                default=DEFAULT_AUTH_OTP_RESEND_LIMIT_RECOVERY,
            ),
            resend_window_seconds=resend_window_seconds,
            cooldown_seconds=_read_positive_int(
                env_var=AUTH_OTP_COOLDOWN_RECOVERY_SECONDS_ENV_VAR,
                default=DEFAULT_AUTH_OTP_COOLDOWN_RECOVERY_SECONDS,
            ),
        )
    if normalized_purpose == "account_deletion_confirm":
        return OtpAbusePolicy(
            ttl_seconds=180,
            max_attempts=3,
            resend_min_interval_seconds=120,
            resend_max_per_window=_read_positive_int(
                env_var=AUTH_OTP_RESEND_LIMIT_ACCOUNT_DELETION_CONFIRM_ENV_VAR,
                default=DEFAULT_AUTH_OTP_RESEND_LIMIT_ACCOUNT_DELETION_CONFIRM,
            ),
            resend_window_seconds=resend_window_seconds,
            cooldown_seconds=_read_positive_int(
                env_var=AUTH_OTP_COOLDOWN_ACCOUNT_DELETION_CONFIRM_SECONDS_ENV_VAR,  # noqa
                default=DEFAULT_AUTH_OTP_COOLDOWN_ACCOUNT_DELETION_CONFIRM_SECONDS,  # noqa
            ),
        )
    if normalized_purpose == "phone_change_confirm":
        return OtpAbusePolicy(
            ttl_seconds=180,
            max_attempts=3,
            resend_min_interval_seconds=60,
            resend_max_per_window=_read_positive_int(
                env_var=AUTH_OTP_RESEND_LIMIT_PHONE_CHANGE_CONFIRM_ENV_VAR,
                default=DEFAULT_AUTH_OTP_RESEND_LIMIT_PHONE_CHANGE_CONFIRM,
            ),
            resend_window_seconds=resend_window_seconds,
            cooldown_seconds=_read_positive_int(
                env_var=AUTH_OTP_COOLDOWN_PHONE_CHANGE_CONFIRM_SECONDS_ENV_VAR,
                default=DEFAULT_AUTH_OTP_COOLDOWN_PHONE_CHANGE_CONFIRM_SECONDS,
            ),
        )
    return OtpAbusePolicy(
        ttl_seconds=600,
        max_attempts=3,
        resend_min_interval_seconds=120,
        resend_max_per_window=_read_positive_int(
            env_var=AUTH_OTP_RESEND_LIMIT_REGISTRATION_VERIFY_ENV_VAR,
            default=DEFAULT_AUTH_OTP_RESEND_LIMIT_REGISTRATION_VERIFY,
        ),
        resend_window_seconds=resend_window_seconds,
        cooldown_seconds=0,
    )


def get_auth_otp_channel_policy_for_purpose(purpose: str) -> OtpChannelPolicy:
    """Return deterministic purpose-scoped OTP channel availability policy."""

    normalized_purpose = purpose.strip().lower()
    if normalized_purpose == "registration_verify":
        phone_otp_enabled = get_auth_registration_phone_otp_enabled()
        return OtpChannelPolicy(
            enabled=phone_otp_enabled,
            channel="sms" if phone_otp_enabled else "email",
        )
    if normalized_purpose == "login_step_up":
        phone_otp_enabled = get_auth_login_phone_otp_enabled()
        return OtpChannelPolicy(
            enabled=phone_otp_enabled,
            channel="sms" if phone_otp_enabled else "email",
        )
    return OtpChannelPolicy(enabled=True, channel="sms")


def _read_positive_int(*, env_var: str, default: int) -> int:
    raw_value = os.getenv(env_var)
    if raw_value is None:
        return default

    normalized_value = raw_value.strip()
    if not normalized_value:
        return default
    try:
        parsed = int(normalized_value)
    except ValueError:
        return default
    if parsed <= 0:
        return default
    return parsed


def _read_ratio_threshold(*, env_var: str, default: float) -> float:
    raw_value = os.getenv(env_var)
    if raw_value is None:
        return default
    normalized_value = raw_value.strip()
    if not normalized_value:
        return default
    try:
        parsed = float(normalized_value)
    except ValueError:
        return default
    if parsed <= 0 or parsed > 1:
        return default
    return parsed


def _read_bool(*, env_var: str, default: bool) -> bool:
    raw_value = os.getenv(env_var)
    if raw_value is None:
        return default
    normalized_value = raw_value.strip().lower()
    if normalized_value in {"1", "true", "yes", "on"}:
        return True
    if normalized_value in {"0", "false", "no", "off"}:
        return False
    return default


def _read_csv_values(*, env_var: str, default: tuple[str, ...]) -> frozenset[str]:
    raw_value = os.getenv(env_var)
    if raw_value is None:
        return frozenset(default)
    normalized_value = raw_value.strip()
    if not normalized_value:
        return frozenset(default)
    values = {segment.strip() for segment in normalized_value.split(",") if segment.strip()}
    if not values:
        return frozenset(default)
    return frozenset(values)


def get_auth_secret_runtime_mode() -> str:
    """Return runtime mode used for auth secret fail-closed policy."""

    raw_value = os.getenv(AUTH_SECRET_RUNTIME_MODE_ENV_VAR)
    if raw_value is None:
        return DEFAULT_AUTH_SECRET_RUNTIME_MODE
    normalized_value = raw_value.strip().lower()
    if normalized_value in {"development", "test", "hackathon", "production"}:
        return normalized_value
    return DEFAULT_AUTH_SECRET_RUNTIME_MODE


def load_auth_secret_config_baseline() -> AuthSecretConfig:
    """Load deterministic auth secret baseline with fail-closed validation."""

    runtime_mode = get_auth_secret_runtime_mode()
    sms_provider_mode = get_auth_otp_sms_provider_mode()
    email_provider_mode = get_auth_otp_email_provider_mode()
    session_signing_key_active = _resolve_secret_value(
        env_var=AUTH_SESSION_SIGNING_KEY_ACTIVE_ENV_VAR,
        runtime_mode=runtime_mode,
    )
    session_signing_key_next = _resolve_optional_secret_value(
        env_var=AUTH_SESSION_SIGNING_KEY_NEXT_ENV_VAR,
    )
    refresh_token_secret_active = _resolve_secret_value(
        env_var=AUTH_REFRESH_TOKEN_SECRET_ACTIVE_ENV_VAR,
        runtime_mode=runtime_mode,
    )
    encryption_key_active = _resolve_secret_value(
        env_var=AUTH_ENCRYPTION_KEY_ACTIVE_ENV_VAR,
        runtime_mode=runtime_mode,
    )
    idempotency_signing_secret = _resolve_secret_value(
        env_var=AUTH_IDEMPOTENCY_SIGNING_SECRET_ENV_VAR,
        runtime_mode=runtime_mode,
    )
    if sms_provider_mode == "africas_talking":
        otp_sms_provider_secret = _resolve_secret_value_from_candidates(
            env_vars=(AT_API_KEY_ENV_VAR, AUTH_OTP_SMS_PROVIDER_SECRET_ENV_VAR),
            runtime_mode=runtime_mode,
        )
    else:
        otp_sms_provider_secret = _resolve_secret_value(
            env_var=AUTH_OTP_SMS_PROVIDER_SECRET_ENV_VAR,
            runtime_mode=runtime_mode,
        )
    otp_email_provider_secret = _resolve_optional_secret_value(
        env_var=AUTH_OTP_EMAIL_PROVIDER_SECRET_ENV_VAR,
    )
    if email_provider_mode == "zoho":
        zoho_client_secret = _resolve_secret_value(
            env_var=AUTH_ZOHO_CLIENT_SECRET_ENV_VAR,
            runtime_mode=runtime_mode,
        )
        zoho_refresh_token = _resolve_secret_value(
            env_var=AUTH_ZOHO_REFRESH_TOKEN_ENV_VAR,
            runtime_mode=runtime_mode,
        )
    else:
        zoho_client_secret = _resolve_optional_secret_value(
            env_var=AUTH_ZOHO_CLIENT_SECRET_ENV_VAR,
        )
        zoho_refresh_token = _resolve_optional_secret_value(
            env_var=AUTH_ZOHO_REFRESH_TOKEN_ENV_VAR,
        )
    (
        rotation_window_start_utc,
        rotation_window_end_utc,
    ) = _resolve_rotation_window()
    _validate_secret_rotation_policy(
        runtime_mode=runtime_mode,
        session_signing_key_active=session_signing_key_active,
        session_signing_key_next=session_signing_key_next,
        rotation_window_start_utc=rotation_window_start_utc,
        rotation_window_end_utc=rotation_window_end_utc,
    )
    return AuthSecretConfig(
        runtime_mode=runtime_mode,
        session_signing_key_active=session_signing_key_active,
        session_signing_key_next=session_signing_key_next,
        refresh_token_secret_active=refresh_token_secret_active,
        encryption_key_active=encryption_key_active,
        idempotency_signing_secret=idempotency_signing_secret,
        otp_sms_provider_secret=otp_sms_provider_secret,
        otp_email_provider_secret=otp_email_provider_secret,
        zoho_client_secret=zoho_client_secret,
        zoho_refresh_token=zoho_refresh_token,
        rotation_window_start_utc=rotation_window_start_utc,
        rotation_window_end_utc=rotation_window_end_utc,
    )


def _resolve_secret_value(*, env_var: str, runtime_mode: str) -> str | None:
    raw_value = os.getenv(env_var)
    normalized_value = "" if raw_value is None else raw_value.strip()
    if not normalized_value:
        if runtime_mode in {"hackathon", "production"}:
            raise AuthSecretConfigError(
                error_code="auth_secret_missing",
                message="Required auth secret configuration is missing.",
                reason="auth_secret_missing",
                details={"env_var": env_var},
            )
        return None
    _validate_secret_format(env_var=env_var, value=normalized_value)
    return normalized_value


def _resolve_secret_value_from_candidates(
    *,
    env_vars: tuple[str, ...],
    runtime_mode: str,
) -> str | None:
    for env_var in env_vars:
        raw_value = os.getenv(env_var)
        normalized_value = "" if raw_value is None else raw_value.strip()
        if not normalized_value:
            continue
        _validate_secret_format(env_var=env_var, value=normalized_value)
        return normalized_value
    if runtime_mode in {"hackathon", "production"}:
        raise AuthSecretConfigError(
            error_code="auth_secret_missing",
            message="Required auth secret configuration is missing.",
            reason="auth_secret_missing",
            details={"env_vars": list(env_vars)},
        )
    return None


def _resolve_optional_secret_value(*, env_var: str) -> str | None:
    raw_value = os.getenv(env_var)
    normalized_value = "" if raw_value is None else raw_value.strip()
    if not normalized_value:
        return None
    _validate_secret_format(env_var=env_var, value=normalized_value)
    return normalized_value


def _validate_secret_format(*, env_var: str, value: str) -> None:
    if len(value) < _AUTH_SECRET_MIN_LENGTH:
        raise AuthSecretConfigError(
            error_code="auth_secret_invalid_format",
            message="Auth secret configuration has invalid format.",
            reason="auth_secret_invalid_format",
            details={
                "env_var": env_var,
                "requirement": f"minimum_length_{_AUTH_SECRET_MIN_LENGTH}",
            },
        )
    if any(character.isspace() for character in value):
        raise AuthSecretConfigError(
            error_code="auth_secret_invalid_format",
            message="Auth secret configuration has invalid format.",
            reason="auth_secret_invalid_format",
            details={"env_var": env_var, "requirement": "no_whitespace"},
        )


def _resolve_rotation_window() -> tuple[str | None, str | None]:
    raw_start = os.getenv(AUTH_SECRET_ROTATION_WINDOW_START_UTC_ENV_VAR)
    raw_end = os.getenv(AUTH_SECRET_ROTATION_WINDOW_END_UTC_ENV_VAR)
    start_value = "" if raw_start is None else raw_start.strip()
    end_value = "" if raw_end is None else raw_end.strip()
    if not start_value and not end_value:
        return None, None
    if not start_value or not end_value:
        raise AuthSecretConfigError(
            error_code="auth_secret_rotation_window_invalid",
            message="Auth secret rotation window configuration is invalid.",
            reason="auth_secret_rotation_window_invalid",
            details={"requirement": "both_start_and_end_required"},
        )
    start_timestamp = _parse_utc_datetime(
        env_var=AUTH_SECRET_ROTATION_WINDOW_START_UTC_ENV_VAR,
        value=start_value,
    )
    end_timestamp = _parse_utc_datetime(
        env_var=AUTH_SECRET_ROTATION_WINDOW_END_UTC_ENV_VAR,
        value=end_value,
    )
    if end_timestamp <= start_timestamp:
        raise AuthSecretConfigError(
            error_code="auth_secret_rotation_window_invalid",
            message="Auth secret rotation window configuration is invalid.",
            reason="auth_secret_rotation_window_invalid",
            details={"requirement": "end_must_be_after_start"},
        )
    return start_value, end_value


def _parse_utc_datetime(*, env_var: str, value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as error:
        raise AuthSecretConfigError(
            error_code="auth_secret_rotation_window_invalid",
            message="Auth secret rotation window configuration is invalid.",
            reason="auth_secret_rotation_window_invalid",
            details={"env_var": env_var, "requirement": "iso8601_utc_datetime"},
        ) from error


def _validate_secret_rotation_policy(
    *,
    runtime_mode: str,
    session_signing_key_active: str | None,
    session_signing_key_next: str | None,
    rotation_window_start_utc: str | None,
    rotation_window_end_utc: str | None,
) -> None:
    if runtime_mode not in {"hackathon", "production"}:
        return
    has_rotation_window = (
        rotation_window_start_utc is not None and rotation_window_end_utc is not None
    )
    has_next_key = session_signing_key_next is not None
    if has_rotation_window and not has_next_key:
        raise AuthSecretConfigError(
            error_code="auth_secret_rotation_window_invalid",
            message="Auth secret rotation window configuration is invalid.",
            reason="auth_secret_rotation_window_invalid",
            details={"requirement": "next_signing_key_required_for_rotation_window"},
        )
    if has_next_key and not has_rotation_window:
        raise AuthSecretConfigError(
            error_code="auth_secret_rotation_window_invalid",
            message="Auth secret rotation window configuration is invalid.",
            reason="auth_secret_rotation_window_invalid",
            details={"requirement": "rotation_window_required_for_next_signing_key"},
        )
    if (
        session_signing_key_active is not None
        and session_signing_key_next is not None
        and session_signing_key_active == session_signing_key_next
    ):
        raise AuthSecretConfigError(
            error_code="auth_secret_rotation_window_invalid",
            message="Auth secret rotation window configuration is invalid.",
            reason="auth_secret_rotation_window_invalid",
            details={"requirement": "next_signing_key_must_differ_from_active"},
        )
