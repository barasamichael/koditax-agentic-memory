"""CockroachDB structural-scope migration regressions for Document AI."""

from __future__ import annotations

from pathlib import Path


def test_structural_scope_cockroach_migration_exposes_required_state() -> None:
    migration = (
        Path("services/document_ai/migrations/cockroachdb/0011_document_ai_structural_scopes.sql")
        .read_text(encoding="utf-8")
        .lower()
    )

    for marker in (
        "create table if not exists document_ai_structural_scopes",
        "uq_document_ai_structural_scopes_scope",
        "uq_document_ai_structural_scopes_identity",
        "uq_document_ai_structural_scopes_ordinal",
        "fk_document_ai_structural_scopes_document_scope",
        "fk_document_ai_structural_scopes_version_scope",
        "fk_document_ai_structural_scopes_artifact_scope",
        "fk_document_ai_structural_scopes_inspection_scope",
        "fk_document_ai_structural_scopes_operation_scope",
        "fk_document_ai_structural_scopes_parent_scope",
        "idx_document_ai_structural_scopes_lookup",
        "idx_document_ai_structural_scopes_inspection",
    ):
        assert marker in migration


def test_structural_scope_cockroach_migration_avoids_trigger_machinery() -> None:
    migration = (
        Path("services/document_ai/migrations/cockroachdb/0011_document_ai_structural_scopes.sql")
        .read_text(encoding="utf-8")
        .lower()
    )

    assert "create trigger" not in migration
    assert "create or replace function" not in migration
