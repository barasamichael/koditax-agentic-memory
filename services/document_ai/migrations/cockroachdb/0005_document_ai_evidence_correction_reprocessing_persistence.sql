-- Document AI CockroachDB evidence, correction, and reprocessing persistence.
--
-- This migration ports the durable evidence lineage, append-only corrections,
-- effective values, invalidations, remappings, and reprocessing candidate
-- records to the CockroachDB lane without PostgreSQL trigger/function logic.

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
    CONSTRAINT uq_document_ai_evidence_requirements_scope
        UNIQUE (tenant_id, evidence_requirement_id),
    CONSTRAINT fk_document_ai_evidence_requirements_version_scope
        FOREIGN KEY (tenant_id, document_version_id)
        REFERENCES document_ai_document_versions (tenant_id, document_version_id)
        ON DELETE RESTRICT
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
        REFERENCES document_ai_evidence_requirements (tenant_id, evidence_requirement_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_evidence_items_version_scope
        FOREIGN KEY (tenant_id, document_version_id)
        REFERENCES document_ai_document_versions (tenant_id, document_version_id)
        ON DELETE RESTRICT
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
        REFERENCES document_ai_evidence_items (tenant_id, evidence_item_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_evidence_sources_element_scope
        FOREIGN KEY (tenant_id, canonical_element_id)
        REFERENCES document_ai_canonical_elements (tenant_id, canonical_element_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_evidence_sources_region_scope
        FOREIGN KEY (tenant_id, source_region_id)
        REFERENCES document_ai_source_regions (tenant_id, source_region_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_evidence_sources_artifact_scope
        FOREIGN KEY (tenant_id, source_artifact_id)
        REFERENCES document_ai_source_artifacts (tenant_id, source_artifact_id)
        ON DELETE RESTRICT,
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
    resolved_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_ai_evidence_conflicts_scope UNIQUE (tenant_id, evidence_conflict_id),
    CONSTRAINT fk_document_ai_evidence_conflicts_item_scope
        FOREIGN KEY (tenant_id, evidence_item_id)
        REFERENCES document_ai_evidence_items (tenant_id, evidence_item_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_evidence_conflicts_other_item_scope
        FOREIGN KEY (tenant_id, conflicting_evidence_item_id)
        REFERENCES document_ai_evidence_items (tenant_id, evidence_item_id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_document_ai_evidence_conflicts_pair
        UNIQUE (tenant_id, evidence_item_id, conflicting_evidence_item_id),
    CONSTRAINT chk_document_ai_evidence_conflicts_distinct CHECK (
        evidence_item_id <> conflicting_evidence_item_id
    ),
    CONSTRAINT chk_document_ai_evidence_conflicts_state CHECK (
        state IN ('open', 'resolving', 'resolved', 'dismissed')
    )
);

CREATE INDEX IF NOT EXISTS idx_document_ai_evidence_requirements_version
    ON document_ai_evidence_requirements (tenant_id, document_version_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_document_ai_evidence_items_requirement
    ON document_ai_evidence_items (tenant_id, evidence_requirement_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_document_ai_evidence_sources_item
    ON document_ai_evidence_sources (tenant_id, evidence_item_id);

CREATE INDEX IF NOT EXISTS idx_document_ai_evidence_conflicts_state
    ON document_ai_evidence_conflicts (tenant_id, state, created_at DESC);

CREATE TABLE IF NOT EXISTS document_ai_corrections (
    correction_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    document_version_id UUID NOT NULL,
    canonical_element_id UUID NOT NULL,
    evidence_item_id UUID NULL,
    supersedes_correction_id UUID NULL,
    reversal_of_correction_id UUID NULL,
    prior_observed_value JSONB NULL,
    prior_normalized_value JSONB NULL,
    corrected_value JSONB NOT NULL,
    reason TEXT NOT NULL,
    actor_user_id UUID NOT NULL,
    correction_state TEXT NOT NULL DEFAULT 'active',
    idempotency_key TEXT NULL,
    source_observed_value JSONB NULL,
    original_interpreted_value JSONB NULL,
    effective_value JSONB NULL,
    policy_version TEXT NOT NULL DEFAULT 'v1',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_ai_corrections_scope UNIQUE (tenant_id, correction_id),
    CONSTRAINT fk_document_ai_corrections_version_scope
        FOREIGN KEY (tenant_id, document_version_id)
        REFERENCES document_ai_document_versions (tenant_id, document_version_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_corrections_element_scope
        FOREIGN KEY (tenant_id, canonical_element_id)
        REFERENCES document_ai_canonical_elements (tenant_id, canonical_element_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_corrections_evidence_scope
        FOREIGN KEY (tenant_id, evidence_item_id)
        REFERENCES document_ai_evidence_items (tenant_id, evidence_item_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_corrections_supersedes
        FOREIGN KEY (tenant_id, supersedes_correction_id)
        REFERENCES document_ai_corrections (tenant_id, correction_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_corrections_reversal
        FOREIGN KEY (tenant_id, reversal_of_correction_id)
        REFERENCES document_ai_corrections (tenant_id, correction_id)
        ON DELETE RESTRICT,
    CONSTRAINT chk_document_ai_corrections_state
        CHECK (correction_state IN ('active', 'reversed', 'superseded'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_document_ai_corrections_idempotency
    ON document_ai_corrections (tenant_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_document_ai_corrections_element
    ON document_ai_corrections (tenant_id, canonical_element_id, created_at DESC);

CREATE TABLE IF NOT EXISTS document_ai_effective_values (
    tenant_id TEXT NOT NULL,
    canonical_element_id UUID NOT NULL,
    source_observed_value JSONB NOT NULL,
    original_interpreted_value JSONB NOT NULL,
    corrected_value JSONB NULL,
    effective_value JSONB NOT NULL,
    active_correction_id UUID NULL,
    correction_state TEXT NOT NULL DEFAULT 'original',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, canonical_element_id),
    CONSTRAINT fk_document_ai_effective_values_element
        FOREIGN KEY (tenant_id, canonical_element_id)
        REFERENCES document_ai_canonical_elements (tenant_id, canonical_element_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_effective_values_correction
        FOREIGN KEY (tenant_id, active_correction_id)
        REFERENCES document_ai_corrections (tenant_id, correction_id)
        ON DELETE RESTRICT,
    CONSTRAINT chk_document_ai_effective_values_state
        CHECK (correction_state IN ('original', 'corrected'))
);

CREATE INDEX IF NOT EXISTS idx_document_ai_effective_values_correction
    ON document_ai_effective_values (tenant_id, active_correction_id);

CREATE TABLE IF NOT EXISTS document_ai_correction_invalidations (
    correction_invalidation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    correction_id UUID NOT NULL,
    dependency_kind TEXT NOT NULL,
    dependency_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_ai_correction_invalidations_scope
        UNIQUE (tenant_id, correction_id, dependency_kind, dependency_id),
    CONSTRAINT fk_document_ai_correction_invalidations_correction
        FOREIGN KEY (tenant_id, correction_id)
        REFERENCES document_ai_corrections (tenant_id, correction_id)
        ON DELETE RESTRICT,
    CONSTRAINT chk_document_ai_correction_invalidations_state
        CHECK (state IN ('pending', 'completed', 'failed')),
    CONSTRAINT chk_document_ai_correction_invalidations_dependency_kind CHECK (
        dependency_kind IN (
            'effective_canonical',
            'retrieval_chunk',
            'embedding',
            'evidence',
            'workflow_projection',
            'cache'
        )
    )
);

CREATE INDEX IF NOT EXISTS idx_document_ai_correction_invalidations_correction
    ON document_ai_correction_invalidations (tenant_id, correction_id, state);

CREATE TABLE IF NOT EXISTS document_ai_reprocessing_candidates (
    reprocessing_candidate_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    document_version_id UUID NOT NULL,
    processing_operation_id UUID NULL,
    prior_active_representation_id UUID NULL,
    candidate_representation_id UUID NULL,
    model_policy_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    canonical_schema_version TEXT NOT NULL,
    embedding_version TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'building',
    validation_report JSONB NOT NULL DEFAULT '{}'::jsonb,
    activated_at TIMESTAMPTZ NULL,
    rolled_back_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_ai_reprocessing_candidates_scope
        UNIQUE (tenant_id, reprocessing_candidate_id),
    CONSTRAINT fk_document_ai_reprocessing_candidate_version
        FOREIGN KEY (tenant_id, document_version_id)
        REFERENCES document_ai_document_versions (tenant_id, document_version_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_reprocessing_candidate_prior
        FOREIGN KEY (tenant_id, prior_active_representation_id)
        REFERENCES document_ai_canonical_representations (tenant_id, canonical_representation_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_reprocessing_candidate_representation
        FOREIGN KEY (tenant_id, candidate_representation_id)
        REFERENCES document_ai_canonical_representations (tenant_id, canonical_representation_id)
        ON DELETE RESTRICT,
    CONSTRAINT chk_document_ai_reprocessing_candidate_state
        CHECK (state IN ('building', 'validated', 'rejected', 'active', 'rolled_back'))
);

CREATE INDEX IF NOT EXISTS idx_document_ai_reprocessing_candidates_scope
    ON document_ai_reprocessing_candidates (tenant_id, document_version_id, state, created_at DESC);

CREATE TABLE IF NOT EXISTS document_ai_correction_remappings (
    correction_remapping_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    reprocessing_candidate_id UUID NOT NULL,
    correction_id UUID NOT NULL,
    prior_stable_key TEXT NOT NULL,
    candidate_stable_key TEXT NULL,
    state TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_document_ai_correction_remappings_candidate
        FOREIGN KEY (tenant_id, reprocessing_candidate_id)
        REFERENCES document_ai_reprocessing_candidates (tenant_id, reprocessing_candidate_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_correction_remappings_correction
        FOREIGN KEY (tenant_id, correction_id)
        REFERENCES document_ai_corrections (tenant_id, correction_id)
        ON DELETE RESTRICT,
    CONSTRAINT chk_document_ai_correction_remappings_state
        CHECK (state IN ('preserved', 'remapped', 'unresolved')),
    CONSTRAINT uq_document_ai_correction_remappings_candidate_correction
        UNIQUE (tenant_id, reprocessing_candidate_id, correction_id)
);

CREATE INDEX IF NOT EXISTS idx_document_ai_correction_remappings_candidate
    ON document_ai_correction_remappings (tenant_id, reprocessing_candidate_id, state);

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
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_ai_workflow_projections_scope
        UNIQUE (tenant_id, workflow_projection_id),
    CONSTRAINT fk_document_ai_workflow_projections_version_scope
        FOREIGN KEY (tenant_id, document_version_id)
        REFERENCES document_ai_document_versions (tenant_id, document_version_id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_document_ai_workflow_projections_version
        UNIQUE (tenant_id, document_version_id, workflow_identity, workflow_version, projection_version),
    CONSTRAINT chk_document_ai_workflow_projections_state
        CHECK (validity_state IN ('valid', 'partial', 'invalidated', 'superseded'))
);

CREATE INDEX IF NOT EXISTS idx_document_ai_workflow_projections_scope
    ON document_ai_workflow_projections (tenant_id, document_version_id, validity_state, created_at DESC);

CREATE TABLE IF NOT EXISTS document_ai_migration_mappings (
    migration_mapping_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    legacy_system TEXT NOT NULL,
    legacy_record_id TEXT NOT NULL,
    document_id UUID NOT NULL,
    document_version_id UUID NULL,
    mapped_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    mapping_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_ai_migration_mappings_scope UNIQUE (tenant_id, migration_mapping_id),
    CONSTRAINT fk_document_ai_migration_mappings_document_scope
        FOREIGN KEY (tenant_id, document_id)
        REFERENCES document_ai_documents (tenant_id, document_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_migration_mappings_version_scope
        FOREIGN KEY (tenant_id, document_version_id)
        REFERENCES document_ai_document_versions (tenant_id, document_version_id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_document_ai_migration_mappings_legacy
        UNIQUE (tenant_id, legacy_system, legacy_record_id)
);

CREATE INDEX IF NOT EXISTS idx_document_ai_migration_mappings_document_scope
    ON document_ai_migration_mappings (tenant_id, document_id, mapped_at DESC);

