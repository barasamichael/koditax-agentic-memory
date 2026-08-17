"""Deterministic rule-depth coverage for forms pre-generation validation logic."""

from __future__ import annotations

import copy
import json
from uuid import uuid5
from uuid import NAMESPACE_URL
from typing import cast
from pathlib import Path

from shared.determinism.input_hash import canonical_json_dumps
from services.forms.app.income_tax.form_mapping import map_finalized_income_tax_output_to_form_ready
from services.forms.app.income_tax.form_validation import validate_income_tax_pre_generation_context
from services.forms.app.income_tax.form_version_binding import bind_income_tax_form_version

GOLDEN_CASE_DIR = Path("eval/golden/tax_core")
FINALIZED_AT = "2026-03-25T11:40:00+03:00"


def test_forms_validation_rules_valid_payload_returns_no_findings() -> None:
    form_ready_output, form_version_binding = _build_mapped_and_bound_outputs(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )

    result = validate_income_tax_pre_generation_context(
        form_ready_output=form_ready_output,
        form_version_binding=form_version_binding,
    )

    assert result["is_valid"] is True
    assert result["validation_status"] == "valid"
    assert result["findings"] == []


def test_forms_validation_rules_missing_required_field_reported_deterministically() -> None:
    form_ready_output, form_version_binding = _build_mapped_and_bound_outputs(
        "income_tax_resident_employment_2021_01_01_case_001.json"
    )
    del form_version_binding["form_version_id"]

    first = validate_income_tax_pre_generation_context(
        form_ready_output=copy.deepcopy(form_ready_output),
        form_version_binding=copy.deepcopy(form_version_binding),
    )
    second = validate_income_tax_pre_generation_context(
        form_ready_output=copy.deepcopy(form_ready_output),
        form_version_binding=copy.deepcopy(form_version_binding),
    )

    assert first["is_valid"] is False
    assert first["validation_status"] == "invalid"
    assert canonical_json_dumps(first["findings"]) == canonical_json_dumps(second["findings"])
    assert any(
        finding["code"] == "forms_required_field_missing"
        and finding["field"] == "form_version_binding.form_version_id"
        for finding in first["findings"]
    )


def test_forms_validation_rules_cross_field_mismatch_reported_deterministically() -> None:
    form_ready_output, form_version_binding = _build_mapped_and_bound_outputs(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )
    form_version_binding["supported_lane_id"] = "non_resident_employment_income_2023_07_01"

    result = validate_income_tax_pre_generation_context(
        form_ready_output=form_ready_output,
        form_version_binding=form_version_binding,
    )

    assert result["is_valid"] is False
    assert any(
        finding["code"] == "forms_cross_field_inconsistent"
        and finding["field"] == "form_version_binding.supported_lane_id"
        for finding in result["findings"]
    )


def test_forms_validation_rules_multiple_violations_are_stably_ordered() -> None:
    form_ready_output, form_version_binding = _build_mapped_and_bound_outputs(
        "income_tax_non_resident_employment_2021_01_01_case_001.json"
    )
    invalid_mapped = copy.deepcopy(form_ready_output)
    invalid_binding = copy.deepcopy(form_version_binding)

    del invalid_binding["form_version_id"]
    invalid_binding["binding_status"] = "pending"
    binding_lineage = cast(dict[str, object], invalid_binding["binding_lineage"])
    binding_lineage["computation_id"] = "mismatch-computation-id"
    invalid_binding["binding_lineage"] = binding_lineage
    form_fields = cast(dict[str, object], invalid_mapped["form_fields"])
    form_fields["refund_due_kes"] = "invalid-money"
    invalid_mapped["form_fields"] = form_fields

    first = validate_income_tax_pre_generation_context(
        form_ready_output=invalid_mapped,
        form_version_binding=invalid_binding,
    )
    second = validate_income_tax_pre_generation_context(
        form_ready_output=copy.deepcopy(invalid_mapped),
        form_version_binding=copy.deepcopy(invalid_binding),
    )

    assert first["is_valid"] is False
    assert canonical_json_dumps(first["findings"]) == canonical_json_dumps(second["findings"])
    finding_codes = {finding["code"] for finding in first["findings"]}
    assert "forms_required_field_missing" in finding_codes
    assert "forms_field_value_invalid" in finding_codes
    assert "forms_cross_field_inconsistent" in finding_codes
    ordered_keys = [
        (finding["field"], finding["code"], finding["message"]) for finding in first["findings"]
    ]
    assert ordered_keys == sorted(ordered_keys)


def _build_mapped_and_bound_outputs(
    fixture_name: str,
) -> tuple[dict[str, object], dict[str, object]]:
    finalized_output = _build_finalized_output(fixture_name)
    form_ready_output = map_finalized_income_tax_output_to_form_ready(
        copy.deepcopy(finalized_output)
    )
    form_version_binding = bind_income_tax_form_version(copy.deepcopy(form_ready_output))
    return form_ready_output, form_version_binding


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
