"""Deterministic auth SLO threshold evaluation and canonical alert payloads."""

from __future__ import annotations

from dataclasses import field
from dataclasses import dataclass
from collections.abc import Mapping
from collections.abc import Sequence

from services.auth.app.config import get_auth_slo_evaluation_window
from services.auth.app.config import get_auth_slo_latency_p95_ms_max
from services.auth.app.config import get_auth_slo_latency_p99_ms_max
from services.auth.app.config import get_auth_slo_login_success_rate_min
from services.auth.app.config import get_auth_slo_otp_verify_success_rate_min
from services.auth.app.config import get_auth_slo_abuse_lockout_spike_threshold
from services.auth.app.config import (
    get_auth_slo_password_reset_success_rate_min,
)
from services.auth.app.config import (
    get_auth_slo_abuse_otp_attempt_spike_threshold,
)
from services.auth.app.metrics import MetricEvent
from services.auth.app.metrics import AUTH_LOGIN_FAILURE_TOTAL
from services.auth.app.metrics import AUTH_LOGIN_SUCCESS_TOTAL
from services.auth.app.metrics import AUTH_SESSION_ISSUED_TOTAL
from services.auth.app.metrics import AUTH_LOCKOUT_APPLIED_TOTAL
from services.auth.app.metrics import AUTH_OTP_VERIFY_FAILURE_TOTAL
from services.auth.app.metrics import AUTH_OTP_VERIFY_SUCCESS_TOTAL
from services.auth.app.metrics import AUTH_REGISTRATION_FAILURE_TOTAL
from services.auth.app.metrics import AUTH_REGISTRATION_SUCCESS_TOTAL
from services.auth.app.metrics import AUTH_PASSWORD_RESET_CONFIRM_FAILURE_TOTAL
from services.auth.app.metrics import AUTH_PASSWORD_RESET_CONFIRM_SUCCESS_TOTAL
from shared.determinism.input_hash import canonical_json_dumps

AUTH_SLO_LOGIN_SUCCESS_RATE_BREACH = "auth_slo_login_success_rate_breach"
AUTH_SLO_OTP_SUCCESS_RATE_BREACH = "auth_slo_otp_success_rate_breach"
AUTH_SLO_PASSWORD_RESET_SUCCESS_RATE_BREACH = (
    "auth_slo_password_reset_success_rate_breach"
)
AUTH_SLO_LATENCY_BREACH = "auth_slo_latency_breach"
AUTH_ABUSE_SPIKE_DETECTED = "auth_abuse_spike_detected"


def _empty_latency_map() -> dict[str, float]:
    return {}


@dataclass(frozen=True)
class AuthSloThresholdPolicy:
    """Represent deterministic auth SLO and alert-threshold policy."""

    evaluation_window: str
    login_success_rate_min: float
    otp_success_rate_min: float
    password_reset_success_rate_min: float
    latency_p95_ms_max: float
    latency_p99_ms_max: float
    abuse_lockout_spike_threshold: int
    abuse_otp_attempt_spike_threshold: int


@dataclass(frozen=True)
class AuthSloMetricSnapshot:
    """Represent deterministic metric snapshot used for auth SLO evaluation."""

    registration_success_total: int = 0
    registration_failure_total: int = 0
    login_success_total: int = 0
    login_failure_total: int = 0
    otp_verify_success_total: int = 0
    otp_verify_failure_total: int = 0
    password_reset_confirm_success_total: int = 0
    password_reset_confirm_failure_total: int = 0
    session_issued_total: int = 0
    lockout_applied_total: int = 0
    otp_attempt_limit_exceeded_total: int = 0
    endpoint_latency_p95_ms: dict[str, float] = field(
        default_factory=_empty_latency_map
    )
    endpoint_latency_p99_ms: dict[str, float] = field(
        default_factory=_empty_latency_map
    )
    correlation_id: str | None = None


@dataclass(frozen=True)
class AuthSloAlert:
    """Represent canonical deterministic auth SLO alert payload."""

    alert_code: str
    severity: str
    metric_name: str
    window: str
    observed_value: float
    threshold_value: float
    reason: str
    correlation_id: str | None

    def as_payload(self) -> dict[str, object]:
        """Return machine-readable canonical alert payload."""

        return {
            "alert_code": self.alert_code,
            "severity": self.severity,
            "metric_name": self.metric_name,
            "window": self.window,
            "observed_value": self.observed_value,
            "threshold_value": self.threshold_value,
            "reason": self.reason,
            "correlation_id": self.correlation_id,
        }


def get_default_auth_slo_threshold_policy() -> AuthSloThresholdPolicy:
    """Return config-driven deterministic auth SLO threshold policy."""

    return AuthSloThresholdPolicy(
        evaluation_window=get_auth_slo_evaluation_window(),
        login_success_rate_min=get_auth_slo_login_success_rate_min(),
        otp_success_rate_min=get_auth_slo_otp_verify_success_rate_min(),
        password_reset_success_rate_min=get_auth_slo_password_reset_success_rate_min(),
        latency_p95_ms_max=float(get_auth_slo_latency_p95_ms_max()),
        latency_p99_ms_max=float(get_auth_slo_latency_p99_ms_max()),
        abuse_lockout_spike_threshold=get_auth_slo_abuse_lockout_spike_threshold(),
        abuse_otp_attempt_spike_threshold=get_auth_slo_abuse_otp_attempt_spike_threshold(),
    )


def build_auth_slo_metric_snapshot_from_metric_events(
    *,
    metric_events: Sequence[MetricEvent],
    endpoint_latency_p95_ms: Mapping[str, float] | None = None,
    endpoint_latency_p99_ms: Mapping[str, float] | None = None,
    correlation_id: str | None = None,
) -> AuthSloMetricSnapshot:
    """Build deterministic auth SLO snapshot from emitted auth metric events."""

    registration_success_total = 0
    registration_failure_total = 0
    login_success_total = 0
    login_failure_total = 0
    otp_verify_success_total = 0
    otp_verify_failure_total = 0
    password_reset_confirm_success_total = 0
    password_reset_confirm_failure_total = 0
    session_issued_total = 0
    lockout_applied_total = 0
    otp_attempt_limit_exceeded_total = 0

    for event in metric_events:
        increment = int(event.value)
        if event.metric_id == AUTH_REGISTRATION_SUCCESS_TOTAL:
            registration_success_total += increment
        elif event.metric_id == AUTH_REGISTRATION_FAILURE_TOTAL:
            registration_failure_total += increment
        elif event.metric_id == AUTH_LOGIN_SUCCESS_TOTAL:
            login_success_total += increment
        elif event.metric_id == AUTH_LOGIN_FAILURE_TOTAL:
            login_failure_total += increment
            if (
                event.dimensions.get("reason_code")
                == "login_step_up_otp_attempt_limit_exceeded"
            ):
                otp_attempt_limit_exceeded_total += increment
        elif event.metric_id == AUTH_OTP_VERIFY_SUCCESS_TOTAL:
            otp_verify_success_total += increment
        elif event.metric_id == AUTH_OTP_VERIFY_FAILURE_TOTAL:
            otp_verify_failure_total += increment
            if (
                event.dimensions.get("reason_code")
                == "otp_attempt_limit_exceeded"
            ):
                otp_attempt_limit_exceeded_total += increment
        elif event.metric_id == AUTH_PASSWORD_RESET_CONFIRM_SUCCESS_TOTAL:
            password_reset_confirm_success_total += increment
        elif event.metric_id == AUTH_PASSWORD_RESET_CONFIRM_FAILURE_TOTAL:
            password_reset_confirm_failure_total += increment
        elif event.metric_id == AUTH_SESSION_ISSUED_TOTAL:
            session_issued_total += increment
        elif event.metric_id == AUTH_LOCKOUT_APPLIED_TOTAL:
            lockout_applied_total += increment

    return AuthSloMetricSnapshot(
        registration_success_total=registration_success_total,
        registration_failure_total=registration_failure_total,
        login_success_total=login_success_total,
        login_failure_total=login_failure_total,
        otp_verify_success_total=otp_verify_success_total,
        otp_verify_failure_total=otp_verify_failure_total,
        password_reset_confirm_success_total=password_reset_confirm_success_total,
        password_reset_confirm_failure_total=password_reset_confirm_failure_total,
        session_issued_total=session_issued_total,
        lockout_applied_total=lockout_applied_total,
        otp_attempt_limit_exceeded_total=otp_attempt_limit_exceeded_total,
        endpoint_latency_p95_ms=(
            {}
            if endpoint_latency_p95_ms is None
            else {
                key: float(endpoint_latency_p95_ms[key])
                for key in sorted(endpoint_latency_p95_ms)
            }
        ),
        endpoint_latency_p99_ms=(
            {}
            if endpoint_latency_p99_ms is None
            else {
                key: float(endpoint_latency_p99_ms[key])
                for key in sorted(endpoint_latency_p99_ms)
            }
        ),
        correlation_id=correlation_id,
    )


def evaluate_auth_slo_thresholds(
    *,
    metrics_snapshot: AuthSloMetricSnapshot,
    policy: AuthSloThresholdPolicy | None = None,
    correlation_id: str | None = None,
) -> tuple[AuthSloAlert, ...]:
    """Evaluate deterministic auth SLO thresholds and return canonical alerts."""

    effective_policy = policy or get_default_auth_slo_threshold_policy()
    effective_correlation_id = (
        correlation_id
        if correlation_id is not None
        else metrics_snapshot.correlation_id
    )
    alerts: list[AuthSloAlert] = []

    _append_success_rate_alert(
        alerts=alerts,
        success_total=metrics_snapshot.login_success_total,
        failure_total=metrics_snapshot.login_failure_total,
        minimum_success_rate=effective_policy.login_success_rate_min,
        alert_code=AUTH_SLO_LOGIN_SUCCESS_RATE_BREACH,
        reason="login_success_rate_below_threshold",
        metric_name="auth.login.success_rate",
        severity="sev2",
        window=effective_policy.evaluation_window,
        correlation_id=effective_correlation_id,
    )
    _append_success_rate_alert(
        alerts=alerts,
        success_total=metrics_snapshot.otp_verify_success_total,
        failure_total=metrics_snapshot.otp_verify_failure_total,
        minimum_success_rate=effective_policy.otp_success_rate_min,
        alert_code=AUTH_SLO_OTP_SUCCESS_RATE_BREACH,
        reason="otp_verify_success_rate_below_threshold",
        metric_name="auth.otp.verify.success_rate",
        severity="sev2",
        window=effective_policy.evaluation_window,
        correlation_id=effective_correlation_id,
    )
    _append_success_rate_alert(
        alerts=alerts,
        success_total=metrics_snapshot.password_reset_confirm_success_total,
        failure_total=metrics_snapshot.password_reset_confirm_failure_total,
        minimum_success_rate=effective_policy.password_reset_success_rate_min,
        alert_code=AUTH_SLO_PASSWORD_RESET_SUCCESS_RATE_BREACH,
        reason="password_reset_confirm_success_rate_below_threshold",
        metric_name="auth.password_reset.confirm.success_rate",
        severity="sev2",
        window=effective_policy.evaluation_window,
        correlation_id=effective_correlation_id,
    )
    _append_latency_alerts(
        alerts=alerts,
        latency_values=metrics_snapshot.endpoint_latency_p95_ms,
        threshold_value=effective_policy.latency_p95_ms_max,
        percentile="p95_ms",
        window=effective_policy.evaluation_window,
        correlation_id=effective_correlation_id,
    )
    _append_latency_alerts(
        alerts=alerts,
        latency_values=metrics_snapshot.endpoint_latency_p99_ms,
        threshold_value=effective_policy.latency_p99_ms_max,
        percentile="p99_ms",
        window=effective_policy.evaluation_window,
        correlation_id=effective_correlation_id,
    )

    if (
        metrics_snapshot.lockout_applied_total
        >= effective_policy.abuse_lockout_spike_threshold
    ):
        alerts.append(
            AuthSloAlert(
                alert_code=AUTH_ABUSE_SPIKE_DETECTED,
                severity="sev2",
                metric_name="auth.lockout.applied_total",
                window=effective_policy.evaluation_window,
                observed_value=float(metrics_snapshot.lockout_applied_total),
                threshold_value=float(
                    effective_policy.abuse_lockout_spike_threshold
                ),
                reason="lockout_spike_threshold_exceeded",
                correlation_id=effective_correlation_id,
            )
        )
    if (
        metrics_snapshot.otp_attempt_limit_exceeded_total
        >= effective_policy.abuse_otp_attempt_spike_threshold
    ):
        alerts.append(
            AuthSloAlert(
                alert_code=AUTH_ABUSE_SPIKE_DETECTED,
                severity="sev2",
                metric_name="auth.otp.attempt_limit_exceeded_total",
                window=effective_policy.evaluation_window,
                observed_value=float(
                    metrics_snapshot.otp_attempt_limit_exceeded_total
                ),
                threshold_value=float(
                    effective_policy.abuse_otp_attempt_spike_threshold
                ),
                reason="otp_attempt_limit_spike_threshold_exceeded",
                correlation_id=effective_correlation_id,
            )
        )

    return tuple(
        sorted(
            alerts,
            key=lambda alert: (
                alert.alert_code,
                alert.metric_name,
                alert.reason,
                alert.window,
            ),
        )
    )


def serialize_auth_slo_alerts(*, alerts: Sequence[AuthSloAlert]) -> str:
    """Serialize alerts using deterministic canonical JSON encoding."""

    return canonical_json_dumps([alert.as_payload() for alert in alerts])


def _append_success_rate_alert(
    *,
    alerts: list[AuthSloAlert],
    success_total: int,
    failure_total: int,
    minimum_success_rate: float,
    alert_code: str,
    reason: str,
    metric_name: str,
    severity: str,
    window: str,
    correlation_id: str | None,
) -> None:
    total = success_total + failure_total
    if total <= 0:
        return
    observed_success_rate = round(success_total / total, 6)
    if observed_success_rate >= minimum_success_rate:
        return
    alerts.append(
        AuthSloAlert(
            alert_code=alert_code,
            severity=severity,
            metric_name=metric_name,
            window=window,
            observed_value=observed_success_rate,
            threshold_value=minimum_success_rate,
            reason=reason,
            correlation_id=correlation_id,
        )
    )


def _append_latency_alerts(
    *,
    alerts: list[AuthSloAlert],
    latency_values: Mapping[str, float],
    threshold_value: float,
    percentile: str,
    window: str,
    correlation_id: str | None,
) -> None:
    for endpoint in sorted(latency_values):
        observed_value = round(float(latency_values[endpoint]), 3)
        if observed_value <= threshold_value:
            continue
        alerts.append(
            AuthSloAlert(
                alert_code=AUTH_SLO_LATENCY_BREACH,
                severity="sev3",
                metric_name=f"auth.endpoint.latency.{percentile}:{endpoint}",
                window=window,
                observed_value=observed_value,
                threshold_value=threshold_value,
                reason=f"{percentile}_latency_exceeds_threshold",
                correlation_id=correlation_id,
            )
        )
