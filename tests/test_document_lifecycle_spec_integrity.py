"""Lightweight integrity checks for governed document lifecycle specification."""

from __future__ import annotations

from pathlib import Path

SPEC_PATH = Path("docs/governance/phase-7-document-lifecycle-spec.md")

REQUIRED_SECTION_HEADINGS = {
    "## status",
    "## state_catalog",
    "## allowed_transitions",
    "## forbidden_transitions",
    "## retention_and_purge_constraints",
    "## compliance_lock_constraints",
    "## audit_requirements",
    "## implementation_notes",
    "## spec_rule_to_migration_trigger_reference",
}
MANDATORY_RUNTIME_STATES = {
    "uploaded",
    "processing",
    "validated",
    "eligible_for_purge",
    "purged",
}


def test_document_lifecycle_spec_exists_and_has_required_sections() -> None:
    assert SPEC_PATH.exists()
    content = SPEC_PATH.read_text(encoding="utf-8").lower()

    for heading in REQUIRED_SECTION_HEADINGS:
        assert heading in content


def test_document_lifecycle_spec_contains_mandatory_runtime_states() -> None:
    content = SPEC_PATH.read_text(encoding="utf-8").lower()

    for state in MANDATORY_RUNTIME_STATES:
        assert state in content


def test_document_lifecycle_spec_has_explicit_forbidden_transitions_section() -> None:
    content = SPEC_PATH.read_text(encoding="utf-8").lower()

    assert "## forbidden_transitions" in content
    assert "forbidden runtime transitions" in content
