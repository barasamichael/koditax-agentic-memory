"""Regression coverage for canonical forms generation-path audit-evidence envelope."""

from __future__ import annotations

import copy
import json
from uuid import uuid5
from uuid import NAMESPACE_URL
from typing import Any
from typing import cast
from pathlib import Path

import yaml
import pytest
from fastapi.testclient import TestClient

from services.forms.app.main import create_app
from shared.determinism.input_hash import canonical_json_dumps

CONTRACT_PATH = Path("contracts/openapi/forms.yaml")
GOLDEN_CASE_DIR = Path("eval/golden/tax_core")
FINALIZED_AT = "2026-03-22T08:45:00+03:00"


def test_generation_path_success_responses_include_complete_audit_evidence_block() -> None:
    app = create_app()
    finalized_output = _build_finalized_output(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )

    with TestClient(app) as client:
        mapping = client.post(
            "/v1/forms/income-tax/mappings",
            json={"finalized_output": finalized_output},
            headers={"X-Correlation-ID": "forms-audit-envelope-corr"},
        )
        mapping_payload = _response_json(mapping)
        binding = client.post(
            "/v1/forms/income-tax/version-bindings",
            json={"mapped_output": mapping_payload["mapping_output"]},
            headers={"X-Correlation-ID": "forms-audit-envelope-corr"},
        )
        binding_payload = _response_json(binding)
        artifact = client.post(
            "/v1/forms/income-tax/artifacts",
            json={
                "finalized_output": finalized_output,
                "form_ready_output": mapping_payload["mapping_output"],
                "form_version_binding": binding_payload["binding_output"],
            },
            headers={"X-Correlation-ID": "forms-audit-envelope-corr"},
        )

    assert mapping.status_code == 200
    assert binding.status_code == 200
    assert artifact.status_code == 201
    _assert_audit_evidence_block(_response_json(mapping)["audit_evidence"])
    _assert_audit_evidence_block(_response_json(binding)["audit_evidence"])
    _assert_audit_evidence_block(_response_json(artifact)["audit_evidence"])


def test_repeated_same_request_yields_stable_audit_evidence_shape() -> None:
    app = create_app()
    finalized_output = _build_finalized_output(
        "income_tax_non_resident_employment_2021_01_01_case_001.json"
    )

    with TestClient(app) as client:
        first = client.post(
            "/v1/forms/income-tax/mappings",
            json={"finalized_output": finalized_output},
            headers={"X-Correlation-ID": "forms-audit-determinism-corr"},
        )
        second = client.post(
            "/v1/forms/income-tax/mappings",
            json={"finalized_output": finalized_output},
            headers={"X-Correlation-ID": "forms-audit-determinism-corr"},
        )

    first_evidence = cast(dict[str, object], _response_json(first)["audit_evidence"])
    second_evidence = cast(dict[str, object], _response_json(second)["audit_evidence"])
    assert set(first_evidence.keys()) == set(second_evidence.keys())
    assert first_evidence["audit_event_id"] == second_evidence["audit_event_id"]
    assert canonical_json_dumps(first_evidence["lineage_reference"]) == canonical_json_dumps(
        second_evidence["lineage_reference"]
    )
    assert first_evidence["event_type"] == second_evidence["event_type"]


def test_audit_evidence_required_field_assertions_fail_for_incomplete_payload() -> None:
    incomplete = {
        "audit_event_id": "abc",
        "event_type": "forms_mapping_completed",
    }
    with pytest.raises(AssertionError):
        _assert_audit_evidence_block(incomplete)


def test_openapi_audit_evidence_schema_matches_runtime_envelope_keys() -> None:
    app = create_app()
    finalized_output = _build_finalized_output(
        "income_tax_resident_employment_2021_01_01_case_001.json"
    )
    with TestClient(app) as client:
        mapping = client.post(
            "/v1/forms/income-tax/mappings",
            json={"finalized_output": finalized_output},
            headers={"X-Correlation-ID": "forms-audit-openapi-parity-corr"},
        )
    runtime_evidence = cast(dict[str, object], _response_json(mapping)["audit_evidence"])
    runtime_keys = set(runtime_evidence.keys())

    contract = _load_contract()
    schemas = cast(dict[str, object], cast(dict[str, object], contract["components"])["schemas"])
    evidence_schema = cast(dict[str, object], schemas["FormsAuditEvidenceEnvelope"])
    schema_required = set(cast(list[str], evidence_schema["required"]))
    schema_properties = set(cast(dict[str, object], evidence_schema["properties"]).keys())

    assert schema_required.issubset(runtime_keys)
    assert runtime_keys.issubset(schema_properties)


def _assert_audit_evidence_block(payload: Any) -> None:
    assert isinstance(payload, dict)
    payload_map = cast(dict[str, object], payload)
    required = {
        "audit_event_id",
        "event_type",
        "event_timestamp",
        "trace_id",
        "correlation_id",
        "lineage_reference",
        "actor_context",
    }
    assert required.issubset(set(payload_map.keys()))
    assert isinstance(payload_map["audit_event_id"], str) and payload_map["audit_event_id"].strip()
    assert isinstance(payload_map["event_type"], str) and payload_map["event_type"].strip()
    assert (
        isinstance(payload_map["event_timestamp"], str) and payload_map["event_timestamp"].strip()
    )
    assert isinstance(payload_map["trace_id"], str) and payload_map["trace_id"].strip()
    assert isinstance(payload_map["correlation_id"], str) and payload_map["correlation_id"].strip()
    assert isinstance(payload_map["lineage_reference"], dict)
    assert isinstance(payload_map["actor_context"], dict)


def _build_finalized_output(fixture_name: str) -> dict[str, object]:
    fixture_path = GOLDEN_CASE_DIR / fixture_name
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    fixture_id = fixture["fixture_id"]
    expected_output = copy.deepcopy(fixture["expected_output"])

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


def _load_contract() -> dict[str, object]:
    loaded = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return cast(dict[str, object], loaded)


def _response_json(response: Any) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)
