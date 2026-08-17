"""Canonical forms error-contract checks for Phase 10.1.3."""

from __future__ import annotations

from typing import Any
from typing import cast
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from services.forms.app.main import create_app
from shared.determinism.input_hash import canonical_json_dumps

CONTRACT_PATH = Path("contracts/openapi/forms.yaml")


def test_forms_health_success_path_is_unchanged() -> None:
    app = create_app()
    with TestClient(app) as client:
        response = client.get("/healthz", headers={"X-Correlation-ID": "forms-error-health-corr"})

    payload = _response_json(response)
    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["service"] == "forms"


def test_forms_major_failure_classes_use_canonical_error_envelope() -> None:
    app = create_app()
    with TestClient(app) as client:
        mapped_payload_invalid = client.post(
            "/v1/forms/income-tax/mappings",
            json={"finalized_output": {"status": "finalized"}},
            headers={"X-Correlation-ID": "forms-error-not-implemented-corr"},
        )
        recognized_health_scope = client.post(
            "/v1/forms/health-contribution/mappings",
            json={"input": {}},
            headers={"X-Correlation-ID": "forms-error-health-domain-corr"},
        )
        invalid_tax_domain = client.post(
            "/v1/forms/mystery-tax/mappings",
            json={"input": {}},
            headers={"X-Correlation-ID": "forms-error-invalid-domain-corr"},
        )
        request_invalid = client.post(
            "/v1/forms/income-tax/mappings",
            json=["invalid", "payload", "shape"],
            headers={"X-Correlation-ID": "forms-error-request-invalid-corr"},
        )
        contract_violation = client.post(
            "/v1/forms/income-tax/mappings",
            json={},
            headers={"X-Correlation-ID": "forms-error-contract-violation-corr"},
        )

    _assert_canonical_error(
        response=mapped_payload_invalid,
        expected_status=400,
        expected_reason="forms_request_invalid",
    )
    _assert_canonical_error(
        response=recognized_health_scope,
        expected_status=400,
        expected_reason="forms_contract_violation",
    )
    _assert_canonical_error(
        response=invalid_tax_domain,
        expected_status=400,
        expected_reason="invalid_tax_domain",
    )
    _assert_canonical_error(
        response=request_invalid,
        expected_status=400,
        expected_reason="forms_request_invalid",
    )
    _assert_canonical_error(
        response=contract_violation,
        expected_status=400,
        expected_reason="forms_contract_violation",
    )


def test_forms_repeated_invalid_request_is_deterministic_for_error_code_and_reason() -> None:
    app = create_app()
    with TestClient(app) as client:
        first = client.post(
            "/v1/forms/income-tax/mappings",
            json=["invalid", "payload", "shape"],
            headers={"X-Correlation-ID": "forms-error-determinism-corr"},
        )
        second = client.post(
            "/v1/forms/income-tax/mappings",
            json=["invalid", "payload", "shape"],
            headers={"X-Correlation-ID": "forms-error-determinism-corr"},
        )

    first_error = _extract_error_detail(first)
    second_error = _extract_error_detail(second)
    assert first.status_code == 400
    assert second.status_code == 400
    assert first_error["error_code"] == second_error["error_code"] == "forms_request_invalid"
    assert first_error["reason"] == second_error["reason"] == "forms_request_invalid"
    assert canonical_json_dumps(
        {"error_code": first_error["error_code"], "reason": first_error["reason"]}
    ) == canonical_json_dumps(
        {"error_code": second_error["error_code"], "reason": second_error["reason"]}
    )


def test_forms_openapi_error_schema_matches_runtime_error_payload_keys() -> None:
    schema = _load_error_schema()
    required = set(cast(list[str], schema["required"]))
    properties = set(cast(dict[str, object], schema["properties"]).keys())

    app = create_app()
    with TestClient(app) as client:
        response = client.post(
            "/v1/forms/income-tax/mappings",
            json={},
            headers={"X-Correlation-ID": "forms-error-contract-parity-corr"},
        )
    detail = _extract_error_detail(response)
    runtime_keys = set(detail.keys())

    assert {"error_code", "message", "reason", "trace_id", "correlation_id"}.issubset(required)
    assert {"error_code", "message", "reason", "trace_id", "correlation_id"}.issubset(properties)
    assert required.issubset(runtime_keys)
    assert runtime_keys.issubset(properties)


def _assert_canonical_error(
    *,
    response: Any,
    expected_status: int,
    expected_reason: str,
) -> None:
    error = _extract_error_detail(response)
    assert response.status_code == expected_status
    assert error["error_code"] == expected_reason
    assert error["reason"] == expected_reason
    assert "traceback" not in str(error).lower()
    assert "exception" not in str(error).lower()


def _load_error_schema() -> dict[str, object]:
    assert CONTRACT_PATH.exists()
    loaded = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    loaded_map = cast(dict[str, object], loaded)
    components = cast(dict[str, object], loaded_map.get("components", {}))
    schemas = cast(dict[str, object], components.get("schemas", {}))
    schema = schemas.get("ErrorEnvelope")
    assert isinstance(schema, dict)
    return cast(dict[str, object], schema)


def _extract_error_detail(response: Any) -> dict[str, object]:
    payload = _response_json(response)
    detail = payload.get("detail")
    assert isinstance(detail, dict)
    detail_map = cast(dict[str, object], detail)
    assert "error_code" in detail_map
    assert "message" in detail_map
    assert "reason" in detail_map
    assert "trace_id" in detail_map
    assert "correlation_id" in detail_map
    return detail_map


def _response_json(response: Any) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)
