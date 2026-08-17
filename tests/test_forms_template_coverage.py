"""Coverage for governed forms template registry and runtime template gating."""

from __future__ import annotations

import copy
import json
from uuid import uuid5
from uuid import NAMESPACE_URL
from typing import Any
from typing import cast
from pathlib import Path

from fastapi.testclient import TestClient

from services.forms.app.main import create_app
from shared.determinism.input_hash import canonical_json_dumps
from services.forms.app.template_registry import build_forms_template_capability_index

GOLDEN_CASE_DIR = Path("eval/golden/tax_core")
FINALIZED_AT = "2026-03-15T09:00:00+03:00"
REQUIRED_DISABLED_TEMPLATE_CODES = frozenset({"IT2", "VAT3", "P10", "P9"})


def test_required_extension_templates_are_present_and_disabled() -> None:
    """Verify required extension templates are registered as disabled capabilities."""

    capability_index = build_forms_template_capability_index()

    assert REQUIRED_DISABLED_TEMPLATE_CODES.issubset(set(capability_index))
    for template_code in sorted(REQUIRED_DISABLED_TEMPLATE_CODES):
        entry = capability_index[template_code]
        assert isinstance(entry, dict)
        assert entry["template_code"] == template_code
        assert entry["status"] == "disabled"
        assert entry["enablement_status"] == "blocked_by_prerequisites"


def test_version_binding_rejects_disabled_template_deterministically() -> None:
    """Verify disabled templates fail closed with canonical deterministic error semantics."""

    app = create_app()
    finalized_output = _build_finalized_output(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )

    with TestClient(app) as client:
        mapping_response = client.post(
            "/v1/forms/income-tax/mappings",
            json={"finalized_output": finalized_output},
            headers={"X-Correlation-ID": "forms-template-coverage-map-corr"},
        )
        assert mapping_response.status_code == 200
        mapping_payload = _response_json(mapping_response)
        mapped_output = cast(dict[str, object], mapping_payload["mapping_output"])
        mapped_output["form_type"] = "IT2"

        blocked_one = client.post(
            "/v1/forms/income-tax/version-bindings",
            json={"mapped_output": mapped_output},
            headers={"X-Correlation-ID": "forms-template-coverage-block-corr"},
        )
        blocked_two = client.post(
            "/v1/forms/income-tax/version-bindings",
            json={"mapped_output": mapped_output},
            headers={"X-Correlation-ID": "forms-template-coverage-block-corr"},
        )

    blocked_one_payload = _response_json(blocked_one)
    blocked_two_payload = _response_json(blocked_two)
    blocked_one_detail = _extract_error_detail(blocked_one_payload)

    assert blocked_one.status_code == 409
    assert blocked_two.status_code == 409
    assert blocked_one_detail["error_code"] == "forms_template_capability_disabled"
    assert blocked_one_detail["reason"] == "forms_template_capability_disabled"
    assert cast(dict[str, object], blocked_one_detail["details"])["template_code"] == "IT2"
    assert canonical_json_dumps(blocked_one_payload) == canonical_json_dumps(blocked_two_payload)


def _build_finalized_output(fixture_name: str) -> dict[str, object]:
    fixture_path = GOLDEN_CASE_DIR / fixture_name
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    fixture_id = cast(str, fixture["fixture_id"])
    expected_output = copy.deepcopy(cast(dict[str, object], fixture["expected_output"]))

    return {
        "computation_id": str(uuid5(NAMESPACE_URL, f"{fixture_id}:computation")),
        "finalization_status": "finalized",
        "finalized_at": FINALIZED_AT,
        "finalized_audit_event_id": str(uuid5(NAMESPACE_URL, f"{fixture_id}:finalized-audit")),
        "tax_type": expected_output["tax_type"],
        "regime_type": expected_output["regime_type"],
        "tax_year": expected_output["tax_year"],
        "rule_version": expected_output["rule_version"],
        "input_hash": expected_output["input_hash"],
        "result_payload": expected_output["result_payload"],
    }


def _extract_error_detail(payload: dict[str, Any]) -> dict[str, object]:
    detail = payload.get("detail")
    assert isinstance(detail, dict)
    assert "error_code" in detail
    assert "message" in detail
    assert "reason" in detail
    return cast(dict[str, object], detail)


def _response_json(response: Any) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)
