"""Verify deterministic runtime capability-gate enforcement for income-tax prompt flows."""

from __future__ import annotations

import pytest

from shared.determinism.input_hash import canonical_json_dumps
from services.orchestration.app.income_tax_capability_gate import IncomeTaxCapabilityGateError
from services.orchestration.app.income_tax_capability_gate import (
    enforce_income_tax_runtime_capability_gate,
)


@pytest.mark.parametrize(
    ("supported_lane_id", "historical_version_id", "tax_year"),
    [
        ("resident_employment_income_2021_01_01", "KIT-VER-20210101-A", 2021),
        ("non_resident_employment_income_2021_01_01", "KIT-VER-20210101-A", 2021),
        ("resident_employment_income_2023_07_01", "KIT-VER-20230701-A", 2023),
        ("non_resident_employment_income_2023_07_01", "KIT-VER-20230701-A", 2023),
        (
            "resident_employment_plus_qualifying_interest_2023_07_01",
            "KIT-VER-20230701-A",
            2023,
        ),
    ],
)
def test_runtime_capability_gate_allows_supported_lane_contexts(
    supported_lane_id: str,
    historical_version_id: str,
    tax_year: int,
) -> None:
    decision = enforce_income_tax_runtime_capability_gate(
        prompt_text="Compute income tax for supported lane scope.",
        supported_lane_id=supported_lane_id,
        historical_version_id=historical_version_id,
        tax_year=tax_year,
    )

    assert decision["gate_status"] == "allowed"
    assert decision["capability_scope"] == "income_tax_vertical_slice"
    assert decision["manifest_version"] == "1.0.0"
    assert decision["supported_lane_id"] == supported_lane_id
    assert decision["historical_version_id"] == historical_version_id
    assert decision["tax_year"] == tax_year


def test_runtime_capability_gate_rejects_unsupported_lane_context() -> None:
    with pytest.raises(IncomeTaxCapabilityGateError) as error_info:
        enforce_income_tax_runtime_capability_gate(
            prompt_text="Compute income tax for unsupported lane scope.",
            supported_lane_id="resident_employment_income_2024_01_01",
            historical_version_id="KIT-VER-20240101-A",
            tax_year=2024,
        )

    assert error_info.value.error_code == "unsupported_prompt_scope"
    assert error_info.value.reason == "unsupported_lane_context"
    assert error_info.value.payload() == {
        "error_code": "unsupported_prompt_scope",
        "message": "Prompt scope is not supported by governed income-tax pilot capability.",
        "reason": "unsupported_lane_context",
        "rejected_context": {
            "supported_lane_id": "resident_employment_income_2024_01_01",
            "historical_version_id": "KIT-VER-20240101-A",
            "tax_year": 2024,
            "tax_domain": "income_tax",
            "prompt_class": "income_tax_prompt_flow",
        },
    }


def test_runtime_capability_gate_rejects_missing_lane_context() -> None:
    with pytest.raises(IncomeTaxCapabilityGateError) as error_info:
        enforce_income_tax_runtime_capability_gate(
            prompt_text="Compute income tax with missing lane context.",
            supported_lane_id=None,
            historical_version_id=None,
            tax_year=None,
        )

    assert error_info.value.error_code == "unsupported_prompt_scope"
    assert error_info.value.reason == "missing_lane_context"
    assert error_info.value.payload()["message"] == (
        "Prompt context does not contain governed lane/version identity for runtime gate."
    )
    assert error_info.value.payload()["rejected_context"] == {
        "supported_lane_id": None,
        "historical_version_id": None,
        "tax_year": None,
        "tax_domain": "income_tax",
        "prompt_class": "income_tax_prompt_flow",
    }


def test_runtime_capability_gate_blocks_unsupported_tax_domain_prompt_scope() -> None:
    with pytest.raises(IncomeTaxCapabilityGateError) as error_info:
        enforce_income_tax_runtime_capability_gate(
            prompt_text="Compute VAT filing output for Q3 and submit to regulator.",
            supported_lane_id=None,
            historical_version_id=None,
            tax_year=None,
        )

    assert error_info.value.error_code == "unsupported_prompt_scope"
    assert error_info.value.reason == "unsupported_domain"
    assert error_info.value.payload()["message"] == (
        "Prompt scope is not supported by governed income-tax pilot capability."
    )
    assert error_info.value.payload()["rejected_context"] == {
        "supported_lane_id": None,
        "historical_version_id": None,
        "tax_year": None,
        "tax_domain": "vat",
        "prompt_class": "income_tax_prompt_flow",
    }


def test_runtime_capability_gate_rejection_is_deterministic() -> None:
    def _unsupported_payload() -> dict[str, object]:
        with pytest.raises(IncomeTaxCapabilityGateError) as error_info:
            enforce_income_tax_runtime_capability_gate(
                prompt_text="Compute VAT filing output for Q3 and submit to regulator.",
                supported_lane_id=None,
                historical_version_id=None,
                tax_year=None,
            )
        return error_info.value.payload()

    assert canonical_json_dumps(_unsupported_payload()) == canonical_json_dumps(
        _unsupported_payload()
    )
