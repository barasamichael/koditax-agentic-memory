"""Deterministic auth SLO threshold policy and breach-evaluation tests."""

from __future__ import annotations

from pathlib import Path

from services.auth.app.observability import AuthSloMetricSnapshot
from services.auth.app.observability import AuthSloThresholdPolicy
from services.auth.app.observability import AUTH_SLO_LATENCY_BREACH
from services.auth.app.observability import AUTH_ABUSE_SPIKE_DETECTED
from services.auth.app.observability import serialize_auth_slo_alerts
from services.auth.app.observability import evaluate_auth_slo_thresholds
from services.auth.app.observability import AUTH_SLO_OTP_SUCCESS_RATE_BREACH
from services.auth.app.observability import AUTH_SLO_LOGIN_SUCCESS_RATE_BREACH
from services.auth.app.observability import AUTH_SLO_PASSWORD_RESET_SUCCESS_RATE_BREACH

POLICY_DOC_PATH = Path("docs/governance/phase-8-auth-slo-and-alert-policy.md")

REQUIRED_POLICY_SECTIONS = {
    "## service_scope",
    "## slo_catalog",
    "## signal_sources_and_metric_mapping",
    "## alert_threshold_matrix",
    "## canonical_alert_payload",
    "## deterministic_alert_codes",
    "## evaluation_window_policy",
    "## escalation_and_runbook_linkage",
    "## change_control",
}


def test_auth_slo_policy_doc_exists_and_contains_required_sections() -> None:
    assert POLICY_DOC_PATH.exists()
    content = POLICY_DOC_PATH.read_text(encoding="utf-8").lower()
    for section in REQUIRED_POLICY_SECTIONS:
        assert section in content
    assert "alert_code" in content
    assert "severity" in content
    assert "metric_name" in content
    assert "observed_value" in content
    assert "threshold_value" in content
    assert "correlation_id" in content
    assert AUTH_SLO_LOGIN_SUCCESS_RATE_BREACH in content
    assert AUTH_SLO_OTP_SUCCESS_RATE_BREACH in content
    assert AUTH_SLO_PASSWORD_RESET_SUCCESS_RATE_BREACH in content
    assert AUTH_SLO_LATENCY_BREACH in content
    assert AUTH_ABUSE_SPIKE_DETECTED in content


def test_healthy_snapshot_produces_no_slo_alerts() -> None:
    alerts = evaluate_auth_slo_thresholds(
        metrics_snapshot=AuthSloMetricSnapshot(
            login_success_total=95,
            login_failure_total=5,
            otp_verify_success_total=90,
            otp_verify_failure_total=10,
            password_reset_confirm_success_total=18,
            password_reset_confirm_failure_total=2,
            lockout_applied_total=3,
            otp_attempt_limit_exceeded_total=2,
            endpoint_latency_p95_ms={"/v1/auth/login": 300.0},
            endpoint_latency_p99_ms={"/v1/auth/login": 600.0},
        ),
        policy=AuthSloThresholdPolicy(
            evaluation_window="5m",
            login_success_rate_min=0.90,
            otp_success_rate_min=0.85,
            password_reset_success_rate_min=0.80,
            latency_p95_ms_max=500.0,
            latency_p99_ms_max=1000.0,
            abuse_lockout_spike_threshold=20,
            abuse_otp_attempt_spike_threshold=20,
        ),
    )
    assert alerts == ()


def test_threshold_breaches_emit_canonical_alert_payloads() -> None:
    alerts = evaluate_auth_slo_thresholds(
        metrics_snapshot=AuthSloMetricSnapshot(
            login_success_total=60,
            login_failure_total=40,
            otp_verify_success_total=50,
            otp_verify_failure_total=50,
            password_reset_confirm_success_total=5,
            password_reset_confirm_failure_total=5,
            lockout_applied_total=30,
            otp_attempt_limit_exceeded_total=31,
            endpoint_latency_p95_ms={"/v1/auth/login": 900.0},
            endpoint_latency_p99_ms={"/v1/auth/login": 1600.0},
            correlation_id="auth-slo-breach-corr-001",
        ),
        policy=AuthSloThresholdPolicy(
            evaluation_window="10m",
            login_success_rate_min=0.95,
            otp_success_rate_min=0.90,
            password_reset_success_rate_min=0.90,
            latency_p95_ms_max=750.0,
            latency_p99_ms_max=1500.0,
            abuse_lockout_spike_threshold=25,
            abuse_otp_attempt_spike_threshold=25,
        ),
    )
    assert alerts
    alert_codes = {alert.alert_code for alert in alerts}
    assert AUTH_SLO_LOGIN_SUCCESS_RATE_BREACH in alert_codes
    assert AUTH_SLO_OTP_SUCCESS_RATE_BREACH in alert_codes
    assert AUTH_SLO_PASSWORD_RESET_SUCCESS_RATE_BREACH in alert_codes
    assert AUTH_SLO_LATENCY_BREACH in alert_codes
    assert AUTH_ABUSE_SPIKE_DETECTED in alert_codes

    for alert in alerts:
        payload = alert.as_payload()
        assert set(payload) == {
            "alert_code",
            "severity",
            "metric_name",
            "window",
            "observed_value",
            "threshold_value",
            "reason",
            "correlation_id",
        }
        assert payload["correlation_id"] == "auth-slo-breach-corr-001"


def test_same_snapshot_and_policy_produce_byte_equivalent_alert_output() -> None:
    snapshot = AuthSloMetricSnapshot(
        login_success_total=7,
        login_failure_total=3,
        otp_verify_success_total=7,
        otp_verify_failure_total=3,
        password_reset_confirm_success_total=7,
        password_reset_confirm_failure_total=3,
        lockout_applied_total=26,
        otp_attempt_limit_exceeded_total=26,
        endpoint_latency_p95_ms={"/v1/auth/login": 900.125},
        endpoint_latency_p99_ms={"/v1/auth/login": 1700.25},
        correlation_id="auth-slo-determinism-corr-001",
    )
    policy = AuthSloThresholdPolicy(
        evaluation_window="5m",
        login_success_rate_min=0.95,
        otp_success_rate_min=0.95,
        password_reset_success_rate_min=0.95,
        latency_p95_ms_max=750.0,
        latency_p99_ms_max=1500.0,
        abuse_lockout_spike_threshold=25,
        abuse_otp_attempt_spike_threshold=25,
    )
    first = serialize_auth_slo_alerts(
        alerts=evaluate_auth_slo_thresholds(metrics_snapshot=snapshot, policy=policy)
    )
    second = serialize_auth_slo_alerts(
        alerts=evaluate_auth_slo_thresholds(metrics_snapshot=snapshot, policy=policy)
    )
    assert first == second
