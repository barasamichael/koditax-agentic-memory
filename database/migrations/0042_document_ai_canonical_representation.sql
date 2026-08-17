-- Milestone 16 / Document Policy 3.19, 3.20, 5.11 and 5.31.
-- Canonical generations are provider-independent, source-scoped and immutable.
BEGIN;

ALTER TABLE document_ai_canonical_representations
    ADD COLUMN IF NOT EXISTS source_artifact_id UUID NULL,
    ADD COLUMN IF NOT EXISTS provider_result_id UUID NULL,
    ADD COLUMN IF NOT EXISTS assembly_policy_version TEXT NULL,
    ADD COLUMN IF NOT EXISTS content_hash_sha256 TEXT NULL;

ALTER TABLE document_ai_canonical_representations
    ADD CONSTRAINT fk_document_ai_canonical_representations_artifact_scope
        FOREIGN KEY (tenant_id, source_artifact_id)
        REFERENCES document_ai_source_artifacts (tenant_id, source_artifact_id) ON DELETE RESTRICT,
    ADD CONSTRAINT fk_document_ai_canonical_representations_provider_result_scope
        FOREIGN KEY (tenant_id, provider_result_id)
        REFERENCES document_ai_provider_results (tenant_id, provider_result_id) ON DELETE RESTRICT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_document_ai_canonical_representation_provider_result
    ON document_ai_canonical_representations (tenant_id, provider_result_id)
    WHERE provider_result_id IS NOT NULL;

ALTER TABLE document_ai_canonical_elements
    ADD COLUMN IF NOT EXISTS stable_key TEXT NULL,
    ADD COLUMN IF NOT EXISTS page_number INTEGER NULL,
    ADD COLUMN IF NOT EXISTS reading_order INTEGER NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_document_ai_canonical_elements_stable_key
    ON document_ai_canonical_elements (tenant_id, canonical_representation_id, stable_key)
    WHERE stable_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_document_ai_canonical_elements_reading_order
    ON document_ai_canonical_elements (tenant_id, canonical_representation_id, page_number, reading_order)
    WHERE page_number IS NOT NULL AND reading_order IS NOT NULL;

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
        REFERENCES document_ai_canonical_representations (tenant_id, canonical_representation_id) ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_canonical_relationships_source_scope
        FOREIGN KEY (tenant_id, source_element_id)
        REFERENCES document_ai_canonical_elements (tenant_id, canonical_element_id) ON DELETE RESTRICT,
    CONSTRAINT fk_document_ai_canonical_relationships_target_scope
        FOREIGN KEY (tenant_id, target_element_id)
        REFERENCES document_ai_canonical_elements (tenant_id, canonical_element_id) ON DELETE RESTRICT,
    CONSTRAINT chk_document_ai_canonical_relationships_ordinal CHECK (ordinal >= 0),
    CONSTRAINT chk_document_ai_canonical_relationships_nonreflexive CHECK (source_element_id <> target_element_id)
);

CREATE OR REPLACE FUNCTION fn_document_ai_canonical_generation_prevent_mutation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.document_version_id IS DISTINCT FROM OLD.document_version_id
       OR NEW.processing_operation_id IS DISTINCT FROM OLD.processing_operation_id
       OR NEW.source_artifact_id IS DISTINCT FROM OLD.source_artifact_id
       OR NEW.provider_result_id IS DISTINCT FROM OLD.provider_result_id
       OR NEW.canonical_schema_version IS DISTINCT FROM OLD.canonical_schema_version
       OR NEW.assembly_policy_version IS DISTINCT FROM OLD.assembly_policy_version
       OR NEW.content_hash_sha256 IS DISTINCT FROM OLD.content_hash_sha256
       OR NEW.representation_payload IS DISTINCT FROM OLD.representation_payload THEN
        RAISE EXCEPTION 'document_ai canonical generations are immutable';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_document_ai_canonical_generation_prevent_mutation ON document_ai_canonical_representations;
CREATE TRIGGER trg_document_ai_canonical_generation_prevent_mutation
BEFORE UPDATE ON document_ai_canonical_representations
FOR EACH ROW EXECUTE FUNCTION fn_document_ai_canonical_generation_prevent_mutation();

COMMIT;
