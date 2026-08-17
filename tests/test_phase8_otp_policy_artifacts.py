"""Integrity checks for phase 8 OTP policy governance artifact."""

from __future__ import annotations

from pathlib import Path

DOC_PATH = Path("docs/governance/phase-8-otp-policy.md")

REQUIRED_SECTIONS = {
    "## otp_purposes",
    "## ttl_policy",
    "## max_attempts_policy",
    "## resend_throttle_policy",
    "## lockout_policy",
    "## channel_routing_policy",
    "## fallback_policy",
    "## anti_enumeration_controls",
    "## audit_requirements",
    "## error_reason_codes",
    "## change_control",
}

REQUIRED_REASON_CODES = {
    "otp_expired",
    "otp_invalid",
    "otp_attempt_limit_exceeded",
    "otp_resend_throttled",
    "otp_channel_unavailable",
    "otp_step_up_required",
    "otp_locked_temporarily",
}


def test_otp_policy_file_exists_and_required_sections_present() -> None:
    assert DOC_PATH.exists()
    content = DOC_PATH.read_text(encoding="utf-8").lower()
    for section in REQUIRED_SECTIONS:
        assert section in content


def test_otp_policy_contains_required_reason_codes() -> None:
    content = DOC_PATH.read_text(encoding="utf-8").lower()
    for code in REQUIRED_REASON_CODES:
        assert code in content


def test_otp_purpose_matrix_includes_deletion_confirm_and_channels() -> None:
    content = DOC_PATH.read_text(encoding="utf-8").lower()
    normalized = " ".join(content.split())
    assert "account_deletion_confirm" in content
    assert "registration_verify" in content
    assert "login_step_up" in content
    assert "recovery" in content
    assert "sms" in content
    assert "email" in content
    assert "fresh re-authentication proof" in normalized
    assert "no channel response should leak user-existence details" in normalized
