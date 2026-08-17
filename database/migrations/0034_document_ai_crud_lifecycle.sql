-- Milestone 8 / FR-015 and FR-018.  User metadata and lifecycle are distinct
-- from immutable source-artifact fields and processing operations.
BEGIN;

ALTER TABLE document_ai_documents
    ADD COLUMN IF NOT EXISTS display_name TEXT NULL,
    ADD COLUMN IF NOT EXISTS category TEXT NULL,
    ADD COLUMN IF NOT EXISTS tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS description TEXT NULL,
    ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 0;

ALTER TABLE document_ai_documents
    ADD CONSTRAINT chk_document_ai_documents_revision_nonnegative
    CHECK (revision >= 0) NOT VALID;

CREATE INDEX IF NOT EXISTS idx_document_ai_documents_visible_scope
    ON document_ai_documents (tenant_id, owner_user_id, state, uploaded_at, document_id);

COMMIT;
