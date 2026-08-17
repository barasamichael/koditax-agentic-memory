"""Integrity checks for phase 7 document release-readiness report artifact."""

from __future__ import annotations

import re
from pathlib import Path

REPORT_PATH = Path("docs/governance/phase-7-document-release-readiness-report.md")

REQUIRED_SECTIONS = {
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

REQUIRED_RISK_FIELDS = {
    "risk_id",
    "severity",
    "description",
    "mitigation",
    "owner",
    "status",
}


def test_report_exists_and_is_markdown_text() -> None:
    assert REPORT_PATH.exists()
    content = REPORT_PATH.read_text(encoding="utf-8")
    assert content.strip()
    assert content.lstrip().startswith("# ")


def test_report_contains_required_section_headings() -> None:
    content = REPORT_PATH.read_text(encoding="utf-8").lower()
    for section in REQUIRED_SECTIONS:
        assert section in content


def test_report_includes_explicit_excluded_scope_terms() -> None:
    content = REPORT_PATH.read_text(encoding="utf-8").lower()
    assert "## excluded_scope" in content
    assert "vat" in content
    assert "wht" in content
    assert "corporate-tax" in content
    assert "health-contribution" in content


def test_report_includes_explicit_recommendation_status_value() -> None:
    content = REPORT_PATH.read_text(encoding="utf-8").lower()
    assert "## recommendation_status" in content
    match = re.search(r"`status`:\s*`(ready_with_controls|not_ready)`", content)
    assert match is not None


def test_residual_risk_register_has_required_fields() -> None:
    content = REPORT_PATH.read_text(encoding="utf-8").lower()
    assert "## residual_risk_register" in content
    for field_name in REQUIRED_RISK_FIELDS:
        assert field_name in content


def test_recommendation_binds_to_uat_and_validation_evidence() -> None:
    content = REPORT_PATH.read_text(encoding="utf-8").lower()
    assert "phase-7-document-uat-acceptance.md" in content
    assert "document-ai-pilot-uat-checklist.md" in content
    assert "document_ai_pilot_uat_signoff_template.json" in content
    assert "tests/test_document_ai_income_tax_pilot_scenarios_e2e.py" in content
    assert "tests/test_income_tax_prompt_flow_e2e.py" in content
    assert "tests/test_document_ai_pilot_uat_artifacts.py" in content
