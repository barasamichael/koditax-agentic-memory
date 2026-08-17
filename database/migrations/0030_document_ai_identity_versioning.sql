-- Milestone 2 / FR-001, FR-002, FR-018, FR-020, SR-012.
-- Extend the existing registry; do not replace legacy document identifiers.
BEGIN;

ALTER TABLE document_ai_documents
    ADD COLUMN IF NOT EXISTS display_name TEXT NULL,
    ADD COLUMN IF NOT EXISTS registry_revision INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS active_document_version_id UUID NULL;

ALTER TABLE document_ai_documents
    ADD CONSTRAINT chk_document_ai_documents_registry_revision
    CHECK (registry_revision >= 0);

ALTER TABLE document_ai_documents
    ADD CONSTRAINT fk_document_ai_documents_active_version_scope
    FOREIGN KEY (tenant_id, active_document_version_id)
    REFERENCES document_ai_document_versions (tenant_id, document_version_id)
    ON DELETE RESTRICT;

ALTER TABLE document_ai_document_versions
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_document_ai_document_versions_idempotency
    ON document_ai_document_versions (tenant_id, document_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_document_ai_source_artifacts_version
    ON document_ai_source_artifacts (tenant_id, document_version_id);

CREATE OR REPLACE FUNCTION fn_document_ai_active_version_same_document()
RETURNS TRIGGER AS $$
DECLARE
    version_document_id UUID;
BEGIN
    IF NEW.active_document_version_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT document_id INTO version_document_id
    FROM document_ai_document_versions
    WHERE tenant_id = NEW.tenant_id
      AND document_version_id = NEW.active_document_version_id;
    IF version_document_id IS DISTINCT FROM NEW.document_id THEN
        RAISE EXCEPTION 'active document version must belong to the same document and tenant';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_document_ai_active_version_same_document
    ON document_ai_documents;
CREATE TRIGGER trg_document_ai_active_version_same_document
    BEFORE INSERT OR UPDATE OF active_document_version_id, tenant_id, document_id
    ON document_ai_documents
    FOR EACH ROW EXECUTE FUNCTION fn_document_ai_active_version_same_document();

COMMIT;
