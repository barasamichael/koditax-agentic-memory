"""Integrity checks for phase 8 auth scope baseline artifact."""

from __future__ import annotations

from pathlib import Path

DOC_PATH = Path("docs/governance/phase-8-auth-scope-baseline.md")

REQUIRED_SECTIONS = {
    "## purpose",
    "## supported_capabilities",
    "## unsupported_capabilities",
    "## non_bypass_rules",
    "## deterministic_authority_boundaries",
    "## step_up_and_policy_requirements",
    "## account_deletion_scope",
    "## error_contract_requirements",
    "## change_control",
}


def test_auth_scope_baseline_file_exists_and_required_sections_present() -> None:
    assert DOC_PATH.exists()
    content = DOC_PATH.read_text(encoding="utf-8").lower()
    for section in REQUIRED_SECTIONS:
        assert section in content


def test_auth_scope_baseline_contains_required_phase8_capabilities() -> None:
    content = DOC_PATH.read_text(encoding="utf-8").lower()
    assert "registration" in content
    assert "oauth" in content
    assert "otp" in content
    assert "sms" in content
    assert "email" in content
    assert "session" in content
    assert "account deletion" in content


def test_auth_scope_baseline_explicit_non_bypass_and_authority_constraints() -> None:
    content = DOC_PATH.read_text(encoding="utf-8").lower()
    normalized = " ".join(content.split())
    assert "## non_bypass_rules" in content
    assert "auth service controls identity and access decisions" in normalized
    assert "high-risk actions require both policy allow decision and step-up proof" in normalized
    assert (
        "no direct side-effect execution is allowed without existing orchestration gates"
        in normalized
    )
    assert "## account_deletion_scope" in content
