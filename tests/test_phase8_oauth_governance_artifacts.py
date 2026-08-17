"""Integrity checks for phase 8 OAuth/OIDC governance artifact."""

from __future__ import annotations

from pathlib import Path

DOC_PATH = Path("docs/governance/phase-8-oauth-provider-governance.md")

REQUIRED_SECTIONS = {
    "## supported_oauth_flows",
    "## pkce_and_state_requirements",
    "## id_token_and_claim_validation_rules",
    "## provider_trust_and_key_rotation_policy",
    "## account_linking_and_jit_provisioning_rules",
    "## conflict_resolution_policy",
    "## degraded_mode_and_outage_handling",
    "## audit_and_trace_requirements",
    "## error_reason_codes",
    "## change_control",
}

REQUIRED_REASON_CODES = {
    "oauth_state_mismatch",
    "oauth_nonce_mismatch",
    "oauth_invalid_signature",
    "oauth_invalid_issuer_or_audience",
    "oauth_claim_mapping_conflict",
    "oauth_provider_unavailable",
    "oauth_linking_requires_user_confirmation",
}


def test_oauth_governance_file_exists_and_required_sections_present() -> None:
    assert DOC_PATH.exists()
    content = DOC_PATH.read_text(encoding="utf-8").lower()
    for section in REQUIRED_SECTIONS:
        assert section in content


def test_oauth_governance_contains_required_reason_codes() -> None:
    content = DOC_PATH.read_text(encoding="utf-8").lower()
    for code in REQUIRED_REASON_CODES:
        assert code in content


def test_oauth_governance_explicit_pkce_state_and_non_bypass_statements() -> None:
    content = DOC_PATH.read_text(encoding="utf-8").lower()
    normalized = " ".join(content.split())
    assert "authorization code with pkce (`s256`) only" in normalized
    assert "## pkce_and_state_requirements" in content
    assert "state" in content
    assert "nonce" in content
    assert "no provider token or claims may bypass local authorization policy" in normalized
    assert "no automatic account merge is allowed on ambiguous identity match" in normalized
