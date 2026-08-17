from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from typing import cast
from collections.abc import Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = REPO_ROOT / "docs" / "governance" / "phase-12-release-gate-rules.json"
REGISTRY_PATH = REPO_ROOT / "docs" / "governance" / "phase-12-requirement-id-registry.json"
PROVIDER_POLICY_PATH = (
    REPO_ROOT / "docs" / "governance" / "phase-12-provider-and-submission-decisions.md"
)


REQUIRED_GATE_FIELDS = {
    "gate_id",
    "gate_name",
    "severity",
    "required_requirement_ids",
    "required_evidence_types",
    "pass_condition",
    "fail_condition",
    "waiver_allowed",
    "waiver_requirements",
}


def _load_rules() -> dict[str, Any]:
    payload = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)


def _load_registry_ids() -> set[str]:
    payload = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, list)
    rows: list[Mapping[str, object]] = [
        cast(Mapping[str, object], row)
        for row in cast(list[object], payload)
        if isinstance(row, Mapping)
    ]
    return {str(row.get("requirement_id", "")) for row in rows if row.get("requirement_id")}


def _gate_map() -> dict[str, Mapping[str, object]]:
    rules = _load_rules()
    gates = rules.get("gates")
    assert isinstance(gates, list)
    gate_list: list[Mapping[str, object]] = [
        cast(Mapping[str, object], gate)
        for gate in cast(list[object], gates)
        if isinstance(gate, Mapping)
    ]
    mapped: dict[str, Mapping[str, object]] = {}
    for gate_map in gate_list:
        gate_id = gate_map.get("gate_id")
        assert isinstance(gate_id, str)
        mapped[gate_id] = gate_map
    return mapped


def test_release_gate_rules_json_structure_valid() -> None:
    rules = _load_rules()
    assert isinstance(rules.get("gate_version"), str)
    gates = rules.get("gates")
    assert isinstance(gates, list)
    gate_list: list[Mapping[str, object]] = [
        cast(Mapping[str, object], gate)
        for gate in cast(list[object], gates)
        if isinstance(gate, Mapping)
    ]
    assert len(gate_list) > 0

    for gate_map in gate_list:
        assert REQUIRED_GATE_FIELDS.issubset(gate_map.keys())
        assert gate_map["severity"] in {"critical", "high", "medium"}
        assert isinstance(gate_map["required_requirement_ids"], list)
        assert isinstance(gate_map["required_evidence_types"], list)
        assert isinstance(gate_map["waiver_allowed"], bool)
        assert isinstance(gate_map["waiver_requirements"], list)


def test_all_gate_required_ids_exist_in_registry() -> None:
    registry_ids = _load_registry_ids()
    rules = _load_rules()
    gates = rules.get("gates")
    assert isinstance(gates, list)
    gate_list: list[Mapping[str, object]] = [
        cast(Mapping[str, object], gate)
        for gate in cast(list[object], gates)
        if isinstance(gate, Mapping)
    ]
    for gate_map in gate_list:
        req_ids = gate_map.get("required_requirement_ids")
        assert isinstance(req_ids, list)
        for req_id in cast(list[object], req_ids):
            assert isinstance(req_id, str)
            assert req_id in registry_ids


def test_critical_gates_defined_for_core_milestones() -> None:
    gate_map = _gate_map()
    required_milestones = {"12.2", "12.3", "12.4", "12.5", "12.12"}
    covered: set[str] = set()
    for gate in gate_map.values():
        if gate.get("severity") != "critical":
            continue
        milestone_scope = gate.get("milestone_scope", [])
        if isinstance(milestone_scope, list):
            covered.update(str(item) for item in cast(list[object], milestone_scope))
    assert required_milestones.issubset(covered)


def test_deferral_gate_enforces_expiry_failure() -> None:
    gate_map = _gate_map()
    deferral_gate = gate_map["G-DEFERRAL-VALIDITY"]
    fail_condition = str(deferral_gate.get("fail_condition", "")).lower()
    assert "expired deferral" in fail_condition
    assert "fail" in fail_condition


def test_provider_lock_gate_requires_zoho_africastalking_kra() -> None:
    gate_map = _gate_map()
    provider_gate = gate_map["G-PROVIDER-LOCK"]
    pass_condition = str(provider_gate.get("pass_condition", "")).lower()
    fail_condition = str(provider_gate.get("fail_condition", "")).lower()

    assert "zoho" in pass_condition
    assert "africa" in pass_condition
    assert "kra" in pass_condition
    assert "mismatch" in fail_condition

    provider_policy_text = PROVIDER_POLICY_PATH.read_text(encoding="utf-8").lower()
    assert "zoho" in provider_policy_text
    assert "africa" in provider_policy_text
    assert "kra api" in provider_policy_text


def test_no_empty_pass_fail_conditions() -> None:
    rules = _load_rules()
    gates = rules.get("gates")
    assert isinstance(gates, list)
    gate_list: list[Mapping[str, object]] = [
        cast(Mapping[str, object], gate)
        for gate in cast(list[object], gates)
        if isinstance(gate, Mapping)
    ]
    for gate_map in gate_list:
        pass_condition = gate_map.get("pass_condition")
        fail_condition = gate_map.get("fail_condition")
        assert isinstance(pass_condition, str)
        assert pass_condition.strip() != ""
        assert isinstance(fail_condition, str)
        assert fail_condition.strip() != ""
