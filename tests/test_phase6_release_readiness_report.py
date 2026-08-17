"""Lightweight integrity checks for phase 6 release-readiness report artifact."""

from __future__ import annotations

from pathlib import Path

REPORT_PATH = Path("docs/governance/phase-6-release-readiness-report.md")

REQUIRED_SECTION_ANCHORS = {
    "## release_scope",
    "## excluded_scope",
    "## evidence_summary",
    "## known_limits",
    "## mitigations_and_controls",
    "## residual_risk_register",
    "## operational_preconditions",
    "## recommendation_status",
    "## reviewers_and_date",
}
REQUIRED_SUPPORTED_LANES = {
    "resident_employment_income_2021_01_01",
    "non_resident_employment_income_2021_01_01",
    "resident_employment_income_2023_07_01",
    "non_resident_employment_income_2023_07_01",
    "resident_employment_plus_qualifying_interest_2023_07_01",
}


def test_release_readiness_report_contains_required_sections() -> None:
    assert REPORT_PATH.exists()
    content = REPORT_PATH.read_text(encoding="utf-8").lower()

    for anchor in REQUIRED_SECTION_ANCHORS:
        assert anchor in content


def test_release_readiness_report_has_explicit_excluded_scope_section() -> None:
    content = REPORT_PATH.read_text(encoding="utf-8").lower()

    assert "## excluded_scope" in content
    assert "vat" in content
    assert "wht" in content
    assert "corporate-tax" in content
    assert "health-contribution" in content


def test_release_readiness_report_scope_references_supported_lanes() -> None:
    content = REPORT_PATH.read_text(encoding="utf-8")

    for lane_id in REQUIRED_SUPPORTED_LANES:
        assert lane_id in content
