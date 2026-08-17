"""Validate core DB schema migration baseline structure and enforcement markers."""

from __future__ import annotations

from pathlib import Path

MIGRATION_FILE = Path("database/migrations/0001_core_schema_baseline.sql")
ALIGNMENT_MIGRATION_FILE = Path("database/migrations/0016_health_contribution_regime_alignment.sql")
KNOWLEDGE_MIGRATION_FILE = Path(
    "database/migrations/0017_knowledge_persistent_catalog_baseline.sql"
)
KNOWLEDGE_HYBRID_MIGRATION_FILE = Path(
    "database/migrations/0018_knowledge_hybrid_retrieval_embeddings.sql"
)


def test_core_migration_declares_all_required_tables() -> None:
    """Verify baseline migration includes all required core table declarations.

    :return: None.
    """

    sql = _load_sql()
    required_tables = (
        "users",
        "sessions",
        "delegations",
        "computations",
        "computation_results",
        "validations",
        "documents",
        "document_extractions",
        "forms",
        "reports",
        "submissions",
        "audit_events",
    )
    for table_name in required_tables:
        assert f"create table {table_name} (" in sql


def test_core_migration_declares_key_constraints_and_triggers() -> None:
    """Verify baseline migration includes required DB-level enforcement objects.

    :return: None.
    """

    sql = _load_sql()
    required_markers = (
        "constraint chk_users_exactly_one_role",
        "constraint uq_sessions_idempotency_key unique (idempotency_key)",
        "constraint uq_submissions_idempotency_key unique (idempotency_key)",
        "constraint chk_computations_health_tax_regime_identifier check",
        "constraint fk_forms_computation_id_computations",
        "constraint fk_submissions_confirmation_event_id_audit_events",
        "create trigger trg_audit_events_prevent_update",
        "create trigger trg_audit_events_prevent_delete",
        "create trigger trg_documents_enforce_state_transition",
        "create trigger trg_documents_prevent_delete_before_eligibility",
    )
    for marker in required_markers:
        assert marker in sql


def test_health_contribution_alignment_migration_updates_regime_contract() -> None:
    """Verify the forward migration aligns persisted computations with health_contribution."""

    sql = ALIGNMENT_MIGRATION_FILE.read_text(encoding="utf-8").lower()

    assert "update computations" in sql
    assert "set regime_type = 'health_contribution'" in sql
    assert "where regime_type = 'health_tax'" in sql
    assert "drop constraint if exists chk_computations_health_tax_regime_identifier" in sql
    assert "regime_type in ('income_tax', 'health_contribution', 'vat', 'other')" in sql
    assert "constraint chk_computations_health_contribution_regime_identifier" in sql


def test_knowledge_persistence_migration_declares_governed_catalog_tables() -> None:
    """Verify the knowledge persistence migration declares governed catalog tables."""

    sql = KNOWLEDGE_MIGRATION_FILE.read_text(encoding="utf-8").lower()

    required_tables = (
        "create table if not exists knowledge_sources",
        "create table if not exists knowledge_source_versions",
        "create table if not exists knowledge_anchors",
        "create table if not exists knowledge_chunks",
        "create table if not exists knowledge_ingestion_jobs",
    )
    for marker in required_tables:
        assert marker in sql


def test_knowledge_persistence_migration_declares_constraints_indexes_and_triggers() -> None:
    """Verify the knowledge persistence migration declares enforcement markers."""

    sql = KNOWLEDGE_MIGRATION_FILE.read_text(encoding="utf-8").lower()

    required_markers = (
        "constraint chk_knowledge_sources_source_class",
        "constraint chk_knowledge_sources_authority_source_class_binding",
        "constraint chk_knowledge_source_versions_source_version_form",
        "constraint chk_knowledge_source_versions_source_input_origin",
        "constraint chk_knowledge_source_versions_searchable_lineage_required",
        "constraint ex_knowledge_source_versions_effective_window_no_overlap",
        "create index if not exists idx_knowledge_sources_tax_domain_authority_source_class",
        "create index if not exists idx_knowledge_source_versions_searchable_effective_window",
        "create trigger trg_knowledge_source_versions_enforce_governed_rules",
        "create trigger trg_knowledge_source_versions_prevent_searchable_mutation",
        "create trigger trg_knowledge_source_versions_prevent_searchable_delete",
        "create trigger trg_knowledge_anchors_enforce_searchable_parent",
        "create trigger trg_knowledge_anchors_prevent_update",
        "create trigger trg_knowledge_chunks_enforce_searchable_parent",
        "create trigger trg_knowledge_chunks_prevent_update",
    )
    for marker in required_markers:
        assert marker in sql


def test_knowledge_hybrid_retrieval_migration_declares_embedding_storage_and_enforcement() -> None:
    """Verify the hybrid retrieval migration declares embedding persistence controls."""

    sql = KNOWLEDGE_HYBRID_MIGRATION_FILE.read_text(encoding="utf-8").lower()

    required_markers = (
        "create table if not exists knowledge_chunk_embeddings",
        "constraint uq_knowledge_chunk_embeddings_chunk_id_embedding_model unique",
        "constraint chk_knowledge_chunk_embeddings_embedding_vector_json_is_array",
        "create index if not exists idx_knowledge_chunk_embeddings_chunk_id_embedding_model",
        "create trigger trg_knowledge_chunk_embeddings_enforce_searchable_parent",
        "create trigger trg_knowledge_chunk_embeddings_prevent_update",
        "create trigger trg_knowledge_chunk_embeddings_prevent_delete",
    )
    for marker in required_markers:
        assert marker in sql


def _load_sql() -> str:
    return MIGRATION_FILE.read_text(encoding="utf-8").lower()
