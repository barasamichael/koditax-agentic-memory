-- Milestone 34 — deterministic structural scopes for source-aware processing.

CREATE TABLE IF NOT EXISTS document_ai_structural_scopes (
    structural_scope_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    document_id UUID NOT NULL,
    document_version_id UUID NOT NULL,
    source_artifact_id UUID NOT NULL,
    source_inspection_id UUID NOT NULL,
    processing_operation_id UUID NOT NULL,
    policy_version TEXT NOT NULL,
    scope_kind TEXT NOT NULL,
    scope_ordinal INTEGER NOT NULL,
    parent_structural_scope_id UUID NULL,
    structural_coordinates JSONB NOT NULL DEFAULT '{}'::jsonb,
    scope_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    scope_identity TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_ai_structural_scopes_scope
        UNIQUE (tenant_id, structural_scope_id),
    CONSTRAINT uq_document_ai_structural_scopes_identity
        UNIQUE (tenant_id, source_inspection_id, scope_identity),
    CONSTRAINT uq_document_ai_structural_scopes_ordinal
        UNIQUE (tenant_id, source_inspection_id, scope_ordinal),
    CONSTRAINT fk_document_ai_structural_scopes_document_scope
        FOREIGN KEY (tenant_id, document_id)
        REFERENCES document_ai_documents (tenant_id, document_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_structural_scopes_version_scope
        FOREIGN KEY (tenant_id, document_version_id)
        REFERENCES document_ai_document_versions (tenant_id, document_version_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_structural_scopes_artifact_scope
        FOREIGN KEY (tenant_id, source_artifact_id)
        REFERENCES document_ai_source_artifacts (tenant_id, source_artifact_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_structural_scopes_inspection_scope
        FOREIGN KEY (tenant_id, source_inspection_id)
        REFERENCES document_ai_source_inspections (tenant_id, source_inspection_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_structural_scopes_operation_scope
        FOREIGN KEY (tenant_id, processing_operation_id)
        REFERENCES document_ai_processing_operations (tenant_id, processing_operation_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_structural_scopes_parent_scope
        FOREIGN KEY (tenant_id, parent_structural_scope_id)
        REFERENCES document_ai_structural_scopes (tenant_id, structural_scope_id)
        ON DELETE RESTRICT,
    CONSTRAINT chk_document_ai_structural_scopes_ordinal CHECK (scope_ordinal >= 0),
    CONSTRAINT chk_document_ai_structural_scopes_kind CHECK (scope_kind IN (
        'document',
        'page_range',
        'slide',
        'worksheet',
        'line_range',
        'row_range',
        'paragraph_range',
        'image_frame'
    ))
);

CREATE INDEX IF NOT EXISTS idx_document_ai_structural_scopes_lookup
    ON document_ai_structural_scopes (
        tenant_id,
        document_version_id,
        policy_version,
        scope_ordinal
    );

CREATE INDEX IF NOT EXISTS idx_document_ai_structural_scopes_inspection
    ON document_ai_structural_scopes (
        tenant_id,
        source_inspection_id,
        scope_kind,
        scope_ordinal
    );
