-- Broad-family source inspection records use safe, provider-independent reasons.
BEGIN;

ALTER TABLE document_ai_source_inspections
    DROP CONSTRAINT IF EXISTS chk_document_ai_source_inspections_reason_code;

ALTER TABLE document_ai_source_inspections
    ADD CONSTRAINT chk_document_ai_source_inspections_reason_code CHECK (reason_code IN (
        'accepted', 'source_empty', 'source_too_large', 'unsupported_format',
        'declared_media_type_mismatch', 'malformed_document', 'encrypted_document',
        'unsafe_active_content', 'archive_not_permitted', 'invalid_office_container',
        'image_dimensions_too_large', 'structured_text_too_deep'
    ));

COMMIT;
