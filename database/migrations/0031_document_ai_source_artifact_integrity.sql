-- Milestone 3 / Document Policy source-authority controls.
-- Existing legacy source metadata is deliberately left untouched: no integrity
-- value is inferred or backfilled from an unverified historical row.
BEGIN;

ALTER TABLE document_ai_source_artifacts
    ADD COLUMN IF NOT EXISTS checksum_algorithm TEXT NULL,
    ADD COLUMN IF NOT EXISTS verified_media_type TEXT NULL;

ALTER TABLE document_ai_source_artifacts
    ADD CONSTRAINT chk_document_ai_source_artifacts_checksum_algorithm
    CHECK (checksum_algorithm IS NULL OR checksum_algorithm = 'sha256'),
    ADD CONSTRAINT chk_document_ai_source_artifacts_verified_media_type
    CHECK (verified_media_type IS NULL OR char_length(btrim(verified_media_type)) > 0);

-- New authoritative rows record the already storage-verified media type.
-- Existing rows remain unresolved rather than being guessed from content_type.
CREATE INDEX IF NOT EXISTS idx_document_ai_source_artifacts_document_version
    ON document_ai_source_artifacts (tenant_id, document_version_id);

COMMIT;
