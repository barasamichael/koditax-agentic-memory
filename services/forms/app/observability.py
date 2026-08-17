"""Deterministic forms metrics baseline and SLO threshold evaluation."""

from __future__ import annotations

import re
import math
from typing import Literal
from dataclasses import field
from dataclasses import dataclass
from collections.abc import Mapping
from collections.abc import Sequence

from shared.determinism.input_hash import canonical_json_dumps

MetricType = Literal["counter", "histogram"]

FORMS_GENERATION_SUCCESS_TOTAL = "forms.generation.success_total"
FORMS_GENERATION_FAILURE_TOTAL = "forms.generation.failure_total"
FORMS_GENERATION_LATENCY_MS = "forms.generation.latency_ms"
FORMS_DOWNLOAD_ISSUANCE_SUCCESS_TOTAL = "forms.download_issuance.success_total"
FORMS_DOWNLOAD_ISSUANCE_FAILURE_TOTAL = "forms.download_issuance.failure_total"
FORMS_DOWNLOAD_ISSUANCE_LATENCY_MS = "forms.download_issuance.latency_ms"
FORMS_DOWNLOAD_ACCESS_DENIED_TOTAL = "forms.download_access_denied.total"

FORMS_SLO_GENERATION_SUCCESS_RATE_BREACH = "forms_slo_generation_success_rate_breach"
FORMS_SLO_GENERATION_LATENCY_BREACH = "forms_slo_generation_latency_breach"
FORMS_SLO_DOWNLOAD_SUCCESS_RATE_BREACH = "forms_slo_download_success_rate_breach"
FORMS_SLO_DOWNLOAD_LATENCY_BREACH = "forms_slo_download_latency_breach"

ALLOWED_METRIC_DIMENSIONS: frozenset[str] = frozenset(
    {"endpoint", "status", "reason_code", "denial_class"}
)
_SENSITIVE_DIMENSION_KEYS: frozenset[str] = frozenset(
    {"password", "token", "authorization", "secret", "credential", "api_key"}
)
_SENSITIVE_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)bearer\s+[a-z0-9._-]+"),
    re.compile(r"(?i)(password|token|authorization|secret|credential|api[_-]?key)\s*[:=]"),
)

_METRIC_TYPES: dict[str, MetricType] = {
    FORMS_GENERATION_SUCCESS_TOTAL: "counter",
    FORMS_GENERATION_FAILURE_TOTAL: "counter",
    FORMS_GENERATION_LATENCY_MS: "histogram",
    FORMS_DOWNLOAD_ISSUANCE_SUCCESS_TOTAL: "counter",
    FORMS_DOWNLOAD_ISSUANCE_FAILURE_TOTAL: "counter",
    FORMS_DOWNLOAD_ISSUANCE_LATENCY_MS: "histogram",
    FORMS_DOWNLOAD_ACCESS_DENIED_TOTAL: "counter",
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


class FormsMetricsEmitter:
    """Collect deterministic metric events for forms operations."""

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
                message="Metric identifier is not part of governed forms baseline.",
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


@dataclass(frozen=True)
class FormsSloThresholdPolicy:
    """Represent deterministic forms SLO threshold policy."""

    evaluation_window: str
    generation_success_rate_min: float
    generation_latency_p95_ms_max: float
    generation_latency_p99_ms_max: float
    download_success_rate_min: float
    download_latency_p95_ms_max: float
    download_latency_p99_ms_max: float


@dataclass(frozen=True)
class FormsSloMetricSnapshot:
    """Represent deterministic metrics snapshot for forms SLO evaluation."""

    generation_success_total: int = 0
    generation_failure_total: int = 0
    download_issuance_success_total: int = 0
    download_issuance_failure_total: int = 0
    generation_latency_ms_samples: tuple[float, ...] = field(default_factory=tuple)
    download_latency_ms_samples: tuple[float, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class FormsSloAlert:
    """Represent canonical deterministic forms SLO breach payload."""

    alert_code: str
    severity: str
    metric_name: str
    window: str
    observed_value: float
    threshold_value: float
    reason: str

    def as_payload(self) -> dict[str, object]:
        """Return canonical machine-readable alert payload."""

        return {
            "alert_code": self.alert_code,
            "severity": self.severity,
            "metric_name": self.metric_name,
            "window": self.window,
            "observed_value": self.observed_value,
            "threshold_value": self.threshold_value,
            "reason": self.reason,
        }


def get_default_forms_slo_threshold_policy() -> FormsSloThresholdPolicy:
    """Return deterministic default SLO policy for forms generation/download paths."""

    return FormsSloThresholdPolicy(
        evaluation_window="30m",
        generation_success_rate_min=0.99,
        generation_latency_p95_ms_max=2500.0,
        generation_latency_p99_ms_max=4000.0,
        download_success_rate_min=0.995,
        download_latency_p95_ms_max=1000.0,
        download_latency_p99_ms_max=2000.0,
    )


def build_forms_slo_metric_snapshot_from_metric_events(
    *,
    metric_events: Sequence[MetricEvent],
) -> FormsSloMetricSnapshot:
    """Build deterministic forms SLO snapshot from emitted metric events."""

    generation_success_total = 0
    generation_failure_total = 0
    download_issuance_success_total = 0
    download_issuance_failure_total = 0
    generation_latency_ms_samples: list[float] = []
    download_latency_ms_samples: list[float] = []

    for event in metric_events:
        increment = int(event.value)
        if event.metric_id == FORMS_GENERATION_SUCCESS_TOTAL:
            generation_success_total += increment
        elif event.metric_id == FORMS_GENERATION_FAILURE_TOTAL:
            generation_failure_total += increment
        elif event.metric_id == FORMS_DOWNLOAD_ISSUANCE_SUCCESS_TOTAL:
            download_issuance_success_total += increment
        elif event.metric_id == FORMS_DOWNLOAD_ISSUANCE_FAILURE_TOTAL:
            download_issuance_failure_total += increment
        elif event.metric_id == FORMS_GENERATION_LATENCY_MS:
            generation_latency_ms_samples.append(float(event.value))
        elif event.metric_id == FORMS_DOWNLOAD_ISSUANCE_LATENCY_MS:
            download_latency_ms_samples.append(float(event.value))

    return FormsSloMetricSnapshot(
        generation_success_total=generation_success_total,
        generation_failure_total=generation_failure_total,
        download_issuance_success_total=download_issuance_success_total,
        download_issuance_failure_total=download_issuance_failure_total,
        generation_latency_ms_samples=tuple(generation_latency_ms_samples),
        download_latency_ms_samples=tuple(download_latency_ms_samples),
    )


def evaluate_forms_slo_thresholds(
    *,
    metrics_snapshot: FormsSloMetricSnapshot,
    policy: FormsSloThresholdPolicy | None = None,
) -> tuple[FormsSloAlert, ...]:
    """Evaluate deterministic forms SLO thresholds and return canonical breaches."""

    effective_policy = policy or get_default_forms_slo_threshold_policy()
    alerts: list[FormsSloAlert] = []

    _append_success_rate_alert(
        alerts=alerts,
        success_total=metrics_snapshot.generation_success_total,
        failure_total=metrics_snapshot.generation_failure_total,
        minimum_success_rate=effective_policy.generation_success_rate_min,
        alert_code=FORMS_SLO_GENERATION_SUCCESS_RATE_BREACH,
        metric_name="forms.generation.success_rate",
        reason=FORMS_SLO_GENERATION_SUCCESS_RATE_BREACH,
        severity="sev2",
        window=effective_policy.evaluation_window,
    )
    _append_success_rate_alert(
        alerts=alerts,
        success_total=metrics_snapshot.download_issuance_success_total,
        failure_total=metrics_snapshot.download_issuance_failure_total,
        minimum_success_rate=effective_policy.download_success_rate_min,
        alert_code=FORMS_SLO_DOWNLOAD_SUCCESS_RATE_BREACH,
        metric_name="forms.download_issuance.success_rate",
        reason=FORMS_SLO_DOWNLOAD_SUCCESS_RATE_BREACH,
        severity="sev2",
        window=effective_policy.evaluation_window,
    )

    _append_latency_alert(
        alerts=alerts,
        metric_name="forms.generation.latency.p95_ms",
        samples=metrics_snapshot.generation_latency_ms_samples,
        percentile=95,
        threshold_value=effective_policy.generation_latency_p95_ms_max,
        alert_code=FORMS_SLO_GENERATION_LATENCY_BREACH,
        reason=FORMS_SLO_GENERATION_LATENCY_BREACH,
        severity="sev3",
        window=effective_policy.evaluation_window,
    )
    _append_latency_alert(
        alerts=alerts,
        metric_name="forms.generation.latency.p99_ms",
        samples=metrics_snapshot.generation_latency_ms_samples,
        percentile=99,
        threshold_value=effective_policy.generation_latency_p99_ms_max,
        alert_code=FORMS_SLO_GENERATION_LATENCY_BREACH,
        reason=FORMS_SLO_GENERATION_LATENCY_BREACH,
        severity="sev3",
        window=effective_policy.evaluation_window,
    )
    _append_latency_alert(
        alerts=alerts,
        metric_name="forms.download_issuance.latency.p95_ms",
        samples=metrics_snapshot.download_latency_ms_samples,
        percentile=95,
        threshold_value=effective_policy.download_latency_p95_ms_max,
        alert_code=FORMS_SLO_DOWNLOAD_LATENCY_BREACH,
        reason=FORMS_SLO_DOWNLOAD_LATENCY_BREACH,
        severity="sev3",
        window=effective_policy.evaluation_window,
    )
    _append_latency_alert(
        alerts=alerts,
        metric_name="forms.download_issuance.latency.p99_ms",
        samples=metrics_snapshot.download_latency_ms_samples,
        percentile=99,
        threshold_value=effective_policy.download_latency_p99_ms_max,
        alert_code=FORMS_SLO_DOWNLOAD_LATENCY_BREACH,
        reason=FORMS_SLO_DOWNLOAD_LATENCY_BREACH,
        severity="sev3",
        window=effective_policy.evaluation_window,
    )

    return tuple(
        sorted(
            alerts,
            key=lambda alert: (
                alert.alert_code,
                alert.metric_name,
                alert.window,
                alert.reason,
            ),
        )
    )


def serialize_forms_slo_alerts(*, alerts: Sequence[FormsSloAlert]) -> str:
    """Serialize SLO alerts as deterministic canonical JSON."""

    return canonical_json_dumps([alert.as_payload() for alert in alerts])


def _append_success_rate_alert(
    *,
    alerts: list[FormsSloAlert],
    success_total: int,
    failure_total: int,
    minimum_success_rate: float,
    alert_code: str,
    metric_name: str,
    reason: str,
    severity: str,
    window: str,
) -> None:
    total = success_total + failure_total
    if total <= 0:
        return
    observed_success_rate = round(success_total / total, 6)
    if observed_success_rate >= minimum_success_rate:
        return
    alerts.append(
        FormsSloAlert(
            alert_code=alert_code,
            severity=severity,
            metric_name=metric_name,
            window=window,
            observed_value=observed_success_rate,
            threshold_value=minimum_success_rate,
            reason=reason,
        )
    )


def _append_latency_alert(
    *,
    alerts: list[FormsSloAlert],
    metric_name: str,
    samples: Sequence[float],
    percentile: int,
    threshold_value: float,
    alert_code: str,
    reason: str,
    severity: str,
    window: str,
) -> None:
    if not samples:
        return
    observed_value = round(_calculate_percentile(samples=samples, percentile=percentile), 3)
    if observed_value <= threshold_value:
        return
    alerts.append(
        FormsSloAlert(
            alert_code=alert_code,
            severity=severity,
            metric_name=metric_name,
            window=window,
            observed_value=observed_value,
            threshold_value=threshold_value,
            reason=reason,
        )
    )


def _calculate_percentile(*, samples: Sequence[float], percentile: int) -> float:
    ordered = sorted(float(sample) for sample in samples)
    if not ordered:
        return 0.0
    rank = max(1, math.ceil((percentile / 100) * len(ordered))) - 1
    return ordered[rank]


_DEFAULT_FORMS_METRICS_EMITTER = FormsMetricsEmitter()


def get_default_forms_metrics_emitter() -> FormsMetricsEmitter:
    """Return default deterministic forms metrics emitter."""

    return _DEFAULT_FORMS_METRICS_EMITTER


def reset_default_forms_metrics_emitter() -> None:
    """Reset default deterministic forms metrics emitter for tests."""

    _DEFAULT_FORMS_METRICS_EMITTER.reset()
