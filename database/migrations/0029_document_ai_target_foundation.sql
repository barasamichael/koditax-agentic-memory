-- Milestone 1 / Document Policy 1.23 and Chapter 10 foundation.
-- This is intentionally additive: the Phase 7 extraction tables remain the
-- operational compatibility path until their callers are migrated.
BEGIN;

CREATE UNIQUE INDEX IF NOT EXISTS uq_document_ai_documents_tenant_document
    ON document_ai_documents (tenant_id, document_id);

CREATE TABLE IF NOT EXISTS document_ai_document_versions (
    document_version_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    document_id UUID NOT NULL,
    version_number INTEGER NOT NULL,
    version_state TEXT NOT NULL DEFAULT 'current',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    supersedes_document_version_id UUID NULL,
    CONSTRAINT uq_document_ai_document_versions_scope UNIQUE (tenant_id, document_version_id),
    CONSTRAINT fk_document_ai_document_versions_document_scope
        FOREIGN KEY (tenant_id, document_id)
        REFERENCES document_ai_documents (tenant_id, document_id) ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_document_versions_supersedes
        FOREIGN KEY (supersedes_document_version_id)
        REFERENCES document_ai_document_versions (document_version_id) ON DELETE RESTRICT,
    CONSTRAINT uq_document_ai_document_versions_number UNIQUE (tenant_id, document_id, version_number),
    CONSTRAINT chk_document_ai_document_versions_number CHECK (version_number > 0),
    CONSTRAINT chk_document_ai_document_versions_state CHECK (
        version_state IN ('current', 'superseded', 'retired', 'purged')
    )
);

CREATE TABLE IF NOT EXISTS document_ai_source_artifacts (
    source_artifact_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    document_version_id UUID NOT NULL,
    storage_key TEXT NOT NULL,
    checksum_sha256 TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    retention_state TEXT NOT NULL DEFAULT 'active',
    integrity_state TEXT NOT NULL DEFAULT 'verified',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_ai_source_artifacts_scope UNIQUE (tenant_id, source_artifact_id),
    CONSTRAINT fk_document_ai_source_artifacts_version_scope
        FOREIGN KEY (tenant_id, document_version_id)
        REFERENCES document_ai_document_versions (tenant_id, document_version_id) ON DELETE RESTRICT,
    CONSTRAINT uq_document_ai_source_artifacts_storage_key UNIQUE (tenant_id, storage_key),
    CONSTRAINT chk_document_ai_source_artifacts_checksum CHECK (
        checksum_sha256 ~ '^[a-f0-9]{64}$'
    ),
    CONSTRAINT chk_document_ai_source_artifacts_size CHECK (size_bytes > 0),
    CONSTRAINT chk_document_ai_source_artifacts_retention_state CHECK (
        retention_state IN ('active', 'held', 'purge_pending', 'purged')
    ),
    CONSTRAINT chk_document_ai_source_artifacts_integrity_state CHECK (
        integrity_state IN ('pending', 'verified', 'mismatch', 'unavailable')
    )
);

CREATE TABLE IF NOT EXISTS document_ai_document_bindings (
    document_binding_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    document_id UUID NOT NULL,
    document_version_id UUID NULL,
    binding_kind TEXT NOT NULL,
    binding_scope TEXT NOT NULL,
    bound_by_user_id UUID NULL,
    bound_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_document_ai_document_bindings_scope_id UNIQUE (tenant_id, document_binding_id),
    CONSTRAINT fk_document_ai_document_bindings_document_scope
        FOREIGN KEY (tenant_id, document_id)
        REFERENCES document_ai_documents (tenant_id, document_id) ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_document_bindings_version_scope
        FOREIGN KEY (tenant_id, document_version_id)
        REFERENCES document_ai_document_versions (tenant_id, document_version_id) ON DELETE RESTRICT,
    CONSTRAINT chk_document_ai_document_bindings_expiry CHECK (
        expires_at IS NULL OR expires_at > bound_at
    )
);

CREATE TABLE IF NOT EXISTS document_ai_processing_operations (
    processing_operation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    document_version_id UUID NOT NULL,
    operation_kind TEXT NOT NULL,
    processing_policy_version TEXT NOT NULL,
    processor_version TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ NULL,
    correlation_id TEXT NOT NULL,
    idempotency_key TEXT NULL,
    request_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_document_ai_processing_operations_scope UNIQUE (tenant_id, processing_operation_id),
    CONSTRAINT fk_document_ai_processing_operations_version_scope
        FOREIGN KEY (tenant_id, document_version_id)
        REFERENCES document_ai_document_versions (tenant_id, document_version_id) ON DELETE RESTRICT,
    CONSTRAINT uq_document_ai_processing_operations_idempotency
        UNIQUE (tenant_id, idempotency_key),
    CONSTRAINT chk_document_ai_processing_operations_state CHECK (
        state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')
    ),
    CONSTRAINT chk_document_ai_processing_operations_completion CHECK (
        (state IN ('succeeded', 'failed', 'cancelled') AND completed_at IS NOT NULL)
        OR (state IN ('queued', 'running') AND completed_at IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS document_ai_processing_work_items (
    processing_work_item_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    processing_operation_id UUID NOT NULL,
    work_kind TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    priority INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    leased_until TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_ai_processing_work_items_scope UNIQUE (tenant_id, processing_work_item_id),
    CONSTRAINT fk_document_ai_processing_work_items_operation_scope
        FOREIGN KEY (tenant_id, processing_operation_id)
        REFERENCES document_ai_processing_operations (tenant_id, processing_operation_id) ON DELETE RESTRICT,
    CONSTRAINT chk_document_ai_processing_work_items_state CHECK (
        state IN ('queued', 'leased', 'succeeded', 'failed', 'cancelled')
    )
);

CREATE TABLE IF NOT EXISTS document_ai_processing_attempts (
    processing_attempt_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    processing_work_item_id UUID NOT NULL,
    attempt_number INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'running',
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ NULL,
    error_code TEXT NULL,
    error_detail JSONB NULL,
    CONSTRAINT uq_document_ai_processing_attempts_scope UNIQUE (tenant_id, processing_attempt_id),
    CONSTRAINT fk_document_ai_processing_attempts_work_item_scope
        FOREIGN KEY (tenant_id, processing_work_item_id)
        REFERENCES document_ai_processing_work_items (tenant_id, processing_work_item_id) ON DELETE RESTRICT,
    CONSTRAINT uq_document_ai_processing_attempts_number
        UNIQUE (tenant_id, processing_work_item_id, attempt_number),
    CONSTRAINT chk_document_ai_processing_attempts_number CHECK (attempt_number > 0),
    CONSTRAINT chk_document_ai_processing_attempts_state CHECK (
        state IN ('running', 'succeeded', 'failed', 'cancelled')
    )
);

CREATE TABLE IF NOT EXISTS document_ai_processing_checkpoints (
    processing_checkpoint_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    processing_attempt_id UUID NOT NULL,
    checkpoint_key TEXT NOT NULL,
    checkpoint_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_ai_processing_checkpoints_scope UNIQUE (tenant_id, processing_checkpoint_id),
    CONSTRAINT fk_document_ai_processing_checkpoints_attempt_scope
        FOREIGN KEY (tenant_id, processing_attempt_id)
        REFERENCES document_ai_processing_attempts (tenant_id, processing_attempt_id) ON DELETE RESTRICT,
    CONSTRAINT uq_document_ai_processing_checkpoints_key
        UNIQUE (tenant_id, processing_attempt_id, checkpoint_key)
);

CREATE TABLE IF NOT EXISTS document_ai_canonical_representations (
    canonical_representation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    document_version_id UUID NOT NULL,
    processing_operation_id UUID NOT NULL,
    canonical_schema_version TEXT NOT NULL,
    processing_policy_family TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'candidate',
    is_active BOOLEAN NOT NULL DEFAULT FALSE,
    representation_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at TIMESTAMPTZ NULL,
    CONSTRAINT uq_document_ai_canonical_representations_scope UNIQUE (tenant_id, canonical_representation_id),
    CONSTRAINT fk_document_ai_canonical_representations_version_scope
        FOREIGN KEY (tenant_id, document_version_id)
        REFERENCES document_ai_document_versions (tenant_id, document_version_id) ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_canonical_representations_operation_scope
        FOREIGN KEY (tenant_id, processing_operation_id)
        REFERENCES document_ai_processing_operations (tenant_id, processing_operation_id) ON DELETE RESTRICT,
    CONSTRAINT chk_document_ai_canonical_representations_state CHECK (
        state IN ('candidate', 'validated', 'active', 'rejected', 'cancelled', 'stale', 'superseded', 'invalidated')
    ),
    CONSTRAINT chk_document_ai_canonical_representations_active CHECK (
        (is_active AND state = 'active' AND activated_at IS NOT NULL)
        OR (NOT is_active)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_document_ai_active_canonical_representation
    ON document_ai_canonical_representations (
        tenant_id, document_version_id, processing_policy_family
    ) WHERE is_active;

CREATE TABLE IF NOT EXISTS document_ai_canonical_elements (
    canonical_element_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    canonical_representation_id UUID NOT NULL,
    parent_element_id UUID NULL,
    element_type TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    observed_value JSONB NULL,
    normalized_value JSONB NULL,
    uncertainty JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_ai_canonical_elements_scope UNIQUE (tenant_id, canonical_element_id),
    CONSTRAINT fk_document_ai_canonical_elements_representation_scope
        FOREIGN KEY (tenant_id, canonical_representation_id)
        REFERENCES document_ai_canonical_representations (tenant_id, canonical_representation_id) ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_canonical_elements_parent
        FOREIGN KEY (parent_element_id)
        REFERENCES document_ai_canonical_elements (canonical_element_id) ON DELETE RESTRICT,
    CONSTRAINT uq_document_ai_canonical_elements_ordinal
        UNIQUE (tenant_id, canonical_representation_id, ordinal),
    CONSTRAINT chk_document_ai_canonical_elements_ordinal CHECK (ordinal >= 0)
);

CREATE TABLE IF NOT EXISTS document_ai_source_regions (
    source_region_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    source_artifact_id UUID NOT NULL,
    canonical_element_id UUID NULL,
    structural_unit_kind TEXT NOT NULL,
    structural_unit_index INTEGER NOT NULL,
    region_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_ai_source_regions_scope UNIQUE (tenant_id, source_region_id),
    CONSTRAINT fk_document_ai_source_regions_artifact_scope
        FOREIGN KEY (tenant_id, source_artifact_id)
        REFERENCES document_ai_source_artifacts (tenant_id, source_artifact_id) ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_source_regions_element_scope
        FOREIGN KEY (tenant_id, canonical_element_id)
        REFERENCES document_ai_canonical_elements (tenant_id, canonical_element_id) ON DELETE RESTRICT,
    CONSTRAINT chk_document_ai_source_regions_index CHECK (structural_unit_index >= 0)
);

CREATE TABLE IF NOT EXISTS document_ai_retrieval_chunks (
    retrieval_chunk_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    canonical_representation_id UUID NOT NULL,
    chunking_version TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    text_content TEXT NOT NULL,
    content_checksum_sha256 TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_ai_retrieval_chunks_scope UNIQUE (tenant_id, retrieval_chunk_id),
    CONSTRAINT fk_document_ai_retrieval_chunks_representation_scope
        FOREIGN KEY (tenant_id, canonical_representation_id)
        REFERENCES document_ai_canonical_representations (tenant_id, canonical_representation_id) ON DELETE RESTRICT,
    CONSTRAINT uq_document_ai_retrieval_chunks_ordinal
        UNIQUE (tenant_id, canonical_representation_id, chunking_version, ordinal),
    CONSTRAINT chk_document_ai_retrieval_chunks_ordinal CHECK (ordinal >= 0),
    CONSTRAINT chk_document_ai_retrieval_chunks_checksum CHECK (
        content_checksum_sha256 ~ '^[a-f0-9]{64}$'
    )
);

CREATE TABLE IF NOT EXISTS document_ai_embedding_records (
    embedding_record_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    retrieval_chunk_id UUID NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_dimensions INTEGER NOT NULL,
    embedding_vector_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_ai_embedding_records_scope UNIQUE (tenant_id, embedding_record_id),
    CONSTRAINT fk_document_ai_embedding_records_chunk_scope
        FOREIGN KEY (tenant_id, retrieval_chunk_id)
        REFERENCES document_ai_retrieval_chunks (tenant_id, retrieval_chunk_id) ON DELETE RESTRICT,
    CONSTRAINT uq_document_ai_embedding_records_model UNIQUE (tenant_id, retrieval_chunk_id, embedding_model),
    CONSTRAINT chk_document_ai_embedding_records_dimensions CHECK (embedding_dimensions > 0),
    CONSTRAINT chk_document_ai_embedding_records_vector CHECK (jsonb_typeof(embedding_vector_json) = 'array')
);

CREATE TABLE IF NOT EXISTS document_ai_evidence_requirements (
    evidence_requirement_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    document_version_id UUID NULL,
    requirement_source TEXT NOT NULL,
    semantic_meaning TEXT NOT NULL,
    expected_value_type TEXT NOT NULL,
    multiplicity TEXT NOT NULL,
    requirement_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    correlation_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_ai_evidence_requirements_scope UNIQUE (tenant_id, evidence_requirement_id),
    CONSTRAINT fk_document_ai_evidence_requirements_version_scope
        FOREIGN KEY (tenant_id, document_version_id)
        REFERENCES document_ai_document_versions (tenant_id, document_version_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS document_ai_evidence_items (
    evidence_item_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    evidence_requirement_id UUID NULL,
    document_version_id UUID NOT NULL,
    semantic_meaning TEXT NOT NULL,
    value_payload JSONB NOT NULL,
    derivation_type TEXT NOT NULL,
    assurance_state TEXT NOT NULL,
    completeness_state TEXT NOT NULL,
    correction_state TEXT NOT NULL DEFAULT 'uncorrected',
    conflict_state TEXT NOT NULL DEFAULT 'none',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_ai_evidence_items_scope UNIQUE (tenant_id, evidence_item_id),
    CONSTRAINT fk_document_ai_evidence_items_requirement_scope
        FOREIGN KEY (tenant_id, evidence_requirement_id)
        REFERENCES document_ai_evidence_requirements (tenant_id, evidence_requirement_id) ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_evidence_items_version_scope
        FOREIGN KEY (tenant_id, document_version_id)
        REFERENCES document_ai_document_versions (tenant_id, document_version_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS document_ai_evidence_sources (
    evidence_source_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    evidence_item_id UUID NOT NULL,
    canonical_element_id UUID NULL,
    source_region_id UUID NULL,
    source_artifact_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_ai_evidence_sources_scope UNIQUE (tenant_id, evidence_source_id),
    CONSTRAINT fk_document_ai_evidence_sources_item_scope
        FOREIGN KEY (tenant_id, evidence_item_id)
        REFERENCES document_ai_evidence_items (tenant_id, evidence_item_id) ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_evidence_sources_element_scope
        FOREIGN KEY (tenant_id, canonical_element_id)
        REFERENCES document_ai_canonical_elements (tenant_id, canonical_element_id) ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_evidence_sources_region_scope
        FOREIGN KEY (tenant_id, source_region_id)
        REFERENCES document_ai_source_regions (tenant_id, source_region_id) ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_evidence_sources_artifact_scope
        FOREIGN KEY (tenant_id, source_artifact_id)
        REFERENCES document_ai_source_artifacts (tenant_id, source_artifact_id) ON DELETE RESTRICT,
    CONSTRAINT chk_document_ai_evidence_sources_provenance CHECK (
        canonical_element_id IS NOT NULL OR source_region_id IS NOT NULL
    )
);

CREATE TABLE IF NOT EXISTS document_ai_evidence_conflicts (
    evidence_conflict_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    evidence_item_id UUID NOT NULL,
    conflicting_evidence_item_id UUID NOT NULL,
    state TEXT NOT NULL DEFAULT 'open',
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_ai_evidence_conflicts_scope UNIQUE (tenant_id, evidence_conflict_id),
    CONSTRAINT fk_document_ai_evidence_conflicts_item_scope
        FOREIGN KEY (tenant_id, evidence_item_id)
        REFERENCES document_ai_evidence_items (tenant_id, evidence_item_id) ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_evidence_conflicts_other_item_scope
        FOREIGN KEY (tenant_id, conflicting_evidence_item_id)
        REFERENCES document_ai_evidence_items (tenant_id, evidence_item_id) ON DELETE RESTRICT,
    CONSTRAINT uq_document_ai_evidence_conflicts_pair
        UNIQUE (tenant_id, evidence_item_id, conflicting_evidence_item_id),
    CONSTRAINT chk_document_ai_evidence_conflicts_distinct CHECK (evidence_item_id <> conflicting_evidence_item_id)
);

CREATE TABLE IF NOT EXISTS document_ai_corrections (
    correction_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    document_version_id UUID NOT NULL,
    canonical_element_id UUID NOT NULL,
    evidence_item_id UUID NULL,
    supersedes_correction_id UUID NULL,
    prior_observed_value JSONB NULL,
    prior_normalized_value JSONB NULL,
    corrected_value JSONB NOT NULL,
    reason TEXT NOT NULL,
    actor_user_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_ai_corrections_scope UNIQUE (tenant_id, correction_id),
    CONSTRAINT fk_document_ai_corrections_version_scope
        FOREIGN KEY (tenant_id, document_version_id)
        REFERENCES document_ai_document_versions (tenant_id, document_version_id) ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_corrections_element_scope
        FOREIGN KEY (tenant_id, canonical_element_id)
        REFERENCES document_ai_canonical_elements (tenant_id, canonical_element_id) ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_corrections_evidence_scope
        FOREIGN KEY (tenant_id, evidence_item_id)
        REFERENCES document_ai_evidence_items (tenant_id, evidence_item_id) ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_corrections_supersedes
        FOREIGN KEY (supersedes_correction_id)
        REFERENCES document_ai_corrections (correction_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS document_ai_workflow_projections (
    workflow_projection_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    document_version_id UUID NOT NULL,
    workflow_identity TEXT NOT NULL,
    workflow_version TEXT NOT NULL,
    projection_version TEXT NOT NULL,
    validity_state TEXT NOT NULL DEFAULT 'valid',
    projection_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    invalidated_at TIMESTAMPTZ NULL,
    CONSTRAINT uq_document_ai_workflow_projections_scope UNIQUE (tenant_id, workflow_projection_id),
    CONSTRAINT fk_document_ai_workflow_projections_version_scope
        FOREIGN KEY (tenant_id, document_version_id)
        REFERENCES document_ai_document_versions (tenant_id, document_version_id) ON DELETE RESTRICT,
    CONSTRAINT uq_document_ai_workflow_projections_version
        UNIQUE (tenant_id, document_version_id, workflow_identity, workflow_version, projection_version),
    CONSTRAINT chk_document_ai_workflow_projections_state CHECK (
        validity_state IN ('valid', 'partial', 'invalidated', 'superseded')
    )
);

CREATE TABLE IF NOT EXISTS document_ai_purge_operations (
    purge_operation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    document_id UUID NOT NULL,
    state TEXT NOT NULL DEFAULT 'requested',
    requested_by_user_id UUID NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ NULL,
    correlation_id TEXT NOT NULL,
    CONSTRAINT uq_document_ai_purge_operations_scope UNIQUE (tenant_id, purge_operation_id),
    CONSTRAINT fk_document_ai_purge_operations_document_scope
        FOREIGN KEY (tenant_id, document_id)
        REFERENCES document_ai_documents (tenant_id, document_id) ON DELETE RESTRICT,
    CONSTRAINT chk_document_ai_purge_operations_state CHECK (
        state IN ('requested', 'running', 'completed', 'failed', 'cancelled')
    )
);

CREATE TABLE IF NOT EXISTS document_ai_purge_targets (
    purge_target_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    purge_operation_id UUID NOT NULL,
    target_kind TEXT NOT NULL,
    target_reference TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    completed_at TIMESTAMPTZ NULL,
    failure_detail JSONB NULL,
    CONSTRAINT uq_document_ai_purge_targets_scope UNIQUE (tenant_id, purge_target_id),
    CONSTRAINT fk_document_ai_purge_targets_operation_scope
        FOREIGN KEY (tenant_id, purge_operation_id)
        REFERENCES document_ai_purge_operations (tenant_id, purge_operation_id) ON DELETE RESTRICT,
    CONSTRAINT uq_document_ai_purge_targets_reference
        UNIQUE (tenant_id, purge_operation_id, target_kind, target_reference),
    CONSTRAINT chk_document_ai_purge_targets_state CHECK (
        state IN ('pending', 'running', 'completed', 'failed', 'skipped')
    )
);

CREATE TABLE IF NOT EXISTS document_ai_migration_mappings (
    migration_mapping_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    legacy_system TEXT NOT NULL,
    legacy_record_id TEXT NOT NULL,
    document_id UUID NOT NULL,
    document_version_id UUID NULL,
    mapped_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    mapping_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_document_ai_migration_mappings_scope UNIQUE (tenant_id, migration_mapping_id),
    CONSTRAINT fk_document_ai_migration_mappings_document_scope
        FOREIGN KEY (tenant_id, document_id)
        REFERENCES document_ai_documents (tenant_id, document_id) ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_migration_mappings_version_scope
        FOREIGN KEY (tenant_id, document_version_id)
        REFERENCES document_ai_document_versions (tenant_id, document_version_id) ON DELETE RESTRICT,
    CONSTRAINT uq_document_ai_migration_mappings_legacy
        UNIQUE (tenant_id, legacy_system, legacy_record_id)
);

CREATE INDEX IF NOT EXISTS idx_document_ai_document_versions_scope
    ON document_ai_document_versions (tenant_id, document_id, version_number DESC);
CREATE INDEX IF NOT EXISTS idx_document_ai_source_artifacts_version
    ON document_ai_source_artifacts (tenant_id, document_version_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_document_ai_document_bindings_scope
    ON document_ai_document_bindings (tenant_id, document_id, binding_kind, bound_at DESC);
CREATE INDEX IF NOT EXISTS idx_document_ai_processing_operations_scope
    ON document_ai_processing_operations (tenant_id, document_version_id, requested_at DESC);
CREATE INDEX IF NOT EXISTS idx_document_ai_processing_work_items_claim
    ON document_ai_processing_work_items (state, available_at, priority DESC);
CREATE INDEX IF NOT EXISTS idx_document_ai_canonical_elements_representation
    ON document_ai_canonical_elements (tenant_id, canonical_representation_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_document_ai_source_regions_artifact
    ON document_ai_source_regions (tenant_id, source_artifact_id, structural_unit_index);
CREATE INDEX IF NOT EXISTS idx_document_ai_retrieval_chunks_representation
    ON document_ai_retrieval_chunks (tenant_id, canonical_representation_id, chunking_version, ordinal);
CREATE INDEX IF NOT EXISTS idx_document_ai_evidence_items_requirement
    ON document_ai_evidence_items (tenant_id, evidence_requirement_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_document_ai_evidence_sources_item
    ON document_ai_evidence_sources (tenant_id, evidence_item_id);
CREATE INDEX IF NOT EXISTS idx_document_ai_corrections_element
    ON document_ai_corrections (tenant_id, canonical_element_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_document_ai_purge_operations_scope
    ON document_ai_purge_operations (tenant_id, document_id, requested_at DESC);

CREATE OR REPLACE FUNCTION fn_document_ai_source_artifacts_prevent_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'document_ai_source_artifacts are immutable after creation';
END;
$$;

DROP TRIGGER IF EXISTS trg_document_ai_source_artifacts_prevent_update
    ON document_ai_source_artifacts;
CREATE TRIGGER trg_document_ai_source_artifacts_prevent_update
    BEFORE UPDATE ON document_ai_source_artifacts
    FOR EACH ROW
    EXECUTE FUNCTION fn_document_ai_source_artifacts_prevent_update();

CREATE OR REPLACE FUNCTION fn_document_ai_corrections_prevent_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'document_ai_corrections preserve historical observations and are immutable';
END;
$$;

DROP TRIGGER IF EXISTS trg_document_ai_corrections_prevent_update
    ON document_ai_corrections;
CREATE TRIGGER trg_document_ai_corrections_prevent_update
    BEFORE UPDATE ON document_ai_corrections
    FOR EACH ROW
    EXECUTE FUNCTION fn_document_ai_corrections_prevent_update();

COMMIT;
