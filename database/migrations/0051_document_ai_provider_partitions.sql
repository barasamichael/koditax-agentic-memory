-- Milestone 37 — deterministic provider-processing partitions for source-aware processing.

CREATE TABLE IF NOT EXISTS document_ai_provider_partitions (
    provider_partition_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    document_id UUID NOT NULL,
    document_version_id UUID NOT NULL,
    source_artifact_id UUID NOT NULL,
    source_inspection_id UUID NOT NULL,
    processing_operation_id UUID NOT NULL,
    policy_version TEXT NOT NULL,
    partition_kind TEXT NOT NULL,
    partition_ordinal INTEGER NOT NULL,
    parent_structural_scope_id UUID NULL,
    structural_scope_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    structural_coordinates JSONB NOT NULL DEFAULT '{}'::jsonb,
    partition_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    partition_identity TEXT NOT NULL,
    estimated_input_bytes INTEGER NOT NULL,
    partition_state TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_ai_provider_partitions_scope
        UNIQUE (tenant_id, provider_partition_id),
    CONSTRAINT uq_document_ai_provider_partitions_identity
        UNIQUE (tenant_id, source_inspection_id, partition_identity),
    CONSTRAINT uq_document_ai_provider_partitions_ordinal
        UNIQUE (tenant_id, source_inspection_id, partition_ordinal),
    CONSTRAINT fk_document_ai_provider_partitions_document_scope
        FOREIGN KEY (tenant_id, document_id)
        REFERENCES document_ai_documents (tenant_id, document_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_provider_partitions_version_scope
        FOREIGN KEY (tenant_id, document_version_id)
        REFERENCES document_ai_document_versions (tenant_id, document_version_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_provider_partitions_artifact_scope
        FOREIGN KEY (tenant_id, source_artifact_id)
        REFERENCES document_ai_source_artifacts (tenant_id, source_artifact_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_provider_partitions_inspection_scope
        FOREIGN KEY (tenant_id, source_inspection_id)
        REFERENCES document_ai_source_inspections (tenant_id, source_inspection_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_provider_partitions_operation_scope
        FOREIGN KEY (tenant_id, processing_operation_id)
        REFERENCES document_ai_processing_operations (tenant_id, processing_operation_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_provider_partitions_parent_scope
        FOREIGN KEY (tenant_id, parent_structural_scope_id)
        REFERENCES document_ai_structural_scopes (tenant_id, structural_scope_id)
        ON DELETE RESTRICT,
    CONSTRAINT chk_document_ai_provider_partitions_ordinal CHECK (partition_ordinal >= 0),
    CONSTRAINT chk_document_ai_provider_partitions_kind CHECK (partition_kind IN (
        'page_range',
        'slide',
        'worksheet',
        'line_range',
        'row_range',
        'paragraph_range',
        'image_frame'
    )),
    CONSTRAINT chk_document_ai_provider_partitions_estimated_input_bytes CHECK (
        estimated_input_bytes > 0
    ),
    CONSTRAINT chk_document_ai_provider_partitions_state CHECK (
        partition_state IN ('active', 'superseded')
    )
);

CREATE INDEX IF NOT EXISTS idx_document_ai_provider_partitions_lookup
    ON document_ai_provider_partitions (
        tenant_id,
        document_version_id,
        policy_version,
        partition_ordinal
    );

CREATE INDEX IF NOT EXISTS idx_document_ai_provider_partitions_inspection
    ON document_ai_provider_partitions (
        tenant_id,
        source_inspection_id,
        partition_kind,
        partition_ordinal
    );

