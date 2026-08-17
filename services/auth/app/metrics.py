"""Centralized deterministic metrics emission for auth operations."""

from __future__ import annotations

import re
from typing import Literal
import logging
from dataclasses import dataclass
from collections.abc import Mapping

from shared.determinism.input_hash import canonical_json_dumps

MetricType = Literal["counter"]

AUTH_LOGIN_SUCCESS_TOTAL = "auth.login.success_total"
AUTH_LOGIN_FAILURE_TOTAL = "auth.login.failure_total"
AUTH_REGISTRATION_SUCCESS_TOTAL = "auth.registration.success_total"
AUTH_REGISTRATION_FAILURE_TOTAL = "auth.registration.failure_total"
AUTH_OTP_CHALLENGE_ISSUED_TOTAL = "auth.otp.challenge_issued_total"
AUTH_OTP_VERIFY_SUCCESS_TOTAL = "auth.otp.verify_success_total"
AUTH_OTP_VERIFY_FAILURE_TOTAL = "auth.otp.verify_failure_total"
AUTH_LOCKOUT_APPLIED_TOTAL = "auth.lockout.applied_total"
AUTH_PASSWORD_RESET_CONFIRM_SUCCESS_TOTAL = "auth.password_reset.confirm_success_total"
AUTH_PASSWORD_RESET_CONFIRM_FAILURE_TOTAL = "auth.password_reset.confirm_failure_total"
AUTH_SESSION_ISSUED_TOTAL = "auth.session.issued_total"
AUTH_SESSION_REFRESH_SUCCESS_TOTAL = "auth.session.refresh_success_total"
AUTH_SESSION_REFRESH_FAILURE_TOTAL = "auth.session.refresh_failure_total"
AUTH_OAUTH_FAILURE_TOTAL = "auth.oauth.failure_total"
AUTH_PERSISTENCE_TRANSACTION_SUCCESS_TOTAL = "auth.persistence.transaction_success_total"
AUTH_PERSISTENCE_TRANSACTION_RETRY_TOTAL = "auth.persistence.transaction_retry_total"
AUTH_PERSISTENCE_TRANSACTION_FAILURE_TOTAL = "auth.persistence.transaction_failure_total"
AUTH_PERSISTENCE_TRANSACTION_AMBIGUOUS_TOTAL = "auth.persistence.transaction_ambiguous_total"

ALLOWED_METRIC_DIMENSIONS: frozenset[str] = frozenset(
    {
        "reason_code",
        "channel",
        "purpose",
        "provider",
    }
)
_SENSITIVE_DIMENSION_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "otp",
        "token",
        "secret",
        "credential",
        "authorization",
    }
)
_SENSITIVE_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)bearer\s+[a-z0-9._-]+"),
    re.compile(r"(?i)(password|otp|token|secret|credential|authorization)\s*[:=]"),
)

_METRIC_TYPES: dict[str, MetricType] = {
    AUTH_LOGIN_SUCCESS_TOTAL: "counter",
    AUTH_LOGIN_FAILURE_TOTAL: "counter",
    AUTH_REGISTRATION_SUCCESS_TOTAL: "counter",
    AUTH_REGISTRATION_FAILURE_TOTAL: "counter",
    AUTH_OTP_CHALLENGE_ISSUED_TOTAL: "counter",
    AUTH_OTP_VERIFY_SUCCESS_TOTAL: "counter",
    AUTH_OTP_VERIFY_FAILURE_TOTAL: "counter",
    AUTH_LOCKOUT_APPLIED_TOTAL: "counter",
    AUTH_PASSWORD_RESET_CONFIRM_SUCCESS_TOTAL: "counter",
    AUTH_PASSWORD_RESET_CONFIRM_FAILURE_TOTAL: "counter",
    AUTH_SESSION_ISSUED_TOTAL: "counter",
    AUTH_SESSION_REFRESH_SUCCESS_TOTAL: "counter",
    AUTH_SESSION_REFRESH_FAILURE_TOTAL: "counter",
    AUTH_OAUTH_FAILURE_TOTAL: "counter",
    AUTH_PERSISTENCE_TRANSACTION_SUCCESS_TOTAL: "counter",
    AUTH_PERSISTENCE_TRANSACTION_RETRY_TOTAL: "counter",
    AUTH_PERSISTENCE_TRANSACTION_FAILURE_TOTAL: "counter",
    AUTH_PERSISTENCE_TRANSACTION_AMBIGUOUS_TOTAL: "counter",
}

LOGGER = logging.getLogger("auth.metrics")


class MetricsPolicyError(ValueError):
    """Represent deterministic metrics-policy validation failures."""

    def __init__(self, *, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class MetricEvent:
    """Represent one deterministic emitted metric event."""

    metric_id: str
    metric_type: MetricType
    value: float
    dimensions: dict[str, str]


class AuthMetricsEmitter:
    """Collect deterministic metric events for auth operations."""

    def __init__(self) -> None:
        self._events: list[MetricEvent] = []

    def increment_counter(
        self,
        metric_id: str,
        *,
        value: int = 1,
        dimensions: Mapping[str, str] | None = None,
    ) -> None:
        metric_type = _METRIC_TYPES.get(metric_id)
        if metric_type is None:
            raise MetricsPolicyError(
                reason="unknown_metric_id",
                message="Metric identifier is not part of governed auth baseline.",
            )
        normalized_dimensions = _normalize_dimensions(dimensions=dimensions)
        self._events.append(
            MetricEvent(
                metric_id=metric_id,
                metric_type=metric_type,
                value=float(value),
                dimensions=normalized_dimensions,
            )
        )

    def increment_counter_non_blocking(
        self,
        metric_id: str,
        *,
        value: int = 1,
        dimensions: Mapping[str, str] | None = None,
    ) -> None:
        try:
            self.increment_counter(metric_id=metric_id, value=value, dimensions=dimensions)
        except MetricsPolicyError as error:
            _emit_metrics_policy_warning(metric_id=metric_id, reason=error.reason)
            return

    def snapshot(self) -> tuple[MetricEvent, ...]:
        """Return deterministic immutable view of emitted metric events."""

        return tuple(self._events)

    def reset(self) -> None:
        """Clear all emitted metric events."""

        self._events.clear()


def _normalize_dimensions(*, dimensions: Mapping[str, str] | None) -> dict[str, str]:
    if dimensions is None:
        return {}
    normalized: dict[str, str] = {}
    for key, raw_value in dimensions.items():
        if key in _SENSITIVE_DIMENSION_KEYS:
            raise MetricsPolicyError(
                reason="sensitive_dimension_key",
                message="Sensitive metric dimension keys are not allowed.",
            )
        if key not in ALLOWED_METRIC_DIMENSIONS:
            raise MetricsPolicyError(
                reason="unsupported_dimension_key",
                message="Metric dimension key is not part of governed baseline dimensions.",
            )
        value = str(raw_value).strip()
        if not value:
            raise MetricsPolicyError(
                reason="invalid_dimension_value",
                message="Metric dimension values must be non-empty strings.",
            )
        if _is_sensitive_dimension_value(value=value):
            raise MetricsPolicyError(
                reason="sensitive_dimension_value",
                message="Sensitive metric dimension values are not allowed.",
            )
        normalized[key] = value
    return {key: normalized[key] for key in sorted(normalized)}


def _is_sensitive_dimension_value(*, value: str) -> bool:
    return any(pattern.search(value) is not None for pattern in _SENSITIVE_VALUE_PATTERNS)


_DEFAULT_METRICS_EMITTER = AuthMetricsEmitter()


def get_default_auth_metrics_emitter() -> AuthMetricsEmitter:
    """Return default deterministic auth metrics emitter."""

    return _DEFAULT_METRICS_EMITTER


def reset_default_auth_metrics_emitter() -> None:
    """Reset default deterministic auth metrics emitter for tests."""

    _DEFAULT_METRICS_EMITTER.reset()


def _emit_metrics_policy_warning(*, metric_id: str, reason: str) -> None:
    warning_event = {
        "metric_id": metric_id,
        "reason": reason,
        "event_type": "auth.metrics.emission",
        "event_status": "warning",
    }
    try:
        LOGGER.warning(canonical_json_dumps(warning_event))
    except Exception:
        return
