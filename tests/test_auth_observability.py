"""Auth observability evaluator and runtime-policy wiring regression tests."""

from __future__ import annotations

from services.auth.app.main import create_app
from services.auth.app.main import evaluate_auth_slo_alerts
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
from services.auth.app.observability import AuthSloMetricSnapshot
from services.auth.app.observability import AuthSloThresholdPolicy
from services.auth.app.observability import AUTH_ABUSE_SPIKE_DETECTED
from services.auth.app.observability import AUTH_SLO_LOGIN_SUCCESS_RATE_BREACH
from services.auth.app.observability import build_auth_slo_metric_snapshot_from_metric_events


def test_snapshot_builder_maps_metric_events_to_slo_snapshot_totals() -> None:
    snapshot = build_auth_slo_metric_snapshot_from_metric_events(
        metric_events=(
            MetricEvent(
                metric_id=AUTH_REGISTRATION_SUCCESS_TOTAL,
                metric_type="counter",
                value=3,
                dimensions={},
            ),
            MetricEvent(
                metric_id=AUTH_REGISTRATION_FAILURE_TOTAL,
                metric_type="counter",
                value=2,
                dimensions={"reason_code": "registration_invalid_kra_pin"},
            ),
            MetricEvent(
                metric_id=AUTH_LOGIN_SUCCESS_TOTAL,
                metric_type="counter",
                value=4,
                dimensions={},
            ),
            MetricEvent(
                metric_id=AUTH_LOGIN_FAILURE_TOTAL,
                metric_type="counter",
                value=1,
                dimensions={"reason_code": "login_invalid_credentials"},
            ),
            MetricEvent(
                metric_id=AUTH_OTP_VERIFY_SUCCESS_TOTAL,
                metric_type="counter",
                value=6,
                dimensions={"channel": "sms", "purpose": "login_step_up"},
            ),
            MetricEvent(
                metric_id=AUTH_OTP_VERIFY_FAILURE_TOTAL,
                metric_type="counter",
                value=7,
                dimensions={"reason_code": "otp_attempt_limit_exceeded"},
            ),
            MetricEvent(
                metric_id=AUTH_PASSWORD_RESET_CONFIRM_SUCCESS_TOTAL,
                metric_type="counter",
                value=5,
                dimensions={},
            ),
            MetricEvent(
                metric_id=AUTH_PASSWORD_RESET_CONFIRM_FAILURE_TOTAL,
                metric_type="counter",
                value=2,
                dimensions={"reason_code": "otp_expired"},
            ),
            MetricEvent(
                metric_id=AUTH_LOCKOUT_APPLIED_TOTAL,
                metric_type="counter",
                value=8,
                dimensions={"reason_code": "login_lockout_active"},
            ),
            MetricEvent(
                metric_id=AUTH_SESSION_ISSUED_TOTAL,
                metric_type="counter",
                value=4,
                dimensions={},
            ),
        ),
        endpoint_latency_p95_ms={"/v1/auth/login": 410.0},
        endpoint_latency_p99_ms={"/v1/auth/login": 790.0},
        correlation_id="auth-observability-corr-001",
    )
    assert snapshot.registration_success_total == 3
    assert snapshot.registration_failure_total == 2
    assert snapshot.login_success_total == 4
    assert snapshot.login_failure_total == 1
    assert snapshot.otp_verify_success_total == 6
    assert snapshot.otp_verify_failure_total == 7
    assert snapshot.password_reset_confirm_success_total == 5
    assert snapshot.password_reset_confirm_failure_total == 2
    assert snapshot.lockout_applied_total == 8
    assert snapshot.otp_attempt_limit_exceeded_total == 7
    assert snapshot.session_issued_total == 4
    assert snapshot.endpoint_latency_p95_ms == {"/v1/auth/login": 410.0}
    assert snapshot.endpoint_latency_p99_ms == {"/v1/auth/login": 790.0}
    assert snapshot.correlation_id == "auth-observability-corr-001"


def test_create_app_wires_default_slo_policy_and_helper_evaluation() -> None:
    app = create_app()
    assert hasattr(app.state, "auth_slo_threshold_policy")
    assert app.state.auth_slo_threshold_policy.evaluation_window

    app.state.auth_slo_threshold_policy = AuthSloThresholdPolicy(
        evaluation_window="5m",
        login_success_rate_min=0.99,
        otp_success_rate_min=0.99,
        password_reset_success_rate_min=0.99,
        latency_p95_ms_max=500.0,
        latency_p99_ms_max=1000.0,
        abuse_lockout_spike_threshold=10,
        abuse_otp_attempt_spike_threshold=10,
    )
    alerts = evaluate_auth_slo_alerts(
        app_instance=app,
        metrics_snapshot=AuthSloMetricSnapshot(
            login_success_total=50,
            login_failure_total=50,
            otp_verify_success_total=99,
            otp_verify_failure_total=1,
            password_reset_confirm_success_total=99,
            password_reset_confirm_failure_total=1,
            lockout_applied_total=11,
            otp_attempt_limit_exceeded_total=0,
            endpoint_latency_p95_ms={"/v1/auth/login": 120.0},
            endpoint_latency_p99_ms={"/v1/auth/login": 250.0},
            correlation_id="auth-observability-corr-002",
        ),
    )
    alert_codes = {alert.alert_code for alert in alerts}
    assert AUTH_SLO_LOGIN_SUCCESS_RATE_BREACH in alert_codes
    assert AUTH_ABUSE_SPIKE_DETECTED in alert_codes
