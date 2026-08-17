"""Failure-path tests for the standalone validation service slice."""

from __future__ import annotations

from typing import Any
from typing import cast

from fastapi.testclient import TestClient

from services.validation.app.main import create_app
from services.validation.app.config import VALIDATION_INTERNAL_API_KEY_HEADER
from services.validation.app.config import VALIDATION_INTERNAL_API_KEY_ENV_VAR


def test_unsupported_validation_domain_fails_closed() -> None:
    with TestClient(create_app()) as client:
        response = client.post(
            "/validate/return",
            headers={"X-Correlation-ID": "validation-unsupported-domain"},
            json={
                "return_id": "RET-FAIL-001",
                "tax_domain": "corporate_tax",
                "mode": "draft",
                "fields": {},
            },
        )

    detail = _detail(response)
    assert response.status_code == 404
    assert detail["error_code"] == "unsupported_validation_scope"
    assert detail["reason"] == "unsupported_validation_scope"
    assert "audit_evidence" in cast(dict[str, object], detail["context"])


def test_unsupported_health_contribution_domain_mode_combination_fails_canonically() -> None:
    with TestClient(create_app()) as client:
        response = client.post(
            "/validate/return",
            headers={"X-Correlation-ID": "validation-unsupported-health-mode"},
            json={
                "return_id": "RET-FAIL-HC-001",
                "tax_domain": "health_contribution",
                "mode": "post_submission_integrity",
                "fields": {
                    "regime_identifier": "sha_shif",
                    "resolved_domain_path": "sha_shif_salaried",
                    "historical_version_id": "HCH-VER-20241001-A",
                },
            },
        )

    detail = _detail(response)
    assert response.status_code == 400
    assert detail["error_code"] == "invalid_validation_request"
    assert detail["reason"] == "invalid_validation_request"
    assert "audit_evidence" in cast(dict[str, object], detail["context"])


def test_missing_required_governed_field_fails_canonically() -> None:
    with TestClient(create_app()) as client:
        first = client.post(
            "/validate/return",
            headers={"X-Correlation-ID": "validation-missing-field"},
            json={"tax_domain": "income_tax", "mode": "draft", "fields": {}},
        )
        second = client.post(
            "/validate/return",
            headers={"X-Correlation-ID": "validation-missing-field"},
            json={"tax_domain": "income_tax", "mode": "draft", "fields": {}},
        )

    first_detail = _detail(first)
    second_detail = _detail(second)
    assert first.status_code == 400
    assert second.status_code == 400
    assert first_detail["error_code"] == "invalid_validation_request"
    assert first_detail["reason"] == second_detail["reason"]
    assert (
        cast(dict[str, object], first_detail["context"])["audit_evidence"]
        == cast(dict[str, object], second_detail["context"])["audit_evidence"]
    )


def test_missing_required_health_fields_rejects_canonically() -> None:
    payload = {
        "return_id": "RET-FAIL-HC-002",
        "tax_domain": "health_contribution",
        "mode": "pre_submission",
        "fields": {
            "regime_identifier": "sha_shif",
            "contribution_basis_kes": "40000.00",
        },
    }

    with TestClient(create_app()) as client:
        first = client.post(
            "/validate/return",
            headers={"X-Correlation-ID": "validation-health-missing-fields"},
            json=payload,
        )
        second = client.post(
            "/validate/return",
            headers={"X-Correlation-ID": "validation-health-missing-fields"},
            json=payload,
        )

    first_result = _result(first)
    second_result = _result(second)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first_result["validation_status"] == "rejected"
    assert first_result == second_result
    issues = cast(list[dict[str, object]], first_result["issues"])
    assert [issue["code"] for issue in issues] == [
        "missing_health_domain_path",
        "missing_health_historical_version_id",
        "invalid_health_primary_effective_date",
        "missing_health_total_contribution",
    ]


def test_non_object_fields_payload_fails_canonically() -> None:
    with TestClient(create_app()) as client:
        response = client.post(
            "/validate/return",
            headers={"X-Correlation-ID": "validation-bad-fields"},
            json={
                "return_id": "RET-FAIL-002",
                "tax_domain": "income_tax",
                "mode": "draft",
                "fields": "not-an-object",
            },
        )

    detail = _detail(response)
    assert response.status_code == 400
    assert detail["error_code"] == "invalid_validation_request"
    assert detail["reason"] == "invalid_validation_request"
    assert "audit_evidence" in cast(dict[str, object], detail["context"])


def test_production_persistence_unavailable_fails_closed(monkeypatch: Any) -> None:
    monkeypatch.setenv("VALIDATION_RUNTIME_MODE", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql://invalid dsn")
    monkeypatch.setenv(VALIDATION_INTERNAL_API_KEY_ENV_VAR, "validation-internal-secret")
    app = create_app()

    with TestClient(app) as client:
        response = client.post(
            "/validate/return",
            headers={
                "X-Correlation-ID": "validation-persistence-unavailable",
                VALIDATION_INTERNAL_API_KEY_HEADER: "validation-internal-secret",
            },
            json={
                "return_id": "RET-FAIL-003",
                "tax_domain": "income_tax",
                "mode": "draft",
                "fields": {"kra_pin": "A123456789B"},
            },
        )

    detail = _detail(response)
    audit_evidence = _audit_evidence(detail)
    assert response.status_code == 503
    assert detail["error_code"] == "validation_persistence_unavailable"
    assert detail["reason"] == "validation_persistence_unavailable"
    assert audit_evidence == {
        "audit_event_id": audit_evidence["audit_event_id"],
        "event_type": "validation_execution_failed",
        "event_time": audit_evidence["event_time"],
        "status": "failed",
    }


def test_production_internal_boundary_requires_configured_secret(monkeypatch: Any) -> None:
    monkeypatch.setenv("VALIDATION_RUNTIME_MODE", "production")
    monkeypatch.delenv(VALIDATION_INTERNAL_API_KEY_ENV_VAR, raising=False)
    app = create_app()

    with TestClient(app) as client:
        response = client.post(
            "/validate/return",
            headers={"X-Correlation-ID": "validation-boundary-unavailable"},
            json={
                "return_id": "RET-FAIL-004",
                "tax_domain": "income_tax",
                "mode": "draft",
                "fields": {"kra_pin": "A123456789B"},
            },
        )

    detail = _detail(response)
    audit_evidence = _audit_evidence(detail)
    assert response.status_code == 503
    assert detail["error_code"] == "validation_internal_boundary_unavailable"
    assert detail["reason"] == "validation_internal_boundary_unavailable"
    assert audit_evidence == {
        "audit_event_id": audit_evidence["audit_event_id"],
        "event_type": "validation_request_rejected",
        "event_time": audit_evidence["event_time"],
        "status": "failed",
    }


def test_production_internal_boundary_rejects_missing_or_invalid_header(monkeypatch: Any) -> None:
    monkeypatch.setenv("VALIDATION_RUNTIME_MODE", "production")
    monkeypatch.setenv(VALIDATION_INTERNAL_API_KEY_ENV_VAR, "validation-internal-secret")
    app = create_app()

    with TestClient(app) as client:
        missing = client.post(
            "/validate/return",
            headers={"X-Correlation-ID": "validation-boundary-forbidden"},
            json={
                "return_id": "RET-FAIL-005",
                "tax_domain": "income_tax",
                "mode": "draft",
                "fields": {"kra_pin": "A123456789B"},
            },
        )
        wrong = client.post(
            "/validate/return",
            headers={
                "X-Correlation-ID": "validation-boundary-forbidden",
                VALIDATION_INTERNAL_API_KEY_HEADER: "wrong-secret",
            },
            json={
                "return_id": "RET-FAIL-005",
                "tax_domain": "income_tax",
                "mode": "draft",
                "fields": {"kra_pin": "A123456789B"},
            },
        )

    missing_detail = _detail(missing)
    wrong_detail = _detail(wrong)
    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert missing_detail["error_code"] == "validation_internal_boundary_forbidden"
    assert wrong_detail["error_code"] == "validation_internal_boundary_forbidden"
    assert missing_detail["reason"] == wrong_detail["reason"]
    assert (
        cast(dict[str, object], missing_detail["context"])["audit_evidence"]
        == cast(dict[str, object], wrong_detail["context"])["audit_evidence"]
    )


def _detail(response: Any) -> dict[str, object]:
    payload = cast(dict[str, object], response.json())
    assert isinstance(payload, dict)
    detail = payload["detail"]
    assert isinstance(detail, dict)
    return cast(dict[str, object], detail)


def _result(response: Any) -> dict[str, object]:
    payload = cast(dict[str, object], response.json())
    assert isinstance(payload, dict)
    result = payload["result"]
    assert isinstance(result, dict)
    return cast(dict[str, object], result)


def _audit_evidence(detail: dict[str, object]) -> dict[str, str]:
    context = cast(dict[str, object], detail["context"])
    evidence = context["audit_evidence"]
    assert isinstance(evidence, dict)
    return cast(dict[str, str], evidence)
