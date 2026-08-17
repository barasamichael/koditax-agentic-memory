"""Integrity checks for phase 7.6 document-module security compliance artifacts."""

from __future__ import annotations

import json
from typing import Any
from typing import cast
from pathlib import Path

CHECKLIST_PATH = Path("docs/governance/phase-7-document-security-compliance-checklist.md")
SIGNOFF_TEMPLATE_PATH = Path(
    "contracts/uat/document_module_security_compliance_signoff_template.json"
)

REQUIRED_CHECKLIST_SECTIONS = {
    "## access_policy_enforcement",
    "## encryption_secret_handling",
    "## redaction_controls",
    "## signed_download_controls",
    "## compliance_override_dual_control",
    "## security_regression_coverage",
}

REQUIRED_EVIDENCE_REFS = {
    "tests/test_document_ai_access_policy_gate.py",
    "tests/test_document_ai_security_encryption_controls.py",
    "tests/test_document_ai_redaction_policy.py",
    "tests/test_document_ai_signed_download_access.py",
    "tests/test_document_ai_compliance_override.py",
    "tests/test_document_ai_security_regression.py",
}

REQUIRED_SIGNOFF_KEYS = {
    "artifact_version",
    "module_scope",
    "build_ref",
    "commit_sha",
    "check_results",
    "residual_risks",
    "decision",
    "approvers",
    "signed_at_utc",
}


def test_checklist_exists_and_contains_required_sections() -> None:
    assert CHECKLIST_PATH.exists()
    content = CHECKLIST_PATH.read_text(encoding="utf-8").lower()

    for section in REQUIRED_CHECKLIST_SECTIONS:
        assert section in content
    assert "check_id" in content
    assert "control_statement" in content
    assert "required_evidence" in content
    assert "status" in content
    assert "owner" in content


def test_signoff_template_parses_and_has_required_top_level_keys() -> None:
    assert SIGNOFF_TEMPLATE_PATH.exists()
    signoff = cast(dict[str, Any], json.loads(SIGNOFF_TEMPLATE_PATH.read_text(encoding="utf-8")))

    assert REQUIRED_SIGNOFF_KEYS.issubset(set(signoff))
    assert signoff["artifact_version"] == "1.0.0"
    assert signoff["module_scope"] == "phase_7_6_document_module_security_privacy_controls"
    assert signoff["decision"] in {"approved", "rejected", "conditional"}

    build_ref = cast(dict[str, Any], signoff["build_ref"])
    assert {"build_id", "environment", "pipeline_run_id"}.issubset(set(build_ref))

    check_results = cast(list[object], signoff["check_results"])
    assert check_results
    first_result = cast(dict[str, Any], check_results[0])
    assert {"check_id", "status", "required_evidence", "owner"}.issubset(set(first_result))
    assert first_result["status"] in {"pass", "fail", "conditional", "not_applicable"}


def test_checklist_and_signoff_scope_align_to_phase_7_6_controls_only() -> None:
    checklist_content = CHECKLIST_PATH.read_text(encoding="utf-8").lower()
    signoff = cast(dict[str, Any], json.loads(SIGNOFF_TEMPLATE_PATH.read_text(encoding="utf-8")))

    assert "phase 7.6" in checklist_content
    assert "out of scope" in checklist_content
    assert "phase 7.7" in checklist_content
    assert "phase 7.8" in checklist_content
    assert "vat" in checklist_content
    assert "wht" in checklist_content
    assert "corporate-tax" in checklist_content
    assert "health-contribution" in checklist_content

    for evidence_ref in REQUIRED_EVIDENCE_REFS:
        assert evidence_ref in checklist_content

    out_of_scope_controls = cast(list[object], signoff["out_of_scope_controls"])
    assert out_of_scope_controls
    normalized_out_of_scope = {str(item) for item in out_of_scope_controls}
    assert "phase_7_7_observability_controls" in normalized_out_of_scope
    assert "phase_7_8_release_decision_packaging" in normalized_out_of_scope
