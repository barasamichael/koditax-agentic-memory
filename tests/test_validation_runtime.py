"""Runtime tests for the standalone validation service slice."""

from __future__ import annotations

from uuid import UUID
from typing import Any
from typing import cast

from fastapi.testclient import TestClient

from services.validation.app.main import create_app
from services.validation.app.audit_events import ValidationAuditEvent
from services.validation.app.validation_rules import evaluate_forms_workflow_validation
from services.validation.app.validation_rules import evaluate_report_workflow_validation
from services.validation.app.validation_store import ValidationExecutionRecord


def test_supported_validation_request_returns_machine_consumable_result() -> None:
    app = create_app()
    payload = {
        "return_id": "RET-VALID-001",
        "tax_domain": "income_tax",
        "mode": "pre_submission",
        "fields": {
            "kra_pin": "A123456789B",
            "period_start": "2024-01-01",
            "period_end": "2024-12-31",
            "amount_total": "100.00",
        },
    }

    with TestClient(app) as client:
        response = client.post(
            "/validate/return",
            headers={"X-Correlation-ID": "validation-runtime-success"},
            json=payload,
        )

    body = _json(response)
    result = cast(dict[str, object], body["result"])
    audit_evidence = cast(dict[str, object], body["audit_evidence"])
    assert response.status_code == 200
    assert result["validation_status"] == "accepted"
    assert audit_evidence["event_type"] == "validation_execution_accepted"
    assert audit_evidence["status"] == "accepted"
    assert "validation_id" in result
    issues = cast(list[dict[str, object]], result["issues"])
    rule_results = cast(list[dict[str, object]], result["rule_results"])
    assert issues[0]["code"] == "validation_passed"
    assert [item["rule_code"] for item in rule_results] == [
        "kra_pin_presence",
        "kra_pin_format",
        "period_range_consistency",
        "amount_total_precision",
    ]


def test_supported_health_contribution_request_returns_machine_consumable_result() -> None:
    app = create_app()
    payload = {
        "return_id": "RET-HC-001",
        "tax_domain": "health_contribution",
        "mode": "pre_submission",
        "fields": {
            "regime_identifier": "sha_shif",
            "resolved_domain_path": "sha_shif_salaried",
            "historical_version_id": "HCH-VER-20241001-A",
            "primary_effective_date": "2024-10-31",
            "contribution_basis_kes": "40000.00",
            "total_contribution_kes": "1100.00",
        },
    }

    with TestClient(app) as client:
        response = client.post(
            "/validate/return",
            headers={"X-Correlation-ID": "validation-health-success"},
            json=payload,
        )

    body = _json(response)
    result = cast(dict[str, object], body["result"])
    audit_evidence = cast(dict[str, object], body["audit_evidence"])
    assert response.status_code == 200
    assert result["validation_status"] == "accepted"
    assert audit_evidence["event_type"] == "validation_execution_accepted"
    issues = cast(list[dict[str, object]], result["issues"])
    rule_results = cast(list[dict[str, object]], result["rule_results"])
    assert issues[0]["code"] == "validation_passed"
    assert [item["rule_code"] for item in rule_results] == [
        "health_contribution_supported_lane_detected",
        "health_contribution_version_binding_consistent",
        "health_contribution_effective_window_consistent",
        "health_contribution_summary_consistent",
    ]


def test_validation_execution_is_recorded_in_store() -> None:
    app = create_app()
    payload = {
        "return_id": "RET-VALID-002",
        "tax_domain": "income_tax",
        "mode": "draft",
        "fields": {"period_start": "2024-01-01", "period_end": "2024-12-31"},
    }

    with TestClient(app) as client:
        response = client.post(
            "/validate/return",
            headers={"X-Correlation-ID": "validation-store-record"},
            json=payload,
        )
        body = _json(response)
        validation_id = cast(str, cast(dict[str, object], body["result"])["validation_id"])
        _ = client
    record = cast(
        ValidationExecutionRecord | None,
        app.state.validation_store.get_record(validation_id=UUID(validation_id)),
    )

    assert response.status_code == 200
    assert record is not None
    assert record.return_id == "RET-VALID-002"
    assert record.tax_domain == "income_tax"


def test_validation_execution_emits_deterministic_audit_event() -> None:
    app = create_app()
    payload = {
        "return_id": "RET-VALID-AUD-001",
        "tax_domain": "income_tax",
        "mode": "draft",
        "fields": {"kra_pin": "A123456789B"},
    }

    with TestClient(app) as client:
        first = client.post(
            "/validate/return",
            headers={"X-Correlation-ID": "validation-audit-success"},
            json=payload,
        )
        second = client.post(
            "/validate/return",
            headers={"X-Correlation-ID": "validation-audit-success"},
            json=payload,
        )

    first_body = _json(first)
    second_body = _json(second)
    events = cast(
        list[ValidationAuditEvent],
        app.state.validation_audit_store.list(correlation_id="validation-audit-success"),
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first_body["audit_evidence"] == second_body["audit_evidence"]
    assert len(events) == 1
    assert events[0]["event_type"] == "validation_execution_accepted"
    assert events[0]["status"] == "accepted"


def test_health_contribution_execution_is_recorded_in_store() -> None:
    app = create_app()
    payload = {
        "return_id": "RET-HC-002",
        "tax_domain": "health_contribution",
        "mode": "draft",
        "fields": {
            "regime_identifier": "nhif_legacy",
            "resolved_domain_path": "nhif_legacy",
            "historical_version_id": "HCH-VER-20221231-REG",
            "primary_effective_date": "2023-05-31",
            "contribution_basis_kes": "45000.00",
            "total_contribution_kes": "1100.00",
        },
    }

    with TestClient(app) as client:
        response = client.post(
            "/validate/return",
            headers={"X-Correlation-ID": "validation-health-store-record"},
            json=payload,
        )
        body = _json(response)
        validation_id = cast(str, cast(dict[str, object], body["result"])["validation_id"])
        _ = client
    record = cast(
        ValidationExecutionRecord | None,
        app.state.validation_store.get_record(validation_id=UUID(validation_id)),
    )

    assert response.status_code == 200
    assert record is not None
    assert record.return_id == "RET-HC-002"
    assert record.tax_domain == "health_contribution"


def test_repeated_identical_requests_return_same_normalized_result() -> None:
    app = create_app()
    payload = {
        "return_id": "RET-VALID-003",
        "tax_domain": "income_tax",
        "mode": "pre_submission",
        "fields": {
            "kra_pin": "INVALID",
            "period_start": "2024-12-31",
            "period_end": "2024-01-01",
            "amount_total": "100.123",
        },
    }

    with TestClient(app) as client:
        first = client.post(
            "/validate/return",
            headers={"X-Correlation-ID": "validation-deterministic"},
            json=payload,
        )
        second = client.post(
            "/validate/return",
            headers={"X-Correlation-ID": "validation-deterministic"},
            json=payload,
        )

    first_body = _json(first)
    second_body = _json(second)
    assert first.status_code == 200
    assert second.status_code == 200
    assert cast(dict[str, object], first_body["result"]) == cast(
        dict[str, object], second_body["result"]
    )
    assert first_body["audit_evidence"] == second_body["audit_evidence"]


def test_forms_workflow_validation_envelope_is_machine_consumable() -> None:
    envelope = evaluate_forms_workflow_validation(
        tax_domain="income_tax",
        finalized_output={
            "finalization_status": "finalized",
            "result_payload": {
                "version_identity": {"historical_version_id": "KIT-VER-20230701-A"},
                "liability_summary": {
                    "chargeable_income_kes": "1080000.00",
                    "net_income_tax_due_kes": "187200.00",
                    "refund_due_kes": "0.00",
                },
            },
        },
    ).to_dict()

    assert envelope["workflow"] == "forms_pre_generation"
    assert envelope["validation_status"] == "accepted"
    rule_results = cast(list[dict[str, object]], envelope["rule_results"])
    assert [item["rule_code"] for item in rule_results] == [
        "forms_income_tax_finalization_ready",
        "forms_income_tax_version_binding_ready",
        "forms_income_tax_liability_summary_ready",
    ]


def test_reports_workflow_validation_envelope_is_machine_consumable() -> None:
    envelope = evaluate_report_workflow_validation(
        tax_domain="health_contribution",
        payload={
            "computation_id": "bf80513f-f7dd-5257-9f4d-656eebc2c2f5",
            "form_id": "85bfa98d-e3e9-5829-aad6-047e7dc97f8c",
            "report_type": "health_contribution_summary",
            "tax_year": 2024,
            "historical_version_id": "HCH-VER-20241001-A",
            "supported_lane_id": "health_contribution_sha_shif_v1_2024_10_01",
        },
    ).to_dict()

    assert envelope["workflow"] == "reports_generation"
    assert envelope["validation_status"] == "accepted"
    rule_results = cast(list[dict[str, object]], envelope["rule_results"])
    assert [item["rule_code"] for item in rule_results] == [
        "reports_validation_required_fields_ready",
        "reports_validation_report_type_ready",
    ]


def _json(response: Any) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)
