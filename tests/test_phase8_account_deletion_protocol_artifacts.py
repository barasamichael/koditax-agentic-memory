"""Integrity checks for phase 8 account deletion governance protocol artifact."""

from __future__ import annotations

from pathlib import Path

DOC_PATH = Path("docs/governance/phase-8-account-deletion-protocol.md")

REQUIRED_SECTIONS = {
    "## purpose_and_scope",
    "## eligibility_and_blockers",
    "## deletion_request_contract",
    "## confirmation_requirements",
    "## cooldown_and_cancel_rules",
    "## execution_modes",
    "## session_and_token_revocation_requirements",
    "## audit_evidence_requirements",
    "## user_notification_requirements",
    "## error_contract_and_reason_codes",
    "## change_control",
}

REQUIRED_REASON_CODES = {
    "deletion_requires_step_up",
    "deletion_cooldown_active",
    "deletion_blocked_by_compliance_lock",
    "deletion_blocked_by_retention_requirement",
    "deletion_request_not_found_or_expired",
}


def test_protocol_file_exists_and_required_sections_present() -> None:
    assert DOC_PATH.exists()
    content = DOC_PATH.read_text(encoding="utf-8").lower()
    for section in REQUIRED_SECTIONS:
        assert section in content


def test_protocol_includes_step_up_and_otp_channel_requirements() -> None:
    content = DOC_PATH.read_text(encoding="utf-8").lower()
    assert "re-authentication" in content
    assert "otp" in content
    assert "sms" in content
    assert "email" in content


def test_protocol_reason_code_section_includes_required_codes() -> None:
    content = DOC_PATH.read_text(encoding="utf-8").lower()
    assert "## error_contract_and_reason_codes" in content
    for reason_code in REQUIRED_REASON_CODES:
        assert reason_code in content
