"""Centralized deterministic metrics emission for document_ai operations."""

from __future__ import annotations

import re
from typing import Literal
from dataclasses import dataclass
from collections.abc import Mapping

MetricType = Literal["counter", "histogram", "gauge"]

DOCUMENT_INGESTION_REQUESTS_TOTAL = "document_ingestion_requests_total"
DOCUMENT_INGESTION_FAILURES_TOTAL = "document_ingestion_failures_total"
DOCUMENT_OUTBOX_PUBLICATIONS_TOTAL = "document_outbox_publications_total"
DOCUMENT_OUTBOX_PUBLICATION_FAILURES_TOTAL = "document_outbox_publication_failures_total"
DOCUMENT_PROCESSING_RETRIES_TOTAL = "document_processing_retries_total"
DOCUMENT_PROCESSING_DEAD_LETTERS_TOTAL = "document_processing_dead_letters_total"

ALLOWED_METRIC_DIMENSIONS: frozenset[str] = frozenset(
    {
        "action",
        "status",
        "reason_code",
        "lane_scope",
    }
)
_SENSITIVE_DIMENSION_KEYS: frozenset[str] = frozenset(
    {
        "token",
        "signature",
        "authorization",
        "secret",
        "password",
        "api_key",
        "object_key",
    }
)
_SENSITIVE_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)bearer\s+[a-z0-9._-]+"),
    re.compile(r"(?i)(authorization|token|secret|signature|api[_-]?key|password)\s*[:=]"),
    re.compile(r"(?i)x-amz-signature="),
)
_METRIC_TYPES: dict[str, MetricType] = {
    DOCUMENT_INGESTION_REQUESTS_TOTAL: "counter",
    DOCUMENT_INGESTION_FAILURES_TOTAL: "counter",
    DOCUMENT_OUTBOX_PUBLICATIONS_TOTAL: "counter",
    DOCUMENT_OUTBOX_PUBLICATION_FAILURES_TOTAL: "counter",
    DOCUMENT_PROCESSING_RETRIES_TOTAL: "counter",
    DOCUMENT_PROCESSING_DEAD_LETTERS_TOTAL: "counter",
}


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


class DocumentAIMetricsEmitter:
    """Collect deterministic metric events for document_ai operations."""

    def __init__(self) -> None:
        self._events: list[MetricEvent] = []

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

    def set_gauge(
        self,
        metric_id: str,
        *,
        value: int | float,
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
        except MetricsPolicyError:
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
        except MetricsPolicyError:
            return

    def set_gauge_non_blocking(
        self,
        metric_id: str,
        *,
        value: int | float,
        dimensions: Mapping[str, str] | None = None,
    ) -> None:
        try:
            self.set_gauge(metric_id=metric_id, value=value, dimensions=dimensions)
        except MetricsPolicyError:
            return

    def snapshot(self) -> tuple[MetricEvent, ...]:
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
            raise MetricsPolicyError(
                reason="unknown_metric_id",
                message="Metric identifier is not part of governed document_ai baseline.",
            )
        normalized_dimensions = _normalize_dimensions(dimensions=dimensions)
        self._events.append(
            MetricEvent(
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
            raise MetricsPolicyError(
                reason="sensitive_dimension_key",
                message="Sensitive metric dimension keys are not allowed.",
            )
        if key not in ALLOWED_METRIC_DIMENSIONS:
            raise MetricsPolicyError(
                reason="unsupported_dimension_key",
                message="Metric dimension key is not part of governed baseline dimensions.",
            )
        value = str(raw_value)
        if _is_sensitive_dimension_value(value=value):
            raise MetricsPolicyError(
                reason="sensitive_dimension_value",
                message="Sensitive metric dimension values are not allowed.",
            )
        normalized[key] = value
    return {key: normalized[key] for key in sorted(normalized)}


def _is_sensitive_dimension_value(*, value: str) -> bool:
    for pattern in _SENSITIVE_VALUE_PATTERNS:
        if pattern.search(value) is not None:
            return True
    return False


_DEFAULT_METRICS_EMITTER = DocumentAIMetricsEmitter()


def get_default_metrics_emitter() -> DocumentAIMetricsEmitter:
    """Return default deterministic metrics emitter."""

    return _DEFAULT_METRICS_EMITTER


def reset_default_metrics_emitter() -> None:
    """Reset default deterministic metrics emitter for tests."""

    _DEFAULT_METRICS_EMITTER.reset()
