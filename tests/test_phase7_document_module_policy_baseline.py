"""Integrity checks for the Phase 7 document module policy baseline doc."""

from __future__ import annotations

from pathlib import Path

DOC_PATH = Path("docs/governance/phase-7-document-module-policy-baseline.md")

REQUIRED_SECTIONS = {
    "## status",
    "## capability_scope",
    "## supported_capabilities",
    "## blocked_capabilities",
    "## action_policy_matrix",
    "## step_up_requirements",
    "## tenant_and_ownership_guardrails",
    "## audit_evidence_requirements",
    "## change_control",
}

REQUIRED_ACTION_ROWS = {
    "upload_session_creation",
    "upload_completion_registration",
    "extraction_trigger",
    "extraction_verification",
    "document_retrieval",
    "lifecycle_trash",
    "lifecycle_restore",
    "lifecycle_purge_eligibility_mark",
    "lifecycle_purge_execute",
}


def test_policy_baseline_doc_exists_and_contains_required_sections() -> None:
    assert DOC_PATH.exists()
    content = DOC_PATH.read_text(encoding="utf-8").lower()

    for section in REQUIRED_SECTIONS:
        assert section in content


def test_policy_baseline_contains_required_action_policy_rows() -> None:
    content = DOC_PATH.read_text(encoding="utf-8").lower()

    for action_id in REQUIRED_ACTION_ROWS:
        assert action_id in content
