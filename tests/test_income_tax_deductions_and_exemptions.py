"""Test governed income-tax deductions and exemptions handling for employment lanes."""

from __future__ import annotations

import copy
import json
from typing import cast
from decimal import Decimal
from pathlib import Path

import pytest

from shared.determinism.input_hash import InputHashError
from services.tax_core.app.engine.executor import execute_computation
from services.tax_core.app.engine.execution_contract import ComputationExecutionRequest
from services.tax_core.app.rules.income_tax.deductions_and_exemptions import (
    apply_supported_resident_employment_deductions_and_exemptions,
)
from services.tax_core.app.rules.income_tax.deductions_and_exemptions import (
    apply_supported_non_resident_employment_deductions_and_exemptions,
)

RESIDENT_GOLDEN_CASE_PATH = Path(
    "eval/golden/tax_core/income_tax_resident_employment_2023_07_01_case_001.json"
)
NON_RESIDENT_GOLDEN_CASE_PATH = Path(
    "eval/golden/tax_core/income_tax_non_resident_employment_2023_07_01_case_001.json"
)


def test_resident_deductions_module_applies_zero_governed_adjustment() -> None:
    """Verify resident first-lane deductions/exemptions keep taxable base unchanged."""

    result = apply_supported_resident_employment_deductions_and_exemptions(
        assessable_income=Decimal("960000.00"),
        deduction_claims=[],
        exemption_claims=[],
    )

    assert result.chargeable_income == Decimal("960000.00")
    assert result.deduction_domain_outcome == {
        "status": "computed",
        "taxable_base_kes": "960000.00",
        "gross_tax_kes": None,
        "creditable_amount_kes": "0.00",
        "final_tax_amount_kes": None,
        "decision_refs": ["ITC-POL-803", "ITC-POL-804"],
    }
    assert result.deduction_impacts == []
    assert result.exemption_impacts == []


def test_non_resident_deductions_module_applies_zero_governed_adjustment() -> None:
    """Verify non-resident first-lane deductions/exemptions keep taxable base unchanged."""

    result = apply_supported_non_resident_employment_deductions_and_exemptions(
        assessable_income=Decimal("960000.00"),
        deduction_claims=[],
        exemption_claims=[],
    )

    assert result.chargeable_income == Decimal("960000.00")
    assert result.deduction_domain_outcome == {
        "status": "computed",
        "taxable_base_kes": "960000.00",
        "gross_tax_kes": None,
        "creditable_amount_kes": "0.00",
        "final_tax_amount_kes": None,
        "decision_refs": ["ITC-POL-803", "ITC-POL-804"],
    }
    assert result.deduction_impacts == []
    assert result.exemption_impacts == []


def test_deductions_module_rejects_unsupported_claims_deterministically() -> None:
    """Verify unsupported deduction claims fail with stable rule input error."""

    with pytest.raises(InputHashError) as error:
        apply_supported_resident_employment_deductions_and_exemptions(
            assessable_income=Decimal("960000.00"),
            deduction_claims=[{"claim_reference_id": "claim-donation-001"}],
            exemption_claims=[],
        )

    assert error.value.reason == "unsupported_deduction_claims"


def test_deductions_module_is_deterministic_for_identical_zero_claim_inputs() -> None:
    """Verify zero-claim deductions/exemptions handling is deterministic."""

    first = apply_supported_resident_employment_deductions_and_exemptions(
        assessable_income=Decimal("960000.00"),
        deduction_claims=[],
        exemption_claims=[],
    )
    second = apply_supported_resident_employment_deductions_and_exemptions(
        assessable_income=Decimal("960000.00"),
        deduction_claims=copy.deepcopy([]),
        exemption_claims=copy.deepcopy([]),
    )

    assert first.chargeable_income == second.chargeable_income
    assert _canonical_json(first.deduction_domain_outcome) == _canonical_json(
        second.deduction_domain_outcome
    )
    assert _canonical_json(first.deduction_impacts) == _canonical_json(second.deduction_impacts)
    assert _canonical_json(first.exemption_impacts) == _canonical_json(second.exemption_impacts)


def test_resident_deductions_golden_fixture_matches_exact_output() -> None:
    """Verify resident golden fixture locks deductions/exemptions behavior."""

    fixture = _load_fixture(RESIDENT_GOLDEN_CASE_PATH)
    actual_output = execute_computation(
        ComputationExecutionRequest.model_validate(fixture["request"])
    ).model_dump(mode="json")

    assert _canonical_json(actual_output) == _canonical_json(fixture["expected_output"])


def test_non_resident_deductions_golden_fixture_matches_exact_output() -> None:
    """Verify non-resident golden fixture locks deductions/exemptions behavior."""

    fixture = _load_fixture(NON_RESIDENT_GOLDEN_CASE_PATH)
    actual_output = execute_computation(
        ComputationExecutionRequest.model_validate(fixture["request"])
    ).model_dump(mode="json")

    assert _canonical_json(actual_output) == _canonical_json(fixture["expected_output"])


def _load_fixture(path: Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8") as fixture_file:
        fixture = json.load(fixture_file)
    return cast(dict[str, object], fixture)


def _canonical_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=True)
