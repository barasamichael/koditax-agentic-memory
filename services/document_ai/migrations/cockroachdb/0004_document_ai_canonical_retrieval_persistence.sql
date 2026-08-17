-- Document AI CockroachDB canonical and retrieval persistence.
--
-- This migration materializes the durable canonical graph, retrieval chunk,
-- and embedding persistence contract used by the current Document AI runtime.
-- It intentionally avoids PostgreSQL-only trigger/function machinery and
-- semantic vector indexing so the CockroachDB lane stays schema-compatible
-- without pre-committing retrieval behavior.

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
    source_artifact_id UUID NULL,
    provider_result_id UUID NULL,
    assembly_policy_version TEXT NULL,
    content_hash_sha256 TEXT NULL,
    canonical_validation_version TEXT NULL,
    validation_report JSONB NOT NULL DEFAULT '{}'::jsonb,
    readiness_state TEXT NOT NULL DEFAULT 'none',
    validated_at TIMESTAMPTZ NULL,
    rejected_at TIMESTAMPTZ NULL,
    CONSTRAINT uq_document_ai_canonical_representations_scope
        UNIQUE (tenant_id, canonical_representation_id),
    CONSTRAINT fk_document_ai_canonical_representations_version_scope
        FOREIGN KEY (tenant_id, document_version_id)
        REFERENCES document_ai_document_versions (tenant_id, document_version_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_canonical_representations_operation_scope
        FOREIGN KEY (tenant_id, processing_operation_id)
        REFERENCES document_ai_processing_operations (tenant_id, processing_operation_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_canonical_representations_artifact_scope
        FOREIGN KEY (tenant_id, source_artifact_id)
        REFERENCES document_ai_source_artifacts (tenant_id, source_artifact_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_canonical_representations_provider_result_scope
        FOREIGN KEY (tenant_id, provider_result_id)
        REFERENCES document_ai_provider_results (tenant_id, provider_result_id)
        ON DELETE RESTRICT,
    CONSTRAINT chk_document_ai_canonical_representations_state CHECK (
        state IN (
            'candidate',
            'validated',
            'active',
            'rejected',
            'cancelled',
            'stale',
            'superseded',
            'invalidated'
        )
    ),
    CONSTRAINT chk_document_ai_canonical_representations_active CHECK (
        (is_active AND state = 'active' AND activated_at IS NOT NULL)
        OR (NOT is_active)
    ),
    CONSTRAINT chk_document_ai_canonical_representations_readiness_state CHECK (
        readiness_state IN ('none', 'partial', 'full')
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_document_ai_active_canonical_representation
    ON document_ai_canonical_representations (tenant_id, document_version_id, processing_policy_family)
    WHERE is_active;

CREATE UNIQUE INDEX IF NOT EXISTS uq_document_ai_canonical_representation_provider_result
    ON document_ai_canonical_representations (tenant_id, provider_result_id)
    WHERE provider_result_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_document_ai_canonical_validation_readiness
    ON document_ai_canonical_representations
       (tenant_id, document_version_id, readiness_state, created_at DESC);

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
    stable_key TEXT NULL,
    page_number INTEGER NULL,
    reading_order INTEGER NULL,
    CONSTRAINT uq_document_ai_canonical_elements_scope UNIQUE (tenant_id, canonical_element_id),
    CONSTRAINT fk_document_ai_canonical_elements_representation_scope
        FOREIGN KEY (tenant_id, canonical_representation_id)
        REFERENCES document_ai_canonical_representations (tenant_id, canonical_representation_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_canonical_elements_parent
        FOREIGN KEY (parent_element_id)
        REFERENCES document_ai_canonical_elements (canonical_element_id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_document_ai_canonical_elements_ordinal
        UNIQUE (tenant_id, canonical_representation_id, ordinal),
    CONSTRAINT chk_document_ai_canonical_elements_ordinal CHECK (ordinal >= 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_document_ai_canonical_elements_stable_key
    ON document_ai_canonical_elements (tenant_id, canonical_representation_id, stable_key)
    WHERE stable_key IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_document_ai_canonical_elements_reading_order
    ON document_ai_canonical_elements (tenant_id, canonical_representation_id, page_number, reading_order)
    WHERE page_number IS NOT NULL AND reading_order IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_document_ai_canonical_elements_representation
    ON document_ai_canonical_elements (tenant_id, canonical_representation_id, ordinal);

CREATE TABLE IF NOT EXISTS document_ai_canonical_relationships (
    canonical_relationship_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    canonical_representation_id UUID NOT NULL,
    source_element_id UUID NOT NULL,
    target_element_id UUID NOT NULL,
    relationship_type TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    relationship_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_document_ai_canonical_relationships_representation_scope
        FOREIGN KEY (tenant_id, canonical_representation_id)
        REFERENCES document_ai_canonical_representations (tenant_id, canonical_representation_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_canonical_relationships_source_scope
        FOREIGN KEY (tenant_id, source_element_id)
        REFERENCES document_ai_canonical_elements (tenant_id, canonical_element_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_canonical_relationships_target_scope
        FOREIGN KEY (tenant_id, target_element_id)
        REFERENCES document_ai_canonical_elements (tenant_id, canonical_element_id)
        ON DELETE RESTRICT,
    CONSTRAINT chk_document_ai_canonical_relationships_ordinal CHECK (ordinal >= 0),
    CONSTRAINT chk_document_ai_canonical_relationships_nonreflexive CHECK (
        source_element_id <> target_element_id
    )
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
        REFERENCES document_ai_source_artifacts (tenant_id, source_artifact_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_source_regions_element_scope
        FOREIGN KEY (tenant_id, canonical_element_id)
        REFERENCES document_ai_canonical_elements (tenant_id, canonical_element_id)
        ON DELETE RESTRICT,
    CONSTRAINT chk_document_ai_source_regions_index CHECK (structural_unit_index >= 0)
);

CREATE INDEX IF NOT EXISTS idx_document_ai_source_regions_structural_lookup
    ON document_ai_source_regions
       (tenant_id, structural_unit_kind, structural_unit_index, canonical_element_id);

CREATE TABLE IF NOT EXISTS document_ai_retrieval_chunks (
    retrieval_chunk_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    document_id UUID NOT NULL,
    document_version_id UUID NOT NULL,
    canonical_representation_id UUID NOT NULL,
    chunk_key TEXT NOT NULL,
    ordinal INTEGER NOT NULL DEFAULT 0,
    content_hash_sha256 TEXT NOT NULL,
    chunking_policy_version TEXT NOT NULL,
    embedding_text TEXT NOT NULL,
    canonical_element_keys JSONB NOT NULL,
    source_location JSONB NOT NULL,
    structural_context JSONB NOT NULL,
    lifecycle_state TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_document_ai_retrieval_chunks_document_scope
        FOREIGN KEY (tenant_id, document_id)
        REFERENCES document_ai_documents (tenant_id, document_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_retrieval_chunks_version_scope
        FOREIGN KEY (tenant_id, document_version_id)
        REFERENCES document_ai_document_versions (tenant_id, document_version_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_retrieval_chunks_representation_scope
        FOREIGN KEY (tenant_id, canonical_representation_id)
        REFERENCES document_ai_canonical_representations (tenant_id, canonical_representation_id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_document_ai_retrieval_chunks_scope UNIQUE (tenant_id, retrieval_chunk_id),
    CONSTRAINT uq_document_ai_retrieval_chunks_identity
        UNIQUE (tenant_id, canonical_representation_id, chunk_key, chunking_policy_version),
    CONSTRAINT chk_document_ai_retrieval_chunks_hash CHECK (content_hash_sha256 ~ '^[a-f0-9]{64}$'),
    CONSTRAINT chk_document_ai_retrieval_chunks_ordinal CHECK (ordinal >= 0),
    CONSTRAINT chk_document_ai_retrieval_chunks_lifecycle
        CHECK (lifecycle_state IN ('active', 'trashed', 'purge_pending', 'purged'))
);

CREATE INDEX IF NOT EXISTS idx_document_ai_retrieval_chunks_scope
    ON document_ai_retrieval_chunks
       (tenant_id, document_id, document_version_id, lifecycle_state, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_document_ai_retrieval_chunks_active_canonical_scope
    ON document_ai_retrieval_chunks
       (tenant_id, document_id, document_version_id, canonical_representation_id)
    WHERE lifecycle_state = 'active';

CREATE TABLE IF NOT EXISTS document_ai_chunk_embeddings (
    chunk_embedding_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    retrieval_chunk_id UUID NOT NULL,
    document_version_id UUID NOT NULL,
    canonical_representation_id UUID NOT NULL,
    content_hash_sha256 TEXT NOT NULL,
    chunking_policy_version TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_version TEXT NOT NULL,
    embedding_dimensions INTEGER NOT NULL,
    embedding VECTOR(1536) NOT NULL,
    index_state TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_document_ai_chunk_embeddings_chunk_scope
        FOREIGN KEY (tenant_id, retrieval_chunk_id)
        REFERENCES document_ai_retrieval_chunks (tenant_id, retrieval_chunk_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_chunk_embeddings_version_scope
        FOREIGN KEY (tenant_id, document_version_id)
        REFERENCES document_ai_document_versions (tenant_id, document_version_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_chunk_embeddings_representation_scope
        FOREIGN KEY (tenant_id, canonical_representation_id)
        REFERENCES document_ai_canonical_representations (tenant_id, canonical_representation_id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_document_ai_chunk_embeddings_model
        UNIQUE (tenant_id, retrieval_chunk_id, embedding_model, embedding_version),
    CONSTRAINT chk_document_ai_chunk_embeddings_dimensions CHECK (embedding_dimensions > 0),
    CONSTRAINT chk_document_ai_chunk_embeddings_state
        CHECK (index_state IN ('active', 'superseded', 'trashed', 'purged'))
);

CREATE INDEX IF NOT EXISTS idx_document_ai_chunk_embeddings_scope
    ON document_ai_chunk_embeddings
       (tenant_id, document_version_id, canonical_representation_id, embedding_model,
        embedding_version, index_state);

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
        REFERENCES document_ai_retrieval_chunks (tenant_id, retrieval_chunk_id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_document_ai_embedding_records_model
        UNIQUE (tenant_id, retrieval_chunk_id, embedding_model),
    CONSTRAINT chk_document_ai_embedding_records_dimensions CHECK (embedding_dimensions > 0),
    CONSTRAINT chk_document_ai_embedding_records_vector
        CHECK (jsonb_typeof(embedding_vector_json) = 'array')
);
