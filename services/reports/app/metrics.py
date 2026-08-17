"""Deterministic reports metrics baseline for lifecycle operations."""

from __future__ import annotations

import re
from typing import Literal
from dataclasses import dataclass
from collections.abc import Mapping

MetricType = Literal["counter", "histogram"]

REPORTS_GENERATION_LATENCY_MS = "reports_generation_latency_ms"
REPORTS_GENERATION_TOTAL = "reports_generation_total"
REPORTS_GENERATION_FAILURES_TOTAL = "reports_generation_failures_total"
REPORTS_DOWNLOAD_LINK_ISSUED_TOTAL = "reports_download_link_issued_total"
REPORTS_DOWNLOAD_EXPIRY_REJECT_TOTAL = "reports_download_expiry_reject_total"

ALLOWED_METRIC_DIMENSIONS: frozenset[str] = frozenset(
    {
        "event_type",
        "status",
        "reason_code",
        "supported_lane_id",
        "historical_version_id",
    }
)
_SENSITIVE_DIMENSION_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "token",
        "authorization",
        "secret",
        "credential",
        "api_key",
        "email",
        "phone",
        "ssn",
    }
)
_SENSITIVE_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)bearer\s+[a-z0-9._-]+"),
    re.compile(r"(?i)(password|token|authorization|secret|credential|api[_-]?key)\s*[:=]"),
    re.compile(r"(?i)[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}"),
)

_METRIC_TYPES: dict[str, MetricType] = {
    REPORTS_GENERATION_LATENCY_MS: "histogram",
    REPORTS_GENERATION_TOTAL: "counter",
    REPORTS_GENERATION_FAILURES_TOTAL: "counter",
    REPORTS_DOWNLOAD_LINK_ISSUED_TOTAL: "counter",
    REPORTS_DOWNLOAD_EXPIRY_REJECT_TOTAL: "counter",
}


class ReportsMetricsPolicyError(ValueError):
    """Represent deterministic reports metrics-policy validation failures."""

    def __init__(self, *, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class ReportMetricEvent:
    """Represent one deterministic emitted report metric event."""

    metric_id: str
    metric_type: MetricType
    value: float
    dimensions: dict[str, str]


class ReportsMetricsEmitter:
    """Collect deterministic metric events for reports operations."""

    def __init__(self) -> None:
        self._events: list[ReportMetricEvent] = []

    def increment_counter(
        self,
        metric_id: str,
        *,
        value: int = 1,
        dimensions: Mapping[str, str] | None = None,
    ) -> None:
        self._emit(metric_id=metric_id, value=float(value), dimensions=dimensions)

    def observe_histogram(
        self,
        metric_id: str,
        *,
        value: float,
        dimensions: Mapping[str, str] | None = None,
    ) -> None:
        self._emit(metric_id=metric_id, value=float(value), dimensions=dimensions)

    def increment_counter_non_blocking(
        self,
        metric_id: str,
        *,
        value: int = 1,
        dimensions: Mapping[str, str] | None = None,
    ) -> None:
        try:
            self.increment_counter(metric_id=metric_id, value=value, dimensions=dimensions)
        except Exception:
            return

    def observe_histogram_non_blocking(
        self,
        metric_id: str,
        *,
        value: float,
        dimensions: Mapping[str, str] | None = None,
    ) -> None:
        try:
            self.observe_histogram(metric_id=metric_id, value=value, dimensions=dimensions)
        except Exception:
            return

    def snapshot(self) -> tuple[ReportMetricEvent, ...]:
        """Return deterministic immutable view of emitted metric events."""

        return tuple(self._events)

    def reset(self) -> None:
        """Clear all emitted metric events."""

        self._events.clear()

    def _emit(
        self,
        *,
        metric_id: str,
        value: float,
        dimensions: Mapping[str, str] | None,
    ) -> None:
        metric_type = _METRIC_TYPES.get(metric_id)
        if metric_type is None:
            raise ReportsMetricsPolicyError(
                reason="unknown_metric_id",
                message="Metric identifier is not part of governed reports baseline.",
            )
        normalized_dimensions = _normalize_dimensions(dimensions=dimensions)
        self._events.append(
            ReportMetricEvent(
                metric_id=metric_id,
                metric_type=metric_type,
                value=value,
                dimensions=normalized_dimensions,
            )
        )


def _normalize_dimensions(*, dimensions: Mapping[str, str] | None) -> dict[str, str]:
    if dimensions is None:
        return {}
    normalized: dict[str, str] = {}
    for key, raw_value in dimensions.items():
        if key in _SENSITIVE_DIMENSION_KEYS:
            raise ReportsMetricsPolicyError(
                reason="sensitive_dimension_key",
                message="Sensitive metric dimension keys are not allowed.",
            )
        if key not in ALLOWED_METRIC_DIMENSIONS:
            raise ReportsMetricsPolicyError(
                reason="unsupported_dimension_key",
                message="Metric dimension key is not part of governed baseline dimensions.",
            )
        value = str(raw_value).strip()
        if not value:
            raise ReportsMetricsPolicyError(
                reason="invalid_dimension_value",
                message="Metric dimension values must be non-empty strings.",
            )
        if _is_sensitive_dimension_value(value=value):
            raise ReportsMetricsPolicyError(
                reason="sensitive_dimension_value",
                message="Sensitive metric dimension values are not allowed.",
            )
        normalized[key] = value
    return {key: normalized[key] for key in sorted(normalized)}


def _is_sensitive_dimension_value(*, value: str) -> bool:
    return any(pattern.search(value) is not None for pattern in _SENSITIVE_VALUE_PATTERNS)


_DEFAULT_REPORTS_METRICS_EMITTER = ReportsMetricsEmitter()


def get_default_reports_metrics_emitter() -> ReportsMetricsEmitter:
    """Return default deterministic reports metrics emitter."""

    return _DEFAULT_REPORTS_METRICS_EMITTER


def reset_default_reports_metrics_emitter() -> None:
    """Reset default deterministic reports metrics emitter for tests."""

    _DEFAULT_REPORTS_METRICS_EMITTER.reset()
