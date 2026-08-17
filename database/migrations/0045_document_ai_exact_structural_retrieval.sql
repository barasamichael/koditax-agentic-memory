-- Milestone 19 — authorized exact and structural retrieval from canonical content.
BEGIN;

CREATE INDEX IF NOT EXISTS idx_document_ai_canonical_elements_full_text
    ON document_ai_canonical_elements
    USING GIN (to_tsvector('simple', COALESCE(normalized_value->>'text', '')));

CREATE INDEX IF NOT EXISTS idx_document_ai_source_regions_structural_lookup
    ON document_ai_source_regions
       (tenant_id, structural_unit_kind, structural_unit_index, canonical_element_id);

CREATE INDEX IF NOT EXISTS idx_document_ai_source_regions_payload_lookup
    ON document_ai_source_regions USING GIN (region_payload);

CREATE INDEX IF NOT EXISTS idx_document_ai_documents_exact_metadata
    ON document_ai_documents
       (tenant_id, owner_user_id, state, active_document_version_id, document_id);

COMMIT;
