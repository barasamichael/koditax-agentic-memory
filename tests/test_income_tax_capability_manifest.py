"""Verify governed income-tax capability manifest integrity for Phase 6 pilot scope."""

from __future__ import annotations

from typing import cast

import pytest

from shared.validation.income_tax_capability_manifest import assert_supported_lane
from shared.validation.income_tax_capability_manifest import CapabilityManifestError
from shared.validation.income_tax_capability_manifest import supported_lane_contexts
from shared.validation.income_tax_capability_manifest import load_income_tax_vertical_slice_manifest

EXPECTED_SUPPORTED_LANE_CONTEXTS = {
    ("resident_employment_income_2021_01_01", "KIT-VER-20210101-A", 2021),
    ("non_resident_employment_income_2021_01_01", "KIT-VER-20210101-A", 2021),
    ("resident_employment_income_2023_07_01", "KIT-VER-20230701-A", 2023),
    ("non_resident_employment_income_2023_07_01", "KIT-VER-20230701-A", 2023),
    (
        "resident_employment_plus_qualifying_interest_2023_07_01",
        "KIT-VER-20230701-A",
        2023,
    ),
}
EXPECTED_UNSUPPORTED_DOMAINS = {
    "vat",
    "withholding_tax_generalized",
    "paye_generalized",
    "health_contribution",
}


def test_capability_manifest_loads_and_has_required_top_level_fields() -> None:
    """Verify manifest parses and exposes required deterministic governance fields."""

    manifest = load_income_tax_vertical_slice_manifest()

    assert manifest["manifest_version"] == "1.0.0"
    assert manifest["capability_scope"] == "income_tax_vertical_slice"
    assert manifest["generated_at"] == "2026-03-20T00:00:00+03:00"
    unsupported_domains = cast(list[str], manifest["unsupported_domains"])
    assert EXPECTED_UNSUPPORTED_DOMAINS.issubset(set(unsupported_domains))


def test_capability_manifest_supported_lane_set_equals_expected_lane_set() -> None:
    """Verify manifest declares exactly the currently implemented supported lane contexts."""

    manifest = load_income_tax_vertical_slice_manifest()

    assert supported_lane_contexts(manifest) == EXPECTED_SUPPORTED_LANE_CONTEXTS


def test_manifest_checker_rejects_unknown_lane_context() -> None:
    """Verify unknown lane/version contexts are rejected deterministically by checker utility."""

    manifest = load_income_tax_vertical_slice_manifest()

    with pytest.raises(CapabilityManifestError) as error_info:
        assert_supported_lane(
            manifest,
            supported_lane_id="resident_employment_income_2024_01_01",
            historical_version_id="KIT-VER-20240101-A",
            tax_year=2024,
        )

    assert error_info.value.reason == "unsupported_lane_context"
