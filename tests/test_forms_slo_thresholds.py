"""Deterministic forms SLO threshold policy and breach-evaluation tests."""

from __future__ import annotations

from pathlib import Path

from services.forms.app.main import create_app
from services.forms.app.main import evaluate_forms_slo_alerts
from services.forms.app.observability import FormsSloMetricSnapshot
from services.forms.app.observability import FormsSloThresholdPolicy
from services.forms.app.observability import serialize_forms_slo_alerts
from services.forms.app.observability import evaluate_forms_slo_thresholds
from services.forms.app.observability import FORMS_SLO_DOWNLOAD_LATENCY_BREACH
from services.forms.app.observability import FORMS_SLO_GENERATION_LATENCY_BREACH
from services.forms.app.observability import FORMS_SLO_DOWNLOAD_SUCCESS_RATE_BREACH
from services.forms.app.observability import FORMS_SLO_GENERATION_SUCCESS_RATE_BREACH

POLICY_DOC_PATH = Path("docs/governance/phase-10-forms-slo-and-metrics-policy.md")

REQUIRED_POLICY_SECTIONS = {
    "## service_scope",
    "## metric_catalog",
    "## slo_catalog",
    "## breach_window_policy",
    "## canonical_breach_payload",
    "## deterministic_reason_codes",
}


def test_forms_slo_policy_doc_exists_and_contains_required_sections() -> None:
    assert POLICY_DOC_PATH.exists()
    content = POLICY_DOC_PATH.read_text(encoding="utf-8").lower()
    for section in REQUIRED_POLICY_SECTIONS:
        assert section in content
    assert "alert_code" in content
    assert "severity" in content
    assert "metric_name" in content
    assert "window" in content
    assert "observed_value" in content
    assert "threshold_value" in content
    assert "reason" in content
    assert FORMS_SLO_GENERATION_SUCCESS_RATE_BREACH in content
    assert FORMS_SLO_GENERATION_LATENCY_BREACH in content
    assert FORMS_SLO_DOWNLOAD_SUCCESS_RATE_BREACH in content
    assert FORMS_SLO_DOWNLOAD_LATENCY_BREACH in content


def test_healthy_snapshot_produces_no_forms_slo_alerts() -> None:
    alerts = evaluate_forms_slo_thresholds(
        metrics_snapshot=FormsSloMetricSnapshot(
            generation_success_total=99,
            generation_failure_total=1,
            download_issuance_success_total=995,
            download_issuance_failure_total=5,
            generation_latency_ms_samples=(800.0, 900.0, 1500.0),
            download_latency_ms_samples=(120.0, 250.0, 300.0),
        ),
        policy=FormsSloThresholdPolicy(
            evaluation_window="30m",
            generation_success_rate_min=0.99,
            generation_latency_p95_ms_max=2500.0,
            generation_latency_p99_ms_max=4000.0,
            download_success_rate_min=0.995,
            download_latency_p95_ms_max=1000.0,
            download_latency_p99_ms_max=2000.0,
        ),
    )
    assert alerts == ()


def test_threshold_breaches_emit_canonical_forms_slo_payloads() -> None:
    alerts = evaluate_forms_slo_thresholds(
        metrics_snapshot=FormsSloMetricSnapshot(
            generation_success_total=80,
            generation_failure_total=20,
            download_issuance_success_total=90,
            download_issuance_failure_total=10,
            generation_latency_ms_samples=(2400.0, 2600.0, 4500.0),
            download_latency_ms_samples=(950.0, 1200.0, 2200.0),
        ),
        policy=FormsSloThresholdPolicy(
            evaluation_window="10m",
            generation_success_rate_min=0.95,
            generation_latency_p95_ms_max=2500.0,
            generation_latency_p99_ms_max=4000.0,
            download_success_rate_min=0.99,
            download_latency_p95_ms_max=1000.0,
            download_latency_p99_ms_max=2000.0,
        ),
    )
    assert alerts
    alert_codes = {alert.alert_code for alert in alerts}
    assert FORMS_SLO_GENERATION_SUCCESS_RATE_BREACH in alert_codes
    assert FORMS_SLO_GENERATION_LATENCY_BREACH in alert_codes
    assert FORMS_SLO_DOWNLOAD_SUCCESS_RATE_BREACH in alert_codes
    assert FORMS_SLO_DOWNLOAD_LATENCY_BREACH in alert_codes
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
        }


def test_same_snapshot_and_policy_produce_byte_equivalent_alert_output() -> None:
    snapshot = FormsSloMetricSnapshot(
        generation_success_total=7,
        generation_failure_total=3,
        download_issuance_success_total=7,
        download_issuance_failure_total=3,
        generation_latency_ms_samples=(2501.0, 4200.0, 5000.0),
        download_latency_ms_samples=(1001.0, 2100.0, 3000.0),
    )
    policy = FormsSloThresholdPolicy(
        evaluation_window="5m",
        generation_success_rate_min=0.95,
        generation_latency_p95_ms_max=2500.0,
        generation_latency_p99_ms_max=4000.0,
        download_success_rate_min=0.95,
        download_latency_p95_ms_max=1000.0,
        download_latency_p99_ms_max=2000.0,
    )
    first = serialize_forms_slo_alerts(
        alerts=evaluate_forms_slo_thresholds(metrics_snapshot=snapshot, policy=policy)
    )
    second = serialize_forms_slo_alerts(
        alerts=evaluate_forms_slo_thresholds(metrics_snapshot=snapshot, policy=policy)
    )
    assert first == second


def test_create_app_wires_forms_slo_policy_and_evaluation_helper() -> None:
    app = create_app()
    assert hasattr(app.state, "forms_slo_threshold_policy")
    assert app.state.forms_slo_threshold_policy.evaluation_window

    app.state.forms_slo_threshold_policy = FormsSloThresholdPolicy(
        evaluation_window="5m",
        generation_success_rate_min=0.99,
        generation_latency_p95_ms_max=1000.0,
        generation_latency_p99_ms_max=1500.0,
        download_success_rate_min=0.99,
        download_latency_p95_ms_max=400.0,
        download_latency_p99_ms_max=600.0,
    )
    alerts = evaluate_forms_slo_alerts(
        app_instance=app,
        metrics_snapshot=FormsSloMetricSnapshot(
            generation_success_total=1,
            generation_failure_total=9,
            download_issuance_success_total=9,
            download_issuance_failure_total=1,
            generation_latency_ms_samples=(1200.0, 1800.0),
            download_latency_ms_samples=(700.0, 900.0),
        ),
    )
    alert_codes = {alert.alert_code for alert in alerts}
    assert FORMS_SLO_GENERATION_SUCCESS_RATE_BREACH in alert_codes
    assert FORMS_SLO_DOWNLOAD_LATENCY_BREACH in alert_codes
