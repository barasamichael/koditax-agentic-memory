"""Verify deterministic form-version binding for supported income-tax form outputs."""

from __future__ import annotations

import copy
import json
from uuid import uuid5
from uuid import NAMESPACE_URL
from typing import cast
from pathlib import Path

import pytest

from services.forms.app.income_tax.form_mapping import map_finalized_income_tax_output_to_form_ready
from services.forms.app.income_tax.form_version_binding import bind_income_tax_form_version
from services.forms.app.income_tax.form_version_binding import IncomeTaxFormVersionBindingError

GOLDEN_CASE_DIR = Path("eval/golden/tax_core")
FINALIZED_AT = "2026-03-15T09:30:00+03:00"


@pytest.mark.parametrize(
    ("fixture_name", "expected_form_version_id", "expected_template_id"),
    [
        (
            "income_tax_resident_employment_2021_01_01_case_001.json",
            "ITX-FORM-20210101-RES-EMP-V1",
            "income_tax_return_resident_employment_2021_01_01_v1",
        ),
        (
            "income_tax_non_resident_employment_2021_01_01_case_001.json",
            "ITX-FORM-20210101-NRES-EMP-V1",
            "income_tax_return_non_resident_employment_2021_01_01_v1",
        ),
        (
            "income_tax_resident_employment_2023_07_01_case_001.json",
            "ITX-FORM-20230701-RES-EMP-V1",
            "income_tax_return_resident_employment_2023_07_01_v1",
        ),
        (
            "income_tax_non_resident_employment_2023_07_01_case_001.json",
            "ITX-FORM-20230701-NRES-EMP-V1",
            "income_tax_return_non_resident_employment_2023_07_01_v1",
        ),
        (
            "income_tax_mixed_resident_employment_plus_qualifying_interest_2023_07_01_case_001.json",
            "ITX-FORM-20230701-RES-EMP-QINT-V1",
            "income_tax_return_resident_employment_plus_qualifying_interest_2023_07_01_v1",
        ),
    ],
)
def test_supported_lanes_bind_to_expected_form_versions(
    fixture_name: str,
    expected_form_version_id: str,
    expected_template_id: str,
) -> None:
    finalized_output = _build_finalized_output(fixture_name)
    form_ready_output = map_finalized_income_tax_output_to_form_ready(finalized_output)

    binding = bind_income_tax_form_version(form_ready_output)
    binding_lineage = _as_object(binding["binding_lineage"])

    assert binding["binding_status"] == "bound"
    assert binding["form_version_id"] == expected_form_version_id
    assert binding["template_id"] == expected_template_id
    assert binding_lineage["computation_id"] == finalized_output["computation_id"]
    assert (
        binding_lineage["finalized_audit_event_id"] == finalized_output["finalized_audit_event_id"]
    )


def test_mixed_income_binding_preserves_historical_version_identity() -> None:
    finalized_output = _build_finalized_output(
        "income_tax_mixed_resident_employment_plus_qualifying_interest_2023_07_01_case_001.json"
    )
    form_ready_output = map_finalized_income_tax_output_to_form_ready(finalized_output)

    binding = bind_income_tax_form_version(form_ready_output)

    assert binding["supported_lane_id"] == "resident_employment_plus_qualifying_interest_2023_07_01"
    assert binding["historical_version_id"] == "KIT-VER-20230701-A"
    assert binding["tax_year"] == 2023


def test_binding_rejects_unsupported_historical_version_context() -> None:
    finalized_output = _build_finalized_output(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )
    form_ready_output = map_finalized_income_tax_output_to_form_ready(finalized_output)
    version_identity = _as_object(form_ready_output["version_identity"])
    version_identity["historical_version_id"] = "KIT-VER-20200901-A"
    form_ready_output["version_identity"] = version_identity

    with pytest.raises(IncomeTaxFormVersionBindingError) as error_info:
        bind_income_tax_form_version(form_ready_output)

    assert error_info.value.reason == "unsupported_form_version_binding"


def test_binding_rejects_unsupported_form_type_scope() -> None:
    finalized_output = _build_finalized_output(
        "income_tax_resident_employment_2023_07_01_case_001.json"
    )
    form_ready_output = map_finalized_income_tax_output_to_form_ready(finalized_output)
    form_ready_output["form_type"] = "vat_return"

    with pytest.raises(IncomeTaxFormVersionBindingError) as error_info:
        bind_income_tax_form_version(form_ready_output)

    assert error_info.value.reason == "unsupported_form_type"


def test_binding_is_deterministic_for_same_form_ready_output() -> None:
    finalized_output = _build_finalized_output(
        "income_tax_non_resident_employment_2021_01_01_case_001.json"
    )
    form_ready_output = map_finalized_income_tax_output_to_form_ready(finalized_output)

    first = bind_income_tax_form_version(copy.deepcopy(form_ready_output))
    second = bind_income_tax_form_version(copy.deepcopy(form_ready_output))

    assert second == first


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


def _as_object(value: object) -> dict[str, object]:
    return cast(dict[str, object], value)
