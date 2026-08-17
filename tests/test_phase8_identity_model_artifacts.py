"""Integrity checks for phase 8 canonical identity model artifact."""

from __future__ import annotations

from pathlib import Path

DOC_PATH = Path("docs/governance/phase-8-identity-model.md")

REQUIRED_SECTIONS = {
    "## identity_subject_model",
    "## tenant_boundary_model",
    "## role_model",
    "## delegation_model",
    "## session_binding_model",
    "## required_auth_context_claims",
    "## forbidden_identity_assumptions",
    "## change_control",
}

REQUIRED_CLAIMS = {
    "user_id",
    "tenant_id",
    "role",
    "session_id",
    "delegation_context",
}


def test_identity_model_file_exists_and_required_sections_present() -> None:
    assert DOC_PATH.exists()
    content = DOC_PATH.read_text(encoding="utf-8").lower()
    for section in REQUIRED_SECTIONS:
        assert section in content


def test_identity_model_includes_required_auth_context_claims() -> None:
    content = DOC_PATH.read_text(encoding="utf-8").lower()
    assert "## required_auth_context_claims" in content
    for claim in REQUIRED_CLAIMS:
        assert claim in content


def test_identity_model_explicit_role_and_delegation_constraints_present() -> None:
    content = DOC_PATH.read_text(encoding="utf-8").lower()
    normalized = " ".join(content.split())
    assert "exactly one role per user is required" in normalized
    assert "delegation does not override tenant or resource ownership controls" in normalized
    assert "signed/trusted boundary artifact" in normalized
