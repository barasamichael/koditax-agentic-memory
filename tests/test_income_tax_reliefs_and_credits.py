"""Test governed income-tax relief and credit handling for employment lanes."""

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
from services.tax_core.app.rules.income_tax.reliefs_and_credits import EmploymentReliefClaim
from services.tax_core.app.rules.income_tax.reliefs_and_credits import (
    apply_supported_resident_employment_reliefs,
)
from services.tax_core.app.rules.income_tax.reliefs_and_credits import (
    apply_supported_non_resident_employment_reliefs,
)

RESIDENT_GOLDEN_CASE_PATH = Path(
    "eval/golden/tax_core/income_tax_resident_employment_2023_07_01_case_001.json"
)
NON_RESIDENT_GOLDEN_CASE_PATH = Path(
    "eval/golden/tax_core/income_tax_non_resident_employment_2023_07_01_case_001.json"
)


def test_resident_reliefs_module_applies_personal_relief_after_gross_tax() -> None:
    """Verify resident relief module applies governed personal relief after base tax."""

    result = apply_supported_resident_employment_reliefs(
        relief_claims=[
            EmploymentReliefClaim(
                relief_type="personal_relief",
                claim_reference_id="claim-personal-relief-001",
                asserted_amount_kes="28800.00",
            )
        ],
        gross_tax=Decimal("225400.00"),
    )

    assert result.total_reliefs == Decimal("28800.00")
    assert result.net_tax_due == Decimal("196600.00")
    assert result.relief_domain_outcome == {
        "status": "computed",
        "taxable_base_kes": None,
        "gross_tax_kes": None,
        "creditable_amount_kes": "28800.00",
        "final_tax_amount_kes": "0.00",
        "decision_refs": [
            "REM-20230701-003",
            "REM-20230701-011",
        ],
    }
    assert result.relief_impacts == [
        {
            "impact_type": "personal_relief",
            "claim_reference_id": "claim-personal-relief-001",
            "status": "applied",
            "impact_amount_kes": "28800.00",
        }
    ]


def test_non_resident_reliefs_module_excludes_resident_only_reliefs() -> None:
    """Verify non-resident relief module applies zero resident-only reliefs."""

    result = apply_supported_non_resident_employment_reliefs(
        relief_claims=[],
        gross_tax=Decimal("225400.00"),
    )

    assert result.total_reliefs == Decimal("0.00")
    assert result.net_tax_due == Decimal("225400.00")
    assert result.relief_domain_outcome == {
        "status": "computed",
        "taxable_base_kes": None,
        "gross_tax_kes": None,
        "creditable_amount_kes": "0.00",
        "final_tax_amount_kes": "0.00",
        "decision_refs": [
            "NREM-20230701-005",
            "NREM-20230701-006",
        ],
    }
    assert result.relief_impacts == []


def test_resident_reliefs_module_rejects_unsupported_claim_type() -> None:
    """Verify unsupported relief types fail deterministically."""

    with pytest.raises(InputHashError) as error:
        apply_supported_resident_employment_reliefs(
            relief_claims=[
                EmploymentReliefClaim(
                    relief_type="withholding_credit",
                    claim_reference_id="claim-unsupported-credit-001",
                    asserted_amount_kes="100.00",
                )
            ],
            gross_tax=Decimal("225400.00"),
        )

    assert error.value.reason == "unsupported_relief_claim_type"


def test_reliefs_module_is_deterministic_for_identical_resident_claims() -> None:
    """Verify resident relief application is deterministic for identical claims."""

    claims = [
        EmploymentReliefClaim(
            relief_type="personal_relief",
            claim_reference_id="claim-personal-relief-001",
            asserted_amount_kes="28800.00",
        )
    ]

    first = apply_supported_resident_employment_reliefs(
        relief_claims=claims,
        gross_tax=Decimal("225400.00"),
    )
    second = apply_supported_resident_employment_reliefs(
        relief_claims=copy.deepcopy(claims),
        gross_tax=Decimal("225400.00"),
    )

    assert _canonical_json(first.relief_domain_outcome) == _canonical_json(
        second.relief_domain_outcome
    )
    assert _canonical_json(first.relief_impacts) == _canonical_json(second.relief_impacts)
    assert first.total_reliefs == second.total_reliefs
    assert first.net_tax_due == second.net_tax_due


def test_resident_relief_golden_fixture_matches_exact_output() -> None:
    """Verify resident golden fixture still locks exact relief behavior."""

    fixture = _load_fixture(RESIDENT_GOLDEN_CASE_PATH)
    actual_output = execute_computation(
        ComputationExecutionRequest.model_validate(fixture["request"])
    ).model_dump(mode="json")

    assert _canonical_json(actual_output) == _canonical_json(fixture["expected_output"])


def test_non_resident_relief_golden_fixture_matches_exact_output() -> None:
    """Verify non-resident golden fixture still locks exact relief behavior."""

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
